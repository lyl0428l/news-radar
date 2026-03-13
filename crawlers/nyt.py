"""
New York Times 爬虫 - 通过 RSS 爬取 NYT 头条 TOP 10
带反 paywall 策略：伪装 Googlebot referer + 多 fallback 路径
"""
import time
import random
from config import DETAIL_FETCH_TIMEOUT
from crawlers.base import RSSCrawler


class NYTCrawler(RSSCrawler):

    detail_selectors = ["[name='articleBody']", ".meteredContent", ".StoryBodyCompanionColumn", "article section"]

    def __init__(self):
        super().__init__()
        self.name = "nyt"
        self.display_name = "NYT"
        self.language = "en"
        self.rss_url = "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"
        self.category = "Top Stories"

    def fetch_detail(self, item: dict) -> dict:
        """
        NYT 有 paywall，策略：
        1. 用 Google Referer 伪装（NYT 对搜索引擎流量放行部分内容）
        2. 回退到 Google Web Cache
        3. 回退到 RSS 的 summary（已在列表页抓到）
        """
        url = item.get("url", "")
        if not url:
            return {}

        # 限流：避免连续请求触发封禁
        time.sleep(random.uniform(1.5, 3.0))

        # 策略 1：带 Google Referer 直接请求
        try:
            resp = self._request(url, timeout=DETAIL_FETCH_TIMEOUT, headers={
                "Referer": "https://www.google.com/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "cross-site",
            })
            if resp and resp.status_code == 200:
                result = self.parse_detail(resp.text, url)
                if result.get("content") and len(result["content"]) > 200:
                    return result
        except Exception as e:
            self.logger.debug(f"[nyt] 直接请求失败: {url} | {e}")

        # 策略 2：Google Web Cache
        try:
            cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{url}"
            resp = self._request(cache_url, timeout=DETAIL_FETCH_TIMEOUT)
            if resp and resp.status_code == 200:
                result = self.parse_detail(resp.text, url)
                if result.get("content") and len(result["content"]) > 200:
                    return result
        except Exception as e:
            self.logger.debug(f"[nyt] Google Cache 失败: {url} | {e}")

        return {}

    def _fetch_all_details(self, items: list) -> list:
        """NYT 串行抓取，避免并发触发封禁"""
        if not items:
            return items

        skip_urls = set()
        try:
            from storage import check_urls_have_content
            all_urls = [item.get("url", "") for item in items if item.get("url")]
            skip_urls = check_urls_have_content(all_urls)
            if skip_urls:
                self.logger.info(f"[{self.name}] 跳过 {len(skip_urls)} 条已有正文的新闻")
        except Exception:
            pass

        results = []
        for item in items:
            if item.get("url", "") in skip_urls:
                results.append(item)
                continue
            detail = self.fetch_detail(item)
            if detail:
                if detail.get("content") and not item.get("content"):
                    item["content"] = detail["content"]
                if detail.get("content_html"):
                    item["content_html"] = detail["content_html"]
                if detail.get("images"):
                    item["images"] = detail["images"]
                if detail.get("videos"):
                    item["videos"] = detail["videos"]
                if detail.get("thumbnail") and not item.get("thumbnail"):
                    item["thumbnail"] = detail["thumbnail"]
                if detail.get("author") and not item.get("author"):
                    item["author"] = detail["author"]
                if detail.get("pub_time") and not item.get("pub_time"):
                    parsed = self.parse_time(detail["pub_time"])
                    if parsed:
                        item["pub_time"] = parsed
                if not item.get("summary") and detail.get("content"):
                    item["summary"] = self.extract_summary(detail["content"])
            results.append(item)

        results.sort(key=lambda x: x.get("rank", 999))
        return results
