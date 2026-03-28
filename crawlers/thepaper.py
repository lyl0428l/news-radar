"""
澎湃新闻爬虫 - 爬取澎湃推荐 TOP 10

列表获取策略（按优先级）：
  1. 首页 __NEXT_DATA__ JSON（Next.js SSR 内嵌数据，最稳定）
     包含 recommendImg（5条）+ recommendTxt（6条）+ channelContent
  2. 首页 HTML <a> 标签匹配（兜底）

正文获取策略（按优先级）：
  1. __NEXT_DATA__ JSON（文章页 Next.js SSR 内嵌数据）
     路径: props.pageProps.detailData.contentDetail
  2. CSS 选择器 + readability 提取器（兜底）

注：原热榜 API（cache.thepaper.cn/contentapi/hotspot/list 等）
    已全部失效（返回404或HTML），不再尝试。

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

# 注：澎湃所有已知内容 API（cache.thepaper.cn、api.thepaper.cn、
# newsDetail_forward 等）均已失效（返回 404 或 HTML 而非 JSON）。
# 正文获取完全依赖页面 __NEXT_DATA__ + readability 兜底。


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
        "[class*='cententWrap']",  # 澎湃正文容器（有hash后缀如 cententWrap__UojXm）
        ".cententWrap",            # 无hash版本
        ".news_txt",               # 旧版正文
        "[class*='leftcontent']",  # 左侧内容区
        "[class*='normalContentWrap']",  # 普通内容包裹
        "article",                 # HTML5 语义标签
        "[class*='article']",      # 模糊匹配
        "[class*='content']",      # 模糊匹配
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

        resp = self._request(self.base_url)
        if resp is None:
            return results

        resp.encoding = "utf-8"
        html = resp.text

        # --- 来源1: 首页 __NEXT_DATA__（包含所有推荐文章，比 <a> 标签更全） ---
        results = self._extract_list_from_next_data(html)
        if len(results) >= 10:
            self.logger.info(f"[thepaper] 首页 __NEXT_DATA__ 获取: {len(results)} 条")
            return results[:10]

        # --- 来源2: 首页 HTML <a> 标签（兜底补充） ---
        seen = {item["url"] for item in results}
        rank = len(results) + 1
        try:
            soup = BeautifulSoup(html, "lxml")
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
                # 去除 "推荐" 前缀（首页 HTML 中部分链接标题带有推荐前缀）
                title = re.sub(r"^推荐", "", title).strip()
                if not title:
                    continue
                results.append(self._make_item(
                    title=title, url=href, rank=rank, category="推荐"
                ))
                rank += 1
                if rank > 10:
                    break
        except Exception as e:
            self.logger.warning(f"[thepaper] 首页 HTML 解析失败: {e}")

        src = "__NEXT_DATA__+HTML" if results else "HTML"
        self.logger.info(f"[thepaper] 首页获取({src}): {len(results)} 条")
        return results

    def _extract_list_from_next_data(self, html: str) -> list[dict]:
        """
        从首页 __NEXT_DATA__ 提取推荐文章列表。
        数据结构: props.pageProps.data 下包含：
          - recommendImg: list[dict] — 头图推荐（约5条）
          - recommendTxt: list[list[dict]] — 文字推荐（嵌套列表，约6条）
          - recommendChannels[].contentList: list[dict] — 频道推荐
        """
        results = []
        try:
            soup = BeautifulSoup(html, "lxml")
            script = soup.find("script", id="__NEXT_DATA__")
            if not script or not script.string:
                return results

            data = json.loads(_safe_str(script.string))
            if not isinstance(data, dict):
                return results

            page_data = _safe_dict(
                _safe_dict(
                    _safe_dict(data.get("props")).get("pageProps")
                ).get("data")
            )
            if not page_data:
                return results

            seen_ids = set()
            rank = 1

            # forwardType="4" 表示外部跳转（新华社/人民日报/央视等），
            # 这些 URL 会被 307 重定向到外部站点，静态请求无法获取正文。
            # 注：forwardType 在 JSON 中是字符串类型。
            _EXTERNAL_FORWARD_TYPES = {"4"}

            def _add_item(item_data: dict, category: str):
                nonlocal rank
                if not isinstance(item_data, dict) or rank > 20:
                    return
                cid = _safe_str(item_data.get("contId") or item_data.get("id"))
                name = _safe_str(item_data.get("name") or item_data.get("title"))
                if not cid or not name or cid in seen_ids:
                    return
                # 跳过外部跳转文章（forwardType="4" → 307 重定向到新华社/人民日报等）
                fwd_type = _safe_str(item_data.get("forwardType"))
                if fwd_type in _EXTERNAL_FORWARD_TYPES:
                    return
                seen_ids.add(cid)
                url = f"{self.base_url}/newsDetail_forward_{cid}"
                results.append(self._make_item(
                    title=name, url=url, rank=rank,
                    summary=_safe_str(item_data.get("summary")),
                    category=category,
                ))
                rank += 1

            # 1. recommendImg — 头图推荐
            for item in _safe_list(page_data.get("recommendImg")):
                _add_item(item, "头条")

            # 2. recommendTxt — 文字推荐（嵌套列表结构）
            for group in _safe_list(page_data.get("recommendTxt")):
                if isinstance(group, list):
                    for item in group:
                        _add_item(item, "推荐")
                elif isinstance(group, dict):
                    _add_item(group, "推荐")

            # 3. recommendChannels[].contentList — 频道推荐
            for channel in _safe_list(page_data.get("recommendChannels")):
                if isinstance(channel, dict):
                    for item in _safe_list(channel.get("contentList")):
                        _add_item(item, "频道")

        except (json.JSONDecodeError, ValueError, AttributeError, TypeError) as e:
            self.logger.debug(f"[thepaper] 首页 __NEXT_DATA__ 解析失败: {e}")

        return results

    # ================================================================
    #  详情页：多策略提取完整正文
    # ================================================================

    def fetch_detail(self, item: dict) -> dict:
        """
        澎湃新闻详情页抓取（纯静态HTTP）。
        策略1：页面 HTML 中的 __NEXT_DATA__（Next.js SSR，最可靠）
        策略2：Next.js _next/data JSON API（SPA页面__NEXT_DATA__不含文章数据时）
        策略3：CSS选择器 + readability 兜底

        注：部分首页推荐文章（如新华社转载）会被 307 重定向到外部站点，
        这些 URL 没有 __NEXT_DATA__，由通用提取器兜底处理。
        """
        if not isinstance(item, dict):
            return {}
        url = _safe_str(item.get("url"))
        if not url:
            return {}

        from config import DETAIL_FETCH_TIMEOUT

        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36"),
            "Referer": "https://www.thepaper.cn/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

        result = {}
        html_text = ""
        try:
            resp = self._request(url, timeout=DETAIL_FETCH_TIMEOUT, headers=headers)
            if resp is not None:
                resp.encoding = "utf-8"
                html_text = resp.text
                # 检测是否被重定向到外部站点（如新华社 xinhuaxmt.com）
                final_url = getattr(resp, "url", url) or url
                if "thepaper.cn" not in final_url:
                    self.logger.debug(
                        f"[thepaper] 外部重定向: {url[:50]} → {final_url[:50]}"
                    )
                    # 仍然尝试提取（外部页面用通用提取器可能有效）
                    from utils.content_extractor import extract_content
                    result = extract_content(resp.text, final_url)
                else:
                    result = self.parse_detail(resp.text, url)
        except Exception as e:
            self.logger.debug(f"[thepaper] HTML请求失败: {url[:60]} | {e}")

        # 策略2：如果__NEXT_DATA__没有文章数据，尝试 _next/data API
        if len(_safe_str(result.get("content"))) < 50:
            api_result = self._fetch_via_next_data_api(url, html_text, headers,
                                                       DETAIL_FETCH_TIMEOUT)
            if api_result and len(_safe_str(api_result.get("content"))) > len(
                    _safe_str(result.get("content"))):
                result = api_result

        # Playwright 渲染由 main.py 统一批量处理
        return result

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
            # 普通文章（最常见）—— detailData.contentDetail
            lambda p: _safe_dict(p.get("detailData", {})).get("contentDetail"),
            # detailData.detailData（双层嵌套，部分版本）
            lambda p: _safe_dict(
                _safe_dict(p.get("detailData", {})).get("detailData", {})
            ).get("contentDetail"),
            # detailData 本身就是 contentDetail（某些页面结构）
            lambda p: p.get("detailData"),
            # 视频文章
            lambda p: p.get("detail"),
            # 图集文章
            lambda p: p.get("newsDetail"),
            # 直接在 pageProps
            lambda p: p.get("contentDetail"),
            # newsData.detail.contentDetail（新版本嵌套）
            lambda p: _safe_dict(
                _safe_dict(p.get("newsData", {})).get("detail", {})
            ).get("contentDetail"),
            # data 子层
            lambda p: _safe_dict(p.get("data", {})).get("contentDetail"),
            lambda p: _safe_dict(p.get("data", {})).get("detail"),
            lambda p: _safe_dict(p.get("data", {})).get("newsDetail"),
            # 其他嵌套
            lambda p: _safe_dict(p.get("initialData", {})).get("contentDetail"),
            lambda p: _safe_dict(p.get("serverData", {})).get("contentDetail"),
            # contId 在 pageProps 顶层但 contentDetail 在更深层
            lambda p: _safe_dict(
                _safe_dict(p.get("detailData", {})).get("data", {})
            ).get("contentDetail"),
        ]

        for path_fn in candidates:
            try:
                val = path_fn(page_props)
                if isinstance(val, dict) and val:
                    # 验证是否是文章数据：有正文内容 或 有标题字段
                    if (val.get("content") or val.get("name") or
                            val.get("contTxt") or val.get("contId") or
                            val.get("summary")):
                        return val
            except Exception:
                continue
        return {}

    @staticmethod
    def _deep_find_detail(data: dict, depth: int = 0) -> dict:
        """
        深度递归搜索包含 content 且是文章数据的字典。
        用于处理未知的新 pageProps 结构。
        放宽匹配条件：只要有 content/contTxt/txt 字段且长度足够即可。
        """
        if depth > 8 or not isinstance(data, dict):
            return {}
        # 判断当前节点是否是文章数据
        content_val = (data.get("content") or data.get("contTxt")
                       or data.get("txt") or data.get("contentHtml") or "")
        # 条件1：有 content + name（标准文章）
        if content_val and data.get("name"):
            return data
        # 条件2：有 content 且长度 > 200（像是正文HTML），且有 contId/id（文章ID）
        if (isinstance(content_val, str) and len(content_val) > 200
                and (data.get("contId") or data.get("id"))):
            return data
        # 条件3：有 contentDetail 子字段
        cd = data.get("contentDetail")
        if isinstance(cd, dict) and cd:
            cd_content = (cd.get("content") or cd.get("contTxt")
                          or cd.get("txt") or "")
            if isinstance(cd_content, str) and len(cd_content) > 50:
                return cd
        # 递归子节点
        for key, val in data.items():
            if isinstance(val, dict):
                found = ThePaperCrawler._deep_find_detail(val, depth + 1)
                if found:
                    return found
        return {}

    def _fetch_via_next_data_api(self, url: str, html_text: str,
                                  headers: dict, timeout: int) -> dict:
        """
        通过 Next.js 的 _next/data API 获取文章数据。
        Next.js 应用在客户端路由时使用 /_next/data/{buildId}/{path}.json 端点，
        这个端点返回与 __NEXT_DATA__ 相同结构的 JSON 数据。
        """
        # 从页面 HTML 提取 buildId
        build_id = ""
        if html_text:
            m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html_text)
            if m:
                build_id = m.group(1)
        if not build_id:
            return {}

        # 从 URL 提取文章 ID 路径
        article_id = _extract_thepaper_id(url)
        if not article_id:
            return {}

        # 构建 _next/data API URL
        api_url = (f"https://www.thepaper.cn/_next/data/{build_id}/"
                   f"newsDetail_forward_{article_id}.json")
        try:
            api_headers = dict(headers)
            api_headers["Accept"] = "application/json"
            resp = self._request(api_url, timeout=timeout, headers=api_headers)
            if resp is not None and resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    page_props = _safe_dict(
                        _safe_dict(data.get("pageProps"))
                    )
                    if not page_props:
                        page_props = _safe_dict(
                            _safe_dict(data.get("props", {})).get("pageProps")
                        )
                    detail = self._find_detail_in_props(page_props)
                    if not detail:
                        detail = self._deep_find_detail(page_props)
                    if detail and isinstance(detail, dict):
                        result = self._parse_detail_dict(detail, url)
                        if len(_safe_str(result.get("content"))) > 50:
                            self.logger.info(
                                f"[thepaper] _next/data API 成功: {url[:50]}"
                            )
                            return result
        except Exception as e:
            self.logger.debug(f"[thepaper] _next/data API 失败: {url[:50]} | {e}")

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

        # 正文 HTML（多个可能的字段名，按优先级）
        content_html = _safe_str(
            detail.get("content") or detail.get("contTxt")
            or detail.get("txt") or detail.get("contentHtml")
        )

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

        # 发布时间（逐步回退：pubTime → publishTime → interactionNum.time）
        pub_time = (
            detail.get("pubTime")
            or detail.get("publishTime")
            or detail.get("pubDate")
        )
        if not pub_time and isinstance(detail.get("interactionNum"), dict):
            pub_time = detail["interactionNum"].get("time", "")
        result["pub_time"] = _safe_str(pub_time)

        return result
