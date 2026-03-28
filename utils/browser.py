"""
Playwright 浏览器渲染工具 — 常驻子进程模式

架构：
  主进程 (scheduler.py)
    ↕ stdin/stdout JSON 通信
  常驻子进程 (_render_worker.py)
    └── Chromium 浏览器实例（启动后常驻，不反复创建/销毁）

优势：
  - Chromium 只启动一次，每篇文章渲染仅需 3-5 秒（之前 15-25 秒）
  - 完全隔离 asyncio 事件循环，无多线程冲突
  - 子进程崩溃自动重启
  - 线程安全（主进程端加锁，一次只发一个请求）
"""
import json
import logging
import subprocess
import sys
import os
import threading
import time
import selectors
import base64

logger = logging.getLogger(__name__)

_WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_render_worker.py")
_lock = threading.Lock()
_worker_proc = None
_worker_start_time = 0
_MAX_WORKER_AGE = 3600
_request_count = 0
_MAX_REQUESTS = 100


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
                page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}",lambda r:r.abort())
                page.route("**/*google*analytics*",lambda r:r.abort())
                page.route("**/*cnzz*",lambda r:r.abort())
                page.route("**/*baidu.com/hm*",lambda r:r.abort())
                page.goto(url,wait_until="domcontentloaded",timeout=timeout*1000)
                if ws:
                    try:
                        page.wait_for_selector(ws,timeout=min(timeout*1000,8000))
                    except:
                        pass
                if wt > 0:
                    page.wait_for_timeout(wt)
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
        # 等待 ready（最多30秒）
        sel = selectors.DefaultSelector()
        sel.register(_worker_proc.stdout, selectors.EVENT_READ)
        events = sel.select(timeout=30)
        sel.unregister(_worker_proc.stdout)
        sel.close()
        if not events:
            logger.error("[browser] Worker 启动超时")
            _kill_worker()
            return False
        line = _worker_proc.stdout.readline().decode("utf-8", errors="replace").strip()
        if not line:
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
    线程安全，子进程崩溃自动重启，Chromium 只启动一次。
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

            # 读取响应（带超时）
            sel = selectors.DefaultSelector()
            sel.register(_worker_proc.stdout, selectors.EVENT_READ)
            events = sel.select(timeout=timeout + 15)
            sel.unregister(_worker_proc.stdout)
            sel.close()
            if not events:
                logger.warning(f"[browser] 渲染超时: {url[:60]}")
                _kill_worker()
                return ""

            resp_line = _worker_proc.stdout.readline().decode("utf-8", errors="replace").strip()
            if not resp_line:
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
            html_line = _worker_proc.stdout.readline().decode("ascii", errors="replace").strip()
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
