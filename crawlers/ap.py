"""
AP News 美联社爬虫 - 爬取 AP 头条 TOP 10
"""
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_EN


class APCrawler(BaseCrawler):

    detail_selectors = [".RichTextStoryBody", ".Article", "[data-key='article']", ".article-body"]

    def __init__(self):
        super().__init__()
        self.name = "ap"
        self.display_name = "AP News"
        self.language = "en"

    def crawl(self) -> list[dict]:
        results = []

        # AP News 首页头条
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
