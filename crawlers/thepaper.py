"""
澎湃新闻爬虫 - 爬取澎湃热榜 TOP 10

内容提取:
  澎湃新闻使用 Next.js，文章数据在 <script id="__NEXT_DATA__"> JSON 中：
    contentDetail.content  — 正文 HTML（含真实图片 URL）
    contentDetail.imgUrlList — 图片列表
    contentDetail.videoDetailInfo — 视频信息
    contentDetail.name — 标题
    contentDetail.author — 作者
    contentDetail.pubTime — 发布时间
  
  旧选择器 .news_txt 等已失效（澎湃改版后 CSS 类名全变为哈希值）。
"""
import re
import json
import logging
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler

logger = logging.getLogger(__name__)


class ThePaperCrawler(BaseCrawler):

    detail_selectors = [
        ".cententWrap__UojXm",  # 新版（哈希类名，可能变）
        "[class*='cententWrap']",  # 模糊匹配
        ".news_txt",            # 旧版回退
        "article",
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

        # 澎湃热榜 API
        api_url = f"{self.base_url}/api/feed/hotspot/list"
        resp = self._request(api_url, params={"pageSize": 10, "pageNum": 1})
        if resp:
            try:
                data = resp.json()
                items = data.get("data", {}).get("list", data.get("data", []))
                if isinstance(items, list):
                    for i, item in enumerate(items[:10], 1):
                        title = item.get("title", item.get("name", "")).strip()
                        cid = item.get("contId", item.get("id", ""))
                        url = item.get("url", "")
                        if not url and cid:
                            url = f"{self.base_url}/newsDetail_forward_{cid}"
                        if title and url:
                            results.append(self._make_item(
                                title=title, url=str(url).strip(), rank=i,
                                summary=item.get("summary", ""),
                                category="热榜",
                            ))
                    if results:
                        return results
            except Exception as e:
                self.logger.warning(f"[thepaper] 热榜 API 失败: {e}")

        # 备选: 首页
        resp = self._request(self.base_url)
        if resp is None:
            return results
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        rank = 1
        seen = set()
        for a in soup.find_all("a", href=True):
            href = str(a["href"]).strip()
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

        return results

    # ================================================================
    #  详情页解析（从 __NEXT_DATA__ 提取）
    # ================================================================

    def parse_detail(self, html: str, url: str) -> dict:
        """
        优先从 __NEXT_DATA__ JSON 提取完整内容，
        回退到通用提取器。
        """
        # 方案 1: __NEXT_DATA__
        result = self._extract_from_next_data(html, url)
        if result and result.get("content") and len(result["content"]) > 50:
            return result

        # 方案 2: 通用提取器
        from utils.content_extractor import extract_content
        return extract_content(html, url, selectors=self.detail_selectors)

    def _extract_from_next_data(self, html: str, url: str) -> dict:
        """从 __NEXT_DATA__ 中提取文章数据"""
        try:
            soup = BeautifulSoup(html, "lxml")
            script = soup.find("script", id="__NEXT_DATA__")
            if not script or not script.string:
                return {}

            data = json.loads(script.string)
            detail = (data.get("props", {})
                      .get("pageProps", {})
                      .get("detailData", {})
                      .get("contentDetail", {}))

            if not detail:
                return {}

            # 标题
            title = detail.get("name", "")

            # 作者
            author = detail.get("author", "")

            # 发布时间
            pub_time = detail.get("pubTime", detail.get("publishTime", ""))

            # 正文 HTML（content 字段直接包含完整的 <p><img> 标签）
            content_html = detail.get("content", detail.get("contTxt", ""))

            # 从正文 HTML 提取纯文本
            content = ""
            if content_html:
                content_soup = BeautifulSoup(content_html, "lxml")
                content = content_soup.get_text(separator="\n")
                content = re.sub(r"\n\s*\n", "\n\n", content).strip()

            # 图片（从 imgUrlList + 正文中的 <img> 提取）
            images = []
            seen_urls = set()

            # 来源 1: imgUrlList
            for img_data in detail.get("imgUrlList", []):
                img_url = img_data.get("url", img_data.get("src", ""))
                desc = img_data.get("description", img_data.get("desc", ""))
                if img_url and img_url not in seen_urls:
                    seen_urls.add(img_url)
                    images.append({"url": img_url, "caption": desc})

            # 来源 2: 正文中的 <img> 标签
            if content_html:
                for img in BeautifulSoup(content_html, "lxml").find_all("img"):
                    src = img.get("src", img.get("data-src", ""))
                    if src and src not in seen_urls and not src.startswith("data:"):
                        seen_urls.add(src)
                        images.append({"url": src, "caption": img.get("alt", "")})

            # 缩略图
            thumbnail = detail.get("picUrl", "")
            if not thumbnail and images:
                thumbnail = images[0]["url"]

            # 视频
            videos = []
            video_info = detail.get("videoDetailInfo", {})
            if isinstance(video_info, dict) and video_info:
                vid_url = video_info.get("playUrl", video_info.get("url", ""))
                poster = video_info.get("coverUrl", "")
                if vid_url:
                    vtype = "m3u8" if ".m3u8" in vid_url else "mp4"
                    videos.append({
                        "url": vid_url, "type": vtype,
                        "poster": poster, "in_content": True,
                    })
                    # 将视频嵌入正文顶部
                    if vtype == "m3u8":
                        vid_tag = (
                            f'<video controls preload="metadata" playsinline '
                            f'data-hls-src="{vid_url}" '
                            f'poster="{poster}" '
                            f'style="width:100%;border-radius:8px;margin-bottom:16px;background:#000;">'
                            f'您的浏览器不支持视频播放</video>'
                        )
                    else:
                        vid_tag = (
                            f'<video controls preload="metadata" '
                            f'poster="{poster}" '
                            f'style="width:100%;border-radius:8px;margin-bottom:16px;background:#000;">'
                            f'<source src="{vid_url}" type="video/mp4">'
                            f'您的浏览器不支持视频播放</video>'
                        )
                    content_html = vid_tag + "\n" + content_html

            # 消毒 HTML
            try:
                from utils.content_extractor import sanitize_html
                content_html = sanitize_html(content_html)
            except ImportError:
                pass

            return {
                "title": title,
                "content": content,
                "content_html": content_html,
                "images": images[:20],
                "videos": videos,
                "thumbnail": thumbnail,
                "author": author,
                "pub_time": pub_time,
            }

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug(f"[thepaper] __NEXT_DATA__ 解析失败: {url[:50]} | {e}")
            return {}
