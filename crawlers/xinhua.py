"""
新华网爬虫 - 爬取新华网首页头条 TOP 10
"""
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH


class XinhuaCrawler(BaseCrawler):

    detail_selectors = ["#detail", "#detailContent", ".article", ".main-aticle"]

    def __init__(self):
        super().__init__()
        self.name = "xinhua"
        self.display_name = "新华网"
        self.language = "zh"

    def crawl(self) -> list[dict]:
        results = []

        # 新华网首页头条
        resp = self._request("https://www.news.cn/")
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
                if not any(k in href for k in ["news.cn/20", "xinhuanet.com/20"]):
                    continue
                if not href.startswith("http"):
                    href = "https://www.news.cn" + href
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
            self.logger.warning(f"[xinhua] HTML 解析失败: {e}")

        return results
