"""
Windows 服务 — 新闻爬虫 + Web 服务后台运行
注册为系统服务后可开机自启、崩溃自动重启、无窗口后台运行。

安装:   python news_service.py install
启动:   python news_service.py start
停止:   python news_service.py stop
卸载:   python news_service.py remove
状态:   python news_service.py status
"""
import sys
import os
import time
import threading
import logging
import logging.handlers

# 确保项目目录在 path 中
SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SERVICE_DIR)
os.chdir(SERVICE_DIR)

import win32serviceutil
import win32service
import win32event
import servicemanager


class NewsCrawlerService(win32serviceutil.ServiceFramework):
    _svc_name_ = "NewsCrawlerService"
    _svc_display_name_ = "News Crawler Service"
    _svc_description_ = (
        "新闻聚合爬虫服务：每小时自动爬取 15 个新闻源 TOP 10 热点，"
        "同时提供 Web 浏览界面 (http://127.0.0.1:5000)"
    )

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.is_alive = True
        self._scheduler = None
        self._web_thread = None

    def SvcStop(self):
        """服务停止"""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.is_alive = False
        win32event.SetEvent(self.stop_event)
        self._log("服务正在停止...")

        # 停止调度器
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass

        self._log("服务已停止")

    def SvcDoRun(self):
        """服务主入口"""
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self._log("服务启动中...")
        self._setup_logging()
        self._run()

    def _log(self, msg):
        """写入 Windows 事件日志"""
        servicemanager.LogInfoMsg(f"[{self._svc_name_}] {msg}")

    def _setup_logging(self):
        """配置文件日志（带轮转，防止磁盘耗尽）"""
        from config import LOG_FILE, LOG_LEVEL, LOG_FORMAT, LOG_DIR
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        logging.basicConfig(
            level=getattr(logging, LOG_LEVEL, logging.INFO),
            format=LOG_FORMAT,
            handlers=[handler],
        )

    def _run(self):
        """主运行逻辑：启动调度器 + Web 服务"""
        from models import init_db
        from config import CRAWL_INTERVAL_HOURS

        init_db()
        self._log("数据库初始化完成")

        # 1. 启动 Web 服务（独立线程）
        self._web_thread = threading.Thread(target=self._run_web, daemon=True)
        self._web_thread.start()
        self._log("Web 服务已启动 (http://127.0.0.1:5000)")

        # 2. 启动调度器（当前线程）
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.interval import IntervalTrigger
            from apscheduler.triggers.cron import CronTrigger
            from main import run_single_crawl
            from storage import cleanup

            self._scheduler = BackgroundScheduler()

            # 每 N 小时爬取
            self._scheduler.add_job(
                run_single_crawl,
                trigger=IntervalTrigger(hours=CRAWL_INTERVAL_HOURS),
                id="news_crawl",
                name="新闻爬取",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=600,
            )

            # 每天凌晨 3 点清理
            self._scheduler.add_job(
                cleanup,
                trigger=CronTrigger(hour=3, minute=0),
                id="data_cleanup",
                name="数据清理",
                max_instances=1,
                coalesce=True,
            )

            self._scheduler.start()
            self._log(f"调度器已启动 | 爬取间隔: {CRAWL_INTERVAL_HOURS} 小时")

            # 立即执行首次爬取
            self._log("执行首次爬取...")
            try:
                run_single_crawl()
                self._log("首次爬取完成")
            except Exception as e:
                self._log(f"首次爬取异常: {e}")

            # 等待停止信号
            while self.is_alive:
                rc = win32event.WaitForSingleObject(self.stop_event, 5000)
                if rc == win32event.WAIT_OBJECT_0:
                    break

        except Exception as e:
            self._log(f"服务运行异常: {e}")
            logging.exception("服务运行异常")
        finally:
            if self._scheduler:
                try:
                    self._scheduler.shutdown(wait=False)
                except Exception:
                    pass

    def _run_web(self):
        """在独立线程中运行 Flask Web 服务（使用 waitress 生产级 WSGI 服务器）"""
        try:
            from web.app import app
            from config import WEB_HOST, WEB_PORT
            from waitress import serve

            self._log(f"Web 服务启动 (waitress) http://{WEB_HOST}:{WEB_PORT}")
            serve(app, host=WEB_HOST, port=WEB_PORT, threads=4)
        except Exception as e:
            self._log(f"Web 服务异常: {e}")
            logging.exception("Web 服务异常")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # 无参数时，作为服务分发器启动（由 SCM 调用）
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(NewsCrawlerService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        # 有参数时，处理 install/start/stop/remove 等命令
        win32serviceutil.HandleCommandLine(NewsCrawlerService)
