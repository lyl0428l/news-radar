"""
通用正文/图片/视频/作者/时间提取器
从新闻详情页 HTML 中提取:
  - content_html: 正文 HTML（保留段落/图片标签）
  - content:      正文纯文本
  - images:       图片列表 [{"url": ..., "caption": ...}, ...]
  - videos:       视频列表 [{"url": ..., "type": "mp4|iframe", "poster": ...}, ...]
  - thumbnail:    封面图 URL
  - author:       作者/发布账号
  - pub_time:     发布时间
"""
import re
import json
import logging
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

# 需要过滤的图片 URL 关键词（logo/icon/广告/追踪像素）
_IMAGE_BLACKLIST_KEYWORDS = [
    "logo", "icon", "avatar", "emoji", "badge", "arrow",
    "btn", "button", "ad_", "advert", "tracker", "pixel",
    "loading", "spinner", "placeholder", "blank", "spacer", "empty",
    "share_", "share.", "wechat", "weibo", "qq_", "facebook", "twitter",
    "google-analytics", "cnzz", "baidu.com/img",
    "icon-share", "share-poster", "share-wechat", "share-moments",
    "share-blog", "share_bbs",
    # 新浪推广图片
    "kaihu", "appendQr", "transform/340", "qr_code", "app2x",
    "w640h130",  # 新浪开户 banner 尺寸
    "w170h170",  # 新浪二维码尺寸
    "cece9e13",  # 新浪通用推广图 hash
    # 通用推广
    "download_app", "app_download", "open-app",
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


def _extract_author(soup: BeautifulSoup) -> str:
    """
    从页面 HTML 提取作者/发布账号。
    按优先级尝试多种方式：
    1. <meta> 标签（og:article:author, author, article:author）
    2. HTML 结构化数据（.author, .byline, [rel=author]）
    3. JSON-LD 中的 author
    """
    # 1. Meta 标签
    for attr_name, attr_val in [
        ("name", "author"),
        ("property", "article:author"),
        ("property", "og:article:author"),
        ("name", "byl"),           # NYT uses this
        ("name", "publisher"),
    ]:
        tag = soup.find("meta", attrs={attr_name: attr_val})
        if tag:
            val = str(tag.get("content", "")).strip()
            if val and not val.isdigit():  # 跳过纯数字 ID（如人民网编辑工号）
                # 清理 "By " 前缀（NYT 的 byl meta 标签格式为 "By Author Name"）
                for prefix in ("By ", "by ", "作者：", "作者:"):
                    if val.startswith(prefix):
                        val = val[len(prefix):].strip()
                        break
                if val:
                    return val[:100]

    # 1b. <meta name="source"> — 人民网等使用（"来源：新华社"）
    source_tag = soup.find("meta", attrs={"name": "source"})
    if source_tag:
        val = str(source_tag.get("content", "")).strip()
        if val:
            # 去除 "来源：" 前缀
            for prefix in ("来源：", "来源:", "Source:", "source:"):
                if val.startswith(prefix):
                    val = val[len(prefix):].strip()
            if val and not val.isdigit():
                return val[:100]

    # 2. HTML 元素
    for selector in [
        ".author", ".byline", ".article-author", ".post-author",
        "[rel='author']", ".writer", ".journalist", ".editor",
        ".news_about .author_name",  # 澎湃
        ".show_author",              # 新浪
        ".post_info .author",        # 网易
        ".article-source",           # 搜狐
        ".source",                   # 腾讯
        "[data-testid='Author']",    # Reuters
        "[data-testid='Byline']",    # Reuters
        "a[href*='/authors/']",      # Reuters 作者链接
    ]:
        try:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(strip=True)
                # 清理常见前缀
                for prefix in ("作者：", "作者:", "来源：", "来源:", "By ", "by ",
                               "记者 ", "记者：", "编辑：", "编辑:"):
                    if text.startswith(prefix):
                        text = text[len(prefix):].strip()
                if text and len(text) < 100:
                    return text
        except Exception:
            continue

    # 3. JSON-LD（支持顶层 author 和 @graph 数组中的 author）
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            # 可能是单个对象或数组（@graph 模式）
            items_to_check = []
            if isinstance(data, dict):
                items_to_check.append(data)
                # Reuters 等使用 @graph 数组
                graph = data.get("@graph", [])
                if isinstance(graph, list):
                    items_to_check.extend(
                        g for g in graph if isinstance(g, dict)
                    )
            elif isinstance(data, list):
                items_to_check.extend(
                    d for d in data if isinstance(d, dict)
                )
            for obj in items_to_check:
                author_data = obj.get("author", {})
                if isinstance(author_data, list) and author_data:
                    author_data = author_data[0]
                if isinstance(author_data, dict):
                    name = author_data.get("name", "")
                    if name:
                        return str(name).strip()[:100]
                elif isinstance(author_data, str) and author_data:
                    return author_data.strip()[:100]
        except Exception:
            continue

    return ""


def _extract_pub_time(soup: BeautifulSoup) -> str:
    """
    从详情页 HTML 提取发布时间。
    按优先级尝试：
    1. <meta> 标签 (article:published_time, datePublished, etc.)
    2. <time> 标签的 datetime 属性
    3. JSON-LD 中的 datePublished
    4. HTML 元素中匹配时间格式的文本
    返回原始时间字符串（由 base.py parse_time 统一解析）。
    """
    # 1. Meta 标签
    for attr_name, attr_val in [
        ("property", "article:published_time"),
        ("property", "og:article:published_time"),
        ("name", "publishdate"),
        ("name", "publish_date"),
        ("name", "pubdate"),
        ("name", "weibo:article:create_at"),
        ("property", "datePublished"),
        ("itemprop", "datePublished"),
    ]:
        tag = soup.find("meta", attrs={attr_name: attr_val})
        if tag:
            val = tag.get("content", "")
            if val and len(str(val)) >= 8:
                return str(val).strip()

    # 2. <time> 标签
    time_tag = soup.find("time", attrs={"datetime": True})
    if time_tag:
        dt = time_tag.get("datetime", "")
        if dt and len(str(dt)) >= 8:
            return str(dt).strip()
    # <time> 不带 datetime 但有文本
    time_tag = soup.find("time")
    if time_tag:
        text = time_tag.get_text(strip=True)
        if text and len(text) >= 8:
            return text

    # 3. JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict):
                for key in ("datePublished", "dateCreated", "uploadDate"):
                    val = data.get(key, "")
                    if val and len(str(val)) >= 8:
                        return str(val).strip()
        except Exception:
            continue

    # 4. HTML 元素中的时间文本
    for selector in [
        ".time", ".date", ".pubdate", ".pub-time", ".article-time",
        ".publish-time", ".news-time", ".article_time", ".post-time",
        "[class*='time']", "[class*='date']",
        ".news_about span",  # 澎湃
    ]:
        try:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(strip=True)
                # 匹配常见日期格式
                m = re.search(r'\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?[\s]*\d{0,2}[:\d]*', text)
                if m:
                    return m.group(0).strip()
        except Exception:
            continue

    return ""


def _is_valid_image_url(url: str) -> bool:
    """验证图片 URL 是否是有效的可下载地址"""
    if not url:
        return False
    # 必须是 http/https 开头
    if not url.startswith(("http://", "https://")):
        return False
    # 过滤 Base64 编码的假 URL（如搜狐的加密路径）
    parsed = urlparse(url)
    path = parsed.path
    # 正常图片路径应该有图片扩展名或者是CDN路径
    # 搜狐的假URL: /a/EPTTjIc7stRUWFVloIo6Ucm... (Base64 junk)
    if len(path) > 50 and not any(path.lower().endswith(ext) for ext in
                                    ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif')):
        # 检查路径是否含有典型的 Base64 字符组合
        if re.search(r'[A-Z][a-z][A-Z].*[+/=]', path):
            return False
    return True


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
            "author": str,         # 作者/发布账号
            "pub_time": str,       # 发布时间（原始字符串）
        }
    """
    result = {
        "content_html": "",
        "content": "",
        "images": [],
        "videos": [],
        "thumbnail": "",
        "author": "",
        "pub_time": "",
    }

    if not html:
        return result

    soup = BeautifulSoup(html, "lxml")

    # --- 第零步：提取作者和发布时间 ---
    result["author"] = _extract_author(soup)
    result["pub_time"] = _extract_pub_time(soup)

    # --- 第一步：从 <meta> 标签提取 og:image 作为候选封面图 ---
    og_image = ""
    og_tag = soup.find("meta", property="og:image")
    if og_tag and og_tag.get("content"):
        og_image = urljoin(url, og_tag["content"])

    # --- 第二步：定位正文容器 ---
    content_el = None
    # 保留短文本的备选容器（选择器匹配但文本 <= 100 字符的情况）
    _short_content_el = None

    # 2a. 尝试指定的 CSS 选择器
    if selectors:
        for sel in selectors:
            try:
                el = soup.select_one(sel)
                if el:
                    text_len = len(el.get_text(strip=True))
                    if text_len > 100:
                        content_el = el
                        break
                    elif text_len > 0 and _short_content_el is None:
                        # 记住第一个有文本但较短的匹配（短新闻/快讯）
                        _short_content_el = el
            except Exception:
                continue

    # 2b. 回退到 readability
    if content_el is None:
        readable_html = _extract_readability_html(html, url)
        if readable_html:
            readable_el = BeautifulSoup(readable_html, "lxml").body
            if readable_el is None:
                readable_el = BeautifulSoup(readable_html, "lxml")
            # readability 提取到有效文本才使用，否则回退到短文本容器
            if readable_el and len(readable_el.get_text(strip=True)) > 0:
                content_el = readable_el

    # 2c. 如果 readability 也没有结果，使用短文本容器（短新闻/快讯场景）
    if content_el is None and _short_content_el is not None:
        content_el = _short_content_el

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

    # 澎湃等网站在 <noscript> 中放真实 img，先提取这些
    for noscript in content_el.find_all("noscript"):
        ns_soup = BeautifulSoup(str(noscript), "lxml")
        for img in ns_soup.find_all("img"):
            img_url = (img.get("src") or "").strip()
            if img_url:
                img_url = urljoin(url, img_url)
                if (not _is_blacklisted_image(img_url) and
                        _is_valid_image_url(img_url) and img_url not in seen_urls):
                    seen_urls.add(img_url)
                    caption = (img.get("alt") or img.get("title") or "").strip()
                    images.append({"url": img_url, "caption": caption})

    for img in content_el.find_all("img"):
        # 尝试多种属性获取图片 URL（覆盖各种 lazy-load 方案）
        # data-src 等 lazy-load 属性优先于 src，因为很多网站 src 指向占位图
        img_url = ""
        for attr in ("data-src", "data-original", "data-lazy-src",
                      "data-actualsrc", "data-url", "data-echo",
                      "data-lazy", "data-original-src", "data-hi-res",
                      "data-lazyload",
                      "src", "srcset"):
            val = img.get(attr)
            if val:
                val = str(val).strip()
                # srcset 取第一个 URL
                if attr == "srcset" and val:
                    val = val.split(",")[0].strip().split()[0]
                if val and not val.startswith("data:"):
                    img_url = val
                    break
        if not img_url:
            continue
        img_url = urljoin(url, img_url)

        if _is_blacklisted_image(img_url):
            continue
        if not _is_valid_image_url(img_url):
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

    # 扫描自定义 video_src 属性（新华网等使用 <span video_src="...mp4"> 嵌入视频）
    for tag in content_el.find_all(attrs={"video_src": True}):
        video_url = str(tag.get("video_src", "")).strip()
        if video_url:
            video_url = urljoin(url, video_url)
            poster = str(tag.get("poster", "")).strip()
            if poster:
                poster = urljoin(url, poster)
            if video_url not in {v["url"] for v in videos}:
                videos.append({
                    "url": video_url,
                    "type": "mp4",
                    "poster": poster,
                })

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

    # 扫描全页面的 video_src 属性（如新华网将视频放在正文容器之外）
    if not videos:
        for tag in soup.find_all(attrs={"video_src": True}):
            video_url = str(tag.get("video_src", "")).strip()
            if video_url:
                video_url = urljoin(url, video_url)
                poster = str(tag.get("poster", "")).strip()
                if poster:
                    poster = urljoin(url, poster)
                videos.append({
                    "url": video_url,
                    "type": "mp4",
                    "poster": poster,
                })
                break  # 只取第一个

    # 3c. 清理正文中的推广/无关元素
    # 网易 "打开网易新闻 查看更多图片" 等 App 推广文字
    _PROMO_SELECTORS = [
        ".ne-open-app",           # 网易 App 推广按钮
        ".open-app-wrap",         # 网易 App 推广容器
        ".article-open-app",     # 网易 App 推广
        ".js-open-app",          # 网易 JS 打开 App
        ".share-module",         # 分享模块
        ".bza",                  # 人民网分享区
    ]
    for sel in _PROMO_SELECTORS:
        try:
            for el in content_el.select(sel):
                el.decompose()
        except Exception:
            pass

    # 移除包含特定推广文字的元素
    _PROMO_TEXTS = ["打开网易新闻", "查看更多图片", "打开APP", "下载APP",
                    "特别声明：以上内容", "举报/反馈"]
    for tag in content_el.find_all(["p", "div", "span", "a"]):
        text = tag.get_text(strip=True)
        if text and any(pt in text for pt in _PROMO_TEXTS):
            # 只移除短文本元素（避免误删长段落中包含这些词的情况）
            if len(text) < 50:
                tag.decompose()

    # 3d. 修复 lazy-load 图片：将 data-src 写入 src
    for img in content_el.find_all("img"):
        src = img.get("src", "")
        data_src = ""
        for attr in ("data-src", "data-original", "data-lazy-src",
                      "data-actualsrc", "data-url", "data-lazyload"):
            val = img.get(attr)
            if val and str(val).strip() and not str(val).strip().startswith("data:"):
                data_src = str(val).strip()
                break
        # 如果 src 是占位图或空，用 data-src 替换
        if data_src and (not src or "empty" in src or "placeholder" in src
                         or "loading" in src or "blank" in src
                         or src.startswith("data:")):
            img["src"] = urljoin(url, data_src)

    # 3e. 如果正文容器内图片太少，从全页面补充
    if len(images) < 2:
        _supplement_images_from_page(soup, images, seen_urls, url)

    # 3f. 提取正文 HTML 和纯文本
    content_html = str(content_el)
    # 纯文本
    content_text = content_el.get_text(separator="\n")
    # 清理多余空行
    content_text = re.sub(r"\n\s*\n", "\n\n", content_text).strip()

    # 3g. 确定封面图
    thumbnail = og_image
    if not thumbnail and images:
        thumbnail = images[0]["url"]

    result["content_html"] = sanitize_html(content_html)
    result["content"] = content_text
    result["images"] = images[:20]  # 最多 20 张
    result["videos"] = videos[:5]   # 最多 5 个
    result["thumbnail"] = thumbnail

    return result


def _supplement_images_from_page(soup, images: list, seen_urls: set, url: str):
    """
    当正文容器内图片不足时，从全页面补充图片。
    搜索范围：
      1. <figure> 中的 <img>（文章配图通常在 figure 中）
      2. <noscript> 中的 <img>（懒加载的 fallback 图片）
      3. 文章相关大区域中的 <img>（class/id 含 article/content/body 的容器）
      4. JSON-LD 中的 image 字段
    """
    # 来源 1: <figure> 中的图片
    for fig in soup.find_all("figure"):
        for img in fig.find_all("img"):
            img_url = _get_best_img_url(img)
            if not img_url or img_url in seen_urls:
                continue
            if _is_blacklisted_image(img_url) or not _is_valid_image_url(img_url):
                continue
            img_url = urljoin(url, img_url)
            seen_urls.add(img_url)
            caption = (img.get("alt") or "").strip()
            figcap = fig.find("figcaption")
            if figcap and not caption:
                caption = figcap.get_text(strip=True)
            images.append({"url": img_url, "caption": caption})

    # 来源 2: <noscript> 中的图片（懒加载 fallback）
    for ns in soup.find_all("noscript"):
        for img in ns.find_all("img"):
            img_url = img.get("src", "")
            if not img_url or img_url.startswith("data:") or img_url in seen_urls:
                continue
            if _is_blacklisted_image(img_url) or not _is_valid_image_url(img_url):
                continue
            img_url = urljoin(url, img_url)
            seen_urls.add(img_url)
            images.append({"url": img_url, "caption": img.get("alt", "")})

    # 来源 3: 文章大区域中的 data-src 懒加载图片
    for container in soup.find_all(["div", "article", "section"],
                                    class_=re.compile(r"article|content|body|text|detail|news|story", re.I)):
        for img in container.find_all("img"):
            img_url = _get_best_img_url(img)
            if not img_url or img_url in seen_urls:
                continue
            if _is_blacklisted_image(img_url) or not _is_valid_image_url(img_url):
                continue
            img_url = urljoin(url, img_url)
            seen_urls.add(img_url)
            images.append({"url": img_url, "caption": img.get("alt", "")})
        if len(images) >= 10:
            break

    # 来源 4: JSON-LD 中的 image 字段
    import json as _json
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = _json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for d in items:
                if not isinstance(d, dict):
                    continue
                img_field = d.get("image", [])
                if isinstance(img_field, str):
                    img_field = [{"url": img_field}]
                elif isinstance(img_field, dict):
                    img_field = [img_field]
                for im in (img_field if isinstance(img_field, list) else []):
                    u = im.get("url", "") if isinstance(im, dict) else str(im)
                    if u and u not in seen_urls:
                        if not _is_blacklisted_image(u) and _is_valid_image_url(u):
                            u = urljoin(url, u)
                            seen_urls.add(u)
                            images.append({"url": u, "caption": ""})
        except (_json.JSONDecodeError, TypeError):
            continue


def _get_best_img_url(img) -> str:
    """从 img 标签中提取最佳图片 URL（优先 data-src 系列）"""
    for attr in ("data-src", "data-original", "data-lazy-src",
                  "data-actualsrc", "data-url", "data-echo",
                  "data-lazy", "data-original-src", "data-lazyload",
                  "src"):
        val = img.get(attr)
        if val:
            val = str(val).strip()
            if val and not val.startswith("data:"):
                return val
    return ""


def sanitize_html(html: str) -> str:
    """
    对 content_html 进行 XSS 消毒。
    白名单策略：只允许安全的 HTML 标签和属性，移除所有脚本和事件处理器。
    """
    try:
        import bleach
    except ImportError:
        # bleach 未安装时回退到正则清理
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"\s+on\w+\s*=\s*[\"'][^\"']*[\"']", "", html, flags=re.IGNORECASE)
        return html

    allowed_tags = [
        "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
        "strong", "em", "b", "i", "u", "s", "del", "ins", "mark",
        "blockquote", "pre", "code", "span", "div",
        "ul", "ol", "li", "dl", "dt", "dd",
        "a", "img", "figure", "figcaption", "picture", "source",
        "video", "audio", "table", "thead", "tbody", "tr", "th", "td",
        "sup", "sub", "abbr", "time",
    ]
    allowed_attrs = {
        "*": ["class", "id", "style", "data-hls-src", "data-component", "data-testid"],
        "a": ["href", "title", "target", "rel"],
        "img": ["src", "alt", "title", "width", "height", "loading", "srcset", "sizes"],
        "video": ["src", "poster", "controls", "preload", "playsinline", "data-hls-src"],
        "audio": ["src", "controls", "preload"],
        "source": ["src", "srcset", "type", "media"],
        "time": ["datetime"],
        "td": ["colspan", "rowspan"],
        "th": ["colspan", "rowspan"],
    }
    # 允许的 URL 协议
    allowed_protocols = ["http", "https", "data"]

    return bleach.clean(
        html,
        tags=allowed_tags,
        attributes=allowed_attrs,
        protocols=allowed_protocols,
        strip=True,
    )


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
