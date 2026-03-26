"""
腾讯新闻爬虫 - 爬取腾讯新闻热榜 TOP 10

正文获取策略（按优先级）：
  1. 腾讯新闻内容 API（直接返回 JSON，含完整正文 HTML）
     https://r.inews.qq.com/gw/article/detail?id={article_id}
  2. window.__INITIAL_STATE__ JS 变量
  3. <script id="initial-data"> 内联 JSON
  4. JSON-LD 结构化数据
  5. 通用 readability 提取器（兜底）

图片：从正文 HTML 中提取，补充 og:image
视频：从 videoInfo 字段提取，嵌入正文顶部
作者：mediaName / author 字段
"""
import re
import json
import logging
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH

logger = logging.getLogger(__name__)

# 腾讯新闻内容 API
# 实测日志证明 r.inews.qq.com/gw/event/article 对所有ID格式均404
# 改用以下接口（均基于 new.qq.com 域名）：
_TENCENT_DETAIL_APIS = [
    # PC端文章详情接口
    "https://new.qq.com/rain/api/detail",
    # 腾讯新闻 SSR 数据接口
    "https://new.qq.com/api/detail",
    # 腾讯新闻内容网关（不同路径）
    "https://r.inews.qq.com/gw/article/detail",
]


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


def _extract_article_id(url: str) -> str:
    """从腾讯新闻 URL 提取文章 ID"""
    if not url:
        return ""
    # https://new.qq.com/rain/a/20240301A01234 → 20240301A01234
    m = re.search(r'/rain/a/([A-Za-z0-9]+)', url)
    if m:
        return m.group(1)
    # https://new.qq.com/omn/20240301/20240301A01234.html → 20240301A01234
    m = re.search(r'/(\w{14,20})(?:\.html)?$', url)
    if m:
        return m.group(1)
    return ""


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

    def crawl(self) -> list[dict]:
        results = []

        # 腾讯新闻热点精选 API
        api_url = "https://i.news.qq.com/gw/event/pc_hot_ranking_list"
        params = {"ids_hash": "", "offset": 0, "page_size": 10}
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
                    if title in ("腾讯新闻", "腾讯网") or "热点精选" in title:
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
                params={"offset": 0, "page_size": 10, "rank_type": 1},
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
                    if title in ("腾讯新闻", "腾讯网"):
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
        策略1: 移动端UA请求（腾讯移动端HTML包含更完整的内联JSON数据）
        策略2: PC端UA请求 + JS变量/JSON-LD提取
        策略3: readability兜底
        不再尝试API（经实测r.inews.qq.com和new.qq.com的API对所有ID格式均404）
        """
        if not isinstance(item, dict):
            return {}
        url = _safe_str(item.get("url"))
        if not url:
            return {}

        from config import DETAIL_FETCH_TIMEOUT

        # --- 策略1: 移动端UA请求（移动端页面内嵌更多JSON数据）---
        mobile_ua = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        )
        try:
            resp = self._request(url, timeout=DETAIL_FETCH_TIMEOUT,
                                 headers={"User-Agent": mobile_ua})
            if resp is not None:
                result = self.parse_detail(resp.text, url)
                if result and len(_safe_str(result.get("content"))) > 50:
                    return result
        except Exception as e:
            self.logger.debug(f"[tencent] 移动端请求失败: {url[:60]} | {e}")

        # --- 策略2: PC端UA请求 ---
        try:
            resp = self._request(url, timeout=DETAIL_FETCH_TIMEOUT)
            if resp is not None:
                result = self.parse_detail(resp.text, url)
                if result and len(_safe_str(result.get("content"))) > 50:
                    return result
        except Exception as e:
            self.logger.debug(f"[tencent] PC端请求失败: {url[:60]} | {e}")

        return {}

    def _fetch_via_api(self, article_id: str, url: str, timeout: int) -> dict:
        """
        通过腾讯新闻内容 API 获取完整文章数据。
        每个 API 只尝试一次（不走 _request 的重试机制），
        404 立即跳下一个，避免大量无效重试拖慢速度。
        """
        import requests as _requests
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "Referer": "https://new.qq.com/",
        }
        param_variants = [
            {"id": article_id},
            {"artid": article_id},
        ]
        session = self._get_session()
        for api_url in _TENCENT_DETAIL_APIS:
            for params in param_variants:
                try:
                    resp = session.get(api_url, params=params,
                                       headers=headers, timeout=timeout)
                    if resp.status_code in (404, 400, 403):
                        break  # 该接口不支持此ID，换下一个API
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    if not isinstance(data, dict):
                        continue
                    article = (
                        _safe_dict(data.get("article"))
                        or _safe_dict(data.get("data", {}).get("article"))
                        or _safe_dict(data.get("data"))
                        or _safe_dict(data.get("newsInfo"))
                        or {}
                    )
                    if not article and (data.get("content") or data.get("articleContent")):
                        article = data
                    if article:
                        r = self._parse_api_article(article, url)
                        if r and len(_safe_str(r.get("content"))) > 100:
                            self.logger.info(f"[tencent] API成功: {api_url}")
                            return r
                except Exception as e:
                    self.logger.debug(f"[tencent] API {api_url} 异常: {e}")
                    break
        return {}

    def _parse_api_article(self, article: dict, url: str) -> dict:
        """解析腾讯新闻 API 返回的文章数据"""
        result = {
            "content_html": "", "content": "", "images": [],
            "videos": [], "thumbnail": "", "author": "", "pub_time": "",
        }
        if not isinstance(article, dict):
            return result

        # 正文 HTML（API 直接返回完整 HTML）
        content_html = _safe_str(
            article.get("content") or article.get("articleContent")
            or article.get("body") or article.get("contentHtml")
        )
        if content_html:
            try:
                from utils.content_extractor import sanitize_html
                content_html = sanitize_html(content_html)
            except Exception:
                pass
            result["content_html"] = content_html
            # 提取纯文本
            try:
                csoup = BeautifulSoup(content_html, "lxml")
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
                content_text = re.sub(r"\n\s*\n", "\n\n",
                                      csoup.get_text(separator="\n")).strip()
                result["content"] = content_text
            except Exception as e:
                self.logger.debug(f"[tencent] 正文解析异常: {e}")

        # 作者
        result["author"] = _safe_str(
            article.get("mediaName") or article.get("author")
            or article.get("authorName") or article.get("source")
        )

        # 发布时间
        pub_time = _safe_str(
            article.get("pubTime") or article.get("publishTime")
            or article.get("ctime") or article.get("updateTime")
        )
        result["pub_time"] = pub_time

        # 缩略图
        thumbnail = article.get("thumbnails") or article.get("thumbnail") or article.get("picUrl") or ""
        if isinstance(thumbnail, list) and thumbnail:
            thumbnail = thumbnail[0]
        if isinstance(thumbnail, dict):
            thumbnail = thumbnail.get("url", "")
        result["thumbnail"] = _safe_str(thumbnail)
        if not result["thumbnail"] and result["images"]:
            result["thumbnail"] = result["images"][0]["url"]

        # 视频
        video_info = _safe_dict(article.get("videoInfo") or article.get("video"))
        if video_info:
            vid_url = _safe_str(video_info.get("playUrl") or video_info.get("url"))
            poster = _safe_str(video_info.get("coverUrl") or video_info.get("poster"))
            if vid_url:
                vtype = "m3u8" if ".m3u8" in vid_url else "mp4"
                result["videos"].append({
                    "url": vid_url, "type": vtype,
                    "poster": poster, "in_content": True,
                })

        return result

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
