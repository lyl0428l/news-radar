"""
BBC News 爬虫 - 通过 RSS 爬取 BBC Top Stories TOP 10
选择器说明:
  - main#main-content article: BBC 文章页主容器（最精确）
  - [data-component='text-block']: BBC 文章段落块的稳定 data 属性
  - article: 语义化回退

视频提取说明:
  BBC 视频完全由客户端 JS 动态加载，HTML 中不包含 <video>/<iframe> 标签。
  视频元数据存储在 <script id="__NEXT_DATA__"> JSON 中，包含 versionId。
  通过 BBC Media Selector API 将 versionId 解析为实际的 HLS/DASH 视频流 URL。
  API: https://open.live.bbc.co.uk/mediaselector/6/select/version/2.0/mediaset/pc/vpid/{versionId}/format/json
"""
import re
import json
import logging
from urllib.parse import urljoin
from crawlers.base import RSSCrawler

logger = logging.getLogger(__name__)

# BBC Media Selector API 模板
_BBC_MEDIA_SELECTOR_URL = (
    "https://open.live.bbc.co.uk/mediaselector/6/select/"
    "version/2.0/mediaset/pc/vpid/{vpid}/format/json"
)

# BBC 图片 URL 模板中的占位符替换
_BBC_IMAGE_WIDTH = "1024"


