"""
AP News 美联社爬虫 - 通过 News Sitemap 爬取 AP 头条 TOP 10

AP News 限流策略:
  AP 对直接 HTML 抓取有严格的 429 限流（无 API、无 RSS）。
  旧方案只靠首页 HTML 抓取列表 + 详情页串行请求，极易触发限流。

新方案:
  列表获取:
    1. News Sitemap（Googlebot UA 可访问，920+ 篇文章，无限流）
    2. Google News RSS（后备，需解析重定向）
    3. AP 首页 HTML（最终后备）
  详情页获取:
    BaseCrawler._request() 遇到 429/403 自动触发 curl_cffi TLS 绕过。
    串行请求 + 3-5 秒延迟避免限流。
"""
import re
import time
import random
import logging
import requests
import feedparser
from bs4 import BeautifulSoup
from config import DETAIL_FETCH_TIMEOUT
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_EN

logger = logging.getLogger(__name__)

# Googlebot UA - 用于访问 News Sitemap
_GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

# AP News Sitemap
_NEWS_SITEMAP_URL = "https://apnews.com/news-sitemap-content.xml"

# Google News RSS for AP
_GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search"
    "?q=site:apnews.com+when:1d&hl=en-US&gl=US&ceid=US:en"
)


class APCrawler(BaseCrawler):

    detail_selectors = [
        ".RichTextStoryBody",
        ".Article",
        "[data-key='article']",
        ".article-body",
    ]

    def __init__(self):
        super().__init__()
        self.name = "ap"
        self.display_name = "AP News"
        self.language = "en"

    # ================================================================
    #  列表获取
    # ================================================================

    def crawl(self) -> list[dict]:
        """
        获取 AP News TOP 10 新闻列表。
        优先 News Sitemap，后备 Google News RSS，最终后备首页 HTML。
        """
        # 方案 1: News Sitemap（最可靠，无限流）
        results = self._crawl_sitemap()
        if results:
            return results

        # 方案 2: Google News RSS
        results = self._crawl_google_news_rss()
        if results:
            return results

        # 方案 3: AP 首页 HTML（最终后备，有限流风险）
        return self._crawl_homepage()

    def _crawl_sitemap(self) -> list[dict]:
        """从 AP News Sitemap 获取最新文章列表"""
        try:
            resp = requests.get(_NEWS_SITEMAP_URL, timeout=15,
                                headers={"User-Agent": _GOOGLEBOT_UA})
            if resp.status_code != 200:
                logger.debug(f"[ap] Sitemap HTTP {resp.status_code}")
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
                # 提取 URL — 只要 /article/ 的
                loc_m = re.search(r"<loc>(https://apnews\.com/article/[^<]+)</loc>", block)
                if not loc_m:
                    continue
                art_url = loc_m.group(1)
                if art_url in seen:
                    continue
                seen.add(art_url)

                # 提取标题（CDATA 包裹）
                title_m = re.search(
                    r"<news:title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</news:title>", block
                )
                title = title_m.group(1).strip() if title_m else ""
                if not title or len(title) < MIN_TITLE_LEN_EN:
                    continue

                # 过滤低价值内容（彩票号码、比赛比分等短讯）
                title_lower = title.lower()
                skip_patterns = (
                    "winning numbers", "lottery", "pick 3", "pick 4",
                    "cash 4", "cash 5", "powerball numbers", "mega millions",
                    "hotwins", "lotto",
                )
                if any(p in title_lower for p in skip_patterns):
                    continue

                # 提取时间
                time_m = re.search(
                    r"<news:publication_date>([^<]+)</news:publication_date>", block
                )
                pub_time = self.parse_time(time_m.group(1)) if time_m else ""

                # 提取图片
                img_m = re.search(r"<image:loc>([^<]+)</image:loc>", block)
                thumbnail = img_m.group(1).replace("&amp;", "&") if img_m else ""

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
                self.logger.info(f"[ap] Sitemap 获取 {len(results)} 条新闻")
            return results

        except Exception as e:
            logger.debug(f"[ap] Sitemap 失败: {e}")
            return []

    def _crawl_google_news_rss(self) -> list[dict]:
        """从 Google News RSS 获取 AP News 文章列表"""
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

                if "apnews.com" not in link:
                    continue
                if not title or link in seen:
                    continue
                seen.add(link)

                pub_time = str(entry.get("published", ""))

                results.append(self._make_item(
                    title=title,
                    url=link,
                    rank=rank,
                    category="Top Stories",
                    pub_time=self.parse_time(pub_time),
                ))
                rank += 1
                if rank > 10:
                    break

            if results:
                self.logger.info(f"[ap] Google News RSS 获取 {len(results)} 条")
            return results
        except Exception as e:
            logger.debug(f"[ap] Google News RSS 失败: {e}")
            return []

    def _crawl_homepage(self) -> list[dict]:
        """AP 首页 HTML 抓取（最终后备，有限流风险）"""
        results = []
        resp = self._request("https://apnews.com/")
        if resp is None:
            return results

        try:
            soup = BeautifulSoup(resp.text, "lxml")
            rank = 1
            seen = set()

            for a in soup.find_all("a", href=True):
                href = str(a["href"]).strip()
                title = a.get_text(strip=True)

                if not title or len(title) < MIN_TITLE_LEN_EN:
                    continue
                if "/article/" not in href:
                    continue
                if not href.startswith("http"):
                    href = "https://apnews.com" + href
                if href in seen:
                    continue
                seen.add(href)

                results.append(self._make_item(
                    title=title, url=href, rank=rank, category="Top Stories"
                ))
                rank += 1
                if rank > 10:
                    break
        except Exception as e:
            self.logger.warning(f"[ap] HTML 解析失败: {e}")

        return results

    @staticmethod
    def _resolve_google_news_url(gnews_url: str) -> str:
        """解析 Google News 重定向 URL"""
        if "apnews.com" in gnews_url:
            return gnews_url
        if "news.google.com" not in gnews_url:
            return gnews_url
        try:
            resp = requests.head(gnews_url, allow_redirects=True, timeout=8,
                                 headers={"User-Agent": "Mozilla/5.0"})
            if "apnews.com" in resp.url:
                return resp.url
        except Exception:
            pass
        return gnews_url

    # ================================================================
    #  详情页获取
    # ================================================================

    def fetch_detail(self, item: dict) -> dict:
        """
        AP News 详情页获取。
        使用 _request()，遇到 429/403 自动触发 curl_cffi TLS 绕过。
        每次请求前 3-5 秒延迟避免限流。
        """
        time.sleep(random.uniform(3.0, 5.0))
        return super().fetch_detail(item)

    def _fetch_all_details(self, items: list) -> list:
        """AP News 串行抓取，避免并发触发 429 限流"""
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

        self.logger.info(f"[ap] 详情页完成: {success_count}/{len(items)} 篇有正文")
        items.sort(key=lambda x: x.get("rank", 999))
        return items
