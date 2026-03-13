"""
Reuters 路透社爬虫 - 通过 RSS 爬取 Reuters 头条 TOP 10
原 HTML 方案被 401 拦截，改用 RSS 订阅源。
Google News RSS 的链接是重定向 URL，需要解析出真实 Reuters URL。
"""
import re
import time
import random
import requests
import feedparser
from config import DETAIL_FETCH_TIMEOUT
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_EN


class ReutersCrawler(BaseCrawler):

    detail_selectors = [".article-body__content", "[data-testid='article-body']",
                        ".StandardArticleBody_body", "article", "[class*='ArticleBody']"]

    def __init__(self):
        super().__init__()
        self.name = "reuters"
        self.display_name = "Reuters"
        self.language = "en"

    def _resolve_google_news_url(self, gnews_url: str) -> str:
        """
        解析 Google News 重定向 URL，获取真实的 Reuters 文章 URL。
        Google News URL 格式: https://news.google.com/rss/articles/...
        需要 HEAD 请求跟随重定向获取 Location。
        """
        if "reuters.com" in gnews_url:
            return gnews_url  # 已经是 Reuters URL
        if "news.google.com" not in gnews_url:
            return gnews_url

        try:
            resp = requests.head(gnews_url, allow_redirects=True, timeout=10,
                                 headers={"User-Agent": self.session.headers.get("User-Agent", "")})
            final_url = resp.url
            if "reuters.com" in final_url:
                return final_url
            # 有时 Google News 会经过多层跳转
            if resp.headers.get("Location"):
                return resp.headers["Location"]
        except Exception as e:
            self.logger.debug(f"[reuters] URL 解析失败: {gnews_url} | {e}")

        return gnews_url

    def crawl(self) -> list[dict]:
        results = []

        # 方案 1: Reuters 官方 RSS（wire 接口）
        rss_urls = [
            "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best",
            "https://news.google.com/rss/search?q=site:reuters.com&hl=en-US&gl=US&ceid=US:en",
        ]

        for rss_url in rss_urls:
            feed = self._fetch_rss(rss_url)
            if feed and feed.entries:
                rank = 1
                seen = set()
                for entry in feed.entries[:15]:
                    title = str(entry.get("title", "")).strip()
                    link = str(entry.get("link", "")).strip()
                    # 解析 Google News 重定向 URL
                    link = self._resolve_google_news_url(link)
                    if not title or not link or link in seen:
                        continue
                    seen.add(link)

                    summary = str(entry.get("summary", entry.get("description", "")))
                    pub_time = str(entry.get("published", ""))

                    # 仅当摘要含 HTML 标签时才调用 clean_text（避免无谓的 BS4 解析）
                    clean_summary = ""
                    if summary:
                        clean_summary = (self.clean_text(summary)[:200]
                                         if "<" in summary and ">" in summary
                                         else summary[:200])
                    results.append(self._make_item(
                        title=title,
                        url=link,
                        rank=rank,
                        summary=clean_summary,
                        category="Top Stories",
                        pub_time=self.parse_time(pub_time),
                    ))
                    rank += 1
                    if rank > 10:
                        break

                if results:
                    return results

        # 方案 2: 备选直接请求（可能被拦截）
        return self._crawl_html_fallback()

    def _fetch_rss(self, url: str):
        """请求 RSS 并解析"""
        resp = self._request(url)
        if resp is None:
            return None
        try:
            return feedparser.parse(resp.content)
        except Exception as e:
            self.logger.warning(f"[reuters] RSS 解析失败: {url} | {e}")
            return None

    def fetch_detail(self, item: dict) -> dict:
        """Reuters 需要特殊 headers 绕过反爬"""
        url = item.get("url", "")
        if not url:
            return {}

        # 先解析 Google News 重定向
        if "news.google.com" in url:
            url = self._resolve_google_news_url(url)

        time.sleep(random.uniform(1.5, 3.0))

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
                if result.get("content") and len(result["content"]) > 100:
                    return result
        except Exception as e:
            self.logger.debug(f"[reuters] 详情页抓取失败: {url} | {e}")

        # Fallback: Google Web Cache
        try:
            cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{url}"
            resp = self._request(cache_url, timeout=DETAIL_FETCH_TIMEOUT)
            if resp and resp.status_code == 200:
                result = self.parse_detail(resp.text, url)
                if result.get("content") and len(result["content"]) > 100:
                    return result
        except Exception as e:
            self.logger.debug(f"[reuters] Google Cache 失败: {url} | {e}")

        return {}

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

        results = []
        for item in items:
            if item.get("url", "") in skip_urls:
                results.append(item)
                continue
            detail = self.fetch_detail(item)
            if detail:
                if detail.get("content") and not item.get("content"):
                    item["content"] = detail["content"]
                if detail.get("content_html"):
                    item["content_html"] = detail["content_html"]
                if detail.get("images"):
                    item["images"] = detail["images"]
                if detail.get("videos"):
                    item["videos"] = detail["videos"]
                if detail.get("thumbnail") and not item.get("thumbnail"):
                    item["thumbnail"] = detail["thumbnail"]
                if detail.get("author") and not item.get("author"):
                    item["author"] = detail["author"]
                if detail.get("pub_time") and not item.get("pub_time"):
                    parsed = self.parse_time(detail["pub_time"])
                    if parsed:
                        item["pub_time"] = parsed
                if not item.get("summary") and detail.get("content"):
                    item["summary"] = self.extract_summary(detail["content"])
            results.append(item)

        results.sort(key=lambda x: x.get("rank", 999))
        return results

    def _crawl_html_fallback(self) -> list:
        """HTML 备选：带额外 headers 尝试绕过基础反爬"""
        from bs4 import BeautifulSoup
        results = []

        resp = self._request(
            "https://www.reuters.com/",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.google.com/",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
            }
        )
        if resp is None:
            return results

        soup = BeautifulSoup(resp.text, "lxml")
        rank = 1
        seen = set()

        for a in soup.find_all("a", href=True):
            href = str(a["href"]).strip()
            title = a.get_text(strip=True)

            if not title or len(title) < MIN_TITLE_LEN_EN:
                continue
            parts = href.strip("/").split("/")
            if len(parts) < 2:
                continue
            if not any(href.startswith(p) for p in [
                "/world/", "/business/", "/technology/",
                "/markets/", "/sustainability/", "/science/",
            ]):
                continue

            full_url = "https://www.reuters.com" + href
            if full_url in seen:
                continue
            seen.add(full_url)

            results.append(self._make_item(
                title=title, url=full_url, rank=rank, category="Top Stories"
            ))
            rank += 1
            if rank > 10:
                break

        return results
