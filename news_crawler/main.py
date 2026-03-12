"""
新闻爬虫主入口 - 爬取→去重→存储→JSON留档
"""
import sys
import os
import logging
import logging.handlers
import importlib
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from typing import NamedTuple, Optional
from config import SITES, MAX_WORKERS, LOG_FILE, LOG_DIR, LOG_LEVEL, LOG_FORMAT
from models import init_db
from storage import save_to_db, save_to_json, log_crawl_start, log_crawl_end


class CrawlResult(NamedTuple):
    """单个站点的爬取结果"""
    name: str                          # 站点显示名
    module: str                        # 模块标识
    items: list[dict]                  # 爬取到的新闻条目
    error: Optional[Exception]         # 异常（成功时为 None）


def setup_logging():
    """配置日志系统：按天轮转 + 错误单独文件"""
    os.makedirs(LOG_DIR, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, LOG_LEVEL))

    # 避免重复添加 handler
    if root_logger.handlers:
        return

    formatter = logging.Formatter(LOG_FORMAT)

    # 按天轮转主日志，保留 30 天
    main_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    main_handler.setLevel(logging.INFO)
    main_handler.setFormatter(formatter)
    main_handler.suffix = "%Y-%m-%d"

    # 错误单独日志
    error_log = os.path.join(LOG_DIR, "error.log")
    error_handler = logging.handlers.TimedRotatingFileHandler(
        error_log,
        when="midnight",
        interval=1,
        backupCount=90,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    error_handler.suffix = "%Y-%m-%d"

    # 控制台
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    root_logger.addHandler(main_handler)
    root_logger.addHandler(error_handler)
    root_logger.addHandler(console_handler)


def load_crawler(module_name: str):
    """动态加载爬虫模块，返回爬虫实例"""
    module = importlib.import_module(f"crawlers.{module_name}")

    # 找到模块中继承 BaseCrawler 的类
    from crawlers.base import BaseCrawler
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (isinstance(attr, type)
                and issubclass(attr, BaseCrawler)
                and attr is not BaseCrawler):
            return attr()

    raise ImportError(f"模块 crawlers.{module_name} 中未找到爬虫类")


def crawl_with_retry(name: str, mod: str,
                     max_retries: int = 3) -> CrawlResult:
    """
    带指数退避的爬取（退避时间较短，避免长时间阻塞线程池 worker）：
    第 1 次失败 → 等 5s
    第 2 次失败 → 等 10s
    第 3 次失败 → 放弃
    """
    logger = logging.getLogger("main")
    # 在重试循环外创建实例，保持 session 连续性
    crawler = load_crawler(mod)

    for attempt in range(1, max_retries + 1):
        start_ms = time.monotonic()
        log_id = log_crawl_start(mod, name)

        try:
            results = crawler.run()
            duration = int((time.monotonic() - start_ms) * 1000)

            if results:
                log_crawl_end(log_id, "success", len(results),
                              duration_ms=duration)
                return CrawlResult(name, mod, results, None)
            else:
                log_crawl_end(log_id, "empty", 0,
                              error_msg="返回 0 条结果", duration_ms=duration)
                return CrawlResult(name, mod, [], None)

        except Exception as e:
            duration = int((time.monotonic() - start_ms) * 1000)
            log_crawl_end(log_id, "failed", 0,
                          error_msg=str(e)[:500], duration_ms=duration)

            if attempt < max_retries:
                wait = 5 * (2 ** (attempt - 1))  # 5s, 10s, 20s
                logger.warning(
                    f"  [RETRY] {name} 第{attempt}次失败, "
                    f"{wait}s 后重试: {e}"
                )
                time.sleep(wait)
            else:
                logger.error(
                    f"  [GIVE UP] {name} 连续{max_retries}次失败: {e}"
                )
                return CrawlResult(name, mod, [], e)

    return CrawlResult(name, mod, [], Exception("超过最大重试次数"))


def run_single_crawl():
    """执行一轮爬取"""
    logger = logging.getLogger("main")
    crawl_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    logger.info("=" * 60)
    logger.info(f"开始新一轮爬取 | {crawl_time}")
    logger.info("=" * 60)

    # 加载所有启用的爬虫
    enabled_sites = [(s.display_name, s.module) for s in SITES if s.enabled]
    all_results = []
    success_count = 0
    fail_list = []

    # 并发爬取（每个站点内部自带重试）
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(crawl_with_retry, name, mod): (name, mod)
            for name, mod in enabled_sites
        }

        for future in as_completed(futures):
            result = future.result()
            if result.error:
                logger.error(f"  [FAIL] {result.name} ({result.module}): {result.error}")
                fail_list.append(result.name)
            elif result.items:
                logger.info(f"  [ OK ] {result.name}: {len(result.items)} 条")
                all_results.extend(result.items)
                success_count += 1
            else:
                # 0 条结果不算失败（可能该站点确实无新内容），仅警告
                logger.warning(f"  [WARN] {result.name}: 0 条（无新内容或被反爬）")
                success_count += 1  # 请求成功但无数据，不计入失败

    # 存储
    if all_results:
        new_count = save_to_db(all_results)
        json_path = save_to_json(all_results, crawl_time)
        logger.info("-" * 60)
        logger.info(f"本轮汇总:")
        logger.info(f"  成功站点: {success_count}/{len(enabled_sites)}")
        logger.info(f"  爬取总数: {len(all_results)} 条")
        logger.info(f"  新增入库: {new_count} 条")
        logger.info(f"  JSON归档: {json_path}")
        if fail_list:
            logger.info(f"  失败站点: {', '.join(fail_list)}")
    else:
        logger.warning("本轮未获取到任何新闻")

    logger.info("=" * 60)
    return all_results


def main():
    setup_logging()
    init_db()
    run_single_crawl()


if __name__ == "__main__":
    main()
