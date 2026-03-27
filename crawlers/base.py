"""
爬虫基类 - 所有站点爬虫的统一接口
"""
import re
import random
import time
import logging
import threading
import requests
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

import sys
import os
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import USER_AGENTS, CRAWL_TIMEOUT, CRAWL_RETRY, CRAWL_DELAY, DETAIL_FETCH_TIMEOUT, DETAIL_MAX_WORKERS

TOP_N = 10  # 每个站点只保留 TOP 10

# 标题最短有效长度（中文约 4 个汉字=8字节，英文约 10 个字符）
# 低于此阈值的文本多为导航/按钮/标签，不是新闻标题
MIN_TITLE_LEN_ZH = 6   # 中文标题最短字符数
MIN_TITLE_LEN_EN = 10  # 英文标题最短字符数


@dataclass
class NewsItem:
    """标准化新闻条目数据类"""
    title: str = ""
    url: str = ""
    source: str = ""
    source_name: str = ""
    summary: str = ""
    content: str = ""
    content_html: str = ""
    category: str = ""
    rank: int = 0
    pub_time: str = ""
    crawl_time: str = ""
    language: str = "zh"
    images: list = field(default_factory=list)
    videos: list = field(default_factory=list)
    thumbnail: str = ""
    author: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "source_name": self.source_name,
            "summary": self.summary,
            "content": self.content,
            "content_html": self.content_html,
            "category": self.category,
            "rank": self.rank,
            "pub_time": self.pub_time,
            "crawl_time": self.crawl_time,
            "language": self.language,
            "images": self.images,
            "videos": self.videos,
            "thumbnail": self.thumbnail,
            "author": self.author,
            "extra": self.extra,
        }


# ========== 相对时间解析的正则 ==========
_RELATIVE_PATTERNS = [
    (re.compile(r"(\d+)\s*秒前"), lambda m: timedelta(seconds=int(m.group(1)))),
    (re.compile(r"(\d+)\s*分钟前"), lambda m: timedelta(minutes=int(m.group(1)))),
    (re.compile(r"(\d+)\s*分前"), lambda m: timedelta(minutes=int(m.group(1)))),
    (re.compile(r"(\d+)\s*小时前"), lambda m: timedelta(hours=int(m.group(1)))),
    (re.compile(r"(\d+)\s*天前"), lambda m: timedelta(days=int(m.group(1)))),
    (re.compile(r"昨天"), lambda m: timedelta(days=1)),
    (re.compile(r"前天"), lambda m: timedelta(days=2)),
]

# 绝对时间格式
_DATETIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S+08:00",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m-%d %H:%M",
    "%m月%d日 %H:%M",
]


