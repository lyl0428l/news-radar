"""
界面新闻爬虫 - 爬取界面首页头条 TOP 10

列表获取（多级回退）:
  1. 首页 HTML（主力，无需 API）
  2. AJAX 分页 API（后备，获取更多文章）
  3. Google News RSS（最终后备，在首页改版为 JS 渲染时仍可用）

优化:
  - 过滤短讯/快报类内容，优先保留有实质内容的长文
  - 按频道分类页补充文章（财经、科技等）
"""
import re
import json
import logging
import requests
import feedparser
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH

logger = logging.getLogger(__name__)

# 界面新闻 AJAX 分页 API（返回 HTML 片段）
_AJAX_API = "https://a.jiemian.com/index.php?m=index&a=indexAjaxJmedia&page={page}"

# Google News RSS 后备
_GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search"
    "?q=site:jiemian.com+when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
)

# 界面新闻频道页（补充更多高质量文章）
_CHANNEL_PAGES = [
    "https://www.jiemian.com/lists/2.html",   # 财经
    "https://www.jiemian.com/lists/62.html",  # 科技
    "https://www.jiemian.com/lists/88.html",  # 商业
]


class JiemianCrawler(BaseCrawler):

    detail_selectors = [".article-content", ".article-main", ".article-view"]

    def __init__(self):
        super().__init__()
        self.name = "jiemian"
        self.display_name = "界面新闻"
        self.language = "zh"

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

        # 方案 3: Google News RSS（最终后备）
        if len(candidates) < 5:
            rss_items = self._crawl_google_news_rss()
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

    def _crawl_google_news_rss(self) -> list:
        """Google News RSS 后备"""
        try:
            resp = requests.get(_GOOGLE_NEWS_RSS, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                return []
            feed = feedparser.parse(resp.content)
            if not feed.entries:
                return []

            results = []
            for entry in feed.entries[:20]:
                title = str(entry.get("title", "")).strip()
                link = str(entry.get("link", "")).strip()
                # 清理标题后缀 " - 界面新闻"
                title = re.sub(r"\s*-\s*(?:界面新闻|Jiemian\.com).*$", "", title)

                # 尝试解析 Google News 重定向
                try:
                    r = requests.head(link, allow_redirects=True, timeout=8,
                                      headers={"User-Agent": "Mozilla/5.0"})
                    if "jiemian.com" in r.url:
                        link = r.url
                except Exception:
                    pass

                if "jiemian.com" not in link:
                    continue
                if not title or len(title) < MIN_TITLE_LEN_ZH:
                    continue

                pub_time = str(entry.get("published", ""))
                results.append(self._make_item(
                    title=title, url=link, rank=0, category="头条",
                    pub_time=self.parse_time(pub_time),
                ))

            return results
        except Exception as e:
            logger.debug(f"[jiemian] Google News RSS 失败: {e}")
            return []

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
