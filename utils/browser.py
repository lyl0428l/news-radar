"""
Playwright 浏览器渲染工具 — 常驻子进程模式（Windows 兼容版）

Windows 上 selectors 不支持 pipe fd，改用线程读取 + Event 超时等待。
"""
import json
import logging
import subprocess
import sys
import os
import threading
import time
import base64

logger = logging.getLogger(__name__)

_WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_render_worker.py")
_lock = threading.Lock()
_worker_proc = None
_worker_start_time = 0
_MAX_WORKER_AGE = 7200  # 2小时（延长worker生命周期，减少重启开销）
_request_count = 0
_MAX_REQUESTS = 200     # 提高到200次请求再重启


def _ensure_worker_script():
    """确保常驻渲染子进程脚本存在"""
    script = r'''import sys, json, base64
def main():
    browser = None
    pw = None
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage",
                  "--disable-gpu","--single-process"],
        )
        sys.stdout.write(json.dumps({"status":"ready"})+"\n")
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(json.dumps({"status":"error","msg":str(e)})+"\n")
        sys.stdout.flush()
        sys.exit(1)
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            req = json.loads(line)
            if req.get("cmd") == "quit":
                break
            url = req.get("url","")
            timeout = req.get("timeout",20)
            ws = req.get("wait_selector")
            wt = req.get("wait_time",2000)
            html = ""
            err = ""
            try:
                ctx = browser.new_context(
                    viewport={"width":1280,"height":800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
                )
                page = ctx.new_page()
                # 屏蔽所有非必要资源（图片/字体/CSS/广告/统计），只保留HTML和JS
                page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot,css}",lambda r:r.abort())
                page.route("**/*google*analytics*",lambda r:r.abort())
                page.route("**/*cnzz*",lambda r:r.abort())
                page.route("**/*baidu.com*",lambda r:r.abort())
                page.route("**/*bdstatic.com*",lambda r:r.abort())
                page.route("**/*doubleclick*",lambda r:r.abort())
                page.route("**/*adservice*",lambda r:r.abort())
                page.route("**/*adsense*",lambda r:r.abort())
                # 页面加载超时使用传入的timeout（秒→毫秒，下限10秒）
                page_timeout = max(timeout * 1000, 10000)
                page.goto(url,wait_until="domcontentloaded",timeout=page_timeout)
                # 等待正文容器出现（最多5秒）
                if ws:
                    try:
                        page.wait_for_selector(ws,timeout=5000)
                    except:
                        pass
                # JS渲染等待（使用传入的wait_time，下限1500ms）
                page.wait_for_timeout(max(wt, 1500))
                html = page.content()
                page.close()
                ctx.close()
            except Exception as e:
                err = str(e)
            resp = {"status":"ok" if html else "error","html_len":len(html),"error":err}
            sys.stdout.write(json.dumps(resp)+"\n")
            sys.stdout.flush()
            if html:
                enc = base64.b64encode(html.encode("utf-8")).decode("ascii")
                sys.stdout.write(enc+"\n")
                sys.stdout.flush()
        except Exception as e:
            try:
                sys.stdout.write(json.dumps({"status":"error","error":str(e)})+"\n")
                sys.stdout.flush()
            except:
                break
    try:
        if browser: browser.close()
    except: pass
    try:
        if pw: pw.stop()
    except: pass

if __name__=="__main__":
    main()
'''
    try:
        with open(_WORKER_SCRIPT, "w", encoding="utf-8") as f:
            f.write(script)
    except Exception as e:
        logger.error(f"[browser] 写入 worker 脚本失败: {e}")


def _readline_with_timeout(proc, timeout):
    """
    从子进程 stdout 读取一行，带超时。
    Windows 上 selectors 不支持 pipe，改用线程 + Event。
    """
    result = [None]
    event = threading.Event()

    def _reader():
        try:
            line = proc.stdout.readline()
            result[0] = line
        except Exception:
            result[0] = b""
        event.set()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    if event.wait(timeout=timeout):
        raw = result[0]
        if raw is None:
            return ""
        return raw.decode("utf-8", errors="replace").strip()
    else:
        # 超时
        return None


