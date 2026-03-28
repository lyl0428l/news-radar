"""
腾讯新闻爬虫 - 爬取腾讯新闻热榜 TOP 10

正文获取策略（按优先级）：
  1. view.inews.qq.com 用PC端UA请求 → 302重定向到 news.qq.com/rain/a/...
     重定向后的页面包含完整SSR正文HTML（.content-article 容器）
  2. window.__INITIAL_STATE__ JS 变量（旧版页面兜底）
  3. JSON-LD 结构化数据（旧版页面兜底）
  4. 通用 readability 提取器（最终兜底）

注意：
  - 移动端UA请求 view.inews.qq.com 返回纯JS SPA壳，完全无正文
  - PC端UA请求会自动重定向，利用服务端渲染（SSR）获取完整HTML
  - __INITIAL_STATE__ 在当前版本腾讯新闻页面中已不存在
"""
import re
import json
import logging
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH

logger = logging.getLogger(__name__)


def _safe_str(val, default="") -> str:
    """安全转字符串，None/非字符串均转为 default"""
    if val is None:
        return default
    try:
        return str(val).strip()
    except Exception:
        return default


def _safe_list(val) -> list:
    """安全转列表"""
    if isinstance(val, list):
        return val
    return []


def _safe_dict(val) -> dict:
    """安全转字典"""
    if isinstance(val, dict):
        return val
    return {}


