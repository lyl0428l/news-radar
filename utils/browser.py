"""
Playwright 浏览器渲染工具

为需要 JS 渲染的新闻站点提供统一的浏览器页面获取能力。
使用单例 Browser 实例，避免每次请求都启动/关闭浏览器。

用法:
    from utils.browser import fetch_page_html
    html = fetch_page_html("https://new.qq.com/rain/a/xxx", timeout=15)

支持:
  - 自动等待页面 JS 渲染完成（networkidle / 关键元素出现）
  - 自动处理浏览器启动/关闭/重启
  - 线程安全（使用锁保护 Browser 实例）
  - 超时保护，避免单个页面阻塞整个爬虫
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

# 全局浏览器实例和锁
_browser = None
_playwright = None
_lock = threading.Lock()
_launch_time = 0
_MAX_BROWSER_AGE = 3600  # 浏览器实例最长存活1小时，超时后重启防内存泄漏


def _ensure_browser():
    """确保浏览器实例可用（线程安全，懒加载）"""
    global _browser, _playwright, _launch_time

    with _lock:
        # 检查是否需要重启（超时或崩溃）
        now = time.time()
        if _browser and (now - _launch_time > _MAX_BROWSER_AGE):
            logger.info("[browser] 浏览器实例超时，重启...")
            _close_browser_unsafe()

        if _browser is not None:
            try:
                # 简单检查浏览器是否还活着
                _browser.contexts
                return _browser
            except Exception:
                logger.warning("[browser] 浏览器已崩溃，重启...")
                _close_browser_unsafe()

        # 启动新浏览器
        try:
            from playwright.sync_api import sync_playwright
            _playwright = sync_playwright().start()
            _browser = _playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-sync",
                    "--disable-translate",
                    "--no-first-run",
                    "--single-process",
                ],
            )
            _launch_time = now
            logger.info("[browser] Chromium 启动成功")
            return _browser
        except Exception as e:
            logger.error(f"[browser] Chromium 启动失败: {e}")
            _browser = None
            _playwright = None
            return None


def _close_browser_unsafe():
    """关闭浏览器（调用者负责加锁）"""
    global _browser, _playwright
    try:
        if _browser:
            _browser.close()
    except Exception:
        pass
    try:
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    _browser = None
    _playwright = None


def close_browser():
    """外部调用：安全关闭浏览器"""
    with _lock:
        _close_browser_unsafe()


def fetch_page_html(url: str, timeout: int = 15,
                    wait_selector: str = None,
                    wait_time: int = 2000) -> str:
    """
    用 Playwright 渲染页面并返回完整 HTML。

    参数:
        url: 页面 URL
        timeout: 页面加载超时（秒）
        wait_selector: 可选，等待指定 CSS 选择器出现（如 ".article-content"）
        wait_time: 额外等待时间（毫秒），确保 JS 渲染完成

    返回:
        渲染后的 HTML 字符串，失败返回空字符串
    """
    browser = _ensure_browser()
    if browser is None:
        return ""

    page = None
    context = None
    try:
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # 屏蔽不必要的资源加载（加速）
        page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}",
                    lambda route: route.abort())
        page.route("**/*google*analytics*", lambda route: route.abort())
        page.route("**/*cnzz*", lambda route: route.abort())
        page.route("**/*baidu.com/hm*", lambda route: route.abort())

        page.goto(url, wait_until="domcontentloaded",
                  timeout=timeout * 1000)

        # 等待关键元素出现（如正文容器）
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector,
                                        timeout=min(timeout * 1000, 8000))
            except Exception:
                pass  # 超时也继续，可能选择器不对但正文已加载

        # 额外等待 JS 渲染
        if wait_time > 0:
            page.wait_for_timeout(wait_time)

        html = page.content()
        return html

    except Exception as e:
        logger.warning(f"[browser] 页面渲染失败: {url[:60]} | {e}")
        return ""

    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass
