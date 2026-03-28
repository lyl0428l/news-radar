"""
界面新闻爬虫 - 爬取界面首页头条 TOP 10

列表获取（多级回退）:
  1. 首页 HTML（主力，无需 API）
  2. 频道分类页补充（财经/科技/商业）
  3. 界面新闻 AJAX API（后备）
  4. 界面新闻 RSS 源（最终后备，无需访问 Google）

优化:
  - 过滤短讯/快报类内容，优先保留有实质内容的长文
  - 按频道分类页补充文章（财经、科技等）
"""
import re
import json
import logging
import feedparser
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH

logger = logging.getLogger(__name__)

# 界面新闻 AJAX 分页 API（返回 HTML 片段）
_AJAX_API = "https://a.jiemian.com/index.php?m=index&a=indexAjaxJmedia&page={page}"

# 界面新闻自有 RSS 源（国内可访问，替代 Google News RSS）
_JIEMIAN_RSS_URLS = [
    "https://www.jiemian.com/rss/",                    # 全站 RSS
    "https://www.jiemian.com/lists/2.html?format=rss", # 财经频道
]

# 界面新闻频道页（补充更多高质量文章）
_CHANNEL_PAGES = [
    "https://www.jiemian.com/lists/2.html",   # 财经
    "https://www.jiemian.com/lists/62.html",  # 科技
    "https://www.jiemian.com/lists/88.html",  # 商业
]


