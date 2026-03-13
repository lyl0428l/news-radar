"""
通用正文/图片/视频提取器
从新闻详情页 HTML 中提取:
  - content_html: 正文 HTML（保留段落/图片标签）
  - content:      正文纯文本
  - images:       图片列表 [{"url": ..., "caption": ...}, ...]
  - videos:       视频列表 [{"url": ..., "type": "mp4|iframe", "poster": ...}, ...]
  - thumbnail:    封面图 URL
"""
import re
import logging
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

# 需要过滤的图片 URL 关键词（logo/icon/广告/追踪像素）
_IMAGE_BLACKLIST_KEYWORDS = [
    "logo", "icon", "avatar", "emoji", "badge", "arrow",
    "btn", "button", "ad_", "advert", "tracker", "pixel",
    "loading", "spinner", "placeholder", "blank", "spacer",
    "share_", "wechat", "weibo", "qq_", "facebook", "twitter",
    "google-analytics", "cnzz", "baidu.com/img",
]

# 需要过滤的图片格式
_IMAGE_BLACKLIST_EXTS = {".gif", ".svg", ".ico"}

# 视频平台 iframe 匹配（用于识别嵌入视频）
_VIDEO_IFRAME_PATTERNS = [
    re.compile(r"youtube\.com/embed/", re.I),
    re.compile(r"youtu\.be/", re.I),
    re.compile(r"player\.bilibili\.com", re.I),
    re.compile(r"v\.qq\.com", re.I),
    re.compile(r"player\.youku\.com", re.I),
    re.compile(r"video\.sina\.com", re.I),
    re.compile(r"tv\.sohu\.com", re.I),
    re.compile(r"bbc\.com/.*video", re.I),
    re.compile(r"cnn\.com/video", re.I),
    re.compile(r"reuters\.tv", re.I),
    re.compile(r"ap\..*video", re.I),
]


def _is_blacklisted_image(url: str) -> bool:
    """判断图片 URL 是否应该被过滤"""
    if not url:
        return True
    url_lower = url.lower()
    # 过滤特定扩展名
    parsed = urlparse(url_lower)
    path = parsed.path
    for ext in _IMAGE_BLACKLIST_EXTS:
        if path.endswith(ext):
            return True
    # 过滤关键词
    for kw in _IMAGE_BLACKLIST_KEYWORDS:
        if kw in url_lower:
            return True
    # data URI（内联小图标）
    if url_lower.startswith("data:"):
        return True
    return False


def _extract_readability_html(html: str, url: str) -> str:
    """用 readability-lxml 提取正文 HTML"""
    try:
        from readability import Document
        doc = Document(html, url=url)
        return doc.summary()
    except Exception as e:
        logger.debug(f"readability 提取失败: {e}")
        return ""


