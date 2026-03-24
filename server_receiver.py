"""
服务器端 - 新闻数据接收 + 存储 + 查询API
部署在阿里云服务器上，接收本地爬虫推送的数据

功能：
  POST /api/push    - 接收本地推送的新闻数据（批量写入）
  GET  /api/news    - 查询新闻列表（支持筛选/分页）
  GET  /api/stats   - 查看统计信息
  GET  /api/health  - 健康检查
  GET  /             - 简单首页
"""
import os
import sys
import json
import sqlite3
import hashlib
import logging
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from flask import Flask, request, jsonify

# ============ 配置 ============
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "news.db")
API_PORT = 5000                    # 容器内端口（宿主机映射到8888）
API_TOKEN = "news-radar-2026"      # 推送鉴权Token（防止别人乱推数据）
LOG_LEVEL = "INFO"

# ============ 日志 ============
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(DB_DIR, "server.log"), encoding="utf-8")
        if os.path.exists(DB_DIR) else logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("server")

# ============ 数据库 ============
_db_lock = threading.Lock()

# URL去重时需要去除的跟踪参数
_URL_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "wfr", "isappinstalled", "wxshare", "nsukey",
    "scene", "clicktime", "enterid", "abtest", "ref", "source_id",
}


def normalize_url(url: str) -> str:
    """URL归一化：去跟踪参数、统一协议、去末尾斜杠"""
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
        scheme = "https"
        query_params = parse_qs(parsed.query, keep_blank_values=False)
        clean_params = {k: v for k, v in query_params.items() if k.lower() not in _URL_TRACKING_PARAMS}
        sorted_query = urlencode(clean_params, doseq=True)
        path = parsed.path.rstrip("/") if parsed.path != "/" else "/"
        return urlunparse((scheme, parsed.netloc.lower(), path, parsed.params, sorted_query, ""))
    except Exception:
        return url.strip()


def make_url_hash(url: str) -> str:
    """生成URL的SHA256哈希"""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def init_db():
    """初始化数据库"""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS news (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url_hash    TEXT UNIQUE,
                title       TEXT NOT NULL,
                url         TEXT NOT NULL,
                source      TEXT NOT NULL,
                source_name TEXT DEFAULT '',
                summary     TEXT DEFAULT '',
                content     TEXT DEFAULT '',
                content_html TEXT DEFAULT '',
                category    TEXT DEFAULT '',
                rank        INTEGER DEFAULT 0,
                pub_time    TEXT DEFAULT '',
                crawl_time  TEXT NOT NULL,
                language    TEXT DEFAULT 'zh',
                images      TEXT DEFAULT '[]',
                videos      TEXT DEFAULT '[]',
                thumbnail   TEXT DEFAULT '',
                author      TEXT DEFAULT '',
                extra_json  TEXT DEFAULT '{}',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_source ON news(source)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_crawl_time ON news(crawl_time)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_language ON news(language)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pub_time ON news(pub_time)")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_url_hash ON news(url_hash)")
        conn.commit()
        logger.info(f"数据库初始化完成: {DB_PATH}")
    finally:
        conn.close()


def _get_conn(readonly=False):
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    return conn


