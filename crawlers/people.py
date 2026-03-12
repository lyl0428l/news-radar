"""
人民网爬虫 - 爬取人民网热榜 TOP 10
"""
import json
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH


class PeopleCrawler(BaseCrawler):

    def __init__(self):
        super().__init__()
        self.name = "people"
        self.display_name = "人民网"
        self.language = "zh"

    def crawl(self) -> list[dict]:
        results = []

        # 人民网热榜 API
        api_url = "https://www.people.com.cn/210801/211150/index.js"
        resp = self._request(api_url)
        if resp:
            try:
                text = resp.text.strip()
                # 可能有 JSONP 包裹
                if "(" in text and text.endswith(")"):
                    text = text[text.index("(") + 1:-1]
                data = json.loads(text)
                items = data if isinstance(data, list) else data.get("items", data.get("list", []))
                for i, item in enumerate(items[:10], 1):
                    title = item.get("title", "").strip()
                    url = item.get("url", "").strip()
                    if title and url:
                        results.append(self._make_item(
                            title=title, url=url, rank=i, category="热榜"
                        ))
                if results:
                    return results
            except Exception as e:
                self.logger.warning(f"[people] 热榜 API 失败: {e}")

        # 备选: 人民网首页前 10 条
        resp = self._request("https://www.people.com.cn/")
        if resp is None:
            return results

        try:
            resp.encoding = resp.apparent_encoding
            soup = BeautifulSoup(resp.text, "lxml")

            rank = 1
            seen = set()
            for a in soup.find_all("a", href=True):
                href = str(a["href"]).strip()
                title = a.get_text(strip=True)
                if (title and len(title) >= MIN_TITLE_LEN_ZH
                        and ("/n1/20" in href or "/n2/20" in href)
                        and href not in seen):
                    seen.add(href)
                    if not href.startswith("http"):
                        href = "https://www.people.com.cn" + href
                    results.append(self._make_item(
                        title=title, url=href, rank=rank, category="头条"
                    ))
                    rank += 1
                    if rank > 10:
                        break
        except Exception as e:
            self.logger.warning(f"[people] HTML 解析失败: {e}")

        return results
