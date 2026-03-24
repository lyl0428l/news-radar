"""
央视新闻爬虫 - 爬取央视头条 TOP 10

列表获取:
  1. 央视新闻频道首页 HTML（news.cctv.com）
  2. 央视网主站 HTML（www.cctv.com）

视频提取:
  央视视频通过 JS 函数 creatMultiPlayerTest(divid, guid, ...) 嵌入页面。
  guid 是 32 位十六进制视频 ID。
  通过 CNTV 视频信息 API 将 guid 解析为实际的 HLS 视频流 URL:
    https://vdn.apps.cntv.cn/api/getHttpVideoInfo.do?pid={guid}
  返回:
    - hls_url: HLS 视频流（.m3u8）
    - image: 视频封面图
    - video.chapters: MP4 分段 URL（备选）
"""
import re
import json
import logging
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH

logger = logging.getLogger(__name__)

# CNTV 视频信息 API
_CNTV_VIDEO_API = "https://vdn.apps.cntv.cn/api/getHttpVideoInfo.do?pid={guid}"

# 从 JS 中提取视频 guid 的正则（两种嵌入模式）
# 模式1 (news.cctv.com): creatMultiPlayerTest("flash_0","fd1f3a941c31402b...","false",...)
_GUID_PATTERN_PLAYER = re.compile(
    r'creatMultiPlayerTest\s*\(\s*"[^"]*"\s*,\s*"([a-f0-9]{32})"',
    re.IGNORECASE,
)
# 模式2 (tv.cctv.com): var guid = "f3a5362a181c43309abd31e79364c592";
_GUID_PATTERN_VAR = re.compile(
    r'var\s+guid\s*=\s*"([a-f0-9]{32})"',
    re.IGNORECASE,
)

# 央视新闻频道列表 API（可能有效的）
_CCTV_LIST_API = (
    "https://api.cntv.cn/NewVideo/getVideoListById"
    "?id=TOPC1451528971114112&p=1&n=20&sort=desc&mode=0&serviceId=tvcctv"
)