def save_news_batch(news_list: list) -> int:
    """批量写入新闻，URL哈希去重，返回新增条数"""
    if not news_list:
        return 0
    inserted = 0
    with _db_lock:
        conn = _get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN")
            for item in news_list:
                url = item.get("url", "")
                if not url or not item.get("title", ""):
                    continue
                url_hash = make_url_hash(url)
                images_json = json.dumps(item.get("images", []), ensure_ascii=False)
                videos_json = json.dumps(item.get("videos", []), ensure_ascii=False)
                try:
                    cursor.execute("""
                        INSERT OR IGNORE INTO news
                        (url_hash, title, url, source, source_name, summary,
                         content, content_html, category, rank, pub_time,
                         crawl_time, language, images, videos, thumbnail,
                         author, extra_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        url_hash,
                        item.get("title", "").strip(),
                        url.strip(),
                        item.get("source", ""),
                        item.get("source_name", ""),
                        item.get("summary", ""),
                        item.get("content", ""),
                        item.get("content_html", ""),
                        item.get("category", ""),
                        item.get("rank", 0),
                        item.get("pub_time", ""),
                        item.get("crawl_time", ""),
                        item.get("language", "zh"),
                        images_json,
                        videos_json,
                        item.get("thumbnail", ""),
                        item.get("author", ""),
                        json.dumps(item.get("extra", {}), ensure_ascii=False),
                    ))
                    if cursor.rowcount > 0:
                        inserted += 1
                except sqlite3.IntegrityError:
                    pass
                except sqlite3.Error as e:
                    logger.warning(f"写入失败: {e}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return inserted


# ============ Flask应用 ============
app = Flask(__name__)


@app.route("/")
def index():
    """首页"""
    conn = _get_conn(readonly=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM news")
        total = cursor.fetchone()[0]
        cursor.execute("SELECT MAX(crawl_time) FROM news")
        last = cursor.fetchone()[0] or "暂无数据"
    finally:
        conn.close()
    return jsonify({
        "service": "News Radar Server",
        "status": "running",
        "total_news": total,
        "last_update": last,
        "api": {
            "push_data": "POST /api/push",
            "query_news": "GET /api/news",
            "stats": "GET /api/stats",
            "health": "GET /api/health",
        }
    })


@app.route("/api/push", methods=["POST"])
def api_push():
    """接收本地推送的新闻数据"""
    # 鉴权
    token = request.headers.get("X-API-Token", "")
    if token != API_TOKEN:
        return jsonify({"ok": False, "msg": "鉴权失败：Token错误"}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "msg": "请求体为空或非JSON格式"}), 400

    news_list = data.get("items", [])
    if not news_list:
        return jsonify({"ok": False, "msg": "items为空"}), 400

    try:
        inserted = save_news_batch(news_list)
        logger.info(f"[PUSH] 接收 {len(news_list)} 条, 新增 {inserted} 条")
        return jsonify({
            "ok": True,
            "received": len(news_list),
            "inserted": inserted,
        })
    except Exception as e:
        logger.error(f"[PUSH] 写入失败: {e}")
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/news")
def api_news():
    """查询新闻列表"""
    source = request.args.get("source", "")
    language = request.args.get("language", "")
    keyword = request.args.get("keyword", "")
    start_time = request.args.get("start_time", "")
    end_time = request.args.get("end_time", "")
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = int(request.args.get("offset", 0))
    except (ValueError, TypeError):
        offset = 0

    conn = _get_conn(readonly=True)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        where = "WHERE 1=1"
        params = []
        if source:
            where += " AND source = ?"
            params.append(source)
        if language:
            where += " AND language = ?"
            params.append(language)
        if keyword:
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            kw = f"%{escaped}%"
            where += " AND (title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\')"
            params.extend([kw, kw])
        if start_time:
            where += " AND crawl_time >= ?"
            params.append(start_time)
        if end_time:
            where += " AND crawl_time <= ?"
            if len(end_time) == 10:
                end_time += " 23:59:59"
            params.append(end_time)

        cursor.execute(f"SELECT COUNT(*) FROM news {where}", params)
        total = cursor.fetchone()[0]

        params.extend([limit, offset])
        cursor.execute(f"SELECT * FROM news {where} ORDER BY crawl_time DESC LIMIT ? OFFSET ?", params)
        rows = cursor.fetchall()
        results = []
        for row in rows:
            item = dict(row)
            for field in ("images", "videos"):
                raw = item.get(field, "[]")
                if isinstance(raw, str):
                    try:
                        item[field] = json.loads(raw) if raw else []
                    except (json.JSONDecodeError, TypeError):
                        item[field] = []
            results.append(item)
        return jsonify({"data": results, "count": len(results), "total": total})
    finally:
        conn.close()


@app.route("/api/stats")
def api_stats():
    """统计信息"""
    conn = _get_conn(readonly=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM news")
        total = cursor.fetchone()[0]
        cursor.execute("""
            SELECT source, source_name, COUNT(*) as count, MAX(crawl_time) as last_crawl
            FROM news GROUP BY source ORDER BY count DESC
        """)
        sources = [{"source": r[0], "source_name": r[1], "count": r[2], "last_crawl": r[3]} for r in cursor.fetchall()]
        cursor.execute("SELECT MAX(crawl_time) FROM news")
        last_update = cursor.fetchone()[0]
        return jsonify({"total": total, "sources": sources, "last_update": last_update})
    finally:
        conn.close()


@app.route("/api/health")
def api_health():
    """健康检查"""
    try:
        conn = _get_conn(readonly=True)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM news")
        total = cursor.fetchone()[0]
        conn.close()
        return jsonify({"status": "healthy", "total_news": total})
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 500


# ============ 启动 ============
if __name__ == "__main__":
    init_db()
    # 使用waitress生产级服务器
    try:
        from waitress import serve
        logger.info(f"服务器启动 (waitress): http://0.0.0.0:{API_PORT}")
        serve(app, host="0.0.0.0", port=API_PORT, threads=4)
    except ImportError:
        logger.info(f"服务器启动 (flask dev): http://0.0.0.0:{API_PORT}")
        app.run(host="0.0.0.0", port=API_PORT, debug=False)
