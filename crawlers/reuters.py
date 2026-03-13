"""
Reuters 路透社爬虫 - 通过 RSS 爬取 Reuters 头条 TOP 10
原 HTML 方案被 401 拦截，改用 RSS 订阅源。
"""
import feedparser
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_EN


class ReutersCrawler(BaseCrawler):

    detail_selectors = [".article-body__content", "[data-testid='article-body']", ".StandardArticleBody_body"]

    def __init__(self):
        super().__init__()
        self.name = "reuters"
        self.display_name = "Reuters"
        self.language = "en"

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
