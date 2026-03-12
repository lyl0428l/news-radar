"""
腾讯新闻爬虫 - 爬取腾讯新闻热榜 TOP 10
"""
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH


class TencentCrawler(BaseCrawler):

    def __init__(self):
        super().__init__()
        self.name = "tencent"
        self.display_name = "腾讯新闻"
        self.language = "zh"

    def crawl(self) -> list[dict]:
        results = []

        # 腾讯新闻热点精选 API
        api_url = "https://i.news.qq.com/gw/event/pc_hot_ranking_list"
        params = {"ids_hash": "", "offset": 0, "page_size": 10}
        resp = self._request(api_url, params=params)
        if resp:
            try:
                data = resp.json()
                # 安全取 idlist[0]：key 缺失或为空列表时均返回 {}
                idlist = data.get("idlist") or []
                first = idlist[0] if idlist else {}
                items = first.get("newslist", [])
                rank = 1
                for item in items:
                    title = item.get("title", "").strip()
                    url = item.get("url", item.get("surl", "")).strip()
                    if not title:
                        continue
                    if not url:
                        article_id = item.get("id", "")
                        if article_id:
                            url = f"https://new.qq.com/rain/a/{article_id}"
                    # 过滤垃圾条目：第一条常为页面描述而非新闻
                    # 特征：URL 为空/非法、标题含"腾讯新闻"站名、或 hotEvent 类型为空
                    if not url or not url.startswith("http"):
                        continue
                    if title in ("腾讯新闻", "腾讯网") or "热点精选" in title:
                        continue
                    if title and url:
                        results.append(self._make_item(
                            title=title, url=url, rank=rank,
                            summary=item.get("abstract", ""),
                            category="热榜",
                        ))
                        rank += 1
                        if rank > 10:
                            break
                if results:
                    return results
            except Exception as e:
                self.logger.warning(f"[tencent] 热榜 API 失败: {e}")

        # 备选: Playwright 渲染首页取前 10
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.goto("https://news.qq.com/", wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(3000)
                    html = page.content()
                finally:
                    browser.close()

                soup = BeautifulSoup(html, "lxml")
                rank = 1
                seen = set()
                for a in soup.find_all("a", href=True):
                    href = str(a["href"]).strip()
                    title = a.get_text(strip=True)
                    if (title and len(title) >= MIN_TITLE_LEN_ZH
                            and "new.qq.com/rain/a/" in href
                            and href not in seen):
                        seen.add(href)
                        if not href.startswith("http"):
                            href = "https:" + href if href.startswith("//") else "https://new.qq.com" + href
                        results.append(self._make_item(
                            title=title, url=href, rank=rank, category="热榜"
                        ))
                        rank += 1
                        if rank > 10:
                            break
        except Exception as e:
            self.logger.warning(f"[tencent] Playwright 备选失败: {e}")

        return results
