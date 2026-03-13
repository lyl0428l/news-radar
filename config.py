"""
新闻爬虫全局配置
"""
import os
from typing import NamedTuple

# ============ 路径配置 ============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
JSON_DIR = os.path.join(DATA_DIR, "json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
DB_PATH = os.path.join(DATA_DIR, "news.db")

# ============ 爬取配置 ============
CRAWL_INTERVAL_HOURS = 1          # 爬取间隔（小时）
CRAWL_TIMEOUT = 15                # 单次请求超时（秒）
CRAWL_RETRY = 2                   # 失败重试次数
CRAWL_DELAY = (1, 3)              # 请求间隔随机范围（秒）
MAX_WORKERS = 8                   # 并发线程数（15 站点分 2 批即可完成）

# ============ User-Agent 池 ============
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# ============ 网站列表 ============

class SiteConfig(NamedTuple):
    """站点配置：显示名称、模块名、语言代码、是否启用"""
    display_name: str
    module: str
    language: str
    enabled: bool


SITES: list[SiteConfig] = [
    # 国内
    SiteConfig("新浪新闻",   "sina",      "zh", True),
    SiteConfig("网易新闻",   "netease",   "zh", True),
    SiteConfig("腾讯新闻",   "tencent",   "zh", True),
    SiteConfig("搜狐新闻",   "sohu",      "zh", True),
    SiteConfig("凤凰新闻",   "ifeng",     "zh", True),
    SiteConfig("新华网",     "xinhua",    "zh", True),
    SiteConfig("人民网",     "people",    "zh", True),
    SiteConfig("央视新闻",   "cctv",      "zh", True),
    SiteConfig("澎湃新闻",   "thepaper",  "zh", True),
    SiteConfig("界面新闻",   "jiemian",   "zh", True),
    # 国外
    SiteConfig("CNN",       "cnn",       "en", True),
    SiteConfig("BBC",       "bbc",       "en", True),
    SiteConfig("Reuters",   "reuters",   "en", True),
    SiteConfig("AP News",   "ap",        "en", True),
    SiteConfig("NYT",       "nyt",       "en", True),
]

# ============ Web 服务配置 ============
WEB_HOST = "127.0.0.1"
WEB_PORT = 5000
WEB_DEBUG = False

# ============ 媒体配置 ============
MEDIA_DIR = os.path.join(DATA_DIR, "media")
MEDIA_IMAGE_DIR = os.path.join(MEDIA_DIR, "images")
MEDIA_MAX_IMAGES = 20             # 每篇文章最多保存图片数
MEDIA_MIN_IMAGE_SIZE = 100        # 最小图片尺寸（像素），过滤 logo/icon
MEDIA_IMAGE_MAX_WIDTH = 1200      # 下载图片最大宽度，超过则压缩
MEDIA_DOWNLOAD_TIMEOUT = 10       # 图片下载超时（秒）
DETAIL_FETCH_TIMEOUT = 12         # 详情页抓取超时（秒）
DETAIL_MAX_WORKERS = 5            # 详情页并发抓取线程数

# ============ 数据保留 ============
DATA_RETAIN_DAYS = 30             # JSON 文件保留天数

# ============ 日志配置 ============
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
LOG_FILE = os.path.join(LOG_DIR, "crawler.log")