class BaseCrawler(ABC):
    """爬虫基类，所有站点爬虫继承此类并实现 crawl() 方法"""

    def __init__(self):
        self.name = ""            # 站点标识，如 "sina"
        self.display_name = ""    # 站点中文名，如 "新浪新闻"
        self.language = "zh"      # zh / en
        self.logger = logging.getLogger(self.__class__.__name__)
        self._thread_local = threading.local()  # 线程本地存储
        self.session = self._build_session()    # 主线程 session

    def _build_session(self) -> requests.Session:
        """构建 requests Session，带默认 UA 和超时"""
        s = requests.Session()
        s.headers.update({
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        return s

    def _get_session(self) -> requests.Session:
        """获取当前线程的 Session（线程安全）。多线程并发时每个线程独立 Session。"""
        if not hasattr(self._thread_local, "session"):
            self._thread_local.session = self._build_session()
        return self._thread_local.session

    # curl_cffi 有效的浏览器 TLS 指纹（经测试可绕过 DataDome 等反爬）
    _CFFI_FINGERPRINTS = ["safari18_0", "firefox", "safari17_0"]

    def _request(self, url: str, method="GET", **kwargs) -> Optional[requests.Response]:
        """
        带重试和延迟的请求方法。
        每次请求使用独立的随机 UA（线程安全）。
        当普通请求被 401/403 拦截时，自动使用 curl_cffi TLS 指纹绕过。
        失败返回 None。
        """
        kwargs.setdefault("timeout", CRAWL_TIMEOUT)
        # 提前取出 caller 传的 headers，避免在重试循环中被 pop 丢失
        caller_headers = dict(kwargs.pop("headers", {}))
        # 是否跳过 curl_cffi 回退（某些场景如 API 调用不需要）
        skip_cffi = kwargs.pop("skip_cffi", False)

        last_status = 0
        for attempt in range(1, CRAWL_RETRY + 1):
            try:
                # 每次重试都带上 caller headers + 随机 UA
                headers = dict(caller_headers)
                headers["User-Agent"] = random.choice(USER_AGENTS)
                session = self._get_session()
                resp = session.request(method, url, headers=headers, **kwargs)
                resp.raise_for_status()
                # 编码检测：防止 GBK 页面乱码
                if resp.encoding and resp.encoding.lower() in ("iso-8859-1",):
                    resp.encoding = resp.apparent_encoding
                return resp
            except requests.RequestException as e:
                last_status = getattr(getattr(e, "response", None), "status_code", 0)
                self.logger.warning(f"[{self.name}] 请求失败 (第{attempt}次): {url} | {e}")
                if attempt < CRAWL_RETRY:
                    delay = random.uniform(*CRAWL_DELAY)
                    time.sleep(delay)

        # 普通请求全部失败后，如果是 401/403/503 反爬拦截，尝试 curl_cffi 绕过
        if not skip_cffi and last_status in (401, 403, 503):
            cffi_resp = self._request_with_cffi(url, method, caller_headers, **kwargs)
            if cffi_resp is not None:
                return cffi_resp

        return None

    def _request_with_cffi(self, url: str, method: str = "GET",
                           extra_headers: dict = None, **kwargs) -> Optional[requests.Response]:
        """
        使用 curl_cffi 模拟真实浏览器 TLS 指纹发起请求。
        当普通 requests 被 DataDome/Cloudflare 等反爬服务拦截时自动调用。
        返回标准 requests.Response 兼容对象，或 None。
        """
        try:
            from curl_cffi import requests as cffi_requests
        except ImportError:
            return None

        fp = random.choice(self._CFFI_FINGERPRINTS)
        timeout = kwargs.get("timeout", CRAWL_TIMEOUT)

        try:
            resp = cffi_requests.request(
                method, url,
                impersonate=fp,
                timeout=timeout,
                headers=extra_headers or {},
            )
            if resp.status_code == 200:
                self.logger.info(f"[{self.name}] curl_cffi({fp}) 绕过成功: {url[:60]}")
                return resp
        except Exception as e:
            self.logger.debug(f"[{self.name}] curl_cffi({fp}) 失败: {url[:60]} | {e}")

        return None

    def _make_item(self, title: str, url: str, rank: int = 0,
                   summary: str = "", category: str = "",
                   pub_time: str = "", content: str = "",
                   content_html: str = "", images: Optional[list] = None,
                   videos: Optional[list] = None, thumbnail: str = "",
                   author: str = "",
                   extra: Optional[dict] = None) -> dict:
        """构造标准新闻条目，rank 为热榜排名（1=最热）"""
        return {
            "title": title.strip() if title else "",
            "url": url.strip() if url else "",
            "source": self.name,
            "source_name": self.display_name,
            "summary": summary.strip() if summary else "",
            "content": content.strip() if content else "",
            "content_html": content_html.strip() if content_html else "",
            "category": category.strip() if category else "",
            "rank": rank,
            "pub_time": pub_time.strip() if pub_time else "",
            "crawl_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "language": self.language,
            "images": images or [],
            "videos": videos or [],
            "thumbnail": thumbnail.strip() if thumbnail else "",
            "author": author.strip() if author else "",
            "extra": extra or {},
        }

    # ========== 工具方法 ==========

    def parse_time(self, raw: str) -> str:
        """
        统一时间解析，支持：
        - 相对时间: '3分钟前', '2小时前', '昨天', '前天'
        - 绝对时间: '2026-03-11 14:00:00', '2026/03/11'
        - 时间戳: 秒级(10位) 或 毫秒级(13位)
        返回标准格式 'YYYY-MM-DD HH:MM:SS'，解析失败返回空字符串。
        """
        if not raw:
            return ""
        raw = raw.strip()

        # 1. 尝试时间戳（纯数字）
        if raw.isdigit():
            ts = int(raw)
            if ts > 1e12:  # 毫秒级
                ts = ts // 1000
            try:
                return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OSError):
                return ""

        # 1.5 尝试 RFC 2822 格式（RSS 常用，如 "Mon, 10 Mar 2026 14:30:00 GMT"）
        try:
            dt = parsedate_to_datetime(raw)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

        # 2. 尝试相对时间
        for pattern, delta_fn in _RELATIVE_PATTERNS:
            m = pattern.search(raw)
            if m:
                dt = datetime.now() - delta_fn(m)
                return dt.strftime("%Y-%m-%d %H:%M:%S")

        # 3. 尝试绝对时间格式
        for fmt in _DATETIME_FORMATS:
            try:
                dt = datetime.strptime(raw, fmt)
                # 缺年份的格式补当年
                if dt.year == 1900:
                    dt = dt.replace(year=datetime.now().year)
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue

        # 4. 都失败了，返回空串
        return ""

    def clean_text(self, html: str) -> str:
        """HTML → 纯文本：去标签、去多余空白"""
        if not html:
            return ""
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            # 去除 script/style
            for tag in soup(["script", "style", "iframe", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator="\n")
        except Exception:
            # fallback: 简单正则去标签
            text = re.sub(r"<[^>]+>", "", html)

        # 合并多个空白行/空格
        text = re.sub(r"\n\s*\n", "\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    def extract_summary(self, content: str, max_len: int = 200) -> str:
        """从全文提取摘要：取前 max_len 字符，在标点处截断"""
        if not content:
            return ""
        text = content[:max_len + 50]  # 多取一些以便找截断点
        if len(text) > max_len:
            # 在标点处截断
            for punct in ("。", "！", "？", ".", "!", "?", "；", ";"):
                idx = text.rfind(punct, 0, max_len + 1)
                if idx > max_len // 2:
                    return text[:idx + 1]
            return text[:max_len] + "..."
        return text

    def validate(self, items: list) -> list:
        """
        过滤无效数据：
        - 空标题或空 URL
        - 标题过短 (< 4 字符)
        - pub_time 在未来 (解析错误)
        - URL 格式非法
        """
        valid = []
        now = datetime.now()
        for item in items:
            title = item.get("title", "").strip() if isinstance(item, dict) else ""
            url = item.get("url", "").strip() if isinstance(item, dict) else ""

            if not title or not url:
                continue
            if len(title) < 4:
                continue
            if not url.startswith(("http://", "https://")):
                continue

            # 检查时间是否在未来（超过 1 小时视为解析错误）
            pub_time = item.get("pub_time", "")
            if pub_time:
                try:
                    pt = datetime.strptime(pub_time, "%Y-%m-%d %H:%M:%S")
                    if pt > now + timedelta(hours=1):
                        item["pub_time"] = ""  # 清除错误时间
                except ValueError:
                    pass

            valid.append(item)

        return valid

    # ========== 详情页抓取 ==========

    # 子类可覆盖：指定正文容器的 CSS 选择器列表（按优先级）
    detail_selectors: list = []

    # 子类可覆盖：是否启用详情页抓取（默认启用）
    enable_detail: bool = True

    def fetch_detail(self, item: dict) -> dict:
        """
        抓取单篇新闻详情页，提取正文/图片/视频。
        返回 {"content", "content_html", "images", "videos", "thumbnail"}。
        子类可重写此方法实现自定义逻辑。

        流程：
        1. 先用 requests 发静态HTTP请求（快速，低资源）
        2. 如果正文不足200字（说明JS渲染问题或提取到导航/版权等垃圾文本），
           用 Playwright 浏览器渲染重试
        """
        if not isinstance(item, dict):
            return {}
        url = item.get("url", "") or ""
        if not url or not isinstance(url, str):
            return {}

        _MIN_CONTENT_LEN = 200  # 正文最低有效长度（低于此值触发Playwright）

        # --- 阶段1: 静态HTTP请求（快速）---
        result = {}
        try:
            resp = self._request(url, timeout=DETAIL_FETCH_TIMEOUT)
            if resp is not None:
                html = resp.text if resp.text else ""
                result = self.parse_detail(html, url)
                if isinstance(result, dict) and len((result.get("content") or "")) >= _MIN_CONTENT_LEN:
                    return result
        except Exception as e:
            self.logger.debug(f"[{self.name}] 静态请求失败: {url[:60]} | {e}")

        # --- 阶段2: Playwright浏览器渲染（正文不足时自动触发）---
        try:
            from utils.browser import fetch_page_html
            # 构建等待选择器（优先用子类定义的选择器）
            wait_sel = None
            if self.detail_selectors:
                wait_sel = self.detail_selectors[0]
            rendered_html = fetch_page_html(
                url, timeout=DETAIL_FETCH_TIMEOUT,
                wait_selector=wait_sel, wait_time=3000,
            )
            if rendered_html:
                rendered_result = self.parse_detail(rendered_html, url)
                if isinstance(rendered_result, dict):
                    new_content = rendered_result.get("content") or ""
                    old_content = (result.get("content") or "") if isinstance(result, dict) else ""
                    if len(new_content) > len(old_content):
                        self.logger.info(f"[{self.name}] Playwright 渲染成功: {url[:60]}")
                        return rendered_result
        except ImportError:
            pass  # Playwright 未安装，跳过
        except Exception as e:
            self.logger.debug(f"[{self.name}] Playwright 渲染失败: {url[:60]} | {e}")

        return result if isinstance(result, dict) else {}

    def _playwright_fallback(self, url: str, existing_result: dict = None) -> dict:
        """
        Playwright 浏览器渲染兜底。
        当静态HTTP请求无法获取完整正文时，子类的 fetch_detail 可调用此方法。
        只有正文不足200字时才会触发 Playwright 渲染。
        200字阈值可以过滤掉导航栏/版权声明等凑数文本。

        参数:
            url: 文章 URL
            existing_result: 静态请求已获取的部分结果（用于比较正文长度）

        返回:
            渲染后提取的 dict 结果，或传入的 existing_result
        """
        _MIN_CONTENT_LEN = 200

        if not existing_result:
            existing_result = {}
        old_content = existing_result.get("content") or "" if isinstance(existing_result, dict) else ""

        # 正文已足够长，不需要 Playwright
        if len(old_content) >= _MIN_CONTENT_LEN:
            return existing_result

        try:
            from utils.browser import fetch_page_html
            wait_sel = self.detail_selectors[0] if self.detail_selectors else None
            rendered_html = fetch_page_html(
                url, timeout=DETAIL_FETCH_TIMEOUT,
                wait_selector=wait_sel, wait_time=3000,
            )
            if rendered_html:
                rendered_result = self.parse_detail(rendered_html, url)
                if isinstance(rendered_result, dict):
                    new_content = rendered_result.get("content") or ""
                    if len(new_content) > len(old_content):
                        self.logger.info(f"[{self.name}] Playwright 渲染成功: {url[:60]}")
                        return rendered_result
        except ImportError:
            pass
        except Exception as e:
            self.logger.debug(f"[{self.name}] Playwright 渲染失败: {url[:60]} | {e}")

        return existing_result

    def parse_detail(self, html: str, url: str) -> dict:
        """
        从详情页 HTML 中提取正文/图片/视频。
        子类可重写以使用站点特定的选择器。
        """
        from utils.content_extractor import extract_content
        return extract_content(html, url, selectors=self.detail_selectors)

    def _fetch_all_details(self, items: list) -> list:
        """并发抓取所有条目的详情页，合并到 items 中。
        已在 DB 中有正文的条目会跳过，避免重复抓取。"""
        if not items:
            return items

        # 检查 DB 中哪些 URL 已有正文（优化：跳过重复抓取）
        skip_urls = set()
        try:
            from storage import check_urls_have_content
            all_urls = [item.get("url", "") for item in items if item.get("url")]
            skip_urls = check_urls_have_content(all_urls)
            if skip_urls:
                self.logger.info(f"[{self.name}] 跳过 {len(skip_urls)} 条已有正文的新闻")
        except Exception:
            pass  # 查询失败不影响正常流程

        items_to_fetch = [item for item in items if item.get("url", "") not in skip_urls]
        items_skipped = [item for item in items if item.get("url", "") in skip_urls]

        def _fetch_one(item):
            if not isinstance(item, dict):
                return item
            try:
                detail = self.fetch_detail(item)
            except Exception as e:
                self.logger.warning(f"[{self.name}] fetch_detail 异常: "
                                    f"{item.get('url', '')[:60]} | {e}")
                detail = {}

            if not isinstance(detail, dict) or not detail:
                return item

            # 正文：详情页正文比现有内容更长时才替换
            existing_content = item.get("content") or ""
            new_content = detail.get("content") or ""
            if isinstance(new_content, str) and len(new_content) > len(existing_content):
                item["content"] = new_content

            # content_html：有值就覆盖（详情页 HTML 优先级高于列表页）
            new_html = detail.get("content_html") or ""
            if isinstance(new_html, str) and new_html.strip():
                item["content_html"] = new_html

            # 图片：以详情页为准（更完整）
            new_images = detail.get("images")
            if isinstance(new_images, list) and new_images:
                item["images"] = new_images

            # 视频
            new_videos = detail.get("videos")
            if isinstance(new_videos, list) and new_videos:
                item["videos"] = new_videos

            # 封面图：只在原来没有时填充
            new_thumb = detail.get("thumbnail") or ""
            if isinstance(new_thumb, str) and new_thumb and not item.get("thumbnail"):
                item["thumbnail"] = new_thumb

            # 作者：只在原来没有时填充
            new_author = detail.get("author") or ""
            if isinstance(new_author, str) and new_author and not item.get("author"):
                item["author"] = new_author

            # 发布时间：只在原来没有时填充，并统一格式化
            new_pub = detail.get("pub_time") or ""
            if isinstance(new_pub, str) and new_pub and not item.get("pub_time"):
                try:
                    parsed = self.parse_time(new_pub)
                    if parsed:
                        item["pub_time"] = parsed
                except Exception:
                    item["pub_time"] = new_pub

            # 摘要：没有摘要时从正文自动生成
            if not item.get("summary"):
                content_for_summary = item.get("content") or ""
                if isinstance(content_for_summary, str) and content_for_summary:
                    try:
                        item["summary"] = self.extract_summary(content_for_summary)
                    except Exception:
                        pass

            return item

        results = list(items_skipped)  # 已跳过的保持原样

        if items_to_fetch:
            with ThreadPoolExecutor(max_workers=DETAIL_MAX_WORKERS) as pool:
                futures = {pool.submit(_fetch_one, item): item for item in items_to_fetch}
                for future in as_completed(futures):
                    try:
                        r = future.result()
                        results.append(r if isinstance(r, dict) else futures[future])
                    except Exception as e:
                        self.logger.warning(f"[{self.name}] 详情页线程异常: {e}")
                        results.append(futures[future])

        # 保持原始排序（按 rank），过滤非 dict 的异常条目
        results = [r for r in results if isinstance(r, dict)]
        results.sort(key=lambda x: x.get("rank", 999) if isinstance(x, dict) else 999)
        return results

    # ========== 核心接口 ==========

    @abstractmethod
    def crawl(self) -> list[dict]:
        """
        执行爬取，返回热榜 TOP 10 新闻列表。
        每个子类必须实现此方法。
        返回的列表按热度排序，rank=1 为最热。
        """
        pass

    def run(self) -> list[dict]:
        """执行爬取，验证+截断为 TOP 10，然后抓取详情页。异常向上抛出。"""
        self.logger.info(f"[{self.name}] 开始爬取: {self.display_name}")
        results = self.crawl()
        # 验证数据有效性
        results = self.validate(results)
        # 强制截断为 TOP 10
        results = results[:TOP_N]
        self.logger.info(f"[{self.name}] 列表爬取完成: {len(results)} 条 (TOP {TOP_N})")

        # 抓取详情页（正文/图片/视频）
        if self.enable_detail and results:
            self.logger.info(f"[{self.name}] 开始抓取详情页...")
            results = self._fetch_all_details(results)
            detail_ok = sum(1 for r in results if r.get("content"))
            self.logger.info(f"[{self.name}] 详情页完成: {detail_ok}/{len(results)} 篇有正文")

            # 下载图片到本地
            self._download_images(results)

        return results

    def _download_images(self, items: list):
        """为所有新闻条目下载图片到本地"""
        try:
            from media_storage import download_images_for_news, download_thumbnail
        except ImportError:
            self.logger.debug(f"[{self.name}] media_storage 不可用，跳过图片下载")
            return

        total_downloaded = 0
        for item in items:
            # 下载正文图片
            images = item.get("images", [])
            if images:
                item["images"] = download_images_for_news(images)
                total_downloaded += sum(1 for img in item["images"] if img.get("local"))

            # 下载缩略图
            thumb = item.get("thumbnail", "")
            if thumb and thumb.startswith("http"):
                local_thumb = download_thumbnail(thumb)
                if local_thumb:
                    item["thumbnail_local"] = local_thumb

        if total_downloaded:
            self.logger.info(f"[{self.name}] 图片下载完成: {total_downloaded} 张")


class RSSCrawler(BaseCrawler):
    """
    通用 RSS 爬虫基类。
    子类只需设置 name / display_name / language / rss_url / category 即可。
    自动处理：请求 → feedparser 解析 → 去重 → 标准化输出。
    """
    rss_url: str = ""
    category: str = "Top Stories"

    def crawl(self) -> list[dict]:
        results = []
        if not self.rss_url:
            return results

        resp = self._request(self.rss_url)
        if resp is None:
            return results

        try:
            import feedparser
            feed = feedparser.parse(resp.content)
        except Exception as e:
            self.logger.warning(f"[{self.name}] RSS 解析失败: {e}")
            return results

        seen = set()
        for i, entry in enumerate(feed.entries[:10], 1):
            title = str(entry.get("title", "")).strip()
            link = str(entry.get("link", "")).strip()
            if not title or not link or link in seen:
                continue
            seen.add(link)

            summary = str(entry.get("summary", ""))
            # 仅当摘要含 HTML 标签时才清洗
            if summary and "<" in summary and ">" in summary:
                summary = self.clean_text(summary)[:200]
            else:
                summary = summary[:200]

            # 从 RSS 条目提取作者（feedparser 会解析 dc:creator / author 字段）
            rss_author = str(entry.get("author", "")).strip()
            if not rss_author:
                # 某些 RSS 源使用 dc:creator
                rss_author = str(entry.get("dc_creator", "")).strip()

            results.append(self._make_item(
                title=title,
                url=link,
                rank=i,
                summary=summary,
                category=self.category,
                pub_time=self.parse_time(str(entry.get("published", ""))),
                author=rss_author,
            ))

        return results
