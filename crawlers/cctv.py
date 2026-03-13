"""
央视新闻爬虫 - 爬取央视头条 TOP 10
多策略：API + 新闻频道页 HTML
"""
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH


class CCTVCrawler(BaseCrawler):

    detail_selectors = [".content_area", "#content_area", ".cnt_bd", ".text_con"]

    def __init__(self):
        super().__init__()
        self.name = "cctv"
        self.display_name = "央视新闻"
        self.language = "zh"

    def crawl(self) -> list[dict]:
        results = []

        # 方案 1: 央视新闻频道列表 API
        results = self._try_api()
        if len(results) >= 5:
            return results

        # 方案 2: 新闻频道首页 HTML
        results = self._try_news_page()
        if len(results) >= 5:
            return results

        # 方案 3: 央视网主站 HTML
        results = self._try_main_page()
        return results

    def _try_api(self) -> list:
        """央视新闻 API 接口 — 使用新闻联播/要闻列表 API"""
        results = []
        # 央视新闻要闻列表 API（国内新闻频道）
        api_urls = [
            "https://news.cctv.com/2019/07/ga498/index.json",
            "https://api.cntv.cn/NewVideo/getVideoListById?id=TOPC1451528971114112&p=1&n=20&sort=desc&mode=0&serviceId=tvcctv",
        ]
        for api_url in api_urls:
            resp = self._request(api_url)
            if resp is None:
                continue
            try:
                data = resp.json()
                # 适配不同接口结构
                items = (
                    data.get("rollData", [])
                    or data.get("data", {}).get("list", [])
                    or data.get("video", {}).get("list", [])
                )
                if isinstance(items, list) and items:
                    for i, item in enumerate(items[:10], 1):
                        title = item.get("title", "").strip()
                        url = item.get("url", item.get("link", "")).strip()
                        if title and url:
                            if not url.startswith("http"):
                                url = "https://news.cctv.com" + url
                            results.append(self._make_item(
                                title=title, url=url, rank=i,
                                category="头条",
                                pub_time=self.parse_time(
                                    item.get("focus_date", item.get("time", ""))
                                ),
                            ))
                    if len(results) >= 5:
                        return results
            except Exception as e:
                self.logger.warning(f"[cctv] API 失败: {api_url} | {e}")
        return results

    def _try_news_page(self) -> list:
        """爬取央视新闻频道页"""
        results = []
        resp = self._request("https://news.cctv.com/")
        if resp is None:
            return results

        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        return self._extract_links(soup, results)

    def _try_main_page(self) -> list:
        """爬取央视网主站"""
        results = []
        resp = self._request("https://www.cctv.com/")
        if resp is None:
            return results

        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        return self._extract_links(soup, results)

    def _extract_links(self, soup: BeautifulSoup, existing: list | None = None) -> list:
        """从页面提取新闻链接，返回新列表（不修改传入的 existing）"""
        results = list(existing) if existing else []
        rank = len(results) + 1
        seen = {item.get("url", "") for item in results}

        for a in soup.find_all("a", href=True):
            href = str(a["href"]).strip()
            title = a.get_text(strip=True)

            if not title or len(title) < MIN_TITLE_LEN_ZH:
                continue
            # CCTV 文章链接特征：包含 cctv.com 和日期路径 /20
            if "cctv.com" not in href:
                continue
            if "/20" not in href:
                continue
            if not href.startswith("http"):
                href = "https:" + href if href.startswith("//") else "https://news.cctv.com" + href
            if href in seen:
                continue

            seen.add(href)
            results.append(self._make_item(
                title=title, url=href, rank=rank, category="头条"
            ))
            rank += 1
            if rank > 10:
                break

        return results
