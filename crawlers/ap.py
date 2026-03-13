"""
AP News 美联社爬虫 - 爬取 AP 头条 TOP 10
"""
import time
import random
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_EN


class APCrawler(BaseCrawler):

    detail_selectors = [".RichTextStoryBody", ".Article", "[data-key='article']", ".article-body"]

    def __init__(self):
        super().__init__()
        self.name = "ap"
        self.display_name = "AP News"
        self.language = "en"

    def fetch_detail(self, item: dict) -> dict:
        """AP News 限流严格，每次请求前随机延迟 2-4 秒"""
        time.sleep(random.uniform(2.0, 4.0))
        return super().fetch_detail(item)

    def _fetch_all_details(self, items: list) -> list:
        """AP News 429 限流严格，串行抓取代替并发"""
        if not items:
            return items

        # 检查 DB 中哪些 URL 已有正文
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

    def crawl(self) -> list[dict]:
        results = []

        # AP News 首页头条
        resp = self._request("https://apnews.com/")
        if resp is None:
            return results

        try:
            soup = BeautifulSoup(resp.text, "lxml")
            rank = 1
            seen = set()

            for a in soup.find_all("a", href=True):
                href = str(a["href"]).strip()
                title = a.get_text(strip=True)

                if not title or len(title) < MIN_TITLE_LEN_EN:
                    continue
                if "/article/" not in href:
                    continue
                if not href.startswith("http"):
                    href = "https://apnews.com" + href
                if href in seen:
                    continue
                seen.add(href)

                results.append(self._make_item(
                    title=title, url=href, rank=rank, category="Top Stories"
                ))
                rank += 1
                if rank > 10:
                    break
        except Exception as e:
            self.logger.warning(f"[ap] HTML 解析失败: {e}")

        return results