class JiemianCrawler(BaseCrawler):

    detail_selectors = [
        ".article-content",       # 正文主体（最常见）
        ".article-main",          # 正文外层容器
        ".article-view",          # 沉浸式阅读模式
        "#article_detail",        # 旧版详情页
        ".article-detail",        # 旧版详情页备用
        ".article-body",          # 备用
        "[class*='articleContent']",
        "[class*='article-content']",
    ]

    def __init__(self):
        super().__init__()
        self.name = "jiemian"
        self.display_name = "界面新闻"
        self.language = "zh"

    def fetch_detail(self, item: dict) -> dict:
        """
        界面新闻详情页抓取（纯静态HTTP）。
        界面新闻是 SSR，正文在首次 HTML 中就有。
        关键：使用完整桌面 UA 并设置 Referer，避免被反爬拦截。
        """
        if not isinstance(item, dict):
            return {}
        url = item.get("url", "") or ""
        if not url:
            return {}

        from config import DETAIL_FETCH_TIMEOUT

        # 界面新闻对 UA 较敏感，用完整桌面浏览器 UA + Referer
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36"),
            "Referer": "https://www.jiemian.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        # 静态 HTTP 请求（Playwright 渲染由 main.py 统一批量处理）
        result = {}
        try:
            resp = self._request(url, timeout=DETAIL_FETCH_TIMEOUT, headers=headers)
            if resp is not None:
                resp.encoding = "utf-8"
                result = self.parse_detail(resp.text, url)
        except Exception as e:
            self.logger.debug(f"[jiemian] 静态请求失败: {url[:60]} | {e}")

        return result

    def parse_detail(self, html: str, url: str) -> dict:
        """界面新闻详情页解析：通用提取器 + 作者/时间补充"""
        from utils.content_extractor import extract_content
        result = extract_content(html, url, selectors=self.detail_selectors)

        if not html:
            return result

        try:
            soup = BeautifulSoup(html, "lxml")

            # 补充作者
            if not result.get("author"):
                # 方案1：.article-author / .author-name / .byline
                for sel in (".article-author", ".author-name", ".byline",
                            ".article__author", "[class*='author']",
                            ".writer", ".reporter", ".article-source"):
                    el = soup.select_one(sel)
                    if el:
                        text = el.get_text(strip=True)
                        text = text.replace("作者：", "").replace("记者：", "").strip()
                        if text and len(text) < 50:
                            result["author"] = text
                            break

            if not result.get("author"):
                # 方案2：<meta name="author">
                m = soup.find("meta", attrs={"name": "author"})
                if m:
                    result["author"] = (m.get("content") or "").strip()

            if not result.get("author"):
                # 方案3：JSON-LD 结构化数据
                for script in soup.find_all("script", type="application/ld+json"):
                    try:
                        import json as _json
                        data = _json.loads(script.string or "")
                        author = data.get("author", {})
                        if isinstance(author, dict):
                            name = author.get("name", "").strip()
                        elif isinstance(author, list) and author:
                            name = (author[0].get("name", "") if isinstance(author[0], dict)
                                    else str(author[0])).strip()
                        else:
                            name = str(author).strip()
                        if name and len(name) < 50:
                            result["author"] = name
                            break
                    except Exception:
                        continue

            # 补充发布时间
            if not result.get("pub_time"):
                # 方案1：<meta property="article:published_time">
                m = (soup.find("meta", attrs={"property": "article:published_time"}) or
                     soup.find("meta", attrs={"name": "pubdate"}) or
                     soup.find("meta", attrs={"name": "publish_date"}))
                if m:
                    val = (m.get("content") or "").strip()
                    if val:
                        result["pub_time"] = self.parse_time(val)

            if not result.get("pub_time"):
                # 方案2：页面时间元素
                for sel in (".article-time", ".publish-time", ".article__time",
                            "time[datetime]", "[class*='time']", "[class*='date']"):
                    el = soup.select_one(sel)
                    if el:
                        # 优先取 datetime 属性
                        dt = el.get("datetime", "") or el.get_text(strip=True)
                        parsed = self.parse_time(dt)
                        if parsed:
                            result["pub_time"] = parsed
                            break

            if not result.get("pub_time"):
                # 方案3：JSON-LD 时间
                for script in soup.find_all("script", type="application/ld+json"):
                    try:
                        import json as _json
                        data = _json.loads(script.string or "")
                        dt = (data.get("datePublished") or
                              data.get("dateModified") or "").strip()
                        if dt:
                            result["pub_time"] = self.parse_time(dt)
                            break
                    except Exception:
                        continue

        except Exception as e:
            self.logger.debug(f"[jiemian] 作者/时间补充失败: {e}")

        return result

    # ================================================================
    #  列表获取
    # ================================================================

    def crawl(self) -> list[dict]:
        """
        获取界面新闻 TOP 10。
        先从首页 + 频道页抓取大量候选文章，再筛选最优 10 篇。
        """
        candidates = []
        seen = set()

        # 方案 1: 首页 HTML
        homepage_items = self._crawl_homepage()
        for it in homepage_items:
            url = it.get("url", "")
            if url not in seen:
                seen.add(url)
                candidates.append(it)

        # 方案 2: 频道页补充
        if len(candidates) < 15:
            channel_items = self._crawl_channels()
            for it in channel_items:
                url = it.get("url", "")
                if url not in seen:
                    seen.add(url)
                    candidates.append(it)

        # 方案 3: 界面新闻自有 RSS（最终后备，国内可直接访问）
        if len(candidates) < 5:
            rss_items = self._crawl_jiemian_rss()
            for it in rss_items:
                url = it.get("url", "")
                if url not in seen:
                    seen.add(url)
                    candidates.append(it)

        if not candidates:
            return []

        # 筛选和排序：优先保留标题长（有实质内容）的文章
        # 短讯标题通常很短（<20字），长文标题通常 >20 字
        candidates.sort(key=lambda x: len(x.get("title", "")), reverse=True)

        # 取 TOP 10 并重新编号
        results = candidates[:10]
        for i, it in enumerate(results, 1):
            it["rank"] = i

        return results

    def _crawl_homepage(self) -> list:
        """抓取界面新闻首页"""
        resp = self._request("https://www.jiemian.com/")
        if resp is None:
            return []
        resp.encoding = "utf-8"
        return self._extract_article_links(resp.text)

    def _crawl_channels(self) -> list:
        """抓取频道分类页补充文章"""
        results = []
        for channel_url in _CHANNEL_PAGES:
            resp = self._request(channel_url)
            if resp is None:
                continue
            resp.encoding = "utf-8"
            items = self._extract_article_links(resp.text)
            results.extend(items)
        return results

    def _crawl_jiemian_rss(self) -> list:
        """界面新闻自有 RSS 源（国内可直接访问，无需 Google）"""
        results = []
        seen = set()
        for rss_url in _JIEMIAN_RSS_URLS:
            try:
                resp = self._request(rss_url, timeout=15)
                if resp is None:
                    continue
                feed = feedparser.parse(resp.content)
                if not feed.entries:
                    continue
                for entry in feed.entries[:20]:
                    title = str(entry.get("title", "")).strip()
                    link = str(entry.get("link", "")).strip()
                    # 清理标题后缀
                    title = re.sub(r"\s*[-|]\s*界面新闻.*$", "", title).strip()
                    if not title or len(title) < MIN_TITLE_LEN_ZH:
                        continue
                    if "jiemian.com" not in link:
                        continue
                    if link in seen:
                        continue
                    seen.add(link)
                    # 提取作者
                    author = str(entry.get("author", "") or
                                 entry.get("dc_creator", "")).strip()
                    pub_time = self.parse_time(str(entry.get("published", "")))
                    results.append(self._make_item(
                        title=title, url=link, rank=0, category="头条",
                        pub_time=pub_time, author=author,
                    ))
                if results:
                    logger.info(f"[jiemian] RSS 获取 {len(results)} 条")
                    return results
            except Exception as e:
                logger.debug(f"[jiemian] RSS {rss_url} 失败: {e}")
        return results

    def _extract_article_links(self, html: str) -> list:
        """从页面 HTML 中提取文章链接"""
        results = []
        seen = set()
        try:
            soup = BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = str(a["href"]).strip()
                title = a.get_text(strip=True)
                if not title or len(title) < MIN_TITLE_LEN_ZH:
                    continue
                if "/article/" not in href:
                    continue
                if not href.startswith("http"):
                    href = "https://www.jiemian.com" + href
                if href in seen:
                    continue
                seen.add(href)

                results.append(self._make_item(
                    title=title, url=href, rank=0, category="头条"
                ))
        except Exception as e:
            logger.warning(f"[jiemian] HTML 解析失败: {e}")

        return results
