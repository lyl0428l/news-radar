"""
搜狐新闻爬虫 - 爬取搜狐热榜 TOP 10
"""
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH


class SohuCrawler(BaseCrawler):

    detail_selectors = [".article", "article.article", "#article-container", "#mp-editor"]

    def __init__(self):
        super().__init__()
        self.name = "sohu"
        self.display_name = "搜狐新闻"
        self.language = "zh"

    def crawl(self) -> list[dict]:
        results = []

        # 搜狐热榜 API
        api_url = "https://v2.sohu.com/integration-api/mix/region/hot"
        params = {"region": "cn", "size": 10}
        resp = self._request(api_url, params=params)
        if resp:
            try:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("data", [])
                for i, item in enumerate(items[:10], 1):
                    title = item.get("title", "").strip()
                    aid = item.get("id", item.get("articleId", ""))
                    url = item.get("url", item.get("mobileUrl", ""))
                    if not url and aid:
                        url = f"https://www.sohu.com/a/{aid}"
                    if title and url:
                        results.append(self._make_item(
                            title=title, url=str(url).strip(), rank=i,
                            category="热榜",
                        ))
                if results:
                    return results
            except Exception as e:
                self.logger.warning(f"[sohu] 热榜 API 失败: {e}")

        # 备选: 搜狐首页前 10 条
        resp = self._request("https://news.sohu.com/")
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
                if (title and len(title) >= MIN_TITLE_LEN_ZH and "sohu.com/a/" in href and href not in seen):
                    seen.add(href)
                    if not href.startswith("http"):
                        href = "https://www.sohu.com" + href
                    results.append(self._make_item(
                        title=title, url=href, rank=rank, category="要闻"
                    ))
                    rank += 1
                    if rank > 10:
                        break
        except Exception as e:
            self.logger.warning(f"[sohu] HTML 解析失败: {e}")

        return results
