"""
凤凰新闻爬虫 - 爬取凤凰热榜 TOP 10
多 API + HTML 备选策略

详情页特殊处理：
  凤凰网文章页的正文/图片/作者数据存储在 JS 变量 `var allData = {...}` 中，
  HTML 中的 <img> 标签 src 全是 base64 占位符，author/source 字段为空。
  因此重写 parse_detail() 从 allData 的 docData 中提取：
    - 图片: docData.imagesInContent[].url
    - 作者: docData.fhhAccountDetail.catename（发布账号名）
    - 正文: docData.contentData.contentList[]（HTML 段落列表）
    - 时间: docData.newsTime
"""
import re
import json
import logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH

logger = logging.getLogger(__name__)


class IfengCrawler(BaseCrawler):

    detail_selectors = [".text_img", ".main_content-body", "#articleContent", ".yc_con_txt"]

    def __init__(self):
        super().__init__()
        self.name = "ifeng"
        self.display_name = "凤凰新闻"
        self.language = "zh"

    def crawl(self) -> list[dict]:
        results = []

        # 方案 1: 凤凰热榜 API (新接口)
        results = self._try_hot_api()
        if len(results) >= 5:
            return results

        # 方案 2: 凤凰新闻客户端 API
        results = self._try_client_api()
        if len(results) >= 5:
            return results

        # 方案 3: 首页 HTML 解析（加宽匹配条件）
        results = self._try_html()
        return results

    def _try_hot_api(self) -> list:
        """凤凰热榜 API"""
        results = []
        api_urls = [
            "https://shankapi.ifeng.com/season/getHotListData/all/1/10",
            "https://api.3g.ifeng.com/api_phoenixtv_allData?type=1&page=1&pageSize=10",
        ]
        for api_url in api_urls:
            resp = self._request(api_url)
            if resp is None:
                continue
            try:
                data = resp.json()
                # 适配不同接口结构
                items = (
                    data.get("data", {}).get("allData", [])
                    or data.get("data", {}).get("list", [])
                    or data.get("data", {}).get("newslist", [])
                    or data.get("data", [])
                )
                if isinstance(items, list):
                    for i, item in enumerate(items[:10], 1):
                        title = item.get("title", "").strip()
                        url = item.get("url", item.get("link", "")).strip()
                        if title and url:
                            if not url.startswith("http"):
                                url = "https:" + url if url.startswith("//") else "https://www.ifeng.com" + url
                            results.append(self._make_item(
                                title=title, url=url, rank=i,
                                category="热榜",
                                summary=item.get("description", item.get("summary", "")),
                                pub_time=self.parse_time(item.get("ctime", item.get("updateTime", ""))),
                            ))
                    if len(results) >= 5:
                        return results
            except Exception as e:
                self.logger.warning(f"[ifeng] API 失败: {api_url} | {e}")

        return results

    def _try_client_api(self) -> list:
        """凤凰新闻频道列表 API"""
        results = []
        resp = self._request(
            "https://nine.ifeng.com/iosf/listData?type=1&id=SYLB10,SYDT10&action=default&pullNum=1",
            headers={"Referer": "https://news.ifeng.com/"}
        )
        if resp is None:
            return results
        try:
            data = resp.json()
            items = data.get("data", [])
            if isinstance(items, list):
                for i, item in enumerate(items[:10], 1):
                    title = item.get("title", "").strip()
                    url = item.get("url", item.get("link", "")).strip()
                    if title and url:
                        if not url.startswith("http"):
                            url = "https:" + url if url.startswith("//") else "https://www.ifeng.com" + url
                        results.append(self._make_item(
                            title=title, url=url, rank=i, category="要闻",
                        ))
        except Exception as e:
            self.logger.warning(f"[ifeng] 客户端 API 失败: {e}")
        return results

    def _try_html(self) -> list[dict]:
        """首页 HTML 解析"""
        results = []
        resp = self._request("https://news.ifeng.com/")
        if resp is None:
            # 再试主站
            resp = self._request("https://www.ifeng.com/")
        if resp is None:
            return results

        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        rank = 1
        seen = set()

        for a in soup.find_all("a", href=True):
            href = str(a["href"]).strip()
            title = a.get_text(strip=True)
            if not title or len(title) < MIN_TITLE_LEN_ZH:
                continue
            # 接受 ifeng.com 域名下的链接
            if "ifeng.com" not in href:
                continue
            if not href.startswith("http"):
                href = "https:" + href if href.startswith("//") else "https://news.ifeng.com" + href
            if href in seen:
                continue
            # 过滤导航/频道链接（路径太短说明不是文章）
            if href.count("/") < 4:
                continue
            seen.add(href)
            results.append(self._make_item(
                title=title, url=href, rank=rank, category="要闻"
            ))
            rank += 1
            if rank > 10:
                break

        return results

    # ========== 详情页：从 JS allData 提取 ==========

    def parse_detail(self, html: str, url: str) -> dict:
        """
        凤凰网专用详情页解析。
        优先从 JS 变量 `var allData = {...}` 中提取结构化数据，
        回退到通用 content_extractor。
        """
        result = self._parse_from_alldata(html, url)
        if result and result.get("content") and len(result["content"]) > 50:
            return result

        # 回退到通用提取器
        from utils.content_extractor import extract_content
        return extract_content(html, url, selectors=self.detail_selectors)

    @staticmethod
    def _parse_from_alldata(html: str, url: str) -> dict:
        """从页面 JS 中的 allData 变量提取文章数据"""
        result = {
            "content_html": "",
            "content": "",
            "images": [],
            "videos": [],
            "thumbnail": "",
            "author": "",
            "pub_time": "",
        }

        # 1. 提取 allData JSON
        m = re.search(r'var\s+allData\s*=\s*(\{.+?\});\s*\n', html, re.DOTALL)
        if not m:
            return result

        try:
            all_data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            return result

        doc = all_data.get("docData")
        if not isinstance(doc, dict):
            return result

        # 2. 提取作者/来源
        #    优先级: fhhAccountDetail.catename > source > author
        author = ""
        fhh = doc.get("fhhAccountDetail")
        if isinstance(fhh, dict):
            author = (fhh.get("catename") or "").strip()
        if not author:
            author = (doc.get("source") or "").strip()
        if not author:
            author = (doc.get("author") or "").strip()
        result["author"] = author

        # 3. 提取发布时间
        result["pub_time"] = (doc.get("newsTime") or doc.get("createTime") or "").strip()

        # 4. 提取图片 — 从 imagesInContent（真实 URL）
        images_data = doc.get("imagesInContent", [])
        if isinstance(images_data, list):
            for img_info in images_data:
                if isinstance(img_info, dict):
                    img_url = (img_info.get("url") or "").strip()
                    if img_url and img_url.startswith("http"):
                        result["images"].append({
                            "url": img_url,
                            "caption": "",
                        })

        # 5. 提取封面图
        bd_img = (doc.get("bdImg") or "").strip()
        if bd_img:
            if bd_img.startswith("//"):
                bd_img = "https:" + bd_img
            result["thumbnail"] = bd_img
        elif result["images"]:
            result["thumbnail"] = result["images"][0]["url"]

        # 6. 提取正文 — 从 contentData.contentList
        content_data = doc.get("contentData")
        if isinstance(content_data, dict):
            content_list = content_data.get("contentList", [])
        elif isinstance(content_data, list):
            content_list = content_data
        else:
            content_list = []

        html_parts = []
        for item in content_list:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "")
            if item_type == "text":
                raw_html = item.get("data", "")
                if raw_html:
                    # 修复 HTML 中 base64 占位符 img → 替换为真实 URL
                    raw_html = IfengCrawler._fix_placeholder_images(
                        raw_html, result["images"], url
                    )
                    html_parts.append(raw_html)
            elif item_type == "img":
                img_url = (item.get("url") or "").strip()
                if img_url:
                    img_url = urljoin(url, img_url)
                    caption = (item.get("alt") or item.get("caption") or "").strip()
                    html_parts.append(
                        f'<figure><img src="{img_url}" alt="{caption}"/>'
                        f'{"<figcaption>" + caption + "</figcaption>" if caption else ""}'
                        f'</figure>'
                    )
                    # 也加入 images 列表（去重）
                    if not any(i["url"] == img_url for i in result["images"]):
                        result["images"].append({"url": img_url, "caption": caption})
            elif item_type == "video":
                vid_data = item.get("data", {})
                if isinstance(vid_data, dict):
                    vid_url = (vid_data.get("playUrl") or vid_data.get("url") or "").strip()
                    poster = (vid_data.get("bigPosterUrl") or vid_data.get("poster") or "").strip()
                else:
                    vid_url = (item.get("url") or item.get("videoUrl") or "").strip()
                    poster = (item.get("poster") or item.get("thumbnail") or "").strip()
                if vid_url:
                    if vid_url.startswith("//"):
                        vid_url = "https:" + vid_url
                    if poster and poster.startswith("//"):
                        poster = "https:" + poster
                    result["videos"].append({
                        "url": vid_url,
                        "type": "mp4",
                        "poster": poster,
                    })
                    # 同时在正文中插入视频标签
                    html_parts.append(
                        f'<video controls preload="metadata"'
                        f'{" poster=" + chr(34) + poster + chr(34) if poster else ""}>'
                        f'<source src="{vid_url}" type="video/mp4">'
                        f'</video>'
                    )

        # 7. 补充：从 videosPluginData 提取文章内嵌视频
        vpd = doc.get("videosPluginData", [])
        if isinstance(vpd, list):
            existing_video_urls = {v["url"] for v in result["videos"]}
            for vp in vpd:
                if not isinstance(vp, dict):
                    continue
                play_url = (vp.get("playUrl") or "").strip()
                if play_url and play_url not in existing_video_urls:
                    poster = (vp.get("bigPosterUrl") or "").strip()
                    if play_url.startswith("//"):
                        play_url = "https:" + play_url
                    if poster and poster.startswith("//"):
                        poster = "https:" + poster
                    result["videos"].append({
                        "url": play_url,
                        "type": "mp4",
                        "poster": poster,
                    })
                    existing_video_urls.add(play_url)
                    # 视频也加入正文 HTML
                    html_parts.append(
                        f'<video controls preload="metadata"'
                        f'{" poster=" + chr(34) + poster + chr(34) if poster else ""}>'
                        f'<source src="{play_url}" type="video/mp4">'
                        f'</video>'
                    )

        content_html = "\n".join(html_parts)
        result["content_html"] = content_html

        # 纯文本
        if content_html:
            soup = BeautifulSoup(content_html, "lxml")
            text = soup.get_text(separator="\n")
            text = re.sub(r"\n\s*\n", "\n\n", text).strip()
            result["content"] = text

        return result

    @staticmethod
    def _fix_placeholder_images(html_fragment: str, images: list, base_url: str) -> str:
        """
        将 HTML 片段中 base64 占位符 <img> 替换为 imagesInContent 中的真实 URL。
        凤凰网的 contentList HTML 中 img src 全是 data:image/png;base64,... 占位符，
        按出现顺序与 imagesInContent 一一对应。
        """
        if not images:
            return html_fragment

        soup = BeautifulSoup(html_fragment, "lxml")
        placeholder_imgs = []
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if src.startswith("data:") or not src:
                placeholder_imgs.append(img)

        # 按顺序替换
        for i, img in enumerate(placeholder_imgs):
            if i < len(images):
                img["src"] = images[i]["url"]
            else:
                # 没有更多真实 URL 了，移除占位图
                img.decompose()

        # 返回修复后的 HTML（去掉 BeautifulSoup 包裹的 <html><body>）
        body = soup.find("body")
        if body:
            return "".join(str(child) for child in body.children)
        return str(soup)
