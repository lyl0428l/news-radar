"""
微信推送通知模块（PushPlus）

功能：
  1. 每轮爬取汇总推送 — 所有新闻标题列表
  2. 重大新闻即时推送 — 标题命中关键词时推送完整正文+图片

PushPlus 官网：https://www.pushplus.plus
  - 微信扫码登录获取 Token
  - 免费用户每天 200 条
  - 支持 HTML 格式，图片用 <img> 标签直接显示
"""
import re
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

# PushPlus API
_PUSHPLUS_API = "https://www.pushplus.plus/send"


def _send(token: str, title: str, content: str,
          topic: str = "", template: str = "html") -> bool:
    """
    调用 PushPlus API 发送一条消息。
    """
    if not token:
        return False

    try:
        payload = {
            "token": token,
            "title": title[:100],
            "content": content[:30000],  # PushPlus 内容限 30000 字
            "template": template,
        }
        if topic:
            payload["topic"] = topic

        resp = requests.post(_PUSHPLUS_API, json=payload, timeout=15)
        data = resp.json()

        if data.get("code") == 200:
            safe_title = title[:30].encode("ascii", "ignore").decode()
            logger.info(f"[推送] 发送成功: {safe_title or title[:30]}")
            return True
        else:
            logger.warning(f"[推送] 发送失败: {data.get('msg', '未知错误')}")
            return False

    except Exception as e:
        logger.warning(f"[推送] 请求异常: {e}")
        return False


def _sanitize_push_html(html: str) -> str:
    """
    清理推送 HTML，移除 PushPlus 服务端不接受的属性，防止"服务端验证错误"。
    主要问题：data-* 属性、onerror/onload 等事件属性、data-hls-src 等自定义属性。
    """
    import re
    # 移除所有 data-* 属性（PushPlus 服务端会拒绝含大量 data- 属性的内容）
    html = re.sub(r'\s+data-[a-z0-9_-]+=(?:"[^"]*"|\'[^\']*\')', '', html, flags=re.IGNORECASE)
    # 移除事件属性（onerror/onload/onclick 等）
    html = re.sub(r'\s+on[a-z]+\s*=\s*(?:"[^"]*"|\'[^\']*\')', '', html, flags=re.IGNORECASE)
    # 移除本地 /media/ 路径图片（推送到微信时本地路径无法访问，用原始URL）
    html = re.sub(
        r'(<img\b[^>]*?\bsrc=["\'])/media/[^"\']+(["\'])',
        lambda m: m.group(0),  # 保留原标签，下方再处理 orig-url 替换
        html,
        flags=re.IGNORECASE,
    )
    # 将 /media/ 路径替换为空（PushPlus 无法访问本地文件）
    html = re.sub(
        r'(<img\b[^>]*?\bsrc=["\'])/media/[^"\']+(["\'])',
        r'\1\2',
        html,
        flags=re.IGNORECASE,
    )
    return html


def _build_article_html(item: dict, show_full: bool = True) -> str:
    """
    构建单篇新闻的 HTML 内容（用于微信推送）。
    优先使用 content_html（保持原文中图片的实际位置），回退到纯文本+图片。
    """
    import json
    import re

    source = item.get("source_name", item.get("source", ""))
    pub_time = item.get("pub_time", "")
    author = item.get("author", "")
    content_html = item.get("content_html", "")
    content = item.get("content", "")

    html = []

    # 来源和时间
    meta_parts = []
    if source:
        meta_parts.append(source)
    if author:
        meta_parts.append(author)
    if pub_time:
        meta_parts.append(pub_time[:16])
    if meta_parts:
        html.append(f'<p style="color:#888;font-size:13px;">{"  |  ".join(meta_parts)}</p>')

    if show_full and content_html:
        # 直接使用 content_html（图文位置与原文一致）
        # 给所有 img 加上样式确保自适应宽度
        body = re.sub(
            r'<img ',
            '<img style="width:100%;border-radius:8px;margin:10px 0;" ',
            content_html
        )
        # 清理 PushPlus 不接受的属性（data-*、onerror 等），防止服务端验证错误
        body = _sanitize_push_html(body)
        # PushPlus 内容限制 30000 字，截断过长的（保守截到 20000 留余量）
        if len(body) > 20000:
            body = body[:20000] + '<p>...（正文过长，已截断）</p>'
        html.append(body)

    elif show_full and content:
        # 回退：用纯文本 + 图片列表（图片只用外部 http URL，不用本地路径）
        thumbnail = item.get("thumbnail", "")
        if thumbnail and thumbnail.startswith("http"):
            html.append(f'<img src="{thumbnail}" style="width:100%;border-radius:8px;margin:10px 0;"/>')

        text = content[:2000]
        if len(content) > 2000:
            text += "\n\n...（正文过长，已截断）"
        for p in text.split("\n"):
            p = p.strip()
            if p:
                html.append(f'<p>{p}</p>')

        # 补充图片（只用外部 URL，跳过本地 /media/ 路径）
        images_raw = item.get("images", [])
        if isinstance(images_raw, str):
            try:
                images_raw = json.loads(images_raw)
            except (json.JSONDecodeError, TypeError):
                images_raw = []
        shown = 0
        for img in images_raw:
            if shown >= 5:
                break
            img_url = img.get("url", "") if isinstance(img, dict) else str(img)
            if img_url and img_url.startswith("http") and img_url != thumbnail:
                html.append(f'<img src="{img_url}" style="width:100%;border-radius:8px;margin:10px 0;"/>')
                shown += 1

    else:
        # 只推摘要
        summary = item.get("summary", content[:200] if content else "")
        if summary:
            html.append(f'<p>{summary}</p>')

    return "\n".join(html)


