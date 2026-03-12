"""
定时调度器 - 每小时自动执行爬取任务
"""
import sys
import os
import signal
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from config import CRAWL_INTERVAL_HOURS
from main import setup_logging, run_single_crawl
from models import init_db
from storage import cleanup

# PID 文件路径，防止重复启动
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "crawler.lock")

# 全局锁文件句柄，进程存活期间保持打开
_lock_handle = None


def acquire_lock() -> bool:
    """
    尝试获取文件锁，防止重复启动。
    利用 Windows 的文件独占锁：进程存活时锁住文件，
    进程退出（包括崩溃）后系统自动释放，不会误判。
    """
    global _lock_handle
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    try:
        import msvcrt
        _lock_handle = open(LOCK_FILE, "w")
        msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        _lock_handle.write(str(os.getpid()))
        _lock_handle.flush()
        return True
    except (OSError, IOError):
        # 无法锁定 = 另一个实例正在运行
        return False
    except ImportError:
        # 非 Windows 平台，用 fcntl
        try:
            import fcntl
            _lock_handle = open(LOCK_FILE, "w")
            fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _lock_handle.write(str(os.getpid()))
            _lock_handle.flush()
            return True
        except (OSError, IOError, ImportError):
            return False


def release_lock():
    """释放文件锁"""
    global _lock_handle
    if _lock_handle:
        try:
            _lock_handle.close()
        except OSError:
            pass
        _lock_handle = None
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def main():
    setup_logging()
    init_db()
    logger = logging.getLogger("scheduler")

    # 防止重复启动（文件锁方式，崩溃后自动释放）
    if not acquire_lock():
        logger.error("检测到另一个调度器实例正在运行，退出。")
        sys.exit(1)

    scheduler = BlockingScheduler()

    # 每 N 小时执行一次爬取
    scheduler.add_job(
        run_single_crawl,
        trigger=IntervalTrigger(hours=CRAWL_INTERVAL_HOURS),
        id="news_crawl",
        name="新闻爬取任务",
        max_instances=1,          # 防止重复执行
        coalesce=True,            # 错过的任务合并执行
        misfire_grace_time=600,   # 10 分钟容错
    )

    # 每天凌晨 3 点清理过期数据
    scheduler.add_job(
        cleanup,
        trigger=CronTrigger(hour=3, minute=0),
        id="data_cleanup",
        name="数据清理任务",
        max_instances=1,
        coalesce=True,
    )

    # 优雅退出
    def shutdown(signum, frame):
        logger.info("收到退出信号，正在关闭调度器...")
        scheduler.shutdown(wait=False)
        release_lock()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    # Windows 不支持 SIGTERM，但在 Linux/Mac 上需要处理 kill 默认信号
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    # 启动时立即执行一次
    logger.info("=" * 60)
    logger.info(f"新闻爬虫调度器启动 | 间隔: {CRAWL_INTERVAL_HOURS} 小时")
    logger.info("=" * 60)
    logger.info("执行首次爬取...")
    run_single_crawl()

    logger.info(f"定时任务已注册:")
    logger.info(f"  - 爬取任务: 每 {CRAWL_INTERVAL_HOURS} 小时")
    logger.info(f"  - 清理任务: 每天凌晨 3:00")
    logger.info("按 Ctrl+C 停止调度器")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器已停止")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