def extract_content(html: str, url: str, selectors: list = None) -> dict:
    """
    从详情页 HTML 提取正文、图片、视频。

    参数:
        html: 完整页面 HTML
        url: 页面 URL（用于拼接相对路径）
        selectors: 可选的 CSS 选择器列表，按优先级尝试匹配正文容器。
                   如果都不匹配或未传，则用 readability 自动提取。

    返回:
        {
            "content_html": str,   # 正文 HTML
            "content": str,        # 正文纯文本
            "images": list,        # [{"url": str, "caption": str}, ...]
            "videos": list,        # [{"url": str, "type": str, "poster": str}, ...]
            "thumbnail": str,      # 封面图 URL
        }
    """
    result = {
        "content_html": "",
        "content": "",
        "images": [],
        "videos": [],
        "thumbnail": "",
    }

    if not html:
        return result

    soup = BeautifulSoup(html, "lxml")

    # --- 第一步：从 <meta> 标签提取 og:image 作为候选封面图 ---
    og_image = ""
    og_tag = soup.find("meta", property="og:image")
    if og_tag and og_tag.get("content"):
        og_image = urljoin(url, og_tag["content"])

    # --- 第二步：定位正文容器 ---
    content_el = None

    # 2a. 尝试指定的 CSS 选择器
    if selectors:
        for sel in selectors:
            try:
                content_el = soup.select_one(sel)
                if content_el and len(content_el.get_text(strip=True)) > 100:
                    break
                content_el = None
            except Exception:
                continue

    # 2b. 回退到 readability
    if content_el is None:
        readable_html = _extract_readability_html(html, url)
        if readable_html:
            content_el = BeautifulSoup(readable_html, "lxml").body
            if content_el is None:
                content_el = BeautifulSoup(readable_html, "lxml")

    if content_el is None:
        return result

    # --- 第三步：从正文容器中提取内容 ---

    # 移除脚本/样式/广告元素
    for tag in content_el.find_all(["script", "style", "noscript",
                                     "iframe[src*='ad']", "ins", ".ad",
                                     ".advertisement"]):
        if isinstance(tag, Tag):
            tag.decompose()

    # 3a. 提取图片
    images = []
    seen_urls = set()
    for img in content_el.find_all("img"):
        # 尝试多种属性获取图片 URL
        img_url = (img.get("src") or img.get("data-src") or
                   img.get("data-original") or img.get("data-lazy-src") or "")
        img_url = img_url.strip()
        if not img_url:
            continue
        img_url = urljoin(url, img_url)

        if _is_blacklisted_image(img_url):
            continue
        if img_url in seen_urls:
            continue
        seen_urls.add(img_url)

        caption = (img.get("alt") or img.get("title") or "").strip()
        # 检查图片后面的 <figcaption> 或 <p class="caption">
        parent = img.parent
        if parent and parent.name == "figure":
            figcap = parent.find("figcaption")
            if figcap and not caption:
                caption = figcap.get_text(strip=True)

        images.append({"url": img_url, "caption": caption})

    # 3b. 提取视频
    videos = []

    # <video> 标签
    for video_tag in content_el.find_all("video"):
        video_url = (video_tag.get("src") or "").strip()
        poster = (video_tag.get("poster") or "").strip()
        # 检查 <source> 子标签
        if not video_url:
            source_tag = video_tag.find("source")
            if source_tag:
                video_url = (source_tag.get("src") or "").strip()
        if video_url:
            video_url = urljoin(url, video_url)
            if poster:
                poster = urljoin(url, poster)
            videos.append({
                "url": video_url,
                "type": "mp4",
                "poster": poster,
            })

    # <iframe> 嵌入视频
    for iframe in content_el.find_all("iframe"):
        iframe_src = (iframe.get("src") or iframe.get("data-src") or "").strip()
        if not iframe_src:
            continue
        iframe_src = urljoin(url, iframe_src)
        for pattern in _VIDEO_IFRAME_PATTERNS:
            if pattern.search(iframe_src):
                videos.append({
                    "url": iframe_src,
                    "type": "iframe",
                    "poster": "",
                })
                break

    # 同时扫描全页面（不只是正文区域）的 <video> 和 <iframe>
    # 因为有些网站视频播放器在正文容器之外
    if not videos:
        for video_tag in soup.find_all("video"):
            video_url = (video_tag.get("src") or "").strip()
            poster = (video_tag.get("poster") or "").strip()
            if not video_url:
                source_tag = video_tag.find("source")
                if source_tag:
                    video_url = (source_tag.get("src") or "").strip()
            if video_url:
                video_url = urljoin(url, video_url)
                if poster:
                    poster = urljoin(url, poster)
                videos.append({
                    "url": video_url,
                    "type": "mp4",
                    "poster": poster,
                })
                break  # 只取第一个

        if not videos:
            for iframe in soup.find_all("iframe"):
                iframe_src = (iframe.get("src") or iframe.get("data-src") or "").strip()
                if not iframe_src:
                    continue
                iframe_src = urljoin(url, iframe_src)
                for pattern in _VIDEO_IFRAME_PATTERNS:
                    if pattern.search(iframe_src):
                        videos.append({
                            "url": iframe_src,
                            "type": "iframe",
                            "poster": "",
                        })
                        break
                if videos:
                    break

    # 3c. 提取正文 HTML 和纯文本
    content_html = str(content_el)
    # 纯文本
    content_text = content_el.get_text(separator="\n")
    # 清理多余空行
    content_text = re.sub(r"\n\s*\n", "\n\n", content_text).strip()

    # 3d. 确定封面图
    thumbnail = og_image
    if not thumbnail and images:
        thumbnail = images[0]["url"]

    result["content_html"] = content_html
    result["content"] = content_text
    result["images"] = images[:20]  # 最多 20 张
    result["videos"] = videos[:5]   # 最多 5 个
    result["thumbnail"] = thumbnail

    return result


def extract_og_image(html: str, url: str) -> str:
    """快速提取 og:image，用于列表页已有缩略图信息时"""
    try:
        soup = BeautifulSoup(html, "lxml")
        tag = soup.find("meta", property="og:image")
        if tag and tag.get("content"):
            return urljoin(url, tag["content"])
    except Exception:
        pass
    return ""
