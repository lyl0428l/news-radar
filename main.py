"""
新闻爬虫主入口 - 爬取→去重→存储→JSON留档
"""
import sys                          # 系统相关功能（如修改模块搜索路径、标准输出流）
import os                           # 操作系统接口（如路径拼接、创建目录）
import logging                      # Python 标准日志模块
import logging.handlers             # 日志处理器扩展（如按时间轮转的文件处理器）
import importlib                    # 动态导入模块（运行时按名称加载爬虫）
import time                         # 时间相关功能（如 sleep 等待、计时）
from datetime import datetime       # 日期时间类，用于获取当前时间并格式化

# ThreadPoolExecutor: 线程池，实现多站点并发爬取
# as_completed: 按完成顺序迭代 future 结果
from concurrent.futures import ThreadPoolExecutor, as_completed

# 将当前文件所在目录加入模块搜索路径，确保能正确导入同级包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from typing import NamedTuple, Optional  # NamedTuple: 具名元组基类; Optional: 可选类型标注

# 从项目配置文件导入：
#   SITES - 所有站点配置列表
#   MAX_WORKERS - 线程池最大并发数
#   LOG_FILE - 主日志文件路径
#   LOG_DIR - 日志目录路径
#   LOG_LEVEL - 日志级别（如 DEBUG/INFO）
#   LOG_FORMAT - 日志输出格式模板
from config import SITES, MAX_WORKERS, LOG_FILE, LOG_DIR, LOG_LEVEL, LOG_FORMAT

from models import init_db          # 数据库初始化函数（建表等）

# 从存储模块导入：
#   save_to_db - 将新闻存入数据库（自动去重）
#   save_to_json - 将新闻保存为 JSON 文件归档
#   log_crawl_start - 记录一次爬取的开始
#   log_crawl_end - 记录一次爬取的结束（含状态、耗时等）
from storage import save_to_db, save_to_json, log_crawl_start, log_crawl_end


# ==================== 数据结构定义 ====================

class CrawlResult(NamedTuple):
    """单个站点的爬取结果（不可变具名元组）"""
    name: str                          # 站点显示名，如 "新浪新闻"
    module: str                        # 模块标识，如 "sina"
    items: list[dict]                  # 爬取到的新闻条目列表，每条是一个字典
    error: Optional[Exception]         # 异常对象，成功时为 None，失败时携带具体异常


# ==================== 日志配置 ====================

def setup_logging():
    """配置日志系统：按天轮转 + 错误单独文件"""

    os.makedirs(LOG_DIR, exist_ok=True)                    # 创建日志目录，已存在则不报错

    root_logger = logging.getLogger()                      # 获取根日志记录器（全局唯一）
    root_logger.setLevel(getattr(logging, LOG_LEVEL))      # 根据配置动态设置日志级别（如 DEBUG/INFO）

    # 如果已经添加过 handler，直接返回，避免重复注册导致日志重复输出
    if root_logger.handlers:
        return

    formatter = logging.Formatter(LOG_FORMAT)              # 创建统一的日志格式化器

    # ---------- 主日志文件（按天轮转，保留 30 天） ----------
    main_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE,                                          # 主日志文件路径
        when="midnight",                                   # 每天午夜（00:00）触发轮转
        interval=1,                                        # 间隔 1 天轮转一次
        backupCount=30,                                    # 最多保留 30 个历史备份文件
        encoding="utf-8",                                  # 使用 UTF-8 编码写入
    )
    main_handler.setLevel(logging.INFO)                    # 主日志只记录 INFO 及以上级别
    main_handler.setFormatter(formatter)                   # 应用日志格式
    main_handler.suffix = "%Y-%m-%d"                       # 轮转后的文件名后缀，如 crawler.log.2026-03-13

    # ---------- 错误日志文件（单独记录，保留 90 天） ----------
    error_log = os.path.join(LOG_DIR, "error.log")         # 拼接错误日志文件完整路径
    error_handler = logging.handlers.TimedRotatingFileHandler(
        error_log,                                         # 错误日志文件路径
        when="midnight",                                   # 每天午夜触发轮转
        interval=1,                                        # 间隔 1 天
        backupCount=90,                                    # 最多保留 90 个历史备份（3个月）
        encoding="utf-8",                                  # UTF-8 编码
    )
    error_handler.setLevel(logging.ERROR)                  # 只记录 ERROR 及以上级别的日志
    error_handler.setFormatter(formatter)                  # 应用日志格式
    error_handler.suffix = "%Y-%m-%d"                      # 轮转文件后缀

    # ---------- 控制台输出 ----------
    console_handler = logging.StreamHandler(sys.stdout)    # 创建标准输出流处理器（打印到终端）
    console_handler.setLevel(logging.INFO)                 # 控制台只输出 INFO 及以上级别
    console_handler.setFormatter(formatter)                # 应用日志格式

    root_logger.addHandler(main_handler)                   # 将主日志处理器注册到根记录器
    root_logger.addHandler(error_handler)                  # 将错误日志处理器注册到根记录器
    root_logger.addHandler(console_handler)                # 将控制台处理器注册到根记录器


