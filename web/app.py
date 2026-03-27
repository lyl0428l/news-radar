"""
Flask Web 服务 - 本地新闻浏览界面
"""
import sys
import os
import time as _time
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import csv
import io
import json
import threading
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, Response
from config import WEB_HOST, WEB_PORT, WEB_DEBUG, SITES, MEDIA_DIR
from storage import (get_news, get_news_by_id, get_stats, get_crawl_health,
                     get_crawl_rounds, mark_read, mark_read_batch,
                     get_news_feed, toggle_favorite, get_favorites, is_favorited,
                     get_daily_stats, get_source_daily_stats, get_push_logs)
from models import init_db

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SECRET_KEY"] = os.urandom(32)


# 请求日志中间件
@app.before_request
def log_request_info():
    """记录请求信息"""
    logger.debug(f"请求: {request.method} {request.path} | IP: {request.remote_addr}")


@app.after_request
def log_response_info(response):
    """记录响应信息"""
    logger.debug(f"响应: {request.method} {request.path} | 状态: {response.status_code}")
    return response


@app.errorhandler(404)
def not_found(error):
    """404 错误处理"""
    logger.warning(f"404 未找到: {request.path}")
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(error):
    """500 错误处理"""
    logger.error(f"500 服务器错误: {request.path} | {error}")
    return render_template("500.html"), 500

# 全局模板变量：所有页面都可使用 {{ site_count }}
@app.context_processor
def inject_globals():
    return {"site_count": len(SITES)}


# ---------- 简易短时缓存（减少首页重复 DB 查询） ----------
_cache: dict = {}
_CACHE_TTL = 30  # 秒


def _cached(key: str, fn, ttl: int = _CACHE_TTL):
    """获取缓存值，过期则重新计算"""
    now = _time.monotonic()
    entry = _cache.get(key)
    if entry and now - entry[0] < ttl:
        return entry[1]
    value = fn()
    _cache[key] = (now, value)
    return value


def _enrich_thumbnails(news_list: list) -> list:
    """
    为列表中的每条新闻设置 thumbnail_local 字段。
    逻辑：
    1. 如果 thumbnail URL 能在 images 列表中找到对应的本地文件，使用它
    2. 否则如果 images 列表中有任何已下载的本地图片，使用第一张作为缩略图
    3. 否则保持原样（使用外部 thumbnail URL）
    """
    for item in news_list:
        if item.get("thumbnail_local"):
            continue  # 已有本地缩略图

        images = item.get("images", [])
        if not images or not isinstance(images, list):
            continue

        thumb_url = item.get("thumbnail", "")

        # 策略 1：精确匹配 thumbnail URL → 对应本地路径
        if thumb_url:
            for img in images:
                if not isinstance(img, dict):
                    continue
                orig_url = img.get("url", "")
                local_path = img.get("local", "")
                if local_path and orig_url:
                    # 精确匹配或 protocol-relative 匹配
                    if orig_url == thumb_url:
                        item["thumbnail_local"] = local_path
                        break
                    if thumb_url.startswith("//") and orig_url.endswith(thumb_url[2:]):
                        item["thumbnail_local"] = local_path
                        break
                    if orig_url.startswith("//") and thumb_url.endswith(orig_url[2:]):
                        item["thumbnail_local"] = local_path
                        break

        # 策略 2：没有精确匹配，使用第一张有本地文件的图片
        if not item.get("thumbnail_local"):
            for img in images:
                if isinstance(img, dict) and img.get("local"):
                    item["thumbnail_local"] = img["local"]
                    break

    return news_list


# ============================================================
#  首页：只显示最新一轮 + 最近 1 小时内的新闻
# ============================================================

