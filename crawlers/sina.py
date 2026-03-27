"""
新浪新闻爬虫 - 爬取新浪热搜榜 TOP 10
"""
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler


class SinaCrawler(BaseCrawler):

    detail_selectors = [".article-body", "#artibody", ".article-content", ".main-content"]

    # 新浪正文中的广告/推广元素（包含开户二维码、APP推广等）
    _SINA_PROMO_SELECTORS = [
        ".app-kaihu-qr", ".appendQr_wrap", ".appendQr_normal",
        ".app-qihuo-kaihu-qr", ".finance_app_sina",
        "[class*='kaihu']", "[class*='Qr']",
        ".article-bottom", ".blk_cir_bottom",
    ]

    def parse_detail(self, html: str, url: str) -> dict:
        """去除新浪推广元素后再提取内容"""
        soup = BeautifulSoup(html, "lxml")
        for sel in self._SINA_PROMO_SELECTORS:
            for el in soup.select(sel):
                el.decompose()
        from utils.content_extractor import extract_content
        return extract_content(str(soup), url, selectors=self.detail_selectors)

    def __init__(self):
        super().__init__()
        self.name = "sina"
        self.display_name = "新浪新闻"
        self.language = "zh"

    def crawl(self) -> list[dict]:
        results = []

        # 新浪热搜榜 API
        api_url = "https://newsapp.sina.cn/api/hotlist"
        params = {"newsId": "HB-1-snhs/top_news_list-all"}
        resp = self._request(api_url, params=params)

        if resp:
            try:
                data = resp.json()
                items = data.get("data", {}).get("hotList", [])
                for i, item in enumerate(items[:10], 1):
                    info = item.get("info", {})
                    title = info.get("title", "").strip()
                    url = info.get("url", "").strip()
                    if not title or not url:
                        continue
                    results.append(self._make_item(
                        title=title, url=url, rank=i,
                        category="热搜",
                    ))
                if results:
                    return results
            except Exception as e:
                self.logger.warning(f"[sina] 热搜 API 解析失败: {e}")

        # 备选: 滚动新闻 API 取前 10
        api_url = "https://feed.mix.sina.com.cn/api/roll/get"
        params = {"pageid": 153, "lid": 2509, "num": 10, "page": 1}
        resp = self._request(api_url, params=params)
        if resp:
            try:
                data = resp.json()
                items = data.get("result", {}).get("data", [])
                for i, item in enumerate(items[:10], 1):
                    title = item.get("title", "").strip()
                    url = item.get("url", "").strip()
                    if title and url:
                        results.append(self._make_item(
                            title=title, url=url, rank=i,
                            summary=item.get("summary", ""),
                            category="要闻",
                            pub_time=self.parse_time(
                                str(item.get("ctime", item.get("intime", "")))
                            ),
                        ))
            except Exception as e:
                self.logger.warning(f"[sina] 滚动 API 解析失败: {e}")

        return results
# auto-deploy test 03/27/2026 09:45:47
# deploy test 2
