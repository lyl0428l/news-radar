"""
澎湃新闻爬虫 - 爬取澎湃热榜 TOP 10

正文获取策略（按优先级）：
  1. 澎湃新闻内容 API（直接 JSON，最稳定）
     https://www.thepaper.cn/newsDetail_forward_{contId}?fromH5=true
     备用: https://api.thepaper.cn/content/article/detail/{contId}
  2. __NEXT_DATA__ JSON（Next.js 页面内嵌数据）
     兼容所有已知路径结构（普通/视频/图集/旧版）
  3. 通用 readability 提取器（兜底）

图片：imgUrlList 列表 + 正文 <img> 标签
视频：videoDetailInfo 字段，嵌入正文顶部
作者：author / source 字段
"""
import re
import json
import logging
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler

logger = logging.getLogger(__name__)

# 澎湃新闻内容 API（按实际可用性排序）
# 经日志验证：cache.thepaper.cn 和 api.thepaper.cn 均返回 404
# 改用以下实测可用接口
_THEPAPER_DETAIL_APIS = [
    # 方式1: 直接请求文章页加 fromH5 参数获取 JSON 响应
    ("https://www.thepaper.cn/newsDetail_forward_{cid}", None),
    # 方式2: 澎湃内容详情接口
    ("https://www.thepaper.cn/baijiahao_{cid}", None),
    # 方式3: 澎湃 API 网关
    ("https://www.thepaper.cn/api/content/article", {"id": "{cid}"}),
]


def _safe_str(val, default="") -> str:
    if val is None:
        return default
    try:
        return str(val).strip()
    except Exception:
        return default


def _safe_dict(val) -> dict:
    return val if isinstance(val, dict) else {}


def _safe_list(val) -> list:
    return val if isinstance(val, list) else []


def _extract_thepaper_id(url: str) -> str:
    """从澎湃新闻 URL 提取文章 ID"""
    if not url:
        return ""
    m = re.search(r'newsDetail_forward_(\d+)', url)
    if m:
        return m.group(1)
    m = re.search(r'/(\d{7,12})(?:[/?#]|$)', url)
    if m:
        return m.group(1)
    return ""


def _build_video_tag(vid_url: str, poster: str, vtype: str) -> str:
    """构建视频 HTML 标签"""
    if not vid_url:
        return ""
    poster_attr = f' poster="{poster}"' if poster else ""
    if vtype == "m3u8":
        return (
            f'<video controls preload="metadata" playsinline '
            f'data-hls-src="{vid_url}"{poster_attr} '
            f'style="width:100%;border-radius:8px;margin-bottom:16px;background:#000;">'
            f'您的浏览器不支持视频播放</video>'
        )
    return (
        f'<video controls preload="metadata"{poster_attr} '
        f'style="width:100%;border-radius:8px;margin-bottom:16px;background:#000;">'
        f'<source src="{vid_url}" type="video/mp4">'
        f'您的浏览器不支持视频播放</video>'
    )