class CCTVCrawler(BaseCrawler):

    detail_selectors = [".content_area", "#content_area", ".cnt_bd", ".text_con"]

    def __init__(self):
        super().__init__()
        self.name = "cctv"
        self.display_name = "央视新闻"
        self.language = "zh"

    # ================================================================
    #  列表获取
    # ================================================================

    def crawl(self) -> list[dict]:
        results = []

        # 方案 1: 新闻频道首页 HTML
        results = self._try_news_page()
        if len(results) >= 5:
            return results

        # 方案 2: 央视网主站 HTML
        results = self._try_main_page()
        return results

    def _try_news_page(self) -> list:
        """爬取央视新闻频道页"""
        resp = self._request("https://news.cctv.com/")
        if resp is None:
            return []
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        return self._extract_links(soup)

    def _try_main_page(self) -> list:
        """爬取央视网主站"""
        resp = self._request("https://www.cctv.com/")
        if resp is None:
            return []
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        return self._extract_links(soup)

    def _extract_links(self, soup: BeautifulSoup) -> list:
        """从页面提取新闻链接"""
        results = []
        rank = 1
        seen = set()

        for a in soup.find_all("a", href=True):
            href = str(a["href"]).strip()
            title = a.get_text(strip=True)

            if not title or len(title) < MIN_TITLE_LEN_ZH:
                continue
            if "cctv.com" not in href:
                continue
            if "/20" not in href:
                continue
            if not href.startswith("http"):
                href = "https:" + href if href.startswith("//") else "https://news.cctv.com" + href
            if href in seen:
                continue
            seen.add(href)

            results.append(self._make_item(
                title=title, url=href, rank=rank, category="头条"
            ))
            rank += 1
            if rank > 10:
                break

        return results

    # ================================================================
    #  详情页解析（正文 + 视频）
    # ================================================================

    def parse_detail(self, html: str, url: str) -> dict:
        """
        自定义央视详情页解析:
        1. 通用提取器获取正文和图片
        2. 从 JS 中提取视频 guid
        3. 通过 CNTV API 获取实际视频 URL
        4. 将视频嵌入 content_html
        """
        from utils.content_extractor import extract_content
        result = extract_content(html, url, selectors=self.detail_selectors)

        # 提取视频
        videos = self._extract_cctv_videos(html)
        if videos:
            result["videos"] = videos
            # 设置缩略图
            if not result.get("thumbnail"):
                for v in videos:
                    if v.get("poster"):
                        result["thumbnail"] = v["poster"]
                        break
            # 将视频嵌入 content_html
            if result.get("content_html"):
                result["content_html"] = self._embed_videos(
                    videos, result["content_html"]
                )
                for v in videos:
                    v["in_content"] = True

        return result

    def _extract_cctv_videos(self, html: str) -> list:
        """
        从央视页面的 JS 代码中提取视频。
        1. 用正则找到所有 creatMultiPlayerTest() 调用中的 guid
        2. 通过 CNTV 视频 API 将 guid 解析为 HLS 视频流
        """
        # 两种模式提取 guid
        guids = _GUID_PATTERN_PLAYER.findall(html)
        guids += _GUID_PATTERN_VAR.findall(html)
        if not guids:
            return []

        videos = []
        seen = set()
        for guid in guids[:5]:  # 最多 5 个视频
            if guid in seen:
                continue
            seen.add(guid)

            video = self._resolve_cntv_video(guid)
            if video:
                videos.append(video)

        return videos

    def _resolve_cntv_video(self, guid: str) -> dict:
        """
        通过 CNTV 视频信息 API 将 guid 解析为实际视频 URL。
        API: https://vdn.apps.cntv.cn/api/getHttpVideoInfo.do?pid={guid}
        返回 HLS URL（优先）或 MP4 URL。
        """
        api_url = _CNTV_VIDEO_API.format(guid=guid)
        try:
            resp = self._request(api_url, timeout=10, skip_cffi=True)
            if resp is None:
                return {}
            data = resp.json()

            # 提取 HLS URL（优先）
            hls_url = data.get("hls_url", "")

            # 提取封面图
            poster = data.get("image", "")

            # 提取标题
            title = data.get("title", "")

            # 如果没有 HLS，从 video.chapters 提取 MP4
            mp4_url = ""
            if not hls_url:
                video = data.get("video", {})
                if isinstance(video, dict):
                    # chapters 是 MP4 分段列表
                    for key in ("chapters", "chapters2", "chapters3"):
                        chapters = video.get(key, [])
                        if isinstance(chapters, list) and chapters:
                            mp4_url = chapters[0].get("url", "")
                            if mp4_url:
                                break
                    # 直接的 url 字段
                    if not mp4_url:
                        mp4_url = video.get("url", "")

            video_url = hls_url or mp4_url
            if not video_url:
                return {}

            vtype = "m3u8" if ".m3u8" in video_url else "mp4"

            return {
                "url": video_url,
                "type": vtype,
                "poster": poster,
                "title": title,
            }

        except Exception as e:
            logger.debug(f"[cctv] CNTV 视频 API 失败: {guid} | {e}")
            return {}

    @staticmethod
    def _embed_videos(videos: list, content_html: str) -> str:
        """将视频嵌入 content_html 正文顶部"""
        if not videos:
            return content_html

        tags = []
        for v in videos:
            vurl = v.get("url", "")
            vtype = v.get("type", "")
            poster = v.get("poster", "")
            title = v.get("title", "")

            if not vurl:
                continue

            poster_attr = f' poster="{poster}"' if poster else ""

            if vtype == "m3u8":
                tag = (
                    f'<video controls preload="metadata" playsinline data-hls-src="{vurl}"'
                    f'{poster_attr}'
                    f' style="width:100%;max-width:100%;border-radius:8px;margin-bottom:12px;background:#000;">'
                    f'您的浏览器不支持视频播放</video>'
                )
            else:
                tag = (
                    f'<video controls preload="metadata"{poster_attr}'
                    f' style="width:100%;max-width:100%;border-radius:8px;margin-bottom:12px;background:#000;">'
                    f'<source src="{vurl}" type="video/mp4">'
                    f'您的浏览器不支持视频播放</video>'
                )

            if title:
                tag += f'<div style="color:#999;font-size:13px;margin-bottom:16px;">{title}</div>'

            tags.append(tag)

        if not tags:
            return content_html

        return "\n".join(tags) + "\n" + content_html