class BBCCrawler(RSSCrawler):

    detail_selectors = ["main#main-content article", "[data-component='text-block']", "article"]

    def __init__(self):
        super().__init__()
        self.name = "bbc"
        self.display_name = "BBC News"
        self.language = "en"
        self.rss_url = "https://feeds.bbci.co.uk/news/rss.xml"
        self.category = "Top Stories"

    # BBC 正文中需要移除的 data-component 类型
    _BBC_REMOVE_COMPONENTS = {
        "headline-block",       # 标题（模板已显示，重复）
        "byline-block",         # 作者/分享按钮（模板已显示，含死 SVG）
        "advertisement-block",  # 广告占位符（空 div）
        "ad-slot",              # 广告槽（空 div）
        "links-block",          # 相关文章推荐（BBC 导航，非正文）
        "tag-list-block",       # 话题标签（BBC 导航，非正文）
    }

    def parse_detail(self, html: str, url: str) -> dict:
        """
        从 BBC 详情页提取正文/图片/视频。
        1. 用通用提取器获取基础内容
        2. 清洗 BBC UI 垃圾（广告、导航、死按钮、SVG 图标等）
        3. 从 __NEXT_DATA__ 提取视频，替换空壳 video-block 为可播放的视频标签
        """
        from utils.content_extractor import extract_content
        result = extract_content(html, url, selectors=self.detail_selectors)

        # --- BBC 专有视频提取（从 __NEXT_DATA__ 中获取） ---
        videos = []
        try:
            videos = self._extract_bbc_videos(html, url)
            if videos:
                result["videos"] = videos
                if not result.get("thumbnail"):
                    for v in videos:
                        if v.get("poster"):
                            result["thumbnail"] = v["poster"]
                            break
        except Exception as e:
            logger.debug(f"[bbc] BBC 视频提取失败: {url} | {e}")

        # --- 清洗 content_html 中的 BBC UI 垃圾 ---
        content_html = result.get("content_html", "")
        if content_html:
            content_html = self._clean_bbc_html(content_html, videos)
            result["content_html"] = content_html
            # 同步更新纯文本
            try:
                from bs4 import BeautifulSoup
                text = BeautifulSoup(content_html, "lxml").get_text(separator="\n")
                text = re.sub(r"\n\s*\n", "\n\n", text).strip()
                if text:
                    result["content"] = text
            except Exception:
                pass

        # 标记视频已嵌入正文
        if videos:
            for v in videos:
                v["in_content"] = True

        return result

    def _clean_bbc_html(self, content_html: str, videos: list) -> str:
        """
        深度清洗 BBC content_html，移除所有 UI 垃圾，只保留正文内容。
        保留: text-block（段落）、subheadline-block（小标题）、image-block（图片）
        移除: headline, byline, ads, links, tags, SVG, buttons, placeholder 图片
        替换: 空壳 video-block → 真实可播放的视频标签
        """
        from bs4 import BeautifulSoup, Tag
        soup = BeautifulSoup(content_html, "lxml")

        # 1. 移除指定的 data-component 块
        for comp_type in self._BBC_REMOVE_COMPONENTS:
            for el in soup.find_all(attrs={"data-component": comp_type}):
                el.decompose()

        # 2. 移除所有 <svg> 元素（分享/保存/播放按钮图标）
        for svg in soup.find_all("svg"):
            svg.decompose()

        # 3. 移除所有 <button> 元素（分享/保存/播放按钮）
        for btn in soup.find_all("button"):
            btn.decompose()

        # 4. 移除 grey-placeholder.png 占位图片
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if "grey-placeholder" in src or "hide-when-no-script" in (img.get("class") or []):
                img.decompose()

        # 5. 处理 video-block：替换为真实可播放的视频标签
        video_idx = 0
        for vblock in soup.find_all(attrs={"data-component": "video-block"}):
            # 提取 video-block 中的 figcaption 作为视频说明
            caption = ""
            figcap = vblock.find("figcaption")
            if figcap:
                caption = figcap.get_text(strip=True)

            # 用真实视频标签替换空壳
            if video_idx < len(videos):
                v = videos[video_idx]
                video_idx += 1
                new_tag = self._make_video_tag(soup, v, caption)
                vblock.replace_with(new_tag)
            else:
                # 没有对应的视频源，移除空壳
                vblock.decompose()

        # 6. 如果有剩余的视频（不在 video-block 中），插入到正文顶部
        if video_idx < len(videos):
            body = soup.find("body") or soup
            first_child = body.find()
            for v in videos[video_idx:]:
                new_tag = self._make_video_tag(soup, v, "")
                if first_child:
                    first_child.insert_before(new_tag)
                else:
                    body.append(new_tag)

        # 7. 移除 figcaption 中 BBC 水印文字 "Getty Images" 等的 <span> 标签
        #    保留 figcaption 本身（有意义的图片说明）
        for span in soup.find_all("span", class_=lambda c: c and "jVqbAn" in c):
            # 这是 BBC 的图片来源标注 span，保留文字内容不变
            pass

        return str(soup)

    @staticmethod
    def _make_video_tag(soup, video: dict, caption: str):
        """为一个视频创建可播放的 HTML 元素"""
        from bs4 import BeautifulSoup, Tag
        vurl = video.get("url", "")
        vtype = video.get("type", "")
        poster = video.get("poster", "")

        wrapper = soup.new_tag("div", style=(
            "margin:20px 0;border-radius:8px;overflow:hidden;background:#000;"
        ))

        if vtype == "m3u8":
            vid = soup.new_tag("video",
                               controls=True, preload="metadata", playsinline="",
                               style="width:100%;display:block;")
            vid["data-hls-src"] = vurl
            if poster:
                vid["poster"] = poster
            vid.string = "您的浏览器不支持视频播放"
            wrapper.append(vid)
        elif vtype == "iframe":
            container = soup.new_tag("div", style=(
                "position:relative;padding-bottom:56.25%;height:0;"))
            iframe = soup.new_tag("iframe", src=vurl, allowfullscreen=True,
                                  loading="lazy",
                                  style="position:absolute;top:0;left:0;width:100%;height:100%;border:none;")
            container.append(iframe)
            wrapper.append(container)
        else:
            vid = soup.new_tag("video",
                               controls=True, preload="metadata",
                               style="width:100%;display:block;")
            if poster:
                vid["poster"] = poster
            source = soup.new_tag("source", src=vurl, type="video/mp4")
            vid.append(source)
            vid.append("您的浏览器不支持视频播放")
            wrapper.append(vid)

        if caption:
            cap_div = soup.new_tag("div", style=(
                "padding:8px 12px;color:#ccc;font-size:13px;"
                "background:#1a1a1a;"))
            cap_div.string = caption
            wrapper.append(cap_div)

        return wrapper

    def _extract_bbc_videos(self, html: str, url: str) -> list:
        """
        从 BBC 页面的 __NEXT_DATA__ JSON 中提取视频信息。
        提取流程:
          1. 解析 __NEXT_DATA__ JSON
          2. 递归搜索所有包含 versionId 的 media 块
          3. 通过 BBC Media Selector API 获取实际视频流 URL
        """
        videos = []

        # 提取 __NEXT_DATA__ JSON
        next_data = self._extract_next_data(html)
        if not next_data:
            return videos

        # 从 __NEXT_DATA__ 中搜索所有视频元数据
        video_metas = []
        self._find_video_metadata(next_data, video_metas)

        if not video_metas:
            return videos

        # 对每个视频元数据，调用 Media Selector API 获取实际 URL
        seen_vpids = set()
        for meta in video_metas[:5]:  # 最多处理 5 个视频
            vpid = meta.get("vpid", "")
            if not vpid or vpid in seen_vpids:
                continue
            seen_vpids.add(vpid)

            poster = meta.get("poster", "")
            video_url = self._resolve_video_url(vpid)

            if video_url:
                videos.append({
                    "url": video_url,
                    "type": "m3u8" if ".m3u8" in video_url else "mp4",
                    "poster": poster,
                })
            else:
                # 即使 API 调用失败，也记录 embed URL 作为后备
                embed_url = f"https://www.bbc.com/news/av-embeds/{vpid}"
                videos.append({
                    "url": embed_url,
                    "type": "iframe",
                    "poster": poster,
                })

        return videos

    def _extract_next_data(self, html: str) -> dict:
        """从 HTML 中提取 __NEXT_DATA__ JSON"""
        # 方法1: 正则匹配 <script id="__NEXT_DATA__"> 标签
        m = re.search(
            r'<script\s+id="__NEXT_DATA__"[^>]*>\s*({.*?})\s*</script>',
            html, re.DOTALL
        )
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # 方法2: 用 BeautifulSoup 解析（更健壮）
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            script = soup.find("script", id="__NEXT_DATA__")
            if script and script.string:
                return json.loads(script.string)
        except Exception:
            pass

        return {}

    def _find_video_metadata(self, obj, results: list, depth: int = 0):
        """
        递归搜索 __NEXT_DATA__ 中的视频元数据。
        BBC 视频数据的特征:
          - 包含 "versionId" 字段的 versions 数组
          - 位于 type="media" 的 block 中
          - 或在 videoMetadata 字段中
        """
        if depth > 20:  # 防止无限递归
            return

        if isinstance(obj, dict):
            # 检查是否是视频元数据块（包含 versions 数组且有 versionId）
            versions = obj.get("versions", [])
            if (isinstance(versions, list) and versions and
                    isinstance(versions[0], dict) and versions[0].get("versionId")):
                vpid = versions[0]["versionId"]
                # 提取海报图
                poster = ""
                image_url = obj.get("imageUrl", "")
                if image_url:
                    poster = self._fix_bbc_image_url(image_url)
                elif obj.get("image", {}).get("rawImage", {}).get("src"):
                    poster = obj["image"]["rawImage"]["src"]

                results.append({"vpid": vpid, "poster": poster})
                return  # 找到一个后不再深入该分支

            # 递归搜索所有值
            for key, val in obj.items():
                self._find_video_metadata(val, results, depth + 1)

        elif isinstance(obj, list):
            for item in obj:
                self._find_video_metadata(item, results, depth + 1)

    def _fix_bbc_image_url(self, image_url: str) -> str:
        """修复 BBC 图片 URL 中的占位符"""
        if not image_url:
            return ""
        # 替换 $widthxn 占位符
        image_url = image_url.replace("$widthxn", f"{_BBC_IMAGE_WIDTH}xn")
        image_url = image_url.replace("$width", _BBC_IMAGE_WIDTH)
        # 确保是完整 URL
        if image_url.startswith("//"):
            image_url = "https:" + image_url
        elif not image_url.startswith("http"):
            image_url = "https://" + image_url
        return image_url

    def _resolve_video_url(self, vpid: str) -> str:
        """
        通过 BBC Media Selector API 将 versionId 解析为实际视频流 URL。
        优先返回 HTTPS HLS (.m3u8) URL。
        """
        api_url = _BBC_MEDIA_SELECTOR_URL.format(vpid=vpid)
        try:
            resp = self._request(api_url, timeout=10)
            if resp is None:
                return ""
            data = resp.json()

            # 遍历 media 数组，找到视频类型的条目
            for media in data.get("media", []):
                if media.get("kind") != "video":
                    continue

                # 从 connection 中找最优的 URL
                best_url = ""
                best_priority = 999

                for conn in media.get("connection", []):
                    href = conn.get("href", "")
                    protocol = conn.get("protocol", "")
                    transfer = conn.get("transferFormat", "")
                    priority = int(conn.get("priority", 99))

                    # 优先 HTTPS HLS
                    if protocol == "https" and transfer == "hls" and href:
                        if priority < best_priority:
                            best_url = href
                            best_priority = priority

                if best_url:
                    return best_url

                # 如果没有 HLS，回退到 DASH 或任何可用的
                for conn in media.get("connection", []):
                    href = conn.get("href", "")
                    protocol = conn.get("protocol", "")
                    if protocol == "https" and href:
                        return href

        except Exception as e:
            logger.debug(f"[bbc] Media Selector API 调用失败: {vpid} | {e}")

        return ""

    @staticmethod
    def _embed_videos_in_html(videos: list, content_html: str) -> str:
        """
        将视频以 <video> 标签形式嵌入到 content_html 正文顶部。
        BBC 原文中视频在正文最上方，文字描述在下方。
        使用 data-hls-src 属性供前端 HLS.js 识别并初始化播放。
        """
        if not videos:
            return content_html

        video_tags = []
        for v in videos:
            vurl = v.get("url", "")
            vtype = v.get("type", "")
            poster = v.get("poster", "")

            if not vurl:
                continue

            if vtype == "iframe":
                tag = (
                    f'<div style="position:relative;padding-bottom:56.25%;height:0;overflow:hidden;'
                    f'margin-bottom:20px;border-radius:8px;background:#000;">'
                    f'<iframe src="{vurl}" style="position:absolute;top:0;left:0;width:100%;height:100%;border:none;" '
                    f'allowfullscreen loading="lazy"></iframe></div>'
                )
            elif vtype == "m3u8":
                poster_attr = f' poster="{poster}"' if poster else ''
                tag = (
                    f'<video controls preload="metadata" playsinline data-hls-src="{vurl}"'
                    f'{poster_attr}'
                    f' style="width:100%;max-width:100%;border-radius:8px;margin-bottom:20px;background:#000;">'
                    f'您的浏览器不支持视频播放</video>'
                )
            else:
                poster_attr = f' poster="{poster}"' if poster else ''
                tag = (
                    f'<video controls preload="metadata"{poster_attr}'
                    f' style="width:100%;max-width:100%;border-radius:8px;margin-bottom:20px;background:#000;">'
                    f'<source src="{vurl}" type="video/mp4">'
                    f'您的浏览器不支持视频播放</video>'
                )
            video_tags.append(tag)

        if not video_tags:
            return content_html

        video_html = "\n".join(video_tags)
        # 视频放在正文顶部
        return video_html + "\n" + content_html
