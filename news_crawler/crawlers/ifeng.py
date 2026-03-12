"""
凤凰新闻爬虫 - 爬取凤凰热榜 TOP 10
多 API + HTML 备选策略
"""
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH


class IfengCrawler(BaseCrawler):

    def __init__(self):
        super().__init__()
        self.name = "ifeng"
        self.display_name = "凤凰新闻"
        self.language = "zh"

    def crawl(self) -> list[dict]:
        results = []

        # 方案 1: 凤凰热榜 API (新接口)
        results = self._try_hot_api()
        if len(results) >= 5:
            return results

        # 方案 2: 凤凰新闻客户端 API
        results = self._try_client_api()
        if len(results) >= 5:
            return results

        # 方案 3: 首页 HTML 解析（加宽匹配条件）
        results = self._try_html()
        return results

    def _try_hot_api(self) -> list:
        """凤凰热榜 API"""
        results = []
        api_urls = [
            "https://shankapi.ifeng.com/season/getHotListData/all/1/10",
            "https://api.3g.ifeng.com/api_phoenixtv_allData?type=1&page=1&pageSize=10",
        ]
        for api_url in api_urls:
            resp = self._request(api_url)
            if resp is None:
                continue
            try:
                data = resp.json()
                # 适配不同接口结构
                items = (
                    data.get("data", {}).get("allData", [])
                    or data.get("data", {}).get("list", [])
                    or data.get("data", {}).get("newslist", [])
                    or data.get("data", [])
                )
                if isinstance(items, list):
                    for i, item in enumerate(items[:10], 1):
                        title = item.get("title", "").strip()
                        url = item.get("url", item.get("link", "")).strip()
                        if title and url:
                            if not url.startswith("http"):
                                url = "https:" + url if url.startswith("//") else "https://www.ifeng.com" + url
                            results.append(self._make_item(
                                title=title, url=url, rank=i,
                                category="热榜",
                                summary=item.get("description", item.get("summary", "")),
                                pub_time=self.parse_time(item.get("ctime", item.get("updateTime", ""))),
                            ))
                    if len(results) >= 5:
                        return results
            except Exception as e:
                self.logger.warning(f"[ifeng] API 失败: {api_url} | {e}")

        return results

    def _try_client_api(self) -> list:
        """凤凰新闻频道列表 API"""
        results = []
        resp = self._request(
            "https://nine.ifeng.com/iosf/listData?type=1&id=SYLB10,SYDT10&action=default&pullNum=1",
            headers={"Referer": "https://news.ifeng.com/"}
        )
        if resp is None:
            return results
        try:
            data = resp.json()
            items = data.get("data", [])
            if isinstance(items, list):
                for i, item in enumerate(items[:10], 1):
                    title = item.get("title", "").strip()
                    url = item.get("url", item.get("link", "")).strip()
                    if title and url:
                        if not url.startswith("http"):
                            url = "https:" + url if url.startswith("//") else "https://www.ifeng.com" + url
                        results.append(self._make_item(
                            title=title, url=url, rank=i, category="要闻",
                        ))
        except Exception as e:
            self.logger.warning(f"[ifeng] 客户端 API 失败: {e}")
        return results

    def _try_html(self) -> list[dict]:
        """首页 HTML 解析"""
        results = []
        resp = self._request("https://news.ifeng.com/")
        if resp is None:
            # 再试主站
            resp = self._request("https://www.ifeng.com/")
        if resp is None:
            return results

        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        rank = 1
        seen = set()

        for a in soup.find_all("a", href=True):
            href = str(a["href"]).strip()
            title = a.get_text(strip=True)
            if not title or len(title) < MIN_TITLE_LEN_ZH:
                continue
            # 接受 ifeng.com 域名下的链接
            if "ifeng.com" not in href:
                continue
            if not href.startswith("http"):
                href = "https:" + href if href.startswith("//") else "https://news.ifeng.com" + href
            if href in seen:
                continue
            # 过滤导航/频道链接（路径太短说明不是文章）
            if href.count("/") < 4:
                continue
            seen.add(href)
            results.append(self._make_item(
                title=title, url=href, rank=rank, category="要闻"
            ))
            rank += 1
            if rank > 10:
                break

        return results