class TencentCrawler(BaseCrawler):

    detail_selectors = [
        ".content-article", ".LEFT .content", "#ArticleContent",
        ".article-content", ".qq_article", ".Cnt-Main-Article-QQ",
        ".content", "[class*='article']", "[class*='content']",
    ]

    def __init__(self):
        super().__init__()
        self.name = "tencent"
        self.display_name = "腾讯新闻"
        self.language = "zh"

    # ================================================================
    #  列表获取
    # ================================================================

    @staticmethod
    def _is_valid_article_url(url: str, title: str) -> bool:
        """
        过滤非文章URL：
        - TIP 开头的ID是专题/引导页（重定向到 babyhome.htm）
        - 包含 babyhome/babygohome 的是腾讯新闻 App 下载页
        - 标题含特定关键词的是站点导航
        """
        if not url:
            return False
        url_lower = url.lower()
        title_lower = title.lower() if title else ""
        # 专题引导页
        if "/rain/a/TIP" in url or "/a/TIP" in url:
            return False
        if "babyhome" in url_lower or "babygohome" in url_lower:
            return False
        # 非新闻标题
        if title in ("腾讯新闻", "腾讯网"):
            return False
        if "热点精选" in title_lower or "每10分钟更新" in title_lower:
            return False
        return True

    def crawl(self) -> list[dict]:
        results = []

        # 腾讯新闻热点精选 API（多请求11条，过滤后保证10条）
        api_url = "https://i.news.qq.com/gw/event/pc_hot_ranking_list"
        params = {"ids_hash": "", "offset": 0, "page_size": 20}
        resp = self._request(api_url, params=params)
        if resp:
            try:
                data = resp.json()
                idlist = _safe_list(data.get("idlist"))
                first = _safe_dict(idlist[0] if idlist else {})
                items = _safe_list(first.get("newslist"))
                rank = 1
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    title = _safe_str(item.get("title"))
                    url = _safe_str(item.get("url") or item.get("surl"))
                    if not title:
                        continue
                    if not url:
                        article_id = _safe_str(item.get("id"))
                        if article_id:
                            url = f"https://new.qq.com/rain/a/{article_id}"
                    if not url or not url.startswith("http"):
                        continue
                    if not self._is_valid_article_url(url, title):
                        continue
                    results.append(self._make_item(
                        title=title, url=url, rank=rank,
                        summary=_safe_str(item.get("abstract")),
                        category="热榜",
                    ))
                    rank += 1
                    if rank > 10:
                        break
                if results:
                    return results
            except Exception as e:
                self.logger.warning(f"[tencent] 热榜 API 失败: {e}")

        # 备选: 腾讯新闻移动端热榜 API（轻量，无需 Playwright）
        try:
            resp2 = self._request(
                "https://i.news.qq.com/gw/event/hot_ranking_list",
                params={"offset": 0, "page_size": 20, "rank_type": 1},
            )
            if resp2:
                data2 = resp2.json()
                idlist2 = _safe_list(data2.get("idlist"))
                first2 = _safe_dict(idlist2[0] if idlist2 else {})
                items2 = _safe_list(first2.get("newslist"))
                rank = 1
                for item in items2:
                    if not isinstance(item, dict):
                        continue
                    title = _safe_str(item.get("title"))
                    url = _safe_str(item.get("url") or item.get("surl"))
                    if not title or not url or not url.startswith("http"):
                        continue
                    if not self._is_valid_article_url(url, title):
                        continue
                    results.append(self._make_item(
                        title=title, url=url, rank=rank,
                        summary=_safe_str(item.get("abstract")),
                        category="热榜",
                    ))
                    rank += 1
                    if rank > 10:
                        break
        except Exception as e:
            self.logger.warning(f"[tencent] 备用 API 失败: {e}")

        return results

    # ================================================================
    #  详情页：多策略提取完整正文
    # ================================================================

    def fetch_detail(self, item: dict) -> dict:
        """
        腾讯新闻详情页抓取。

        核心策略：PC端UA + 跟随重定向
        - view.inews.qq.com 会 302 重定向到 news.qq.com/rain/a/...
        - 重定向后的页面是SSR渲染，包含完整正文HTML（.content-article容器）
        - 不使用移动端UA（移动端返回纯JS SPA壳，无任何正文）
        - 必须显式设置 PC UA，否则 _request 的随机 UA 可能命中移动端 UA

        不再尝试内容API（经实测r.inews.qq.com和new.qq.com的API对所有ID格式均404）
        """
        if not isinstance(item, dict):
            return {}
        url = _safe_str(item.get("url"))
        if not url:
            return {}

        from config import DETAIL_FETCH_TIMEOUT

        # 显式 PC 端 UA + Referer（关键：view.inews.qq.com 必须用桌面UA触发302重定向）
        pc_headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36"),
            "Referer": "https://news.qq.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        result = {}
        try:
            resp = self._request(url, timeout=DETAIL_FETCH_TIMEOUT,
                                 headers=pc_headers)
            if resp is not None:
                resp.encoding = "utf-8"
                # 获取最终URL（可能经过302重定向）
                final_url = getattr(resp, "url", url) or url
                result = self.parse_detail(resp.text, final_url)

                # 如果 view.inews.qq.com 没有重定向且正文为空，
                # 主动构造 new.qq.com/rain/a/ URL 重试
                if (len(_safe_str(result.get("content"))) < 50
                        and "view.inews.qq.com" in url):
                    rain_url = self._convert_to_rain_url(url)
                    if rain_url and rain_url != url:
                        resp2 = self._request(rain_url, timeout=DETAIL_FETCH_TIMEOUT,
                                              headers=pc_headers)
                        if resp2 is not None:
                            resp2.encoding = "utf-8"
                            result2 = self.parse_detail(resp2.text, rain_url)
                            if len(_safe_str(result2.get("content"))) > len(
                                    _safe_str(result.get("content"))):
                                result = result2
        except Exception as e:
            self.logger.debug(f"[tencent] 请求失败: {url[:60]} | {e}")

        # Playwright 渲染由 main.py 统一批量处理
        return result

    @staticmethod
    def _convert_to_rain_url(url: str) -> str:
        """将 view.inews.qq.com URL 转换为 new.qq.com/rain/a/ URL"""
        if not url:
            return ""
        # view.inews.qq.com/a/{article_id} → new.qq.com/rain/a/{article_id}
        parsed = urlparse(url)
        path = parsed.path  # e.g. /a/20260328A01234
        if path.startswith("/a/"):
            article_id = path[3:].strip("/")
            if article_id:
                return f"https://new.qq.com/rain/a/{article_id}"
        # 也可能是 /w/{id} 格式
        m = re.search(r'/[aw]/([A-Za-z0-9]+)', path)
        if m:
            return f"https://new.qq.com/rain/a/{m.group(1)}"
        return ""

    def parse_detail(self, html: str, url: str) -> dict:
        """从页面 HTML 多路提取正文，API 不可用时的回退方案"""
        if not html or not isinstance(html, str):
            from utils.content_extractor import extract_content
            return extract_content("", url, selectors=self.detail_selectors)

        result = self._extract_from_js(html, url)
        if result and len(_safe_str(result.get("content"))) > 50:
            return result

        # 最终回退到通用提取器
        from utils.content_extractor import extract_content
        return extract_content(html, url, selectors=self.detail_selectors)

    def _extract_from_js(self, html: str, url: str) -> dict:
        """从 HTML 内嵌的 JS 变量中提取正文数据"""
        result = {
            "content_html": "", "content": "", "images": [],
            "videos": [], "thumbnail": "", "author": "", "pub_time": "",
        }

        # 方案1: window.__INITIAL_STATE__
        for pat in [
            r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\});\s*(?:window|var|</script>)',
            r'window\.__INITIAL_STATE__\s*=\s*(\{.+\})',
        ]:
            m = re.search(pat, html, re.DOTALL)
            if not m:
                continue
            try:
                data = json.loads(m.group(1))
                for path in [
                    lambda d: d.get("data", {}).get("articleDetail"),
                    lambda d: d.get("articleDetail"),
                    lambda d: d.get("detail"),
                    lambda d: d.get("store", {}).get("articleDetail"),
                ]:
                    try:
                        detail = path(data)
                        if isinstance(detail, dict) and detail:
                            r = self._parse_tencent_detail(detail, url)
                            if len(_safe_str(r.get("content"))) > 50:
                                return r
                    except Exception:
                        continue
            except (json.JSONDecodeError, ValueError, AttributeError):
                continue

        # 方案2: <script id="initial-data"> 或 id 含 initial 的 script
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            return result

        for script in soup.find_all("script", id=re.compile(r"initial|__DATA__", re.I)):
            try:
                text = _safe_str(script.string)
                if not text:
                    continue
                data = json.loads(text)
                for key in ("articleDetail", "data", "detail", "article"):
                    sub = _safe_dict(data.get(key))
                    if sub:
                        r = self._parse_tencent_detail(sub, url)
                        if len(_safe_str(r.get("content"))) > 50:
                            return r
                # data.data.articleDetail
                inner = _safe_dict(_safe_dict(data.get("data")).get("articleDetail"))
                if inner:
                    r = self._parse_tencent_detail(inner, url)
                    if len(_safe_str(r.get("content"))) > 50:
                        return r
            except Exception:
                continue

        # 方案3: JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                raw = _safe_str(script.string)
                if not raw:
                    continue
                data = json.loads(raw)
                if not isinstance(data, dict):
                    continue
                article_body = _safe_str(data.get("articleBody"))
                if len(article_body) > 50:
                    result["content"] = article_body
                    result["content_html"] = "".join(
                        f"<p>{p.strip()}</p>"
                        for p in article_body.split("\n") if p.strip()
                    )
                    # 作者
                    author_raw = data.get("author")
                    if isinstance(author_raw, dict):
                        result["author"] = _safe_str(author_raw.get("name"))
                    elif isinstance(author_raw, list) and author_raw:
                        result["author"] = _safe_str(
                            author_raw[0].get("name") if isinstance(author_raw[0], dict)
                            else author_raw[0]
                        )
                    else:
                        result["author"] = _safe_str(author_raw)
                    result["pub_time"] = _safe_str(data.get("datePublished"))
                    img = data.get("image")
                    if isinstance(img, dict):
                        result["thumbnail"] = _safe_str(img.get("url"))
                    elif isinstance(img, str):
                        result["thumbnail"] = img
                    return result
            except Exception:
                continue

        return result

    @staticmethod
    def _parse_tencent_detail(detail: dict, url: str) -> dict:
        """从腾讯新闻的 articleDetail 字典提取正文数据"""
        result = {
            "content_html": "", "content": "", "images": [],
            "videos": [], "thumbnail": "", "author": "", "pub_time": "",
        }
        if not isinstance(detail, dict):
            return result

        content_html = _safe_str(
            detail.get("content") or detail.get("articleContent")
            or detail.get("body") or detail.get("contentHtml")
        )
        if content_html:
            try:
                from utils.content_extractor import sanitize_html
                content_html = sanitize_html(content_html)
            except Exception:
                pass
            result["content_html"] = content_html
            try:
                csoup = BeautifulSoup(content_html, "lxml")
                content_text = re.sub(r"\n\s*\n", "\n\n",
                                      csoup.get_text(separator="\n")).strip()
                result["content"] = content_text
                # 提取图片
                seen = set()
                for img in csoup.find_all("img"):
                    src = _safe_str(img.get("src") or img.get("data-src"))
                    if src and src.startswith("http") and src not in seen:
                        seen.add(src)
                        result["images"].append({
                            "url": src,
                            "caption": _safe_str(img.get("alt")),
                            "in_content": True,
                        })
            except Exception:
                pass

        # 作者
        author = detail.get("author") or detail.get("authorName") or detail.get("mediaName") or ""
        if isinstance(author, dict):
            author = author.get("name", "")
        result["author"] = _safe_str(author)

        # 发布时间
        result["pub_time"] = _safe_str(
            detail.get("pubTime") or detail.get("publishTime") or detail.get("ctime")
        )

        # 缩略图
        thumbnail = detail.get("thumbnails") or detail.get("thumbnail") or detail.get("picUrl") or ""
        if isinstance(thumbnail, list) and thumbnail:
            thumbnail = thumbnail[0]
        if isinstance(thumbnail, dict):
            thumbnail = thumbnail.get("url", "")
        result["thumbnail"] = _safe_str(thumbnail)
        if not result["thumbnail"] and result["images"]:
            result["thumbnail"] = result["images"][0]["url"]

        return result
