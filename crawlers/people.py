"""
人民网爬虫 - 爬取人民网热榜 TOP 10
"""
import json
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH


class PeopleCrawler(BaseCrawler):

    # 人民网正文容器选择器（覆盖多版本页面结构）
    detail_selectors = [
        ".rm_txt_con", "#rwb_zw", ".text_con",
        ".col-1", ".article", ".text_c",
        "#p_content", ".show_text", ".content",
        "[class*='article']", "[class*='content']",
    ]

    def parse_detail(self, html: str, url: str) -> dict:
        """人民网专用详情页解析：通用提取器 + 作者/来源/时间补充"""
        from utils.content_extractor import extract_content
        result = extract_content(html, url, selectors=self.detail_selectors)

        if not html:
            return result

        try:
            soup = BeautifulSoup(html, "lxml")

            # 补充作者
            if not result.get("author"):
                # 方案1：.col-1-1 p.sou / .author / .article-author
                for sel in (".col-1-1 .sou", ".author", ".article-author",
                            ".edit", "[class*='author']", "[class*='source']",
                            ".article_info .sou", ".info .sou"):
                    el = soup.select_one(sel)
                    if el:
                        text = el.get_text(strip=True)
                        # 过滤"来源："前缀
                        text = text.replace("来源：", "").replace("来源:", "").strip()
                        if text and len(text) < 50:
                            result["author"] = text
                            break

            if not result.get("author"):
                # 方案2：<meta name="author">
                m = soup.find("meta", attrs={"name": "author"})
                if m:
                    result["author"] = (m.get("content") or "").strip()

            if not result.get("author"):
                # 方案3：<meta name="source"> 人民网特有字段
                m = soup.find("meta", attrs={"name": "source"})
                if m:
                    result["author"] = (m.get("content") or "").strip()

            # 补充发布时间
            if not result.get("pub_time"):
                for sel in (".col-1-1 .sou span", ".pubtime", ".article_info .time",
                            ".info .time", "[class*='time']", "[class*='date']"):
                    el = soup.select_one(sel)
                    if el:
                        text = el.get_text(strip=True)
                        parsed = self.parse_time(text)
                        if parsed:
                            result["pub_time"] = parsed
                            break

        except Exception as e:
            self.logger.debug(f"[people] 作者/时间补充失败: {e}")

        return result

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