def _start_worker():
    global _worker_proc, _worker_start_time, _request_count
    _ensure_worker_script()
    try:
        _worker_proc = subprocess.Popen(
            [sys.executable, _WORKER_SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        _worker_start_time = time.time()
        _request_count = 0

        # 等待 ready（最多60秒，Chromium首次启动较慢）
        line = _readline_with_timeout(_worker_proc, 60)
        if line is None:
            logger.error("[browser] Worker 启动超时")
            _kill_worker()
            return False
        if not line:
            logger.error("[browser] Worker 无响应")
            _kill_worker()
            return False

        resp = json.loads(line)
        if resp.get("status") == "ready":
            logger.info("[browser] Chromium 常驻进程启动成功")
            return True
        logger.error(f"[browser] Worker 启动失败: {resp.get('msg')}")
        _kill_worker()
        return False
    except Exception as e:
        logger.error(f"[browser] Worker 启动异常: {e}")
        _kill_worker()
        return False


def _kill_worker():
    global _worker_proc
    if _worker_proc:
        try:
            _worker_proc.kill()
        except Exception:
            pass
        try:
            _worker_proc.wait(timeout=5)
        except Exception:
            pass
        _worker_proc = None


def _ensure_worker():
    global _worker_proc, _request_count
    if _worker_proc is not None:
        if _worker_proc.poll() is not None:
            logger.warning("[browser] Worker 已退出，重启...")
            _worker_proc = None
        elif time.time() - _worker_start_time > _MAX_WORKER_AGE:
            logger.info("[browser] Worker 超时重启")
            _kill_worker()
        elif _request_count >= _MAX_REQUESTS:
            logger.info("[browser] Worker 请求数达上限，重启")
            _kill_worker()
    if _worker_proc is None:
        return _start_worker()
    return True


def fetch_page_html(url: str, timeout: int = 20,
                    wait_selector: str = None,
                    wait_time: int = 2000) -> str:
    """
    用常驻 Playwright 子进程渲染页面，返回完整 HTML。
    Windows 兼容：用线程+Event替代selectors做超时读取。
    """
    global _request_count
    with _lock:
        if not _ensure_worker():
            return ""
        try:
            req = json.dumps({
                "cmd": "render", "url": url, "timeout": timeout,
                "wait_selector": wait_selector, "wait_time": wait_time,
            }) + "\n"
            _worker_proc.stdin.write(req.encode("utf-8"))
            _worker_proc.stdin.flush()

            # 读取响应元数据（带超时，页面15秒+JS2秒+容器等待5秒+通信余量3秒=25秒）
            resp_line = _readline_with_timeout(_worker_proc, timeout + 10)
            if resp_line is None:
                logger.warning(f"[browser] 渲染超时: {url[:60]}")
                _kill_worker()
                return ""
            if not resp_line:
                logger.warning(f"[browser] Worker 无响应: {url[:60]}")
                _kill_worker()
                return ""

            resp = json.loads(resp_line)
            _request_count += 1

            if resp.get("status") != "ok" or resp.get("html_len", 0) == 0:
                err = resp.get("error", "")
                if err:
                    logger.debug(f"[browser] 渲染失败: {url[:60]} | {err[:100]}")
                return ""

            # 读取 HTML（base64 编码）
            html_line = _readline_with_timeout(_worker_proc, 10)
            if not html_line:
                return ""

            html = base64.b64decode(html_line).decode("utf-8", errors="replace")
            if len(html) > 500:
                logger.info(f"[browser] 渲染成功: {url[:60]} ({len(html)} bytes)")
            return html
        except Exception as e:
            logger.warning(f"[browser] 通信异常: {url[:60]} | {e}")
            _kill_worker()
            return ""


def fetch_pages_batch(url_configs: list) -> dict:
    """
    批量渲染多个页面，返回 {url: html_str} 的字典。
    所有 URL 串行渲染（共用同一个 Chromium 实例），避免并发竞争。

    参数:
        url_configs: [{"url": str, "wait_selector": str|None, "wait_time": int}, ...]

    返回:
        {url: html_str}  渲染成功的页面 HTML
    """
    results = {}
    if not url_configs:
        return results

    total = len(url_configs)
    logger.info(f"[browser] 批量渲染开始: {total} 个页面")

    for i, config in enumerate(url_configs, 1):
        url = config.get("url", "")
        if not url:
            continue
        wait_selector = config.get("wait_selector")
        wait_time = config.get("wait_time", 2000)
        timeout = config.get("timeout", 20)
        try:
            html = fetch_page_html(
                url, timeout=timeout,
                wait_selector=wait_selector,
                wait_time=wait_time,
            )
            if html and len(html) > 500:
                results[url] = html
            # 每 10 页或最后一页输出进度
            if i % 10 == 0 or i == total:
                logger.info(f"[browser] 渲染进度: {i}/{total} (成功 {len(results)})")
        except Exception as e:
            logger.debug(f"[browser] 批量渲染单页失败: {url[:60]} | {e}")
            continue

    logger.info(f"[browser] 批量渲染完成: {len(results)}/{total} 成功")
    return results


def close_browser():
    with _lock:
        if _worker_proc and _worker_proc.poll() is None:
            try:
                _worker_proc.stdin.write(json.dumps({"cmd": "quit"}).encode("utf-8") + b"\n")
                _worker_proc.stdin.flush()
                _worker_proc.wait(timeout=10)
            except Exception:
                _kill_worker()
        else:
            _kill_worker()
