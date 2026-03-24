"""
New York Times 爬虫 - 通过 RSS 爬取 NYT 头条 TOP 10

反爬策略:
  NYT 有 paywall + 403 拦截。BaseCrawler._request() 中的 curl_cffi 自动回退
  已解决 403 问题（TLS 指纹绕过），无需额外处理。
  Google Web Cache 仅作为最终后备。

内容提取:
  NYT 使用 React SSR，正文在 article section 中。
  图片分布在:
    1. <figure> 中的 <img>/<picture>/<source> 标签（hero 图片有 src，正文图片懒加载）
    2. JSON-LD 中的 image 字段
    3. HTML 中 static01.nyt.com 的图片 URL
  本爬虫自定义 parse_detail() 整合所有图片来源，清洗 NYT UI 垃圾。
"""
import re
import json
import time
import random
import logging
from bs4 import BeautifulSoup
from config import DETAIL_FETCH_TIMEOUT
from crawlers.base import RSSCrawler

logger = logging.getLogger(__name__)

# NYT 图片 URL 模式（文章配图，排除 logo/icon/author-avatar）
_NYT_IMAGE_PATTERN = re.compile(
    r"https://static01\.nyt\.com/images/\d{4}/\d{2}/\d{2}/(?:multimedia|business|world|"
    r"us|arts|sports|science|technology|style|magazine|opinion|books|travel|food|"
    r"health|podcasts|video|climate)[^\"'\s)]+\.(?:jpg|jpeg|png|webp)",
    re.IGNORECASE,
)

# NYT 中需要移除的非正文元素
_NYT_REMOVE_SELECTORS = [
    "[data-testid='inline-message']",       # 付费墙提示
    "[data-testid='bottom']",               # 底部推荐
    "[data-testid='standard-dock']",        # 底部工具栏
    "[data-testid='share-tools']",          # 分享按钮
    "[class*='newsletter']",                # 邮件订阅
    "[class*='related']",                   # 相关文章
    "[data-testid='in-story-masthead']",    # 文章头导航
    ".ad",                                  # 广告
    "svg",                                  # SVG 图标
    "button",                               # 按钮
    "[role='toolbar']",                     # 工具栏
    "[aria-label='Site Navigation']",       # 导航栏
    "[data-testid='photoviewer-children']", # 图片查看器 UI
]


class NYTCrawler(RSSCrawler):

    detail_selectors = [
        "[name='articleBody']",
        ".meteredContent",
        ".StoryBodyCompanionColumn",
        "article section",
    ]

    def __init__(self):
        super().__init__()
        self.name = "nyt"
        self.display_name = "NYT"
        self.language = "en"
        self.rss_url = "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"
        self.category = "Top Stories"

    # ================================================================
    #  详情页获取
    # ================================================================

    def fetch_detail(self, item: dict) -> dict:
        """
        NYT 详情页获取。
        _request() 内部遇到 403 会自动触发 curl_cffi TLS 绕过。
        Google Web Cache 作为最终后备。
        """
        url = item.get("url", "")
        if not url:
            return {}

        time.sleep(random.uniform(1.5, 3.0))

        # 策略 1：直接请求（curl_cffi 自动回退处理 403）
        try:
            resp = self._request(url, timeout=DETAIL_FETCH_TIMEOUT, headers={
                "Referer": "https://www.google.com/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "cross-site",
            })
            if resp and resp.status_code == 200:
                result = self.parse_detail(resp.text, url)
                if result.get("content") and len(result["content"]) > 200:
                    return result
        except Exception as e:
            logger.debug(f"[nyt] 直接请求失败: {url[:60]} | {e}")

        time.sleep(random.uniform(1.0, 2.0))

        # 策略 2：Google Web Cache（后备）
        try:
            cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{url}"
            resp = self._request(cache_url, timeout=DETAIL_FETCH_TIMEOUT, skip_cffi=True)
            if resp and resp.status_code == 200:
                result = self.parse_detail(resp.text, url)
                if result.get("content") and len(result["content"]) > 200:
                    return result
        except Exception as e:
            logger.debug(f"[nyt] Google Cache 失败: {url[:60]} | {e}")

        return {}

    # ================================================================
    #  页面解析
    # ================================================================

    def parse_detail(self, html: str, url: str) -> dict:
        """
        自定义 NYT 页面解析:
        1. 通用提取器获取正文
        2. NYT 专有图片提取（HTML img/source + JSON-LD + 正则）
        3. 清洗 NYT UI 垃圾
        """
        from utils.content_extractor import extract_content
        result = extract_content(html, url, selectors=self.detail_selectors)

        # 补充 NYT 图片提取
        nyt_images = self._extract_nyt_images(html, url)
        if nyt_images and not result.get("images"):
            result["images"] = nyt_images
        elif nyt_images and result.get("images"):
            # 合并，去重
            existing_urls = {img.get("url", "") for img in result["images"]}
            for img in nyt_images:
                if img["url"] not in existing_urls:
                    result["images"].append(img)
                    existing_urls.add(img["url"])

        # 设置缩略图
        if not result.get("thumbnail") and result.get("images"):
            result["thumbnail"] = result["images"][0].get("url", "")

        # 清洗 content_html
        if result.get("content_html"):
            result["content_html"] = self._clean_nyt_html(
                result["content_html"], result.get("images", [])
            )
            # 同步纯文本
            try:
                text = BeautifulSoup(result["content_html"], "lxml").get_text(separator="\n")
                text = re.sub(r"\n\s*\n", "\n\n", text).strip()
                if text and len(text) > len(result.get("content", "")):
                    result["content"] = text
            except Exception:
                pass

        # 补充作者（从 JSON-LD 提取）
        if not result.get("author"):
            result["author"] = self._extract_nyt_author(html)

        return result

    # ================================================================
    #  NYT 图片提取
    # ================================================================

    def _extract_nyt_images(self, html: str, url: str) -> list:
        """
        多来源提取 NYT 文章图片:
        1. <figure> 中的 <img>/<picture>/<source> 标签
        2. JSON-LD 中的 image 字段
        3. 正则匹配 static01.nyt.com 图片 URL
        """
        images = []
        seen_urls = set()
        soup = BeautifulSoup(html, "lxml")

        # 来源 1: <figure> 中的图片
        for fig in soup.find_all("figure"):
            img_url = ""
            caption = ""

            # 优先从 <picture><source> 中提取高分辨率图
            for source in fig.find_all("source"):
                srcset = source.get("srcset", "")
                if srcset and "static01.nyt.com" in srcset:
                    # srcset 格式: "url1 widthw, url2 widthw"
                    parts = srcset.split(",")
                    best_url = ""
                    best_width = 0
                    for part in parts:
                        part = part.strip()
                        match = re.match(r"(\S+)\s+(\d+)w", part)
                        if match:
                            u, w = match.group(1), int(match.group(2))
                            if w > best_width:
                                best_url, best_width = u, w
                    if best_url:
                        img_url = best_url
                        break

            # 回退到 <img> 标签
            if not img_url:
                img_tag = fig.find("img")
                if img_tag:
                    img_url = img_tag.get("src", "")
                    if not img_url or "data:" in img_url:
                        img_url = img_tag.get("data-src", "")

            # 提取 figcaption
            figcap = fig.find("figcaption")
            if figcap:
                caption = figcap.get_text(strip=True)

            # 过滤
            if not img_url or "static01.nyt.com" not in img_url:
                continue
            if self._is_nyt_icon(img_url):
                continue
            if img_url in seen_urls:
                continue
            seen_urls.add(img_url)

            images.append({"url": img_url, "caption": caption})

        # 来源 2: JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                items_list = data if isinstance(data, list) else [data]
                for d in items_list:
                    if not isinstance(d, dict):
                        continue
                    img_field = d.get("image", [])
                    if isinstance(img_field, str):
                        img_field = [{"url": img_field}]
                    elif isinstance(img_field, dict):
                        img_field = [img_field]
                    for im in img_field:
                        u = im.get("url", "") if isinstance(im, dict) else str(im)
                        if u and "static01.nyt.com" in u and u not in seen_urls:
                            if not self._is_nyt_icon(u):
                                seen_urls.add(u)
                                images.append({"url": u, "caption": ""})
            except (json.JSONDecodeError, TypeError):
                continue

        # 来源 3: 正则全文搜索
        for match in _NYT_IMAGE_PATTERN.finditer(html):
            img_url = match.group(0)
            # 取最高画质（去掉尺寸后缀）
            clean_url = re.sub(r"-\d+x\d+\.", ".", img_url)
            if clean_url not in seen_urls and not self._is_nyt_icon(clean_url):
                seen_urls.add(clean_url)
                images.append({"url": clean_url, "caption": ""})

        return images[:20]  # 限制最多 20 张

    @staticmethod
    def _is_nyt_icon(url: str) -> bool:
        """判断是否是 NYT 的 logo/icon/作者头像（非文章配图）"""
        skip_patterns = (
            "/icons/", "/logos/", "/reader-center/author-",
            "t_logo_", "nyt-logo", "favicon",
        )
        return any(p in url for p in skip_patterns)

    # ================================================================
    #  NYT 作者提取
    # ================================================================

    @staticmethod
    def _extract_nyt_author(html: str) -> str:
        """从 JSON-LD 提取作者"""
        try:
            soup = BeautifulSoup(html, "lxml")
            for script in soup.find_all("script", type="application/ld+json"):
                data = json.loads(script.string or "")
                items_list = data if isinstance(data, list) else [data]
                for d in items_list:
                    if not isinstance(d, dict):
                        continue
                    authors = d.get("author", [])
                    if isinstance(authors, dict):
                        authors = [authors]
                    if isinstance(authors, list):
                        names = [a.get("name", "") for a in authors
                                 if isinstance(a, dict) and a.get("name")]
                        if names:
                            return ", ".join(names)
        except Exception:
            pass
        return ""

    # ================================================================
    #  NYT HTML 清洗
    # ================================================================

    @staticmethod
    def _clean_nyt_html(content_html: str, images: list) -> str:
        """
        清洗 NYT 页面中的 UI 垃圾，并将图片嵌入正文。
        NYT 正文中的图片大多是懒加载的空 <figure>，需要用提取到的图片 URL 填充。
        """
        soup = BeautifulSoup(content_html, "lxml")

        # 1. 移除 UI 垃圾
        for selector in _NYT_REMOVE_SELECTORS:
            for el in soup.select(selector):
                el.decompose()

        # 2. 修复空 <figure>：填充图片
        img_idx = 0
        for fig in soup.find_all("figure"):
            existing_img = fig.find("img")
            # 如果 figure 已有可见图片，跳过
            if existing_img and existing_img.get("src") and "data:" not in existing_img.get("src", ""):
                continue
            # 用提取到的图片填充
            if img_idx < len(images):
                img_data = images[img_idx]
                img_idx += 1
                # 创建 img 标签
                new_img = soup.new_tag(
                    "img",
                    src=img_data["url"],
                    alt=img_data.get("caption", ""),
                    style="width:100%;border-radius:8px;",
                    loading="lazy",
                )
                # 清空 figure 中的旧内容，保留 figcaption
                figcap = fig.find("figcaption")
                fig.clear()
                fig.append(new_img)
                if figcap:
                    fig.append(figcap)

        return str(soup)

    # ================================================================
    #  串行抓取（避免触发封禁）
    # ================================================================

    def _fetch_all_details(self, items: list) -> list:
        """NYT 串行抓取，避免并发触发封禁"""
        if not items:
            return items

        skip_urls = set()
        try:
            from storage import check_urls_have_content
            all_urls = [item.get("url", "") for item in items if item.get("url")]
            skip_urls = check_urls_have_content(all_urls)
            if skip_urls:
                self.logger.info(f"[{self.name}] 跳过 {len(skip_urls)} 条已有正文的新闻")
        except Exception:
            pass

        for item in items:
            if item.get("url", "") in skip_urls:
                continue
            detail = self.fetch_detail(item)
            if detail:
                for key in ("content", "content_html", "images", "videos",
                            "thumbnail", "author"):
                    if detail.get(key) and not item.get(key):
                        item[key] = detail[key]
                if detail.get("pub_time") and not item.get("pub_time"):
                    parsed = self.parse_time(detail["pub_time"])
                    if parsed:
                        item["pub_time"] = parsed
                if not item.get("summary") and detail.get("content"):
                    item["summary"] = self.extract_summary(detail["content"])

        items.sort(key=lambda x: x.get("rank", 999))
        return items
