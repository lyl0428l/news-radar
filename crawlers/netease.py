"""
网易新闻爬虫 - 爬取网易热榜 TOP 10
"""
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH


class NeteaseCrawler(BaseCrawler):

    # .article-body 优先：网易移动端（m.163.com）使用此容器
    # #content / .post_body / .post_text 为 PC 端回退
    detail_selectors = [".article-body", ".post_body", ".post_text", "#content"]

    def __init__(self):
        super().__init__()
        self.name = "netease"
        self.display_name = "网易新闻"
        self.language = "zh"

    def crawl(self) -> list[dict]:
        results = []

        # 网易新闻热点排行榜
        api_url = "https://m.163.com/fe/api/hot/news/flow"
        params = {"size": 10}
        resp = self._request(api_url, params=params)
        if resp:
            try:
                data = resp.json()
                # 防御性取值：data["data"] 可能是 dict 或 list
                raw = data.get("data", {})
                if isinstance(raw, dict):
                    items = raw.get("list", [])
                elif isinstance(raw, list):
                    items = raw
                else:
                    items = []
                if isinstance(items, list):
                    for i, item in enumerate(items[:10], 1):
                        title = item.get("title", "").strip()
                        url = item.get("url", item.get("docurl", item.get("skipURL", ""))).strip()
                        if title and url:
                            results.append(self._make_item(
                                title=title, url=url, rank=i,
                                summary=item.get("digest", ""),
                                category="热榜",
                            ))
                    if results:
                        return results
            except Exception as e:
                self.logger.warning(f"[netease] 热榜 API 失败: {e}")

        # 备选: 网易首页提取前 10 条
        resp = self._request("https://news.163.com/")
        if resp is None:
            return results

        try:
            resp.encoding = "gbk"
            soup = BeautifulSoup(resp.text, "lxml")
            rank = 1
            seen = set()
            for a in soup.find_all("a", href=True):
                href = str(a["href"]).strip()
                title = a.get_text(strip=True)
                if (title and len(title) >= MIN_TITLE_LEN_ZH and "163.com" in href
                        and href.startswith("https://") and href not in seen):
                    seen.add(href)
                    results.append(self._make_item(
                        title=title, url=href, rank=rank, category="要闻"
                    ))
                    rank += 1
                    if rank > 10:
                        break
        except Exception as e:
            self.logger.warning(f"[netease] HTML 解析失败: {e}")

        return results
