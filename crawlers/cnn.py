"""
CNN 爬虫 - 多通道获取 CNN 头条 TOP 10

列表获取（三级回退）:
  1. CNN RSS（rss.cnn.com，可能被墙）
  2. Google News RSS（后备，国内可达）
  3. CNN 首页 HTML（edition.cnn.com 直连可达）
"""
import re
import logging
import requests
import feedparser
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, RSSCrawler, MIN_TITLE_LEN_EN

logger = logging.getLogger(__name__)

_GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search"
    "?q=site:cnn.com+when:1d&hl=en-US&gl=US&ceid=US:en"
)


class CNNCrawler(BaseCrawler):

    detail_selectors = [".article__content", ".zn-body__paragraph", ".l-container"]

    def __init__(self):
        super().__init__()
        self.name = "cnn"
        self.display_name = "CNN"
        self.language = "en"

    def crawl(self) -> list[dict]:
        # 方案 1: CNN RSS
        results = self._crawl_rss()
        if results:
            return results

        # 方案 2: Google News RSS
        results = self._crawl_google_news()
        if results:
            return results

        # 方案 3: CNN 首页 HTML
        return self._crawl_homepage()

    def _crawl_rss(self) -> list:
        """CNN 官方 RSS"""
        try:
            resp = self._request("https://rss.cnn.com/rss/edition.rss", timeout=10)
            if resp and resp.status_code == 200:
                feed = feedparser.parse(resp.content)
                if feed.entries:
                    results = []
                    for i, entry in enumerate(feed.entries[:10], 1):
                        title = str(entry.get("title", "")).strip()
                        link = str(entry.get("link", "")).strip()
                        if title and link:
                            results.append(self._make_item(
                                title=title, url=link, rank=i, category="Top Stories"
                            ))
                    if results:
                        self.logger.info(f"[cnn] RSS 获取 {len(results)} 条")
                        return results
        except Exception as e:
            self.logger.debug(f"[cnn] RSS 失败: {e}")
        return []

    def _crawl_google_news(self) -> list:
        """Google News RSS 后备"""
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
                # 清理标题后缀
                title = re.sub(r"\s*-\s*CNN.*$", "", title).strip()

                # 解析 Google News 重定向
                try:
                    r = requests.head(link, allow_redirects=True, timeout=8,
                                      headers={"User-Agent": "Mozilla/5.0"})
                    if "cnn.com" in r.url:
                        link = r.url
                except Exception:
                    pass

                if "cnn.com" not in link:
                    continue
                if not title or link in seen:
                    continue
                seen.add(link)

                results.append(self._make_item(
                    title=title, url=link, rank=rank, category="Top Stories"
                ))
                rank += 1
                if rank > 10:
                    break

            if results:
                self.logger.info(f"[cnn] Google News 获取 {len(results)} 条")
            return results
        except Exception as e:
            self.logger.debug(f"[cnn] Google News 失败: {e}")
            return []

    def _crawl_homepage(self) -> list:
        """CNN 首页 HTML 抓取"""
        resp = self._request("https://edition.cnn.com/", timeout=15)
        if resp is None:
            return []

        try:
            soup = BeautifulSoup(resp.text, "lxml")
            results = []
            seen = set()
            rank = 1

            for a in soup.find_all("a", href=True):
                href = str(a["href"]).strip()
                title = a.get_text(strip=True)
                if not title or len(title) < MIN_TITLE_LEN_EN:
                    continue
                # 只要文章链接（含日期路径）
                if not re.search(r"/\d{4}/\d{2}/\d{2}/", href):
                    continue
                if not href.startswith("http"):
                    href = "https://edition.cnn.com" + href
                if href in seen:
                    continue
                seen.add(href)

                results.append(self._make_item(
                    title=title, url=href, rank=rank, category="Top Stories"
                ))
                rank += 1
                if rank > 10:
                    break

            if results:
                self.logger.info(f"[cnn] 首页 HTML 获取 {len(results)} 条")
            return results
        except Exception as e:
            self.logger.warning(f"[cnn] HTML 解析失败: {e}")
            return []
