"""
界面新闻爬虫 - 爬取界面首页头条 TOP 10
"""
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH


class JiemianCrawler(BaseCrawler):

    detail_selectors = [".article-content", ".article_content", ".article-main"]

    def __init__(self):
        super().__init__()
        self.name = "jiemian"
        self.display_name = "界面新闻"
        self.language = "zh"

    def crawl(self) -> list[dict]:
        results = []

        resp = self._request("https://www.jiemian.com/")
        if resp is None:
            return results

        try:
            resp.encoding = "utf-8"
            soup = BeautifulSoup(resp.text, "lxml")

            rank = 1
            seen = set()
            for a in soup.find_all("a", href=True):
                href = str(a["href"]).strip()
                title = a.get_text(strip=True)
                if not title or len(title) < MIN_TITLE_LEN_ZH:
                    continue
                if "/article/" not in href:
                    continue
                if not href.startswith("http"):
                    href = "https://www.jiemian.com" + href
                if href in seen:
                    continue
                seen.add(href)
                results.append(self._make_item(
                    title=title, url=href, rank=rank, category="头条"
                ))
                rank += 1
                if rank > 10:
                    break
        except Exception as e:
            self.logger.warning(f"[jiemian] HTML 解析失败: {e}")

        return results