# ============================================================
#  每轮爬取汇总推送
# ============================================================

def push_crawl_summary(items: list, success_count: int,
                       total_sites: int, fail_list: list):
    """
    推送每轮爬取结果的前2条新闻（全局列表顺序，取最先完成爬取的来源的前2条）。
    每条新闻独立推送一条微信消息，包含完整正文和图片。
    """
    from config import (PUSH_ENABLED, PUSH_TOKEN, PUSH_TOPIC,
                        PUSH_SUMMARY_ENABLED)

    if not PUSH_ENABLED or not PUSH_SUMMARY_ENABLED or not PUSH_TOKEN:
        return
    if not items:
        return

    # 优先取有完整正文（content_html 非空）的前2条，确保推送内容完整
    # 回退：若无 content_html 则取有纯文本 content 的，最后兜底取前2条
    top2 = [i for i in items if i.get("content_html") and len(i.get("content_html", "")) > 50][:2]
    if len(top2) < 2:
        supplement = [i for i in items
                      if i not in top2 and i.get("content") and len(i.get("content", "")) > 50]
        top2 = (top2 + supplement)[:2]
    if not top2:
        top2 = items[:2]

    for idx, item in enumerate(top2, start=1):
        item_title = item.get("title", "无标题")
        source = item.get("source_name", item.get("source", ""))
        push_title = f"【{source}】{item_title[:50]}"

        html_parts = []

        # 标题
        html_parts.append(f'<h3 style="margin:0 0 8px 0;">{item_title}</h3>')

        # 来源、时间
        pub_time = item.get("pub_time", "")
        meta_parts = []
        if source:
            meta_parts.append(source)
        if pub_time:
            meta_parts.append(pub_time[:16])
        if meta_parts:
            html_parts.append(
                f'<p style="color:#888;font-size:13px;margin:0 0 10px 0;">'
                f'{"  |  ".join(meta_parts)}</p>'
            )

        html_parts.append("<hr/>")

        # 正文（含图片）
        html_parts.append(_build_article_html(item, show_full=True))

        content = "\n".join(html_parts)
        _send(PUSH_TOKEN, push_title, content, topic=PUSH_TOPIC)

    logger.info(f"[推送] 本轮推送排名前 {len(top2)} 条新闻")


# ============================================================
#  重大新闻即时推送（含完整正文+图片）
# ============================================================

def push_breaking_news(item: dict, keyword: str):
    """
    推送重大新闻 — 包含完整正文和图片，在微信里直接阅读。
    """
    from config import PUSH_ENABLED, PUSH_TOKEN, PUSH_TOPIC, PUSH_BREAKING_ENABLED

    if not PUSH_ENABLED or not PUSH_BREAKING_ENABLED or not PUSH_TOKEN:
        return

    news_title = item.get("title", "未知新闻")
    source = item.get("source_name", item.get("source", ""))

    title = f"[{source}] {news_title[:40]}"

    # 构建完整文章 HTML
    html_parts = []
    html_parts.append(f'<h3>{news_title}</h3>')
    html_parts.append(f'<p style="color:#e74c3c;font-size:13px;">关键词命中: {keyword}</p>')
    html_parts.append("<hr/>")
    html_parts.append(_build_article_html(item, show_full=True))

    html = "\n".join(html_parts)
    _send(PUSH_TOKEN, title, html, topic=PUSH_TOPIC)


def check_and_push_breaking(items: list):
    """
    检查新闻列表中是否有重大新闻，有则推送完整内容。
    同一轮最多推 10 条。
    """
    from config import (PUSH_ENABLED, PUSH_BREAKING_ENABLED,
                        PUSH_BREAKING_KEYWORDS)

    if not PUSH_ENABLED or not PUSH_BREAKING_ENABLED:
        return

    if not PUSH_BREAKING_KEYWORDS:
        return

    pushed = 0
    for item in items:
        if pushed >= 10:
            break
        title = item.get("title", "")
        title_lower = title.lower()
        for kw in PUSH_BREAKING_KEYWORDS:
            if kw.lower() in title_lower:
                push_breaking_news(item, kw)
                pushed += 1
                break

    if pushed:
        logger.info(f"[推送] 本轮推送 {pushed} 条重大新闻")


# ============================================================
#  推送测试
# ============================================================

def test_push(token: str = "") -> bool:
    """测试推送是否正常工作"""
    if not token:
        from config import PUSH_TOKEN
        token = PUSH_TOKEN

    if not token:
        print("错误: 未配置 PUSH_TOKEN")
        return False

    return _send(
        token,
        "推送测试",
        "<p>PushPlus 微信推送配置成功。</p>"
        "<p>你将在每轮爬取后收到新闻汇总，"
        "重大新闻会即时推送完整正文和图片。</p>",
    )
