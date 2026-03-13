"""
澎湃新闻爬虫 - 爬取澎湃热榜 TOP 10
"""
from crawlers.base import BaseCrawler


class ThePaperCrawler(BaseCrawler):

    detail_selectors = [".news_txt", ".news_content", ".article-content", ".index_content"]

    def __init__(self):
        super().__init__()
        self.name = "thepaper"
        self.display_name = "澎湃新闻"
        self.language = "zh"
        self.base_url = "https://www.thepaper.cn"

    def crawl(self) -> list[dict]:
        results = []

        # 澎湃热榜 API
        api_url = f"{self.base_url}/api/feed/hotspot/list"
        resp = self._request(api_url, params={"pageSize": 10, "pageNum": 1})
        if resp:
            try:
                data = resp.json()
                items = data.get("data", {}).get("list", data.get("data", []))
                if isinstance(items, list):
                    for i, item in enumerate(items[:10], 1):
                        title = item.get("title", item.get("name", "")).strip()
                        cid = item.get("contId", item.get("id", ""))
                        url = item.get("url", "")
                        if not url and cid:
                            url = f"{self.base_url}/newsDetail_forward_{cid}"
                        if title and url:
                            results.append(self._make_item(
                                title=title, url=str(url).strip(), rank=i,
                                summary=item.get("summary", ""),
                                category="热榜",
                            ))
                    if results:
                        return results
            except Exception as e:
                self.logger.warning(f"[thepaper] 热榜 API 失败: {e}")

        # 备选: 首页前 10 条
        from bs4 import BeautifulSoup
        resp = self._request(self.base_url)
        if resp is None:
            return results

        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        rank = 1
        seen = set()
        for a in soup.find_all("a", href=True):
            href = str(a["href"]).strip()
            title = a.get_text(strip=True)
            if "newsDetail_forward_" not in href:
                continue
            if not title or len(title) < 8:
                continue
            if href.startswith("/"):
                href = self.base_url + href
            if href in seen:
                continue
            seen.add(href)
            results.append(self._make_item(
                title=title, url=href, rank=rank, category="热榜"
            ))
            rank += 1
            if rank > 10:
                break

        return results