# ==================== 爬虫加载 ====================

def load_crawler(module_name: str):
    """动态加载爬虫模块，返回爬虫实例"""

    # 根据模块名动态导入 crawlers 包下的对应模块（如 crawlers.sina）
    module = importlib.import_module(f"crawlers.{module_name}")

    from crawlers.base import BaseCrawler                  # 导入爬虫基类，用于类型判断
    for attr_name in dir(module):                          # 遍历模块中的所有属性名称
        attr = getattr(module, attr_name)                  # 获取属性的实际对象
        if (isinstance(attr, type)                         # 判断是否是一个类（而非函数/变量）
                and issubclass(attr, BaseCrawler)          # 判断是否继承自 BaseCrawler
                and attr is not BaseCrawler):              # 排除 BaseCrawler 自身（只要子类）
            return attr()                                  # 找到后立即实例化并返回

    # 遍历完都没找到合适的爬虫类，抛出导入错误
    raise ImportError(f"模块 crawlers.{module_name} 中未找到爬虫类")


# ==================== 带重试的爬取 ====================

def crawl_with_retry(name: str, mod: str,
                     max_retries: int = 3) -> CrawlResult:
    """
    带指数退避的爬取（退避时间较短，避免长时间阻塞线程池 worker）：
    第 1 次失败 → 等 5s
    第 2 次失败 → 等 10s
    第 3 次失败 → 放弃
    """
    logger = logging.getLogger("main")                     # 获取名为 "main" 的日志记录器
    # 在重试循环外创建爬虫实例，保持 session/cookie 的连续性
    crawler = load_crawler(mod)                             # 加载并实例化对应的爬虫

    for attempt in range(1, max_retries + 1):              # 从第 1 次尝试到第 max_retries 次
        start_ms = time.monotonic()                        # 记录本次尝试的起始时间（单调时钟，不受系统时间调整影响）
        log_id = log_crawl_start(mod, name)                # 在数据库中记录本次爬取开始，返回记录 ID

        try:
            results = crawler.run()                        # 执行爬虫的 run() 方法，获取新闻列表
            duration = int((time.monotonic() - start_ms) * 1000)  # 计算本次爬取耗时（转为毫秒）

            if results:                                    # 如果爬取到了数据（列表非空）
                log_crawl_end(log_id, "success", len(results),
                              duration_ms=duration)        # 记录爬取成功：状态、条目数、耗时
                return CrawlResult(name, mod, results, None)  # 返回成功结果，error 为 None

            else:                                          # 爬取成功但结果为空（0条）
                log_crawl_end(log_id, "empty", 0,
                              error_msg="返回 0 条结果", duration_ms=duration)  # 记录空结果
                return CrawlResult(name, mod, [], None)    # 返回空列表，error 仍为 None（不算失败）

        except Exception as e:                             # 捕获爬取过程中的所有异常
            duration = int((time.monotonic() - start_ms) * 1000)  # 计算本次耗时
            log_crawl_end(log_id, "failed", 0,
                          error_msg=str(e)[:500], duration_ms=duration)  # 记录失败日志（错误信息截断500字符防过长）

            if attempt < max_retries:                      # 如果还没用完所有重试机会
                wait = 5 * (2 ** (attempt - 1))            # 计算指数退避等待时间：5s → 10s → 20s
                logger.warning(
                    f"  [RETRY] {name} 第{attempt}次失败, "
                    f"{wait}s 后重试: {e}"                  # 输出重试警告日志
                )
                time.sleep(wait)                           # 阻塞等待指定秒数后进入下一次重试
            else:                                          # 已用完所有重试机会
                logger.error(
                    f"  [GIVE UP] {name} 连续{max_retries}次失败: {e}"  # 输出放弃错误日志
                )
                return CrawlResult(name, mod, [], e)       # 返回失败结果，携带最后一次的异常对象

    # 理论上不会执行到这里（for 循环内必定 return），作为兜底保护
    return CrawlResult(name, mod, [], Exception("超过最大重试次数"))


# ==================== 单轮爬取主流程 ====================

