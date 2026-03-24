"""
日志配置模块 - 集中管理日志设置
"""
import os
import logging
import logging.handlers
from config import LOG_DIR, LOG_FILE, LOG_LEVEL, LOG_FORMAT


def setup_logging(level=None, log_file=None):
    """
    配置日志系统：按天轮转 + 错误单独文件 + 控制台输出
    
    参数:
        level: 日志级别，覆盖配置文件中的 LOG_LEVEL
        log_file: 日志文件路径，覆盖配置文件中的 LOG_FILE
    """
    # 使用参数或配置文件中的值
    if level is None:
        level = LOG_LEVEL
    if log_file is None:
        log_file = LOG_FILE
    
    # 创建日志目录
    os.makedirs(LOG_DIR, exist_ok=True)
    
    # 获取根日志记录器
    root_logger = logging.getLogger()
    
    # 如果已经配置过，直接返回
    if root_logger.handlers:
        return
    
    # 设置日志级别
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # 创建格式化器
    formatter = logging.Formatter(LOG_FORMAT)
    
    # ========== 主日志文件（按天轮转，保留30天） ==========
    main_handler = logging.handlers.TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    main_handler.setLevel(logging.INFO)
    main_handler.setFormatter(formatter)
    main_handler.suffix = "%Y-%m-%d"
    
    # ========== 错误日志文件（单独记录，保留90天） ==========
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
    
    # ========== 调试日志文件（仅在DEBUG级别时启用） ==========
    if level.upper() == "DEBUG":
        debug_log = os.path.join(LOG_DIR, "debug.log")
        debug_handler = logging.handlers.TimedRotatingFileHandler(
            debug_log,
            when="midnight",
            interval=1,
            backupCount=7,  # 调试日志只保留7天
            encoding="utf-8",
        )
        debug_handler.setLevel(logging.DEBUG)
        debug_handler.setFormatter(formatter)
        debug_handler.suffix = "%Y-%m-%d"
        root_logger.addHandler(debug_handler)
    
    # ========== 控制台输出 ==========
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    # 控制台使用简化格式
    console_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_formatter)
    
    # 注册处理器
    root_logger.addHandler(main_handler)
    root_logger.addHandler(error_handler)
    root_logger.addHandler(console_handler)


def get_logger(name):
    """
    获取指定名称的日志记录器
    
    参数:
        name: 日志记录器名称，通常使用 __name__
    
    返回:
        logging.Logger 实例
    """
    return logging.getLogger(name)