"""
Reuters 路透社爬虫 - 多级策略确保稳定抓取

Reuters 使用 DataDome 企业级反爬保护，文章页面被 401 拦截。
直接请求、Playwright、各种 Referer 伪装均会被拦截。

稳定方案:
  列表获取:
    1. Reuters News Sitemap（最可靠，Googlebot UA 可访问，含图片 URL）
    2. Google News RSS（后备，需解析重定向获取真实 URL）
  全文获取:
    1. 直接请求 + Fusion.globalContent JSON 提取（DataDome 间歇性放行）
    2. Google Web Cache（逐步废弃中，作为后备）
    3. 如以上全部失败，从 Sitemap 描述 + RSS 摘要中拼凑摘要

Reuters 使用 Arc Publishing (Fusion) 框架，正文数据嵌入在页面的
Fusion.globalContent JSON 中的 content_elements 数组里。
"""
import re
import json
import time
import random
import logging
import requests
import feedparser
from bs4 import BeautifulSoup
from config import DETAIL_FETCH_TIMEOUT
from crawlers.base import BaseCrawler

logger = logging.getLogger(__name__)

# Googlebot UA - 用于访问 News Sitemap（Reuters robots.txt 允许）
_GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

# News Sitemap URL
_NEWS_SITEMAP_URL = "https://www.reuters.com/arc/outboundfeeds/news-sitemap/?outputType=xml"

# Google News RSS
_GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search"
    "?q=site:reuters.com+when:1d&hl=en-US&gl=US&ceid=US:en"
)


