"""
新闻爬虫全局配置

敏感配置（密码、Token、服务器地址）通过环境变量读取，优先级：
  环境变量 > 代码默认值（仅用于本地开发，生产环境必须通过 .env 或系统环境变量覆盖）

本地开发：复制 .env.example 为 .env，填入真实值
服务器部署：deploy.sh 自动生成 /opt/news-radar/.env，或手动配置系统环境变量
"""
import os
from typing import NamedTuple


def _env(key: str, default: str = "") -> str:
    """读取环境变量，未设置时返回默认值"""
    return os.environ.get(key, default)

# ============ 路径配置 ============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
JSON_DIR = os.path.join(DATA_DIR, "json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
DB_PATH = os.path.join(DATA_DIR, "news.db")

# ============ 爬取配置 ============
CRAWL_INTERVAL_HOURS = 1          # 爬取间隔（小时）
CRAWL_TIMEOUT = 15                # 单次请求超时（秒）
CRAWL_RETRY = 3                   # 失败重试次数
CRAWL_DELAY = (1, 3)              # 请求间隔随机范围（秒）
MAX_WORKERS = 5                   # 并发线程数（2GB内存服务器，限制峰值线程）

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
    # 国外（当前网络环境无法访问，暂时关闭。配置代理后改回 True 即可恢复）
    SiteConfig("CNN",       "cnn",       "en", False),
    SiteConfig("BBC",       "bbc",       "en", False),
    SiteConfig("Reuters",   "reuters",   "en", False),
    SiteConfig("AP News",   "ap",        "en", False),
    SiteConfig("NYT",       "nyt",       "en", False),
]

# ============ Web 服务配置 ============
# WEB_HOST = "127.0.0.1"  # 只允许本机访问
WEB_HOST = "0.0.0.0"      # 允许外网访问（部署到阿里云时使用）
WEB_PORT = 8888            # Web 服务监听端口，浏览器访问 http://服务器IP:8888
WEB_DEBUG = False

# ============ 媒体配置 ============
MEDIA_DIR = os.path.join(DATA_DIR, "media")
MEDIA_IMAGE_DIR = os.path.join(MEDIA_DIR, "images")
MEDIA_MAX_IMAGES = 20             # 每篇文章最多保存图片数
MEDIA_MIN_IMAGE_SIZE = 100        # 最小图片尺寸（像素），过滤 logo/icon
MEDIA_IMAGE_MAX_WIDTH = 1200      # 下载图片最大宽度，超过则压缩
MEDIA_DOWNLOAD_TIMEOUT = 10       # 图片下载超时（秒）
DETAIL_FETCH_TIMEOUT = 30         # 详情页抓取超时（秒），含Playwright渲染需要足够时间
DETAIL_MAX_WORKERS = 2            # 详情页并发抓取线程数（2GB内存服务器）
                                  # MAX_WORKERS=5 × DETAIL_MAX_WORKERS=2 = 最多10个详情页线程

# ============ 数据保留 ============
DATA_RETAIN_DAYS = 2              # JSON 文件保留天数

# ============ 微信推送配置（PushPlus） ============
# 获取 Token：访问 https://www.pushplus.plus 微信扫码登录后复制 Token
PUSH_ENABLED = _env("PUSH_ENABLED", "true").lower() == "true"
PUSH_TOKEN = _env("PUSH_TOKEN", "")  # 必须通过 .env 或环境变量设置，不要在代码中写真实 Token
PUSH_TOPIC = _env("PUSH_TOPIC", "")        # 群组编码（留空=只推给自己）

# 每轮爬取汇总推送
PUSH_SUMMARY_ENABLED = True         # 是否推送每轮爬取汇总
PUSH_SUMMARY_COUNT = 1              # 每轮汇总推送条数（免费版200条/天，24轮×1条=24条/天）

# 重大新闻即时推送（标题命中关键词就推）
PUSH_BREAKING_ENABLED = True        # 是否推送重大新闻
PUSH_BREAKING_MAX_PER_ROUND = 3     # 每轮最多推送重大新闻条数（24轮×3条=72条/天，加汇总共96条，安全）
PUSH_BREAKING_KEYWORDS = [
    # 战争/冲突（真正重大）
    "开战", "宣战", "核武", "核攻击", "军事冲突",
    # 重大灾害
    "地震", "海啸", "坠机", "沉船",
    # 经济危机
    "股市熔断", "金融危机", "经济崩溃",
    # 政治
    "政变", "弹劾", "紧急状态",
    # 英文（真正重大）
    "earthquake", "tsunami", "nuclear",
]

# ============ 远程同步配置 ============
SYNC_ENABLED = _env("SYNC_ENABLED", "false").lower() == "true"  # 默认关闭，需显式开启
SYNC_SERVER_URL = _env("SYNC_SERVER_URL", "")          # 远程服务器地址，如 http://1.2.3.4
SYNC_USERNAME = _env("SYNC_USERNAME", "")              # 后端登录用户名
SYNC_PASSWORD = _env("SYNC_PASSWORD", "")              # 后端登录密码
SYNC_API_TOKEN = ""                                    # （已废弃，保留兼容）
SYNC_BATCH_SIZE = 50                                   # 每批推送条数

# ============ 日志配置 ============
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
LOG_FILE = os.path.join(LOG_DIR, "crawler.log")
