"""
人民网爬虫 - 爬取人民网热榜 TOP 10

数据源策略（按优先级）：
1. 首页抓取（最可靠，返回当日最新文章，来源多个子域名）
2. 热榜 API（可能数据过时或包含大量重复，作为补充）
3. 多频道首页补充（确保来源多样性）
"""
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH


class PeopleCrawler(BaseCrawler):

    # 人民网正文容器选择器（覆盖多版本页面结构和多子域名）
    # 按实测匹配率排序：.rm_txt_con 覆盖 29/40，.text_con 覆盖 4/40
    detail_selectors = [
        ".rm_txt_con",           # 新版人民网正文（最常见，覆盖 ~73% 页面）
        ".text_con",             # 旧版正文（cpc/theory 频道）
        "#rwb_zw",               # 人民网主站正文
        ".text_c",               # 旧版正文变体
        "#p_content",            # 理论频道等
        ".col-1",                # 多栏布局主栏（edu 频道）
        ".article",              # 通用
        ".show_text",            # 展示类文章
        ".d2txt_con",            # 图片频道
        ".box_con",              # 专题页
        ".content_area",         # 备用
        ".content",              # 通用（pic 频道）
        "#articleContent",       # 备用
        ".paper_detail",         # 人民日报电子版 paper.people.com.cn
        "#ozoom",                # 人民日报电子版正文区域
        "#detail",               # 部分频道使用 (env, legal)
        ".w1200",                # 宽版布局正文容器
        ".article-content",      # 新版本部分子域名
        ".news_con",             # 社会频道
        "[class*='rm_txt']",     # 模糊匹配人民网特有类名
        "[class*='text_con']",   # 模糊匹配
        "[class*='article']",    # 模糊匹配（health 频道命中）
        "[class*='content']",    # 模糊匹配
    ]

    def fetch_detail(self, item: dict) -> dict:
        """
        人民网详情页抓取。
        人民网有多个子域名，页面编码不统一（GBK/UTF-8），
        需要特殊处理编码检测。
        对于被 403 拦截的子域名，设置合适的 Referer。
        """
        if not isinstance(item, dict):
            return {}
        url = item.get("url", "") or ""
        if not url:
            return {}

        from config import DETAIL_FETCH_TIMEOUT

        # 根据子域名动态设置 Referer（部分子域名检查 Referer）
        parsed = urlparse(url)
        host = parsed.hostname or ""
        referer = f"http://{host}/"

        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36"),
            "Referer": referer,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        result = {}
        try:
            resp = self._request(url, timeout=DETAIL_FETCH_TIMEOUT, headers=headers)
            if resp is not None:
                # 人民网子域名编码不统一，用 apparent_encoding 自动检测
                if resp.apparent_encoding:
                    resp.encoding = resp.apparent_encoding
                else:
                    # 检查是否为 GBK/GB2312
                    content_type = resp.headers.get("Content-Type", "")
                    if "gb" in content_type.lower():
                        resp.encoding = "gbk"
                    elif "utf" in content_type.lower():
                        resp.encoding = "utf-8"
                    else:
                        resp.encoding = resp.apparent_encoding or "utf-8"

                result = self.parse_detail(resp.text, url)
        except Exception as e:
            self.logger.debug(f"[people] 静态请求失败: {url[:60]} | {e}")

        # Playwright 渲染由 base.py _fetch_all_details 统一批量处理
        return result

    def parse_detail(self, html: str, url: str) -> dict:
        """人民网专用详情页解析：通用提取器 + 作者/来源/时间补充"""
        if not html:
            from utils.content_extractor import extract_content
            return extract_content("", url, selectors=self.detail_selectors)

        # 根据子域名选择最优选择器顺序
        parsed_url = urlparse(url)
        host = (parsed_url.hostname or "").lower()
        selectors = list(self.detail_selectors)

        # paper.people.com.cn（人民日报电子版）使用特殊选择器
        if "paper.people" in host:
            selectors = ["#ozoom", ".paper_detail", ".article"] + selectors
        # cpc.people.com.cn（中共新闻网）
        elif "cpc.people" in host:
            selectors = [".text_con", "#p_content", ".text_c"] + selectors
        # theory.people.com.cn（理论频道）
        elif "theory.people" in host:
            selectors = [".text_con", "#p_content"] + selectors
        # env.people.com.cn（环保频道）
        elif "env.people" in host:
            selectors = ["#detail", ".rm_txt_con"] + selectors

        # 预处理：移除分享/推广元素
        try:
            soup_pre = BeautifulSoup(html, "lxml")
            for sel in (".bza", ".share", ".article-share",
                        ".editor", "[class*='share']"):
                for el in soup_pre.select(sel):
                    el.decompose()
            html = str(soup_pre)
        except Exception:
            pass

        from utils.content_extractor import extract_content
        result = extract_content(html, url, selectors=selectors)

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

    def _is_today_url(self, url: str) -> bool:
        """判断 URL 中的日期是否为今天或昨天（允许1天偏差）"""
        today = datetime.now()
        yesterday = today - timedelta(days=1)
        # 人民网 URL 格式: /n1/2026/0328/... 或 /n2/2026/0328/...
        m = re.search(r'/(\d{4})/(\d{2})(\d{2})/', url)
        if m:
            try:
                url_date = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return url_date.date() >= yesterday.date()
            except ValueError:
                pass
        return True  # 无法解析日期则不过滤

    def _extract_links_from_html(self, html: str, base_url: str,
                                  max_count: int = 20) -> list[dict]:
        """从 HTML 页面提取文章链接（去重，过滤标题过短的）"""
        soup = BeautifulSoup(html, "lxml")
        results = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = str(a["href"]).strip()
            title = a.get_text(strip=True)
            if (title and len(title) >= MIN_TITLE_LEN_ZH
                    and ("/n1/20" in href or "/n2/20" in href)
                    and href not in seen):
                seen.add(href)
                if not href.startswith("http"):
                    href = urljoin(base_url, href)
                results.append({"title": title, "url": href})
                if len(results) >= max_count:
                    break
        return results

    def _crawl_api(self) -> list[dict]:
        """
        数据源1: 热榜 API（去重 + 新鲜度校验）
        注意: 该 API 可能返回过时数据（已观察到冻结在数月前的情况），
        因此需要验证文章日期。
        """
        api_url = "https://www.people.com.cn/210801/211150/index.js"
        resp = self._request(api_url)
        if not resp:
            return []

        try:
            text = resp.text.strip()
            if "(" in text and text.endswith(")"):
                text = text[text.index("(") + 1:-1]
            data = json.loads(text)
            items = data if isinstance(data, list) else data.get("items", data.get("list", []))

            # 去重 + 日期过滤
            seen = set()
            results = []
            for item in items:
                title = item.get("title", "").strip()
                url = item.get("url", "").strip()
                if title and url and url not in seen and self._is_today_url(url):
                    seen.add(url)
                    results.append({"title": title, "url": url})
                    if len(results) >= 10:
                        break

            if results:
                self.logger.info(f"[people] 热榜 API: {len(results)} 条（已去重+日期过滤）")
            return results
        except Exception as e:
            self.logger.warning(f"[people] 热榜 API 解析失败: {e}")
            return []

    def _crawl_homepage(self) -> list[dict]:
        """数据源2: 人民网首页（最可靠，返回当日最新文章）"""
        resp = self._request("https://www.people.com.cn/")
        if not resp:
            return []

        try:
            resp.encoding = resp.apparent_encoding
            return self._extract_links_from_html(resp.text, "https://www.people.com.cn/")
        except Exception as e:
            self.logger.warning(f"[people] 首页解析失败: {e}")
            return []

    def _crawl_channel(self, channel_url: str, channel_name: str) -> list[dict]:
        """数据源3: 各频道首页补充（确保来源多样性）"""
        resp = self._request(channel_url)
        if not resp:
            return []

        try:
            if resp.apparent_encoding:
                resp.encoding = resp.apparent_encoding
            return self._extract_links_from_html(resp.text, channel_url, max_count=5)
        except Exception as e:
            self.logger.debug(f"[people] {channel_name}频道解析失败: {e}")
            return []

    def crawl(self) -> list[dict]:
        """
        多数据源策略：
        1. 优先使用首页文章（最可靠、最新鲜）
        2. 热榜 API 作为补充（去重 + 新鲜度校验）
        3. 如果首页 + API 条数不足，从各频道补充
        保证结果不重复、来源多样、数据新鲜
        """
        seen_urls = set()
        results = []

        def _add_items(items: list, category: str):
            """去重添加文章"""
            for item in items:
                url = item.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    results.append(self._make_item(
                        title=item["title"],
                        url=url,
                        rank=len(results) + 1,
                        category=category,
                    ))

        # 数据源1: 首页（最可靠）
        homepage_items = self._crawl_homepage()
        if homepage_items:
            _add_items(homepage_items[:10], "头条")
            self.logger.info(f"[people] 首页获取: {min(len(homepage_items), 10)} 条")

        # 数据源2: 热榜 API（补充，经过去重+日期过滤）
        if len(results) < 10:
            api_items = self._crawl_api()
            if api_items:
                _add_items(api_items, "热榜")

        # 数据源3: 各频道首页补充（确保来源多样性）
        if len(results) < 10:
            channels = [
                ("http://politics.people.com.cn/GB/1024/index.html", "时政"),
                ("http://finance.people.com.cn/", "财经"),
                ("http://society.people.com.cn/", "社会"),
            ]
            for channel_url, channel_name in channels:
                if len(results) >= 10:
                    break
                channel_items = self._crawl_channel(channel_url, channel_name)
                if channel_items:
                    _add_items(channel_items[:3], channel_name)

        # 更新 rank 确保连续
        for i, item in enumerate(results[:10], 1):
            item["rank"] = i

        return results[:10]
