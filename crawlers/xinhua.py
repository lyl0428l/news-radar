"""
新华网爬虫 - 爬取新华网首页头条 TOP 10

正文获取策略（按优先级）：
  1. 新华网内容 API（直接返回 JSON，含完整正文段落列表）
     https://www.news.cn/api/detail?id={article_id}
  2. CSS 选择器提取正文容器（14 个选择器覆盖多版本页面）
  3. 移动端 m.xinhuanet.com 静态 HTML（PC 端 JS 渲染失败时兜底）
  4. readability 自动提取

视频特殊处理：
  新华网用 <span class="pageVideo" video_src="...mp4"> 嵌入视频，
  需要额外扫描此自定义标签。
"""
import re
import json
import logging
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH

logger = logging.getLogger(__name__)

# 新华网内容 API 列表（逐一尝试）
_XINHUA_DETAIL_APIS = [
    "https://www.news.cn/api/detail",
    "https://h5.xinhuaxmt.com/vh512/share/news",
    "https://www.news.cn/newsInterface/getDetailByArticleId",
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


def _extract_xinhua_article_id(url: str) -> str:
    """从新华网 URL 提取文章 ID"""
    if not url:
        return ""
    # https://www.news.cn/20240301/abc123/c_1234567890.htm → c_1234567890
    m = re.search(r'/(c_\d+)(?:\.htm)?', url)
    if m:
        return m.group(1)
    # https://www.news.cn/politics/2024-03/01/c_1234567890.htm
    m = re.search(r'/(c_\w+)(?:\.htm[l]?)?$', url)
    if m:
        return m.group(1)
    return ""


class XinhuaCrawler(BaseCrawler):

    detail_selectors = [
        "#detail", "#detailContent", "#detailMain",
        ".detail", ".article-content", ".article",
        ".main-article", ".main_content", ".content",
        "#article", ".newsContent", ".news-content",
        ".ht-content", ".content_area",
        "[class*='detail']", "[class*='article']",
    ]

    def __init__(self):
        super().__init__()
        self.name = "xinhua"
        self.display_name = "新华网"
        self.language = "zh"

    # ================================================================
    #  列表获取
    # ================================================================

    def crawl(self) -> list[dict]:
        results = []
        resp = self._request("https://www.news.cn/")
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

    # ================================================================
    #  详情页：多策略提取完整正文
    # ================================================================

    def fetch_detail(self, item: dict) -> dict:
        """
        新华网详情页抓取。
        策略1：新华网内容 API（直接 JSON）
        策略2：PC 端 HTML
        策略3：移动端 HTML（PC 正文不足时兜底）
        """
        if not isinstance(item, dict):
            return {}
        url = _safe_str(item.get("url"))
        if not url:
            return {}

        from config import DETAIL_FETCH_TIMEOUT

        # 策略1：新华网内容 API
        article_id = _extract_xinhua_article_id(url)
        if article_id:
            result = self._fetch_via_api(article_id, url, DETAIL_FETCH_TIMEOUT)
            if result and len(_safe_str(result.get("content"))) > 100:
                self.logger.info(f"[xinhua] API 提取成功: {url[:60]}")
                return result

        # 策略2：PC 端 HTML
        try:
            resp = self._request(url, timeout=DETAIL_FETCH_TIMEOUT)
            if resp is not None:
                result = self.parse_detail(resp.text, url)
                if len(_safe_str(result.get("content"))) >= 100:
                    return result

                # 策略3：移动端兜底
                # 新华网移动端不是简单替换域名，需要保持原路径
                # www.news.cn/20260326/xxx/c_yyy.htm → www.news.cn/20260326/xxx/c_yyy.htm
                # 移动端使用相同域名不同 UA，或 m.news.cn
                for mobile_url in self._build_mobile_urls(url):
                    if not mobile_url or mobile_url == url:
                        continue
                    try:
                        m_resp = self._request(
                            mobile_url, timeout=DETAIL_FETCH_TIMEOUT,
                            headers={"User-Agent": (
                                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                                "Version/17.0 Mobile/15E148 Safari/604.1"
                            )}
                        )
                        if m_resp is not None:
                            m_result = self.parse_detail(m_resp.text, mobile_url)
                            if (len(_safe_str(m_result.get("content")))
                                    > len(_safe_str(result.get("content")))):
                                self.logger.info(f"[xinhua] 移动端正文更完整: {url[:60]}")
                                return m_result
                    except Exception:
                        continue
                return result
        except Exception as e:
            self.logger.warning(f"[xinhua] 详情页抓取失败: {url[:60]} | {e}")

        return {}

    def _fetch_via_api(self, article_id: str, url: str, timeout: int) -> dict:
        """通过新华网内容 API 获取完整文章，逐一尝试多个接口和参数"""
        result = {}
        param_variants = [
            {"id": article_id},
            {"articleId": article_id},
            {"newsId": article_id},
            {"id": article_id, "type": "text"},
        ]
        for api_url in _XINHUA_DETAIL_APIS:
            for params in param_variants:
                try:
                    resp = self._request(api_url, params=params,
                                         timeout=timeout, skip_cffi=True)
                    if resp is None:
                        continue
                    if resp.status_code in (404, 400):
                        break
                    data = resp.json()
                    if not isinstance(data, dict):
                        continue
                    content_data = (
                        _safe_dict(data.get("content"))
                        or _safe_dict(data.get("data"))
                        or _safe_dict(data.get("newsInfo"))
                        or data
                    )
                    result = self._parse_api_content(content_data, url)
                    if len(_safe_str(result.get("content"))) > 100:
                        self.logger.info(f"[xinhua] API成功: {api_url.split('/')[-1]}")
                        return result
                except Exception as e:
                    self.logger.debug(f"[xinhua] API 失败: {api_url} | {e}")
                    break
        return result

    def _parse_api_content(self, data: dict, url: str) -> dict:
        """解析新华网 API 返回的文章数据"""
        result = {
            "content_html": "", "content": "", "images": [],
            "videos": [], "thumbnail": "", "author": "", "pub_time": "",
        }
        if not isinstance(data, dict):
            return result

        # 新华网 API 有时把正文放在 content 字段（HTML字符串或段落列表）
        content_raw = data.get("content") or data.get("body") or data.get("text") or ""
        if isinstance(content_raw, list):
            # 段落列表 → 拼接 HTML
            paragraphs = []
            for p in content_raw:
                if isinstance(p, dict):
                    ptype = _safe_str(p.get("type", "text"))
                    if ptype == "text":
                        text = _safe_str(p.get("content") or p.get("text"))
                        if text:
                            paragraphs.append(f"<p>{text}</p>")
                    elif ptype == "image":
                        img_url = _safe_str(p.get("url") or p.get("src"))
                        if img_url:
                            caption = _safe_str(p.get("desc") or p.get("caption"))
                            paragraphs.append(
                                f'<figure><img src="{img_url}" alt="{caption}"/>'
                                f'{"<figcaption>" + caption + "</figcaption>" if caption else ""}'
                                f'</figure>'
                            )
                            result["images"].append({
                                "url": img_url, "caption": caption, "in_content": True
                            })
                elif isinstance(p, str) and p.strip():
                    paragraphs.append(f"<p>{p}</p>")
            content_html = "\n".join(paragraphs)
        elif isinstance(content_raw, str):
            content_html = content_raw
        else:
            content_html = ""

        if content_html:
            try:
                from utils.content_extractor import sanitize_html
                content_html = sanitize_html(content_html)
            except Exception:
                pass
            result["content_html"] = content_html
            try:
                csoup = BeautifulSoup(content_html, "lxml")
                result["content"] = re.sub(
                    r"\n\s*\n", "\n\n", csoup.get_text(separator="\n")
                ).strip()
                # 提取图片（去重）
                seen = set()
                for img in csoup.find_all("img"):
                    src = _safe_str(img.get("src") or img.get("data-src"))
                    if src and src.startswith("http") and src not in seen:
                        seen.add(src)
                        if not any(i["url"] == src for i in result["images"]):
                            result["images"].append({
                                "url": src,
                                "caption": _safe_str(img.get("alt")),
                                "in_content": True,
                            })
            except Exception:
                pass

        # 作者
        result["author"] = _safe_str(
            data.get("author") or data.get("source") or data.get("editor")
        )
        # 发布时间
        result["pub_time"] = _safe_str(
            data.get("pubTime") or data.get("pubdate") or data.get("publishTime")
        )
        # 封面图
        thumbnail = data.get("thumbnail") or data.get("picUrl") or data.get("coverImg") or ""
        if isinstance(thumbnail, list) and thumbnail:
            thumbnail = thumbnail[0]
        if isinstance(thumbnail, dict):
            thumbnail = thumbnail.get("url", "")
        result["thumbnail"] = _safe_str(thumbnail)
        if not result["thumbnail"] and result["images"]:
            result["thumbnail"] = result["images"][0]["url"]

        return result

    def parse_detail(self, html: str, url: str) -> dict:
        """
        新华网专用详情页解析。
        通用提取器 + 新华网特有视频标签扫描。
        """
        if not html or not isinstance(html, str):
            from utils.content_extractor import extract_content
            return extract_content("", url, selectors=self.detail_selectors)

        from utils.content_extractor import extract_content
        result = extract_content(html, url, selectors=self.detail_selectors)

        # 补充：扫描新华网自定义视频标签（不重复添加）
        if not result.get("videos"):
            videos = self._extract_xinhua_videos(html, url)
            if videos:
                result["videos"] = videos
                if not result.get("thumbnail"):
                    for v in videos:
                        if v.get("poster"):
                            result["thumbnail"] = v["poster"]
                            break

        return result

    @staticmethod
    def _build_mobile_urls(url: str) -> list:
        """
        构建新华网移动端备用 URL 列表。
        新华网移动端有多种形式，逐一尝试。
        """
        mobile_urls = []
        if not url:
            return mobile_urls
        # 方式1: 替换域名为 m.news.cn（新华社移动端）
        m1 = url.replace("www.news.cn", "m.news.cn")
        if m1 != url:
            mobile_urls.append(m1)
        # 方式2: xinhuanet.com → m.xinhuanet.com
        m2 = url.replace("www.xinhuanet.com", "m.xinhuanet.com")
        if m2 != url:
            mobile_urls.append(m2)
        # 方式3: 保持原 URL 但用移动端 UA 请求（在 fetch_detail 中处理）
        return mobile_urls

    @staticmethod
    def _extract_xinhua_videos(html: str, url: str) -> list:
        """
        提取新华网特有的视频嵌入：
        1. <span class="pageVideo" video_src="...mp4" poster="...">
        2. 页面 JS 中的 vodpub*.v.news.cn mp4 URL
        """
        videos = []
        seen_urls = set()

        try:
            soup = BeautifulSoup(html, "lxml")
            for tag in soup.find_all(attrs={"video_src": True}):
                video_url = _safe_str(tag.get("video_src"))
                if not video_url or video_url in seen_urls:
                    continue
                video_url = urljoin(url, video_url)
                poster = _safe_str(tag.get("poster"))
                if poster:
                    poster = urljoin(url, poster)
                seen_urls.add(video_url)
                videos.append({"url": video_url, "type": "mp4", "poster": poster})
        except Exception:
            pass

        if not videos:
            try:
                mp4_urls = re.findall(
                    r'https?://vodpub\d*\.v\.news\.cn/[^\s"\'<>]+\.mp4[^\s"\'<>]*',
                    html
                )
                for mp4_url in mp4_urls:
                    if mp4_url not in seen_urls:
                        seen_urls.add(mp4_url)
                        videos.append({"url": mp4_url, "type": "mp4", "poster": ""})
            except Exception:
                pass

        return videos
