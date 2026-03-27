"""
Playwright 浏览器渲染工具

解决方案：用独立子进程执行 Playwright 渲染，完全避免多线程 asyncio 冲突。
Playwright 的 sync API 内部依赖 asyncio 事件循环，在 ThreadPoolExecutor
的子线程中调用会报 "Cannot switch to a different thread"。
用 subprocess 在独立 Python 进程中渲染，主进程通过 stdin/stdout 通信。

用法:
    from utils.browser import fetch_page_html
    html = fetch_page_html("https://new.qq.com/rain/a/xxx", timeout=15)
"""
import json
import logging
import subprocess
import sys
import os
import tempfile

logger = logging.getLogger(__name__)

# 渲染脚本路径
_RENDER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_render_page.py")


def _ensure_render_script():
    """确保渲染子脚本存在"""
    if os.path.exists(_RENDER_SCRIPT):
        return
    # 动态创建渲染子脚本
    script_content = r'''
import sys, json

def render(url, timeout, wait_selector, wait_time):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                       "--disable-gpu", "--single-process"],
            )
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
                # 屏蔽不必要资源
                page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}",
                            lambda route: route.abort())
                page.route("**/*google*analytics*", lambda route: route.abort())
                page.route("**/*cnzz*", lambda route: route.abort())
                page.route("**/*baidu.com/hm*", lambda route: route.abort())

                page.goto(url, wait_until="domcontentloaded",
                          timeout=timeout * 1000)
                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector,
                                                timeout=min(timeout * 1000, 8000))
                    except Exception:
                        pass
                if wait_time > 0:
                    page.wait_for_timeout(wait_time)
                html = page.content()
                page.close()
                context.close()
                return html
            finally:
                browser.close()
    except Exception as e:
        return ""

if __name__ == "__main__":
    args = json.loads(sys.stdin.readline())
    html = render(args["url"], args["timeout"],
                  args.get("wait_selector"), args.get("wait_time", 2000))
    # 输出长度行 + HTML内容
    encoded = html.encode("utf-8")
    sys.stdout.buffer.write(f"{len(encoded)}\n".encode("utf-8"))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
'''
    with open(_RENDER_SCRIPT, "w", encoding="utf-8") as f:
        f.write(script_content)


def fetch_page_html(url: str, timeout: int = 15,
                    wait_selector: str = None,
                    wait_time: int = 2000) -> str:
    """
    用独立子进程执行 Playwright 渲染页面，返回完整 HTML。
    完全避免多线程 asyncio 冲突（"Cannot switch to a different thread"）。

    参数:
        url: 页面 URL
        timeout: 页面加载超时（秒）
        wait_selector: 可选，等待指定 CSS 选择器出现
        wait_time: 额外等待时间（毫秒）

    返回:
        渲染后的 HTML 字符串，失败返回空字符串
    """
    _ensure_render_script()

    args = json.dumps({
        "url": url,
        "timeout": timeout,
        "wait_selector": wait_selector,
        "wait_time": wait_time,
    })

    try:
        proc = subprocess.Popen(
            [sys.executable, _RENDER_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        stdout, stderr = proc.communicate(
            input=(args + "\n").encode("utf-8"),
            timeout=timeout + 30,  # 子进程超时比页面超时多30秒
        )
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            if err:
                logger.debug(f"[browser] 子进程错误: {url[:60]} | {err[:200]}")
            return ""

        # 解析输出：第一行是长度，后面是HTML
        lines = stdout.split(b"\n", 1)
        if len(lines) < 2:
            return ""
        try:
            content_len = int(lines[0].strip())
        except ValueError:
            return ""
        html = lines[1][:content_len].decode("utf-8", errors="replace")
        if len(html) > 500:
            logger.info(f"[browser] 渲染成功: {url[:60]} ({len(html)} bytes)")
        return html

    except subprocess.TimeoutExpired:
        logger.warning(f"[browser] 子进程超时: {url[:60]}")
        try:
            proc.kill()
        except Exception:
            pass
        return ""
    except Exception as e:
        logger.warning(f"[browser] 子进程失败: {url[:60]} | {e}")
        return ""


def close_browser():
    """兼容旧接口（子进程模式无需关闭）"""
    pass