class ReutersCrawler(BaseCrawler):

    detail_selectors = [
        "[data-testid='article-body']",
        ".article-body__content",
        "article",
        "[class*='ArticleBody']",
    ]

    def __init__(self):
        super().__init__()
        self.name = "reuters"
        self.display_name = "Reuters"
        self.language = "en"
        # Sitemap 中提取的图片映射 {article_url: image_url}
        self._sitemap_images: dict[str, str] = {}

    # ================================================================
    #  列表获取
    # ================================================================

    def crawl(self) -> list[dict]:
        """
        获取 Reuters TOP 10 新闻列表。
        优先使用 News Sitemap，后备 Google News RSS。
        """
        # 方案 1: News Sitemap（最可靠）
        results = self._crawl_sitemap()
        if results:
            return results

        # 方案 2: Google News RSS
        results = self._crawl_google_news_rss()
        if results:
            return results

        self.logger.warning("[reuters] 所有列表获取方案均失败")
        return []

    # 非英文 URL 路径前缀（需过滤）
    _NON_EN_PREFIXES = ("/es/", "/de/", "/ja/", "/fr/", "/pt/", "/ar/", "/zh/")

    def _crawl_sitemap(self) -> list[dict]:
        """
        从 Reuters News Sitemap 获取最新文章列表。
        Sitemap 结构:
          <url>
            <loc>文章 URL</loc>
            <lastmod>修改时间</lastmod>
            <news:news>
              <news:language>en</news:language>
              <news:title><![CDATA[标题]]></news:title>
            </news:news>
            <image:image>
              <image:loc>图片 URL</image:loc>
              <image:caption><![CDATA[图片说明]]></image:caption>
            </image:image>
          </url>
        """
        try:
            resp = requests.get(_NEWS_SITEMAP_URL, timeout=15,
                                headers={"User-Agent": _GOOGLEBOT_UA})
            if resp.status_code != 200:
                self.logger.debug(f"[reuters] Sitemap HTTP {resp.status_code}")
                return []

            xml = resp.text

            # 按 <url>...</url> 逐条解析
            url_blocks = re.findall(r"<url>(.*?)</url>", xml, re.DOTALL)
            if not url_blocks:
                return []

            results = []
            seen = set()
            rank = 1

            for block in url_blocks:
                # 提取 URL
                loc_m = re.search(r"<loc>(https://www\.reuters\.com/[^<]+)</loc>", block)
                if not loc_m:
                    continue
                art_url = loc_m.group(1).replace("&amp;", "&")

                # 过滤非英文
                if any(art_url.startswith(f"https://www.reuters.com{p}") for p in self._NON_EN_PREFIXES):
                    continue
                # 过滤非文章（没有日期的 URL）
                if not re.search(r"\d{4}-\d{2}-\d{2}", art_url):
                    continue
                if art_url in seen:
                    continue
                seen.add(art_url)

                # 提取标题（CDATA 包裹）
                title_m = re.search(
                    r"<news:title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</news:title>", block
                )
                title = title_m.group(1).strip() if title_m else ""
                if not title:
                    slug = art_url.rstrip("/").split("/")[-1]
                    title = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", slug).replace("-", " ").title()

                # 提取图片
                img_m = re.search(r"<image:loc>([^<]+)</image:loc>", block)
                thumbnail = img_m.group(1).replace("&amp;", "&") if img_m else ""
                if thumbnail:
                    self._sitemap_images[art_url] = thumbnail

                # 提取图片说明
                cap_m = re.search(
                    r"<image:caption>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</image:caption>", block
                )
                img_caption = cap_m.group(1).strip() if cap_m else ""

                # 提取时间
                time_m = re.search(r"<lastmod>([^<]+)</lastmod>", block)
                pub_time = self.parse_time(time_m.group(1)) if time_m else ""

                results.append(self._make_item(
                    title=title,
                    url=art_url,
                    rank=rank,
                    category="Top Stories",
                    pub_time=pub_time,
                    thumbnail=thumbnail,
                ))
                rank += 1
                if rank > 10:
                    break

            if results:
                self.logger.info(f"[reuters] Sitemap 获取 {len(results)} 条新闻")
            return results

        except Exception as e:
            self.logger.debug(f"[reuters] Sitemap 失败: {e}")
            return []

    def _crawl_google_news_rss(self) -> list[dict]:
        """从 Google News RSS 获取 Reuters 文章列表"""
        try:
            resp = requests.get(_GOOGLE_NEWS_RSS, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                return []
            feed = feedparser.parse(resp.content)
            if not feed.entries:
                return []

            results = []
            seen = set()
            rank = 1
            for entry in feed.entries[:20]:
                title = str(entry.get("title", "")).strip()
                link = str(entry.get("link", "")).strip()
                # 解析 Google News 重定向
                link = self._resolve_google_news_url(link)
                if "reuters.com" not in link:
                    continue
                if not title or link in seen:
                    continue
                seen.add(link)

                summary = str(entry.get("summary", ""))
                if summary and "<" in summary:
                    summary = self.clean_text(summary)[:200]
                else:
                    summary = summary[:200]

                pub_time = str(entry.get("published", ""))

                results.append(self._make_item(
                    title=title,
                    url=link,
                    rank=rank,
                    summary=summary,
                    category="Top Stories",
                    pub_time=self.parse_time(pub_time),
                ))
                rank += 1
                if rank > 10:
                    break

            if results:
                self.logger.info(f"[reuters] Google News RSS 获取 {len(results)} 条")
            return results

        except Exception as e:
            self.logger.debug(f"[reuters] Google News RSS 失败: {e}")
            return []

    def _resolve_google_news_url(self, gnews_url: str) -> str:
        """解析 Google News 重定向 URL"""
        if "reuters.com" in gnews_url:
            return gnews_url
        if "news.google.com" not in gnews_url:
            return gnews_url
        try:
            resp = requests.head(gnews_url, allow_redirects=True, timeout=8,
                                 headers={"User-Agent": "Mozilla/5.0"})
            if "reuters.com" in resp.url:
                return resp.url
        except Exception:
            pass
        return gnews_url

    # ================================================================
    #  详情页获取（多级回退）
    # ================================================================

    def fetch_detail(self, item: dict) -> dict:
        """
        获取文章详情：多级回退策略。
        Level 1: _request()（普通请求 → 被401时自动触发 base 层 curl_cffi 绕过）
        Level 2: Google Web Cache（逐步废弃中，作为最终后备）
        """
        url = item.get("url", "")
        if not url:
            return {}

        time.sleep(random.uniform(1.5, 3.0))

        # Level 1: _request() — 内部自动处理 401 → curl_cffi 回退
        try:
            resp = self._request(url, timeout=DETAIL_FETCH_TIMEOUT, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.google.com/",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "cross-site",
            })
            if resp and resp.status_code == 200 and len(resp.text) > 5000:
                result = self._parse_reuters_page(resp.text, url)
                if result and result.get("content") and len(result["content"]) > 100:
                    return result
        except Exception as e:
            self.logger.debug(f"[reuters] 直接请求失败: {url[:60]} | {e}")

        time.sleep(random.uniform(1.0, 2.0))

        # Level 2: Google Web Cache（后备）
        try:
            cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{url}"
            resp = self._request(cache_url, timeout=DETAIL_FETCH_TIMEOUT, skip_cffi=True)
            if resp and resp.status_code == 200 and len(resp.text) > 5000:
                result = self._parse_reuters_page(resp.text, url)
                if result and result.get("content") and len(result["content"]) > 100:
                    return result
        except Exception as e:
            self.logger.debug(f"[reuters] Google Cache 失败: {url[:60]} | {e}")

        return {}

    # ================================================================
    #  Reuters 页面解析（Fusion/Arc Publishing 架构）
    # ================================================================

    def _parse_reuters_page(self, html: str, url: str) -> dict:
        """
        解析 Reuters 文章页面。
        优先从 Fusion.globalContent JSON 提取结构化数据，
        后备使用 CSS 选择器 + 通用提取器。
        """
        # 尝试从 Fusion JSON 提取
        result = self._extract_from_fusion(html, url)
        if result and result.get("content") and len(result["content"]) > 100:
            return result

        # 后备：使用通用提取器
        from utils.content_extractor import extract_content
        result = extract_content(html, url, selectors=self.detail_selectors)

        # 清洗 Reuters HTML 垃圾
        if result.get("content_html"):
            result["content_html"] = self._clean_reuters_html(result["content_html"])

        return result

    def _extract_from_fusion(self, html: str, url: str) -> dict:
        """
        从 Fusion.globalContent JSON 中提取文章数据。
        Reuters 使用 Arc Publishing 的 Fusion 框架，文章数据嵌入在页面 JS 中。
        """
        # 匹配 Fusion.globalContent = {...};
        m = re.search(
            r'Fusion\.globalContent\s*=\s*(\{.+?\});\s*Fusion\.',
            html, re.DOTALL
        )
        if not m:
            return {}

        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return {}

        result_data = data.get("result", data)

        # 提取标题
        title = result_data.get("title", "")

        # 提取作者
        authors = result_data.get("authors", [])
        author = ", ".join(a.get("name", "") for a in authors if a.get("name"))

        # 提取发布时间
        pub_time = result_data.get("published_time", result_data.get("display_time", ""))

        # 提取缩略图
        thumbnail = ""
        thumb_data = result_data.get("thumbnail", {})
        if isinstance(thumb_data, dict):
            thumbnail = thumb_data.get("url", "")
            # 有时缩略图在 resizes 中
            resizes = thumb_data.get("resizes", [])
            if resizes and isinstance(resizes[0], dict):
                thumbnail = resizes[0].get("url", thumbnail)

        # 提取正文（content_elements）
        content_elements = result_data.get("content_elements", [])
        text_parts = []
        html_parts = []
        images = []
        videos = []

        for el in content_elements:
            if not isinstance(el, dict):
                continue
            el_type = el.get("type", "")

            if el_type in ("text", "paragraph"):
                content_html = el.get("content", "")
                if content_html:
                    html_parts.append(f"<p>{content_html}</p>")
                    # 清理 HTML 标签获取纯文本
                    text = BeautifulSoup(content_html, "lxml").get_text(strip=True)
                    if text:
                        text_parts.append(text)

            elif el_type == "header":
                level = el.get("level", 2)
                content = el.get("content", "")
                if content:
                    html_parts.append(f"<h{level}>{content}</h{level}>")
                    text = BeautifulSoup(content, "lxml").get_text(strip=True)
                    if text:
                        text_parts.append(text)

            elif el_type == "image":
                img_url = el.get("url", "")
                caption = el.get("caption", "")
                alt = el.get("alt_text", caption)
                if img_url:
                    images.append({"url": img_url, "caption": caption})
                    img_tag = f'<figure><img src="{img_url}" alt="{alt}" style="width:100%;border-radius:8px;"/>'
                    if caption:
                        img_tag += f'<figcaption style="color:#666;font-size:13px;margin-top:4px;">{caption}</figcaption>'
                    img_tag += '</figure>'
                    html_parts.append(img_tag)

            elif el_type == "video":
                vid_url = el.get("url", "")
                if vid_url:
                    videos.append({"url": vid_url, "type": "mp4"})
                    html_parts.append(
                        f'<video controls preload="metadata" style="width:100%;border-radius:8px;">'
                        f'<source src="{vid_url}" type="video/mp4">视频无法播放</video>'
                    )

            elif el_type == "list":
                items = el.get("items", [])
                list_type = el.get("list_type", "unordered")
                tag = "ol" if list_type == "ordered" else "ul"
                li_html = "".join(f"<li>{it.get('content', '')}</li>" for it in items if isinstance(it, dict))
                if li_html:
                    html_parts.append(f"<{tag}>{li_html}</{tag}>")

        # 如果没有 content_elements，直接返回空
        if not text_parts:
            return {}

        content = "\n\n".join(text_parts)
        content_html = "\n".join(f"<p>{p}</p>" if not p.startswith("<") else p for p in html_parts)

        # 如果 Sitemap 有图片但 content_elements 没有，补充
        sitemap_img = self._sitemap_images.get(url, "")
        if sitemap_img and not thumbnail:
            thumbnail = sitemap_img
        if sitemap_img and not images:
            images.append({"url": sitemap_img, "caption": ""})

        return {
            "title": title,
            "content": content,
            "content_html": content_html,
            "images": images,
            "videos": videos,
            "thumbnail": thumbnail,
            "author": author,
            "pub_time": pub_time,
        }

    @staticmethod
    def _clean_reuters_html(content_html: str) -> str:
        """清洗 Reuters HTML 中的广告和导航垃圾"""
        soup = BeautifulSoup(content_html, "lxml")
        # 移除广告、导航、推荐等
        for sel in [
            "[data-testid='ad']", ".ad-slot", "[class*='advertisement']",
            "[data-testid='related']", ".trust-badge", "[class*='newsletter']",
            "svg", "button",
        ]:
            for el in soup.select(sel):
                el.decompose()
        return str(soup)

    # ================================================================
    #  详情页抓取控制
    # ================================================================

    def _fetch_all_details(self, items: list) -> list:
        """Reuters 串行抓取，避免并发触发封禁"""
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

        success_count = 0
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
                if detail.get("content"):
                    success_count += 1

        self.logger.info(
            f"[reuters] 详情页完成: {success_count}/{len(items)} 篇有正文"
        )
        items.sort(key=lambda x: x.get("rank", 999))
        return items