@app.route("/")
def index():
    """首页 - 最新新闻（最近 1 小时）"""
    source = request.args.get("source", "")
    language = request.args.get("language", "")
    keyword = request.args.get("keyword", "")
    try:
        page = int(request.args.get("page", 1))
    except (ValueError, TypeError):
        page = 1
    per_page = 50

    # 默认只查最近 1 小时内的数据
    one_hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    news_list, total = get_news(
        source=source or None,
        language=language or None,
        keyword=keyword or None,
        start_time=one_hour_ago,
        end_time=None,
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    news_list = _enrich_thumbnails(news_list)

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    stats = _cached("stats", get_stats)
    sites = [(s.display_name, s.module) for s in SITES]

    # 检查爬虫异常
    alerts = []
    try:
        health = get_crawl_health()
        for h in health:
            if h["level"] == "critical":
                alerts.append(f"{h['source_name'] or h['source']} 连续失败")
            elif h["level"] == "warning":
                alerts.append(f"{h['source_name'] or h['source']} 近期有失败")
    except Exception:
        pass

    return render_template(
        "index.html",
        news_list=news_list,
        stats=stats,
        sites=sites,
        current_source=source,
        current_language=language,
        current_keyword=keyword,
        current_page=page,
        total_pages=total_pages,
        total_count=total,
        alerts=alerts,
    )


# ============================================================
#  历史新闻：完整筛选 + 分页 + 导出
# ============================================================

@app.route("/archive")
def archive():
    """历史新闻 - 全量数据浏览"""
    source = request.args.get("source", "")
    language = request.args.get("language", "")
    keyword = request.args.get("keyword", "")
    start_time = request.args.get("start_time", "")
    end_time = request.args.get("end_time", "")
    crawl_round = request.args.get("crawl_round", "")
    try:
        page = int(request.args.get("page", 1))
    except (ValueError, TypeError):
        page = 1
    per_page = 50

    # datetime-local 提交格式 "2026-03-11T14:00" → "2026-03-11 14:00"
    # 保留原始值（含 T）给模板回显，转换后的值给 DB 查询
    start_time_display = start_time  # 给 datetime-local 回显用
    end_time_display = end_time
    if start_time and "T" in start_time:
        start_time = start_time.replace("T", " ")
    if end_time and "T" in end_time:
        end_time = end_time.replace("T", " ")

    # 如果选了爬取轮次，用精确的 crawl_time 覆盖时间范围
    effective_start = start_time or None
    effective_end = end_time or None
    if crawl_round:
        effective_start = crawl_round
        effective_end = crawl_round

    news_list, total = get_news(
        source=source or None,
        language=language or None,
        keyword=keyword or None,
        start_time=effective_start,
        end_time=effective_end,
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    news_list = _enrich_thumbnails(news_list)

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    stats = _cached("stats", get_stats)
    sites = [(s.display_name, s.module) for s in SITES]
    rounds = _cached("rounds", lambda: get_crawl_rounds(limit=50))

    return render_template(
        "archive.html",
        news_list=news_list,
        stats=stats,
        sites=sites,
        rounds=rounds,
        current_source=source,
        current_language=language,
        current_keyword=keyword,
        current_start_time=start_time_display,
        current_end_time=end_time_display,
        current_crawl_round=crawl_round,
        current_page=page,
        total_pages=total_pages,
        total_count=total,
    )


# ============================================================
#  API 接口
# ============================================================

@app.route("/api/news")
def api_news():
    """API 接口 - 返回 JSON 数据"""
    source = request.args.get("source", "")
    language = request.args.get("language", "")
    keyword = request.args.get("keyword", "")
    start_time = request.args.get("start_time", "")
    end_time = request.args.get("end_time", "")
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = int(request.args.get("offset", 0))
    except (ValueError, TypeError):
        offset = 0

    news_list, total = get_news(
        source=source or None,
        language=language or None,
        keyword=keyword or None,
        start_time=start_time or None,
        end_time=end_time or None,
        limit=limit,
        offset=offset,
    )
    return jsonify({"data": news_list, "count": len(news_list), "total": total})


@app.route("/api/stats")
def api_stats():
    """API 接口 - 统计信息"""
    return jsonify(get_stats())


@app.route("/api/health")
def api_health():
    """API 接口 - 爬虫健康状态"""
    return jsonify(get_crawl_health())


@app.route("/api/mark_read/<int:news_id>", methods=["POST"])
def api_mark_read(news_id):
    """标记新闻为已读"""
    updated = mark_read(news_id)
    if not updated:
        logger.warning(f"标记已读失败: 新闻ID {news_id} 不存在")
        return jsonify({"ok": False, "msg": "新闻不存在"}), 404
    logger.debug(f"标记已读: 新闻ID {news_id}")
    return jsonify({"ok": True})


@app.route("/health")
def health_page():
    """爬虫健康状态面板"""
    health = get_crawl_health()
    return render_template("health.html", health=health)


# ============================================================
#  新闻详情页
# ============================================================

@app.route("/news/<int:news_id>")
def news_detail(news_id):
    """新闻详情页 - 显示正文、图片、视频"""
    item = get_news_by_id(news_id)
    if not item:
        return render_template("detail.html", item=None), 404

    # 自动标记为已读（查看详情即表示已读）
    if not item.get("is_read"):
        try:
            mark_read(news_id)
            item["is_read"] = 1
        except Exception:
            pass

    # 将 content_html 中的外部图片 URL 替换为本地 /media/ 路径
    item = _rewrite_content_images(item)

    return render_template("detail.html", item=item)


def _rewrite_content_images(item: dict) -> dict:
    """
    将 content_html 中的 <img> src 替换为本地已下载的图片路径。
    同时修复 protocol-relative URL (//xxx → https://xxx)。
    并标记哪些图片已嵌入正文（in_content），避免图片画廊重复展示。

    逻辑：
    1. 从 images JSON 字段构建 {原始URL → /media/本地路径} 的映射表
       同时添加 path 部分的映射，以匹配 content_html 中的相对路径
    2. 扫描 content_html 中所有 <img> 的 src 属性，匹配则替换为本地路径
    3. 对于相对路径，先用原文链接的 domain 补全再匹配
    4. 不匹配的外部 URL 补全 https: 前缀
    5. 为正文中的 <img> 添加 onerror fallback 到原始 URL
    6. 已成功嵌入正文的图片标记 in_content=True
    """
    import re
    from urllib.parse import urlparse, urljoin

    content_html = item.get("content_html", "")
    images = item.get("images", [])

    # 从原文链接提取 base URL，用于补全相对路径
    article_url = item.get("url", "")
    base_url = ""
    if article_url:
        parsed = urlparse(article_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

    if not content_html or not images:
        # 即使没有 images 映射，也修复 <img> 中的 protocol-relative URLs
        if content_html:
            item["content_html"] = re.sub(
                r'(<img\b[^>]*\bsrc=["\'])//([^"\']+)',
                r'\1https://\2',
                content_html,
                flags=re.IGNORECASE,
            )
        return item

    # 1. 构建映射表
    #    url_to_local: {各种形式的URL → /media/本地路径}
    #    url_to_orig:  {各种形式的URL → 完整原始URL}（用于 onerror fallback）
    url_to_local = {}
    url_to_orig = {}
    for img in images:
        if not isinstance(img, dict):
            continue
        orig_url = img.get("url", "")
        local_path = img.get("local", "")
        if orig_url and local_path:
            local_media = f"/media/{local_path}"
            # 完整 URL
            url_to_local[orig_url] = local_media
            url_to_orig[orig_url] = orig_url
            # protocol-relative 变体
            if orig_url.startswith("https://"):
                key = "//" + orig_url[8:]
                url_to_local[key] = local_media
                url_to_orig[key] = orig_url
            elif orig_url.startswith("http://"):
                key = "//" + orig_url[7:]
                url_to_local[key] = local_media
                url_to_orig[key] = orig_url
            # path 部分（匹配相对路径 src="/NMediaFile/..."）
            orig_path = urlparse(orig_url).path
            if orig_path and orig_path not in url_to_local:
                url_to_local[orig_path] = local_media
                url_to_orig[orig_path] = orig_url

    # 追踪哪些原始 URL 的图片已嵌入正文
    matched_orig_urls = set()

    # onerror fallback 脚本：本地图片加载失败时回退到原始 URL
    _onerror_tpl = (
        'onerror="if(this.dataset.origUrl&&this.src.indexOf(\'/media/\')!==-1)'
        '{this.src=this.dataset.origUrl;this.removeAttribute(\'onerror\')}"'
    )

    # 2. 替换 content_html 中的 img src
    def _replace_img_tag(match):
        """替换整个 <img ...> 标签，同时注入 data-orig-url 和 onerror"""
        full_tag = match.group(0)

        # 提取当前 src
        src_m = re.search(r'\bsrc=["\']([^"\']+)["\']', full_tag, re.IGNORECASE)
        if not src_m:
            return full_tag

        src_url = src_m.group(1)
        new_src = None
        orig_url_for_fallback = ""

        # 精确匹配（完整 URL 或相对路径）
        if src_url in url_to_local:
            new_src = url_to_local[src_url]
            orig_url_for_fallback = url_to_orig.get(src_url, "")

        # 补全 protocol-relative 后再匹配
        elif src_url.startswith("//"):
            full_url = "https:" + src_url
            if full_url in url_to_local:
                new_src = url_to_local[full_url]
                orig_url_for_fallback = url_to_orig.get(full_url, "")
            else:
                # 没匹配到本地，至少补全 https:
                new_src = "https:" + src_url

        # 相对路径：用原文链接的 domain 补全后再匹配
        elif base_url and src_url.startswith("/"):
            full_url = base_url + src_url
            if full_url in url_to_local:
                new_src = url_to_local[full_url]
                orig_url_for_fallback = url_to_orig.get(full_url, "")
            elif src_url in url_to_local:
                new_src = url_to_local[src_url]
                orig_url_for_fallback = url_to_orig.get(src_url, "")

        if new_src is None:
            return full_tag

        # 记录该图片已嵌入正文
        if orig_url_for_fallback:
            matched_orig_urls.add(orig_url_for_fallback)

        # 替换 src
        new_tag = re.sub(
            r'(\bsrc=["\'])[^"\']+(["\'])',
            lambda m: m.group(1) + new_src + m.group(2),
            full_tag,
            count=1,
            flags=re.IGNORECASE,
        )

        # 注入 data-orig-url 和 onerror（仅对本地图片）
        if orig_url_for_fallback and "/media/" in new_src:
            # 移除已有的 onerror（如果有）
            new_tag = re.sub(r'\s*onerror="[^"]*"', '', new_tag, flags=re.IGNORECASE)
            new_tag = re.sub(r'\s*onerror=\'[^\']*\'', '', new_tag, flags=re.IGNORECASE)
            # 在 <img 后注入属性
            new_tag = new_tag.replace(
                '<img ',
                f'<img data-orig-url="{orig_url_for_fallback}" {_onerror_tpl} ',
                1,
            )

        return new_tag

    # 匹配整个 <img ...> 标签（非贪婪匹配到 > 或 />）
    item["content_html"] = re.sub(
        r'<img\b[^>]*>',
        _replace_img_tag,
        content_html,
        flags=re.IGNORECASE,
    )

    # 3. 标记已在正文中出现的图片
    for img in images:
        if isinstance(img, dict):
            img["in_content"] = img.get("url", "") in matched_orig_urls

    return item


# ============================================================
#  媒体文件路由（本地图片访问）
# ============================================================

@app.route("/media/<path:filename>")
def serve_media(filename):
    """提供本地媒体文件访问（严格路径遍历防护）"""
    from flask import send_from_directory, abort
    import posixpath

    # 规范化路径（消除 .、..、URL 编码的 %2e%2e 等）
    safe_name = posixpath.normpath("/" + filename).lstrip("/")

    # 绝对路径或空路径 → 拒绝
    if not safe_name or safe_name.startswith("/"):
        abort(403)

    # 双重检查：只允许访问以 images/ 开头的文件（媒体目录结构固定）
    if not safe_name.startswith("images/"):
        abort(403)

    # 确保最终路径在 MEDIA_DIR 内（防止符号链接逃逸）
    abs_path = os.path.realpath(os.path.join(MEDIA_DIR, safe_name))
    media_root = os.path.realpath(MEDIA_DIR)
    if not abs_path.startswith(media_root + os.sep) and abs_path != media_root:
        abort(403)

    return send_from_directory(MEDIA_DIR, safe_name)


@app.route("/api/feed")
def api_feed():
    """无限滚动接口 - 热度排行"""
    source = request.args.get("source", "")
    keyword = request.args.get("keyword", "")
    try:
        last_id = int(request.args.get("last_id", 0)) or None
    except (ValueError, TypeError):
        last_id = None
    try:
        limit = min(int(request.args.get("limit", 20)), 50)
    except (ValueError, TypeError):
        limit = 20
    try:
        hours = int(request.args.get("hours", 0)) or None
    except (ValueError, TypeError):
        hours = None

    news_list, has_more = get_news_feed(
        source=source or None,
        keyword=keyword or None,
        last_id=last_id,
        limit=limit,
        hours=hours,
    )
    news_list = _enrich_thumbnails(news_list)
    return jsonify({
        "data": news_list,
        "has_more": has_more,
        "last_id": news_list[-1]["id"] if news_list else None,
    })


@app.route("/api/favorite/<int:news_id>", methods=["POST"])
def api_toggle_favorite(news_id):
    """切换收藏状态"""
    result = toggle_favorite(news_id)
    return jsonify(result)


@app.route("/api/favorites")
def api_favorites():
    """获取收藏列表"""
    try:
        page = int(request.args.get("page", 1))
    except (ValueError, TypeError):
        page = 1
    per_page = 20
    news_list, total = get_favorites(limit=per_page, offset=(page - 1) * per_page)
    news_list = _enrich_thumbnails(news_list)
    return jsonify({"data": news_list, "total": total})


@app.route("/favorites")
def favorites_page():
    """收藏夹页面"""
    news_list, total = get_favorites(limit=50)
    news_list = _enrich_thumbnails(news_list)
    sites = [(s.display_name, s.module) for s in SITES]
    return render_template("favorites.html", news_list=news_list,
                           total=total, sites=sites)


@app.route("/api/mark_read_all", methods=["POST"])
def api_mark_read_all():
    """标记全部为已读"""
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    if ids:
        updated = mark_read_batch(ids)
    else:
        updated = 0
    return jsonify({"ok": True, "updated": updated})


@app.route("/stats")
def stats_page():
    """数据统计页面"""
    daily = get_daily_stats(days=14)
    source_daily = get_source_daily_stats(days=7)
    push_logs = get_push_logs(limit=50)
    stats = _cached("stats", get_stats)
    sites = [(s.display_name, s.module) for s in SITES]
    return render_template("stats.html", daily=daily,
                           source_daily=source_daily,
                           push_logs=push_logs,
                           stats=stats, sites=sites)


@app.route("/api/daily_stats")
def api_daily_stats():
    """每日爬取量统计"""
    try:
        days = int(request.args.get("days", 14))
    except (ValueError, TypeError):
        days = 14
    return jsonify(get_daily_stats(days=days))


@app.route("/api/source_stats")
def api_source_stats():
    """各来源占比统计"""
    return jsonify(get_source_daily_stats(days=7))


@app.route("/api/push_logs")
def api_push_logs():
    """推送记录"""
    return jsonify(get_push_logs(limit=50))


@app.route("/api/export")
def api_export():
    """导出新闻数据为 CSV 或 JSON（遵循当前筛选条件）"""
    fmt = request.args.get("format", "csv")
    source = request.args.get("source", "")
    language = request.args.get("language", "")
    keyword = request.args.get("keyword", "")
    start_time = request.args.get("start_time", "")
    end_time = request.args.get("end_time", "")

    logger.info(f"数据导出请求: 格式={fmt}, 来源={source}, 语言={language}, 关键词={keyword}")

    # 导出最多 5000 条
    news_list, _ = get_news(
        source=source or None,
        language=language or None,
        keyword=keyword or None,
        start_time=start_time or None,
        end_time=end_time or None,
        limit=5000,
        offset=0,
    )

    logger.info(f"数据导出完成: 共 {len(news_list)} 条")

    if fmt == "json":
        data = json.dumps(news_list, ensure_ascii=False, indent=2)
        return Response(
            data,
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=news_export.json"},
        )

    # 默认 CSV
    fields = ["id", "title", "url", "source", "source_name", "category",
              "rank", "pub_time", "crawl_time", "language", "summary"]
    output = io.StringIO()
    # 写 BOM 头让 Excel 正确识别 UTF-8
    output.write("\ufeff")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for item in news_list:
        writer.writerow(item)

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=news_export.csv"},
    )


# 手动爬取状态
_crawl_lock = threading.Lock()
_crawl_running = False


@app.route("/api/crawl", methods=["POST"])
def api_trigger_crawl():
    """手动触发一轮爬取（异步执行，防止阻塞 Web 请求）"""
    # 安全限制：只允许 localhost 调用
    remote = request.remote_addr or ""
    if remote not in ("127.0.0.1", "::1", "localhost"):
        logger.warning(f"手动爬取被拒绝: 非本地访问 {remote}")
        return jsonify({"ok": False, "msg": "仅限本机访问"}), 403

    global _crawl_running
    with _crawl_lock:
        if _crawl_running:
            logger.info("手动爬取被拒绝: 爬取正在进行中")
            return jsonify({"ok": False, "msg": "爬取正在进行中，请稍后再试"}), 409
        _crawl_running = True

    def do_crawl():
        global _crawl_running
        try:
            logger.info("手动爬取任务开始执行")
            from main import run_single_crawl
            run_single_crawl()
            logger.info("手动爬取任务执行完成")
        except Exception as e:
            logger.error(f"手动爬取任务异常: {e}")
        finally:
            with _crawl_lock:
                _crawl_running = False

    t = threading.Thread(target=do_crawl, daemon=True)
    t.start()
    logger.info(f"手动爬取任务已启动 | IP: {remote}")
    return jsonify({"ok": True, "msg": "爬取任务已启动，请稍后刷新页面查看结果"})


@app.route("/api/crawl/status")
def api_crawl_status():
    """查询手动爬取是否正在运行"""
    return jsonify({"running": _crawl_running})


@app.route("/api/push/test", methods=["POST"])
def api_push_test():
    """测试微信推送是否正常"""
    remote = request.remote_addr or ""
    if remote not in ("127.0.0.1", "::1", "localhost"):
        logger.warning(f"推送测试被拒绝: 非本地访问 {remote}")
        return jsonify({"ok": False, "msg": "仅限本机访问"}), 403
    try:
        logger.info(f"推送测试请求 | IP: {remote}")
        from utils.notify import test_push
        ok = test_push()
        if ok:
            logger.info("推送测试成功")
            return jsonify({"ok": True, "msg": "推送成功！请检查微信"})
        else:
            logger.warning("推送测试失败")
            return jsonify({"ok": False, "msg": "推送失败，请检查 PUSH_TOKEN 配置"})
    except Exception as e:
        logger.error(f"推送测试异常: {e}")
        return jsonify({"ok": False, "msg": f"错误: {e}"})


def main():
    from main import setup_logging
    setup_logging()
    init_db()
    logger.info(f"Web 服务启动: http://{WEB_HOST}:{WEB_PORT}")
    # use_reloader=False：防止与 APScheduler 同进程时调度器启动两次
    app.run(host=WEB_HOST, port=WEB_PORT, debug=WEB_DEBUG, use_reloader=False)


if __name__ == "__main__":
    main()
