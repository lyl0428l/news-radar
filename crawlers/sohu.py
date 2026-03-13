"""
搜狐新闻爬虫 - 爬取搜狐热榜 TOP 10

详情页特殊处理：
  搜狐文章页的图片 <img data-src> 是 Base64 加密的垃圾值，不是真实 URL。
  真实图片 URL 存储在 JS 变量 `var cfgs = { imgsList: [...] }` 中。
  作者/来源名存储在 `<meta name="mediaid">` 标签中。
  重写 parse_detail() 从这些位置提取数据。
"""
import re
import logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH

logger = logging.getLogger(__name__)


class SohuCrawler(BaseCrawler):

    detail_selectors = [".article", "article.article", "#article-container", "#mp-editor"]

    def __init__(self):
        super().__init__()
        self.name = "sohu"
        self.display_name = "搜狐新闻"
        self.language = "zh"

    @staticmethod
    def _fix_sohu_url(url: str) -> str:
        """修正搜狐 URL：处理 protocol-relative / 缺协议 / 双前缀等情况"""
        if not url:
            return ""
        url = str(url).strip()
        # protocol-relative: //www.sohu.com/a/xxx → https://www.sohu.com/a/xxx
        if url.startswith("//"):
            url = "https:" + url
        # 纯路径: /a/xxx → https://www.sohu.com/a/xxx
        elif url.startswith("/") and not url.startswith("//"):
            url = "https://www.sohu.com" + url
        # 无协议的域名: www.sohu.com/a/xxx
        elif not url.startswith("http") and "sohu.com" in url:
            url = "https://" + url
        # 修复双前缀: https://www.sohu.com//www.sohu.com/a/xxx
        if "sohu.com//www.sohu.com" in url:
            url = "https://www.sohu.com" + url.split("sohu.com//www.sohu.com", 1)[1]
        if "sohu.com/www.sohu.com" in url:
            url = "https://www.sohu.com" + url.split("sohu.com/www.sohu.com", 1)[1]
        return url

    def crawl(self) -> list[dict]:
        results = []

        # 搜狐热榜 API
        api_url = "https://v2.sohu.com/integration-api/mix/region/hot"
        params = {"region": "cn", "size": 10}
        resp = self._request(api_url, params=params)
        if resp:
            try:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("data", [])
                for i, item in enumerate(items[:10], 1):
                    title = item.get("title", "").strip()
                    aid = item.get("id", item.get("articleId", ""))
                    url = item.get("url", item.get("mobileUrl", ""))
                    if not url and aid:
                        url = f"https://www.sohu.com/a/{aid}"
                    url = self._fix_sohu_url(url)
                    if title and url:
                        results.append(self._make_item(
                            title=title, url=url, rank=i,
                            category="热榜",
                        ))
                if results:
                    return results
            except Exception as e:
                self.logger.warning(f"[sohu] 热榜 API 失败: {e}")

        # 备选: 搜狐首页前 10 条
        resp = self._request("https://news.sohu.com/")
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
                if "/a/" not in href and "sohu.com/a/" not in href:
                    continue
                href = self._fix_sohu_url(href)
                if href in seen:
                    continue
                seen.add(href)
                results.append(self._make_item(
                    title=title, url=href, rank=rank, category="要闻"
                ))
                rank += 1
                if rank > 10:
                    break
        except Exception as e:
            self.logger.warning(f"[sohu] HTML 解析失败: {e}")

        return results

    # ========== 详情页：从 JS cfgs + meta 标签提取 ==========

    def parse_detail(self, html: str, url: str) -> dict:
        """
        搜狐专用详情页解析。
        1. 用通用 content_extractor 提取正文内容
        2. 覆盖作者：从 <meta name="mediaid"> 提取（来源账号名）
        3. 覆盖图片：从 JS var cfgs 中的 imgsList 提取真实图片 URL
        4. 用真实图片 URL 替换 content_html 中的加密 data-src
        """
        from utils.content_extractor import extract_content
        result = extract_content(html, url, selectors=self.detail_selectors)

        soup = BeautifulSoup(html, "lxml")

        # --- 提取作者：<meta name="mediaid"> ---
        meta_mediaid = soup.find("meta", attrs={"name": "mediaid"})
        if meta_mediaid:
            author = str(meta_mediaid.get("content", "")).strip()
            if author:
                result["author"] = author

        # --- 提取图片：从 var cfgs 的 imgsList 提取 ---
        real_images = self._extract_imgs_list(html, url)
        if real_images:
            result["images"] = real_images
            # 用第一张作为缩略图（如果还没有）
            if not result.get("thumbnail"):
                result["thumbnail"] = real_images[0]["url"]

        # 如果 imgsList 为空，尝试 og:image 作为缩略图
        if not result.get("thumbnail"):
            og_img = soup.find("meta", property="og:image")
            if og_img:
                og_url = str(og_img.get("content", "")).strip()
                if og_url:
                    if og_url.startswith("//"):
                        og_url = "https:" + og_url
                    result["thumbnail"] = og_url

        # --- 用真实图片替换 content_html 中的加密 img ---
        if real_images and result.get("content_html"):
            result["content_html"] = self._replace_encrypted_images(
                result["content_html"], real_images, url
            )

        return result

    @staticmethod
    def _extract_imgs_list(html: str, base_url: str) -> list:
        """
        从 JS `var cfgs = { imgsList: [{url: "..."}, ...] }` 中提取真实图片 URL。
        """
        images = []
        match = re.search(r'imgsList\s*:\s*\[([^\]]*)\]', html, re.DOTALL)
        if not match:
            return images

        imgs_raw = match.group(1)
        # 提取所有 "url": "..." 对
        img_urls = re.findall(r'"url"\s*:\s*"([^"]+)"', imgs_raw)
        for img_url in img_urls:
            img_url = img_url.strip()
            if img_url:
                if img_url.startswith("//"):
                    img_url = "https:" + img_url
                elif not img_url.startswith("http"):
                    img_url = urljoin(base_url, img_url)
                images.append({"url": img_url, "caption": ""})

        return images

    @staticmethod
    def _replace_encrypted_images(content_html: str, real_images: list, base_url: str) -> str:
        """
        替换 content_html 中加密的 <img data-src="加密值"> 为真实图片 URL。
        按出现顺序与 imgsList 一一对应。
        """
        soup = BeautifulSoup(content_html, "lxml")
        encrypted_imgs = []
        for img in soup.find_all("img"):
            src = str(img.get("src", ""))
            data_src = str(img.get("data-src", ""))
            # 加密的 data-src 不是以 http 或 // 开头
            if data_src and not data_src.startswith(("http", "//")):
                encrypted_imgs.append(img)
            elif not src and not data_src:
                encrypted_imgs.append(img)

        # 按顺序替换
        for i, img in enumerate(encrypted_imgs):
            if i < len(real_images):
                img["src"] = real_images[i]["url"]
                # 清除加密的 data-src
                if img.get("data-src"):
                    del img["data-src"]
            else:
                img.decompose()

        body = soup.find("body")
        if body:
            return "".join(str(child) for child in body.children)
        return str(soup)
