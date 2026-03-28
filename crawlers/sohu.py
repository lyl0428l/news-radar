"""
搜狐新闻爬虫 - 爬取搜狐热榜 TOP 10

正文获取策略（按优先级）：
  1. JS 变量 ssp_var / articleConfig / window.__INITIAL_STATE__ 直接含正文
  2. <article> / .article 等 CSS 选择器提取正文容器
  3. 移动端 m.sohu.com 静态 HTML（PC 端正文容器依赖 JS 渲染时的兜底）
  4. readability 自动提取

图片特殊处理：
  搜狐文章页的 <img data-src> 是 Base64 加密的垃圾值，不是真实 URL。
  真实图片 URL 存储在 JS 变量 cfgs.imgsList 中。
  作者/来源名存储在 <meta name="mediaid"> 或 JS 变量中。
"""
import re
import json
import logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from crawlers.base import BaseCrawler, MIN_TITLE_LEN_ZH

logger = logging.getLogger(__name__)


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


class SohuCrawler(BaseCrawler):

    detail_selectors = [
        "article#mp-editor",      # 当前版本最精确（<article class="article" id="mp-editor">）
        "article.article",        # 带类名的文章标签
        "#mp-editor",             # 自媒体文章编辑器容器
        ".left.main .text article",  # 带父容器路径
        ".article",               # 文章通用
        "#article-container .article",  # 文章容器内的文章
        "#article-container",     # 文章容器
        ".article-content",       # 文章内容
        "#articleContent",        # 备用
        ".news-content",          # 新闻内容
        ".article_content",       # 下划线版本
        ".text",                  # 简版（注意：搜狐 .text 是外层容器，含标题）
        ".post-content",          # 自媒体文章
        ".main-content",          # 主要内容
        "[data-role='article']",  # data属性选择
        "[class*='article']",     # 模糊匹配
        "[class*='content']",     # 模糊匹配
    ]

    def __init__(self):
        super().__init__()
        self.name = "sohu"
        self.display_name = "搜狐新闻"
        self.language = "zh"

    @staticmethod
    def _fix_sohu_url(url: str) -> str:
        """修正搜狐 URL，处理各种格式异常"""
        if not url:
            return ""
        url = _safe_str(url)
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/") and not url.startswith("//"):
            url = "https://www.sohu.com" + url
        elif not url.startswith("http") and "sohu.com" in url:
            url = "https://" + url
        if "sohu.com//www.sohu.com" in url:
            url = "https://www.sohu.com" + url.split("sohu.com//www.sohu.com", 1)[1]
        if "sohu.com/www.sohu.com" in url:
            url = "https://www.sohu.com" + url.split("sohu.com/www.sohu.com", 1)[1]
        return url

    # ================================================================
    #  列表获取
    # ================================================================

    def crawl(self) -> list[dict]:
        results = []

        # API方案1：热榜 API（可能 503）
        resp = self._request(
            "https://v2.sohu.com/integration-api/mix/region/hot",
            params={"region": "cn", "size": 10}
        )
        if resp:
            try:
                data = resp.json()
                items = _safe_list(data if isinstance(data, list) else data.get("data"))
                for i, item in enumerate(items[:10], 1):
                    if not isinstance(item, dict):
                        continue
                    title = _safe_str(item.get("title"))
                    aid = _safe_str(item.get("id") or item.get("articleId"))
                    url = _safe_str(item.get("url") or item.get("mobileUrl"))
                    if not url and aid:
                        url = f"https://www.sohu.com/a/{aid}"
                    url = self._fix_sohu_url(url)
                    if title and url:
                        results.append(self._make_item(
                            title=title, url=url, rank=i, category="热榜",
                        ))
                if results:
                    return results
            except Exception as e:
                self.logger.warning(f"[sohu] 热榜 API 失败: {e}")

        # API方案2：搜狐热点新闻流 API（热榜API 503时的备选）
        if not results:
            try:
                resp2 = self._request(
                    "https://v2.sohu.com/integration-api/mix/region/newsList",
                    params={"secId": "focus", "size": 15}
                )
                if resp2:
                    data2 = resp2.json()
                    items2 = _safe_list(
                        data2 if isinstance(data2, list) else data2.get("data")
                    )
                    rank = 1
                    for item in items2:
                        if not isinstance(item, dict):
                            continue
                        title = _safe_str(item.get("title"))
                        aid = _safe_str(item.get("id") or item.get("articleId"))
                        url = _safe_str(item.get("url") or item.get("mobileUrl"))
                        if not url and aid:
                            url = f"https://www.sohu.com/a/{aid}"
                        url = self._fix_sohu_url(url)
                        if title and url:
                            results.append(self._make_item(
                                title=title, url=url, rank=rank, category="要闻",
                            ))
                            rank += 1
                            if rank > 10:
                                break
                    if results:
                        return results
            except Exception as e:
                self.logger.debug(f"[sohu] 新闻流 API 失败: {e}")

        # 备选：搜狐首页 HTML
        resp = self._request("https://news.sohu.com/")
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
                if "/a/" not in href and "sohu.com/a/" not in href:
                    continue
                href = self._fix_sohu_url(href)
                if not href or href in seen:
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

    # ================================================================
    #  详情页：多策略提取完整正文
    # ================================================================

    def fetch_detail(self, item: dict) -> dict:
        """
        搜狐详情页抓取（纯静态HTTP）：
        1. 尝试搜狐内容API（直接获取JSON格式正文）
        2. PC端静态请求 + JS变量提取
        3. 移动端 m.sohu.com（返回更简洁的HTML，正文更易提取）
        """
        if not isinstance(item, dict):
            return {}
        url = _safe_str(item.get("url"))
        if not url:
            return {}

        from config import DETAIL_FETCH_TIMEOUT

        # 策略0：尝试搜狐内容API
        article_id = self._extract_article_id(url)
        if article_id:
            api_result = self._fetch_via_api(article_id, url, DETAIL_FETCH_TIMEOUT)
            if api_result and len(_safe_str(api_result.get("content"))) >= 100:
                self.logger.info(f"[sohu] API提取成功: {url[:60]}")
                return api_result

        # 策略1：PC端请求
        result = {}
        try:
            headers = {
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/131.0.0.0 Safari/537.36"),
                "Referer": "https://www.sohu.com/",
            }
            resp = self._request(url, timeout=DETAIL_FETCH_TIMEOUT, headers=headers)
            if resp is not None:
                resp.encoding = "utf-8"
                result = self.parse_detail(resp.text, url)
                content = _safe_str(result.get("content"))
                if len(content) >= 100:
                    return result
        except Exception as e:
            self.logger.debug(f"[sohu] PC端请求失败: {url[:60]} | {e}")

        # 策略2：移动端请求（搜狐移动端返回更完整的静态HTML）
        try:
            mobile_url = url.replace("www.sohu.com", "m.sohu.com")
            if mobile_url != url:
                m_headers = {
                    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                                   "AppleWebKit/605.1.15 Safari/604.1"),
                    "Referer": "https://m.sohu.com/",
                }
                m_resp = self._request(mobile_url, timeout=DETAIL_FETCH_TIMEOUT,
                                       headers=m_headers)
                if m_resp is not None:
                    m_resp.encoding = "utf-8"
                    m_result = self.parse_detail(m_resp.text, mobile_url)
                    if len(_safe_str(m_result.get("content"))) > len(_safe_str(result.get("content"))):
                        self.logger.info(f"[sohu] 移动端正文更完整: {url[:60]}")
                        result = m_result
        except Exception as e:
            self.logger.debug(f"[sohu] 移动端请求失败: {url[:60]} | {e}")

        # Playwright 渲染由 main.py 统一批量处理
        return result

    @staticmethod
    def _extract_article_id(url: str) -> str:
        """从搜狐 URL 提取文章 ID"""
        if not url:
            return ""
        m = re.search(r'/a/(\d+)', url)
        if m:
            return m.group(1)
        return ""

    def _fetch_via_api(self, article_id: str, url: str, timeout: int) -> dict:
        """通过搜狐内容API获取文章正文"""
        result = {
            "content_html": "", "content": "", "images": [],
            "videos": [], "thumbnail": "", "author": "", "pub_time": "",
        }
        api_urls = [
            f"https://v2.sohu.com/article-detail-api/article/{article_id}",
            f"https://api.sohu.com/api/v3/article/{article_id}",
        ]
        session = self._get_session()
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"),
            "Referer": "https://www.sohu.com/",
        }
        for api_url in api_urls:
            try:
                resp = session.get(api_url, headers=headers, timeout=timeout)
                if resp.status_code in (404, 403, 503):
                    continue
                if resp.status_code != 200:
                    continue
                data = resp.json()
                if not isinstance(data, dict):
                    continue
                # 搜索正文字段
                content_html = ""
                for key in ("content", "articleContent", "body", "text", "htmlContent"):
                    val = data.get(key)
                    if isinstance(val, str) and len(val) > 200 and "<p" in val.lower():
                        content_html = val
                        break
                if not content_html:
                    # 深搜 data 子结构
                    inner = data.get("data", {})
                    if isinstance(inner, dict):
                        for key in ("content", "articleContent", "body"):
                            val = inner.get(key)
                            if isinstance(val, str) and len(val) > 200:
                                content_html = val
                                break
                if content_html:
                    from utils.content_extractor import sanitize_html
                    result["content_html"] = sanitize_html(content_html)
                    try:
                        soup = BeautifulSoup(content_html, "lxml")
                        result["content"] = re.sub(
                            r"\n\s*\n", "\n\n", soup.get_text(separator="\n")
                        ).strip()
                    except Exception:
                        pass
                    result["author"] = _safe_str(
                        data.get("author") or data.get("authorName") or
                        data.get("source") or data.get("mediaNick", "")
                    )
                    result["pub_time"] = _safe_str(
                        data.get("pubTime") or data.get("publishTime") or
                        data.get("createTime", "")
                    )
                    if result["content"]:
                        return result
            except Exception as e:
                self.logger.debug(f"[sohu] API {api_url} 失败: {e}")
                continue
        return result

    def parse_detail(self, html: str, url: str) -> dict:
        """
        搜狐专用详情页解析。
        先尝试从 JS 变量提取正文，再用 CSS 选择器 + readability 兜底。
        重要发现：搜狐文章页的静态 HTML 中正文是完整存在的，
        关键是 article#mp-editor 选择器 + 预清理噪音元素。
        """
        if not html or not isinstance(html, str):
            from utils.content_extractor import extract_content
            return extract_content("", url, selectors=self.detail_selectors)

        # 预处理：移除搜狐特有的噪音元素
        try:
            soup_pre = BeautifulSoup(html, "lxml")
            # 移除 "返回搜狐，查看更多" 链接及其父 <p>
            back_link = soup_pre.find("a", id="backsohucom")
            if back_link:
                parent_p = back_link.find_parent("p")
                if parent_p:
                    parent_p.decompose()
                else:
                    back_link.decompose()
            # 移除 "责任编辑" 声明
            for el in soup_pre.find_all("span", class_="backword"):
                parent = el.find_parent("p") or el.find_parent("div")
                if parent:
                    parent.decompose()
                else:
                    el.decompose()
            # 移除平台声明
            for el in soup_pre.select(".statement, .article-source-wrap"):
                el.decompose()
            html = str(soup_pre)
        except Exception:
            pass

        # 策略1：从 JS 变量直接提取完整正文
        js_result = self._extract_from_js(html, url)
        if js_result and len(_safe_str(js_result.get("content"))) > 100:
            # 补充作者和图片
            self._enrich_result(js_result, html, url)
            return js_result

        # 策略2：CSS 选择器 + readability（搜狐静态 HTML 已含完整正文）
        from utils.content_extractor import extract_content
        result = extract_content(html, url, selectors=self.detail_selectors)

        # 补充作者、图片（搜狐特有来源）
        self._enrich_result(result, html, url)

        return result

    def _extract_from_js(self, html: str, url: str) -> dict:
        """
        从搜狐页面 JS 变量中提取正文。
        搜狐文章正文数据可能存在以下位置：
          1. var ssp_var = {...}  中的 content/article 字段
          2. window.articleConfig = {...}
          3. window.__INITIAL_STATE__ 或 window.data
          4. var articleData = {...}
        """
        result = {
            "content_html": "", "content": "", "images": [],
            "videos": [], "thumbnail": "", "author": "", "pub_time": "",
        }

        # 搜集所有候选 JS 对象字符串
        js_patterns = [
            r'var\s+ssp_var\s*=\s*(\{.+?\})\s*;',
            r'window\.articleConfig\s*=\s*(\{.+?\})\s*;',
            r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*[;\n]',
            r'var\s+articleData\s*=\s*(\{.+?\})\s*;',
            r'window\.data\s*=\s*(\{.+?\})\s*;',
            r'var\s+cfg\s*=\s*(\{.+?\})\s*;',
        ]

        for pat in js_patterns:
            for m in re.finditer(pat, html, re.DOTALL):
                try:
                    data = json.loads(m.group(1))
                    if not isinstance(data, dict):
                        continue
                    # 深度搜索 content/article/body 字段
                    content_html = self._find_content_in_dict(data)
                    if content_html and len(content_html) > 200:
                        try:
                            csoup = BeautifulSoup(content_html, "lxml")
                            content_text = re.sub(
                                r"\n\s*\n", "\n\n",
                                csoup.get_text(separator="\n")
                            ).strip()
                            if len(content_text) > 100:
                                result["content_html"] = content_html
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
                                # 提取作者
                                result["author"] = _safe_str(
                                    self._find_field(data, ["author", "authorName",
                                                            "mediaNick", "mediaName",
                                                            "source", "from"])
                                )
                                result["pub_time"] = _safe_str(
                                    self._find_field(data, ["pubTime", "publishTime",
                                                            "createTime", "releaseTime"])
                                )
                                return result
                        except Exception:
                            continue
                except (json.JSONDecodeError, ValueError):
                    continue

        return result

    @staticmethod
    def _find_content_in_dict(data: dict, depth: int = 0) -> str:
        """递归搜索字典中的 content/article/body 字段"""
        if depth > 4:
            return ""
        for key in ("content", "articleContent", "body", "articleBody",
                    "htmlContent", "text", "article"):
            val = data.get(key)
            if isinstance(val, str) and len(val) > 200 and "<p" in val:
                return val
        # 递归子字典
        for key, val in data.items():
            if isinstance(val, dict):
                found = SohuCrawler._find_content_in_dict(val, depth + 1)
                if found:
                    return found
        return ""

    @staticmethod
    def _find_field(data: dict, keys: list, depth: int = 0) -> str:
        """递归搜索字典中的指定字段之一"""
        if depth > 4:
            return ""
        for key in keys:
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for key, val in data.items():
            if isinstance(val, dict):
                found = SohuCrawler._find_field(val, keys, depth + 1)
                if found:
                    return found
        return ""

    def _enrich_result(self, result: dict, html: str, url: str):
        """
        补充搜狐特有的作者、图片来源：
        1. 作者从 <meta name="mediaid"> 提取
        2. 图片从 JS cfgs.imgsList 提取真实 URL，替换正文中的加密占位图
        """
        if not isinstance(result, dict):
            return
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            return

        # 补充作者
        if not result.get("author"):
            meta = soup.find("meta", attrs={"name": "mediaid"})
            if meta:
                author = _safe_str(meta.get("content"))
                if author:
                    result["author"] = author
            if not result.get("author"):
                meta2 = soup.find("meta", attrs={"name": "author"})
                if meta2:
                    result["author"] = _safe_str(meta2.get("content"))

        # 提取真实图片 URL（覆盖加密 data-src）
        real_images = self._extract_imgs_list(html, url)
        if real_images:
            # 图片列表以 imgsList 的为准
            result["images"] = real_images
            if not result.get("thumbnail"):
                result["thumbnail"] = real_images[0]["url"]
            # 替换正文中的加密占位图
            if result.get("content_html"):
                result["content_html"] = self._replace_encrypted_images(
                    result["content_html"], real_images, url
                )

        # og:image 作为兜底封面图
        if not result.get("thumbnail"):
            og = soup.find("meta", property="og:image")
            if og:
                og_url = _safe_str(og.get("content"))
                if og_url:
                    if og_url.startswith("//"):
                        og_url = "https:" + og_url
                    result["thumbnail"] = og_url

    @staticmethod
    def _extract_imgs_list(html: str, base_url: str) -> list:
        """从 JS cfgs.imgsList 提取真实图片 URL"""
        images = []
        try:
            match = re.search(r'imgsList\s*:\s*\[([^\]]*)\]', html, re.DOTALL)
            if not match:
                return images
            imgs_raw = match.group(1)
            img_urls = re.findall(r'"url"\s*:\s*"([^"]+)"', imgs_raw)
            for img_url in img_urls:
                img_url = img_url.strip()
                if not img_url:
                    continue
                if img_url.startswith("//"):
                    img_url = "https:" + img_url
                elif not img_url.startswith("http"):
                    img_url = urljoin(base_url, img_url)
                images.append({"url": img_url, "caption": "", "in_content": True})
        except Exception:
            pass
        return images

    @staticmethod
    def _replace_encrypted_images(content_html: str, real_images: list, base_url: str) -> str:
        """替换正文中加密的 <img data-src="加密值"> 为真实图片 URL"""
        if not content_html or not real_images:
            return content_html
        try:
            soup = BeautifulSoup(content_html, "lxml")
            encrypted_imgs = []
            for img in soup.find_all("img"):
                src = _safe_str(img.get("src"))
                data_src = _safe_str(img.get("data-src"))
                if data_src and not data_src.startswith(("http", "//")):
                    encrypted_imgs.append(img)
                elif not src and not data_src:
                    encrypted_imgs.append(img)
            for i, img in enumerate(encrypted_imgs):
                if i < len(real_images):
                    img["src"] = real_images[i]["url"]
                    if img.get("data-src"):
                        del img["data-src"]
                else:
                    try:
                        img.decompose()
                    except Exception:
                        pass
            body = soup.find("body")
            if body:
                return "".join(str(child) for child in body.children)
            return str(soup)
        except Exception:
            return content_html