def run_single_crawl():
    """执行一轮完整的爬取流程：并发爬取 → 汇总结果 → 存储入库 + JSON归档"""

    logger = logging.getLogger("main")                     # 获取 "main" 日志记录器
    crawl_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # 获取当前时间并格式化为字符串

    logger.info("=" * 60)                                  # 输出分隔线（60个等号）
    logger.info(f"开始新一轮爬取 | {crawl_time}")            # 输出本轮爬取的开始时间
    logger.info("=" * 60)                                  # 输出分隔线

    # 从配置中筛选出所有启用的站点，提取 (显示名, 模块名) 元组列表
    enabled_sites = [(s.display_name, s.module) for s in SITES if s.enabled]
    all_results = []                                       # 存放本轮所有站点爬取到的新闻条目
    success_count = 0                                      # 成功站点计数器
    fail_list = []                                         # 失败站点名称列表

    # ---------- 并发爬取（线程池） ----------
    # 创建线程池，最大并发线程数由 MAX_WORKERS 配置决定
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 为每个启用的站点提交一个爬取任务到线程池
        # futures 字典：key 是 Future 对象，value 是 (站点名, 模块名) 元组
        futures = {
            executor.submit(crawl_with_retry, name, mod): (name, mod)
            for name, mod in enabled_sites
        }

        # 按任务完成的先后顺序迭代结果（先完成的先处理）
        for future in as_completed(futures):
            try:
                result = future.result()                   # 获取该站点的爬取结果（CrawlResult）
            except Exception as exc:
                name, mod = futures[future]
                logger.error(f"  [CRASH] {name} ({mod}) 线程异常: {exc}")
                fail_list.append(name)
                continue

            if result.error:                               # 如果有异常 → 该站点爬取彻底失败
                logger.error(f"  [FAIL] {result.name} ({result.module}): {result.error}")  # 输出失败日志
                fail_list.append(result.name)              # 将失败站点名加入列表

            elif result.items:                             # 爬取成功且有数据
                logger.info(f"  [ OK ] {result.name}: {len(result.items)} 条")  # 输出成功日志和条目数
                all_results.extend(result.items)           # 将该站点的新闻条目合并到总结果列表
                success_count += 1                         # 成功站点计数 +1

            else:                                          # 爬取成功但返回 0 条数据
                # 0 条结果不算失败（可能该站点确实无新内容），仅输出警告
                logger.warning(f"  [WARN] {result.name}: 0 条（无新内容或被反爬）")
                success_count += 1                         # 请求本身成功，不计入失败

    # ---------- 存储阶段 ----------
    if all_results:                                        # 如果本轮有爬取到数据
        new_count = save_to_db(all_results)                # 将所有新闻存入数据库，返回去重后的新增条数
        json_path = save_to_json(all_results, crawl_time)  # 将所有新闻保存为 JSON 文件，返回文件路径

        logger.info("-" * 60)                              # 输出分隔线（60个短横线）
        logger.info(f"本轮汇总:")                            # 输出汇总标题
        logger.info(f"  成功站点: {success_count}/{len(enabled_sites)}")  # 成功数/总数
        logger.info(f"  爬取总数: {len(all_results)} 条")    # 本轮爬取到的总条数
        logger.info(f"  新增入库: {new_count} 条")            # 去重后实际新增入库的条数
        logger.info(f"  JSON归档: {json_path}")               # JSON 归档文件的保存路径

        if fail_list:                                      # 如果有失败的站点
            logger.info(f"  失败站点: {', '.join(fail_list)}")  # 输出所有失败站点名称（逗号分隔）

        # ---------- 远程同步 ----------
        try:
            from config import SYNC_ENABLED, SYNC_SERVER_URL, SYNC_USERNAME, SYNC_PASSWORD
            if SYNC_ENABLED and SYNC_SERVER_URL:
                from utils.sync_remote import sync_to_remote
                sync_result = sync_to_remote(
                    all_results,
                    server_url=SYNC_SERVER_URL,
                    username=SYNC_USERNAME,
                    password=SYNC_PASSWORD,
                )
                if sync_result["ok"]:
                    logger.info(f"  远程同步: 成功推送 {sync_result['total_sent']} 条, "
                                f"服务器新增 {sync_result['total_inserted']} 条")
                else:
                    logger.warning(f"  远程同步: 部分失败 | {sync_result['errors']}")
        except Exception as e:
            logger.debug(f"远程同步失败（不影响正常爬取）: {e}")

        # ---------- 微信推送 ----------
        try:
            from utils.notify import push_crawl_summary, check_and_push_breaking
            # 1. 重大新闻即时推送（优先，让用户第一时间看到）
            check_and_push_breaking(all_results)
            # 2. 每轮爬取汇总推送
            push_crawl_summary(
                all_results, success_count, len(enabled_sites), fail_list
            )
        except Exception as e:
            logger.debug(f"推送失败（不影响正常爬取）: {e}")

    else:                                                  # 本轮没有爬取到任何数据
        logger.warning("本轮未获取到任何新闻")                 # 输出警告

    logger.info("=" * 60)                                  # 输出结束分隔线
    return all_results                                     # 返回本轮所有爬取结果列表


# ==================== 程序主入口 ====================

def main():
    """程序主入口函数，依次完成：初始化日志 → 初始化数据库 → 执行一轮爬取"""
    setup_logging()                                        # 初始化日志系统（配置文件日志 + 控制台输出）
    init_db()                                              # 初始化数据库（创建表结构等）
    run_single_crawl()                                     # 执行一轮完整的新闻爬取流程


# 当直接运行本文件时（而非被其他模块 import 时），执行 main() 函数
if __name__ == "__main__":
    main()
