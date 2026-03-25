"""
腾讯新闻爬虫 - 爬取腾讯新闻热榜 TOP 10

详情页特殊处理：
  腾讯新闻 new.qq.com 的正文数据通过 JS 渲染，静态 HTML 中正文容器为空。
  文章内容存储在多个可能的 JS 变量/JSON 结构中：
    1. window.__INITIAL_STATE__.data.articleDetail.content
    2. window.DATA.articleDetail.content
    3. <script type="application/json" id="initial-data"> 中的 JSON
  上述均提取失败时，回退到 readability 自动提取。
"""
import re
import json
import logging
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH

logger = logging.getLogger(__name__)


class TencentCrawler(BaseCrawler):

    detail_selectors = [
        ".content-article", ".LEFT .content", "#ArticleContent", ".article-content",
        ".qq_article", ".Cnt-Main-Article-QQ", ".content",
    ]

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

    # ========== 详情页：从 JS 变量提取正文 ==========

    def parse_detail(self, html: str, url: str) -> dict:
        """
        腾讯新闻专用详情页解析。
        优先从 JS 变量提取完整正文，回退到通用提取器。
        """
        result = self._extract_from_js(html, url)
        if result and result.get("content") and len(result["content"]) > 50:
            return result

        # 回退到通用提取器
        from utils.content_extractor import extract_content
        return extract_content(html, url, selectors=self.detail_selectors)

    def _extract_from_js(self, html: str, url: str) -> dict:
        """
        从腾讯新闻页面的 JS 变量中提取文章正文。
        腾讯新闻的正文数据存在几种位置：
          1. window.__INITIAL_STATE__ 中的 articleDetail
          2. <script id="initial-data" type="application/json"> 内联 JSON
          3. 页面内 JSON-LD 的 articleBody
        """
        result = {
            "content_html": "", "content": "", "images": [], "videos": [],
            "thumbnail": "", "author": "", "pub_time": "",
        }

        # --- 方案1: 尝试提取 window.__INITIAL_STATE__ ---
        patterns = [
            r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\});\s*(?:window|var|</script>)',
            r'window\.__INITIAL_STATE__\s*=\s*(\{.+\})',
        ]
        for pat in patterns:
            m = re.search(pat, html, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                    detail = (
                        data.get("data", {}).get("articleDetail", {})
                        or data.get("articleDetail", {})
                        or data.get("detail", {})
                        or {}
                    )
                    if detail:
                        extracted = self._parse_tencent_detail(detail, url)
                        if extracted.get("content") and len(extracted["content"]) > 50:
                            return extracted
                except (json.JSONDecodeError, ValueError):
                    pass

        # --- 方案2: <script id="initial-data"> 内联 JSON ---
        soup = BeautifulSoup(html, "lxml")
        for script in soup.find_all("script", id=re.compile(r"initial", re.I)):
            try:
                text = script.string or ""
                if not text.strip():
                    continue
                data = json.loads(text)
                detail = (
                    data.get("data", {}).get("articleDetail", {})
                    or data.get("articleDetail", {})
                    or {}
                )
                if detail:
                    extracted = self._parse_tencent_detail(detail, url)
                    if extracted.get("content") and len(extracted["content"]) > 50:
                        return extracted
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

        # --- 方案3: 从 JSON-LD 提取基本信息 ---
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if not isinstance(data, dict):
                    continue
                article_body = data.get("articleBody", "")
                if article_body and len(article_body) > 50:
                    result["content"] = article_body
                    result["content_html"] = "".join(
                        f"<p>{p.strip()}</p>" for p in article_body.split("\n") if p.strip()
                    )
                    result["author"] = (
                        data.get("author", {}).get("name", "")
                        if isinstance(data.get("author"), dict)
                        else str(data.get("author", ""))
                    )
                    result["pub_time"] = data.get("datePublished", "")
                    result["thumbnail"] = (
                        data.get("image", {}).get("url", "")
                        if isinstance(data.get("image"), dict)
                        else str(data.get("image", ""))
                    )
                    return result
            except (json.JSONDecodeError, TypeError):
                pass

        return result

    @staticmethod
    def _parse_tencent_detail(detail: dict, url: str) -> dict:
        """从腾讯新闻的 articleDetail 字典提取正文数据"""
        result = {
            "content_html": "", "content": "", "images": [], "videos": [],
            "thumbnail": "", "author": "", "pub_time": "",
        }

        # 正文 HTML
        content_html = (detail.get("content") or detail.get("articleContent")
                        or detail.get("body") or "")
        if content_html:
            try:
                content_soup = BeautifulSoup(content_html, "lxml")
                content_text = content_soup.get_text(separator="\n")
                content_text = re.sub(r"\n\s*\n", "\n\n", content_text).strip()
                result["content_html"] = content_html
                result["content"] = content_text

                # 从正文提取图片
                seen = set()
                for img in content_soup.find_all("img"):
                    src = img.get("src") or img.get("data-src") or ""
                    if src and src.startswith("http") and src not in seen:
                        seen.add(src)
                        result["images"].append({"url": src, "caption": img.get("alt", "")})
            except Exception:
                pass

        # 作者
        author = (detail.get("author") or detail.get("authorName")
                  or detail.get("mediaName") or "")
        if isinstance(author, dict):
            author = author.get("name", "")
        result["author"] = str(author).strip()

        # 发布时间
        pub_time = (detail.get("pubTime") or detail.get("publishTime")
                    or detail.get("ctime") or "")
        result["pub_time"] = str(pub_time).strip()

        # 缩略图
        thumbnail = (detail.get("thumbnails") or detail.get("thumbnail")
                     or detail.get("picUrl") or "")
        if isinstance(thumbnail, list) and thumbnail:
            thumbnail = thumbnail[0]
        if isinstance(thumbnail, dict):
            thumbnail = thumbnail.get("url", "")
        result["thumbnail"] = str(thumbnail).strip()
        if not result["thumbnail"] and result["images"]:
            result["thumbnail"] = result["images"][0]["url"]

        return result