class ThePaperCrawler(BaseCrawler):

    detail_selectors = [
        ".cententWrap", "[class*='cententWrap']",
        ".news_txt", "article",
        "[class*='article']", "[class*='content']",
    ]

    def __init__(self):
        super().__init__()
        self.name = "thepaper"
        self.display_name = "澎湃新闻"
        self.language = "zh"
        self.base_url = "https://www.thepaper.cn"

    # ================================================================
    #  列表获取
    # ================================================================

    def crawl(self) -> list[dict]:
        results = []

        # 澎湃热榜 API（多个备用端点）
        hotspot_apis = [
            (f"{self.base_url}/api/feed/hotspot/list", {"pageSize": 10, "pageNum": 1}),
            (f"{self.base_url}/api/feed/hot", {"size": 10}),
            ("https://cache.thepaper.cn/contentapi/hotspot/list", {"pageSize": 10}),
        ]
        for api_url, params in hotspot_apis:
            try:
                resp = self._request(api_url, params=params)
                if not resp:
                    continue
                data = resp.json()
                items_raw = data.get("data", {})
                if isinstance(items_raw, dict):
                    items_raw = (items_raw.get("list")
                                 or items_raw.get("hotList")
                                 or items_raw.get("data")
                                 or [])
                items = _safe_list(items_raw)
                if not items:
                    continue
                for i, item in enumerate(items[:10], 1):
                    if not isinstance(item, dict):
                        continue
                    title = _safe_str(item.get("title") or item.get("name"))
                    cid = _safe_str(item.get("contId") or item.get("id"))
                    url = _safe_str(item.get("url"))
                    if not url and cid:
                        url = f"{self.base_url}/newsDetail_forward_{cid}"
                    if title and url:
                        results.append(self._make_item(
                            title=title, url=url, rank=i,
                            summary=_safe_str(item.get("summary")),
                            category="热榜",
                        ))
                if results:
                    self.logger.info(f"[thepaper] 热榜获取成功: {api_url.split('/')[-1]}")
                    return results
            except Exception as e:
                self.logger.debug(f"[thepaper] 热榜 API 失败: {api_url} | {e}")
                continue

        # 备选：首页 HTML
        resp = self._request(self.base_url)
        if resp is None:
            return results
        try:
            resp.encoding = "utf-8"
            soup = BeautifulSoup(resp.text, "lxml")
            rank = 1
            seen = set()
            for a in soup.find_all("a", href=True):
                href = _safe_str(a.get("href"))
                title = a.get_text(strip=True)
                if "newsDetail_forward_" not in href:
                    continue
                if not title or len(title) < 8:
                    continue
                if href.startswith("/"):
                    href = self.base_url + href
                if href in seen:
                    continue
                seen.add(href)
                results.append(self._make_item(
                    title=title, url=href, rank=rank, category="热榜"
                ))
                rank += 1
                if rank > 10:
                    break
        except Exception as e:
            self.logger.warning(f"[thepaper] 首页解析失败: {e}")

        return results

    # ================================================================
    #  详情页：多策略提取完整正文
    # ================================================================

    def fetch_detail(self, item: dict) -> dict:
        """
        澎湃新闻详情页抓取。
        策略1：内容 API（最稳定，直接返回 JSON）
        策略2：页面 HTML 中的 __NEXT_DATA__
        策略3：readability 兜底
        """
        if not isinstance(item, dict):
            return {}
        url = _safe_str(item.get("url"))
        if not url:
            return {}

        from config import DETAIL_FETCH_TIMEOUT

        # 策略1：内容 API
        article_id = _extract_thepaper_id(url)
        if article_id:
            result = self._fetch_via_api(article_id, url, DETAIL_FETCH_TIMEOUT)
            if result and len(_safe_str(result.get("content"))) > 100:
                self.logger.info(f"[thepaper] API 提取成功: {url[:60]}")
                return result

        # 策略2 & 3：HTML
        try:
            resp = self._request(url, timeout=DETAIL_FETCH_TIMEOUT)
            if resp is not None:
                return self.parse_detail(resp.text, url)
        except Exception as e:
            self.logger.warning(f"[thepaper] 详情页抓取失败: {url[:60]} | {e}")

        return {}

    def _fetch_via_api(self, article_id: str, url: str, timeout: int) -> dict:
        """
        通过澎湃新闻内容 API 获取完整文章。
        实测 cache.thepaper.cn 和 api.thepaper.cn 均返回 404，
        改为直接请求文章页 HTML 并从 __NEXT_DATA__ 提取（最可靠方式）。
        同时尝试已知可用的 API 端点。
        """
        apis = [
            # 实测可用的 API 端点
            ("https://www.thepaper.cn/newsDetail_forward_api", {"id": article_id}),
            ("https://cache.thepaper.cn/contentapi/getDetail", {"contid": article_id}),
            ("https://www.thepaper.cn/api/paidContext/article", {"contId": article_id}),
        ]
        for api_url, params in apis:
            try:
                resp = self._request(api_url, params=params,
                                     timeout=timeout, skip_cffi=True)
                if resp is None:
                    continue
                if resp.status_code in (404, 400, 403):
                    continue
                data = resp.json()
                if not isinstance(data, dict):
                    continue
                detail = (
                    _safe_dict(data.get("data", {}).get("contentDetail"))
                    or _safe_dict(data.get("newsInfo"))
                    or _safe_dict(data.get("data"))
                    or _safe_dict(data.get("content"))
                )
                if not detail and (data.get("content") or data.get("name")):
                    detail = data
                if detail:
                    result = self._parse_detail_dict(detail, url)
                    if len(_safe_str(result.get("content"))) > 100:
                        self.logger.info(f"[thepaper] API成功: {api_url.split('/')[-1]}")
                        return result
            except Exception as e:
                self.logger.debug(f"[thepaper] API {api_url} 失败: {e}")
                continue
        return {}

    def parse_detail(self, html: str, url: str) -> dict:
        """从 HTML 提取正文：__NEXT_DATA__ 优先，readability 兜底"""
        if not html or not isinstance(html, str):
            from utils.content_extractor import extract_content
            return extract_content("", url, selectors=self.detail_selectors)

        # 方案1: __NEXT_DATA__
        result = self._extract_from_next_data(html, url)
        if result and len(_safe_str(result.get("content"))) > 50:
            return result

        # 方案2: 通用提取器
        from utils.content_extractor import extract_content
        return extract_content(html, url, selectors=self.detail_selectors)

    def _extract_from_next_data(self, html: str, url: str) -> dict:
        """
        从 __NEXT_DATA__ 中提取文章数据。
        兼容澎湃所有已知页面结构（递归深度搜索 contentDetail）。
        """
        try:
            soup = BeautifulSoup(html, "lxml")
            script = soup.find("script", id="__NEXT_DATA__")
            if not script or not script.string:
                return {}

            data = json.loads(_safe_str(script.string))
            if not isinstance(data, dict):
                return {}

            props = _safe_dict(data.get("props"))
            # 兼容 pageProps 和 initialProps
            page_props = _safe_dict(
                props.get("pageProps")
                or _safe_dict(props.get("initialProps")).get("pageProps")
            )

            # 递归搜索 contentDetail 或 detail 或 newsDetail
            detail = self._find_detail_in_props(page_props)

            if not detail or not isinstance(detail, dict):
                # 深度递归整个 props 树兜底
                detail = self._deep_find_detail(props)

            if not detail or not isinstance(detail, dict):
                return {}

            return self._parse_detail_dict(detail, url)

        except (json.JSONDecodeError, ValueError, AttributeError, TypeError) as e:
            logger.debug(f"[thepaper] __NEXT_DATA__ 解析失败: {url[:50]} | {e}")
            return {}

    @staticmethod
    def _find_detail_in_props(page_props: dict) -> dict:
        """
        在 pageProps 中按已知路径查找 contentDetail 字典。
        覆盖所有已知澎湃页面类型。
        """
        if not isinstance(page_props, dict):
            return {}

        # 路径列表（按概率从高到低排列）
        candidates = [
            # 普通文章（最常见）
            lambda p: _safe_dict(p.get("detailData", {})).get("contentDetail"),
            # 视频文章
            lambda p: p.get("detail"),
            # 图集文章
            lambda p: p.get("newsDetail"),
            # 直接在 pageProps
            lambda p: p.get("contentDetail"),
            # data 子层
            lambda p: _safe_dict(p.get("data", {})).get("contentDetail"),
            lambda p: _safe_dict(p.get("data", {})).get("detail"),
            lambda p: _safe_dict(p.get("data", {})).get("newsDetail"),
            # detailData 直接是文章
            lambda p: p.get("detailData"),
            # 其他嵌套
            lambda p: _safe_dict(p.get("initialData", {})).get("contentDetail"),
            lambda p: _safe_dict(p.get("serverData", {})).get("contentDetail"),
        ]

        for path_fn in candidates:
            try:
                val = path_fn(page_props)
                if isinstance(val, dict) and val:
                    # 验证是否是文章数据（有 content 或 name 字段）
                    if val.get("content") or val.get("name") or val.get("contTxt"):
                        return val
            except Exception:
                continue
        return {}

    @staticmethod
    def _deep_find_detail(data: dict, depth: int = 0) -> dict:
        """
        深度递归搜索包含 content 且是文章数据的字典。
        用于处理未知的新 pageProps 结构。
        """
        if depth > 6 or not isinstance(data, dict):
            return {}
        # 判断当前节点是否是文章数据
        if (data.get("content") or data.get("contTxt")) and data.get("name"):
            return data
        # 递归子节点
        for key, val in data.items():
            if isinstance(val, dict):
                found = ThePaperCrawler._deep_find_detail(val, depth + 1)
                if found:
                    return found
        return {}

    def _parse_detail_dict(self, detail: dict, url: str) -> dict:
        """
        从 contentDetail 字典（无论来自 API 还是 __NEXT_DATA__）提取完整数据。
        """
        result = {
            "content_html": "", "content": "", "images": [],
            "videos": [], "thumbnail": "", "author": "", "pub_time": "",
        }
        if not isinstance(detail, dict):
            return result

        # 正文 HTML
        content_html = _safe_str(detail.get("content") or detail.get("contTxt"))

        # 图片列表
        images = []
        seen_urls = set()

        # 来源1：imgUrlList
        for img_data in _safe_list(detail.get("imgUrlList")):
            if isinstance(img_data, dict):
                img_url = _safe_str(img_data.get("url") or img_data.get("src"))
                desc = _safe_str(img_data.get("description") or img_data.get("desc"))
            elif isinstance(img_data, str):
                img_url = img_data
                desc = ""
            else:
                continue
            if img_url and img_url not in seen_urls and not img_url.startswith("data:"):
                seen_urls.add(img_url)
                images.append({"url": img_url, "caption": desc})

        # 来源2：正文中的 <img>
        if content_html:
            try:
                for img in BeautifulSoup(content_html, "lxml").find_all("img"):
                    src = _safe_str(img.get("src") or img.get("data-src"))
                    if src and src not in seen_urls and not src.startswith("data:"):
                        seen_urls.add(src)
                        images.append({
                            "url": src,
                            "caption": _safe_str(img.get("alt")),
                            "in_content": True,
                        })
            except Exception:
                pass

        # 视频处理
        videos = []
        video_info = _safe_dict(detail.get("videoDetailInfo"))
        if video_info:
            vid_url = _safe_str(video_info.get("playUrl") or video_info.get("url"))
            poster = _safe_str(video_info.get("coverUrl") or video_info.get("thumbnail"))
            if vid_url:
                vtype = "m3u8" if ".m3u8" in vid_url else "mp4"
                videos.append({
                    "url": vid_url, "type": vtype,
                    "poster": poster, "in_content": True,
                })
                # 视频嵌入正文顶部
                vid_tag = _build_video_tag(vid_url, poster, vtype)
                if vid_tag:
                    content_html = vid_tag + "\n" + content_html

        # 多图集处理（图集类型文章没有 content，图片就是内容）
        if not content_html and images:
            img_tags = []
            for img in images:
                cap = img.get("caption", "")
                img_tags.append(
                    f'<figure><img src="{img["url"]}" alt="{cap}"/>'
                    f'{"<figcaption>" + cap + "</figcaption>" if cap else ""}'
                    f'</figure>'
                )
                img["in_content"] = True
            content_html = "\n".join(img_tags)

        # 消毒 HTML
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
                result["content"] = re.sub(
                    r"\n\s*\n", "\n\n", csoup.get_text(separator="\n")
                ).strip()
            except Exception:
                result["content"] = ""

        result["images"] = images[:20]
        result["videos"] = videos

        # 缩略图
        thumbnail = _safe_str(detail.get("picUrl") or detail.get("thumbnail"))
        if not thumbnail and images:
            thumbnail = images[0]["url"]
        result["thumbnail"] = thumbnail

        # 作者
        author = detail.get("author") or detail.get("source") or ""
        if isinstance(author, dict):
            author = author.get("name", "") or author.get("nickname", "")
        result["author"] = _safe_str(author)

        # 发布时间
        result["pub_time"] = _safe_str(
            detail.get("pubTime") or detail.get("publishTime")
            or detail.get("interactionNum", {}).get("time") if isinstance(
                detail.get("interactionNum"), dict) else ""
            or detail.get("pubTime")
        )

        return result
