"""
新华网爬虫 - 爬取新华网首页头条 TOP 10

详情页特殊处理：
  新华网视频嵌入方式是自定义标签 <span class="pageVideo" video_src="...mp4">，
  标准的 <video>/<iframe> 提取器无法识别。
  重写 parse_detail() 在通用提取基础上补充扫描 video_src 属性。
"""
import re
import logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH

logger = logging.getLogger(__name__)


class XinhuaCrawler(BaseCrawler):

    detail_selectors = ["#detail", "#detailContent", ".article", ".main-article", ".content"]

    def __init__(self):
        super().__init__()
        self.name = "xinhua"
        self.display_name = "新华网"
        self.language = "zh"

    def crawl(self) -> list[dict]:
        results = []

        # 新华网首页头条
        resp = self._request("https://www.news.cn/")
        if resp is None:
            return results

        try:
            resp.encoding = "utf-8"
            soup = BeautifulSoup(resp.text, "lxml")

            rank = 1
            seen = set()
            for a in soup.find_all("a", href=True):
                href = str(a["href"]).strip()
                title = a.get_text(strip=True)

                if not title or len(title) < MIN_TITLE_LEN_ZH:
                    continue
                if not any(k in href for k in ["news.cn/20", "xinhuanet.com/20"]):
                    continue
                if not href.startswith("http"):
                    href = "https://www.news.cn" + href
                if href in seen:
                    continue
                seen.add(href)

                results.append(self._make_item(
                    title=title, url=href, rank=rank, category="头条"
                ))
                rank += 1
                if rank > 10:
                    break
        except Exception as e:
            self.logger.warning(f"[xinhua] HTML 解析失败: {e}")

        return results

    # ========== 详情页：补充新华网 video_src 视频提取 ==========

    def parse_detail(self, html: str, url: str) -> dict:
        """
        新华网专用详情页解析。
        在通用 content_extractor 基础上，额外扫描新华网特有的
        <span class="pageVideo" video_src="...mp4" poster="..."> 标签
        和页面中直接出现的 vodpub*.v.news.cn mp4 URL。
        """
        from utils.content_extractor import extract_content
        result = extract_content(html, url, selectors=self.detail_selectors)

        # 如果通用提取器已经找到视频，直接返回
        if result.get("videos"):
            return result

        # 补充：扫描新华网自定义视频标签
        videos = self._extract_xinhua_videos(html, url)
        if videos:
            result["videos"] = videos
            logger.info(f"[xinhua] 从 video_src 提取到 {len(videos)} 个视频: {url}")

        return result

    @staticmethod
    def _extract_xinhua_videos(html: str, url: str) -> list:
        """
        提取新华网特有的视频嵌入：
        1. <span class="pageVideo" video_src="...mp4" poster="...">
        2. 直接出现在 HTML/JS 中的 vodpub*.v.news.cn/*.mp4 URL
        """
        videos = []
        seen_urls = set()

        # 方法1：从 HTML 标签属性 video_src 提取
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all(attrs={"video_src": True}):
            video_url = str(tag.get("video_src", "")).strip()
            if video_url and video_url not in seen_urls:
                video_url = urljoin(url, video_url)
                poster = str(tag.get("poster", "")).strip()
                if poster:
                    poster = urljoin(url, poster)
                seen_urls.add(video_url)
                videos.append({
                    "url": video_url,
                    "type": "mp4",
                    "poster": poster,
                })

        # 方法2：正则扫描 vodpub*.v.news.cn 的 mp4 URL
        if not videos:
            mp4_urls = re.findall(
                r'https?://vodpub\d*\.v\.news\.cn/[^\s"\'<>]+\.mp4[^\s"\'<>]*',
                html
            )
            for mp4_url in mp4_urls:
                if mp4_url not in seen_urls:
                    seen_urls.add(mp4_url)
                    # 尝试从附近找 poster 图
                    poster = ""
                    poster_match = re.search(
                        r'poster="([^"]+)"[^>]*video_src="' + re.escape(mp4_url),
                        html
                    )
                    if poster_match:
                        poster = urljoin(url, poster_match.group(1))
                    videos.append({
                        "url": mp4_url,
                        "type": "mp4",
                        "poster": poster,
                    })

        return videos
