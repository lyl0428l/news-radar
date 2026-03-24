"""
存储模块 - SQLite 写入 + JSON 归档 + URL 去重 + 数据清理
"""
import sqlite3
import json
import logging
import os
import re
import hashlib
import shutil
import tempfile
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from config import DB_PATH, JSON_DIR, DATA_RETAIN_DAYS

logger = logging.getLogger(__name__)

# 全局写锁：防止多线程并发写 SQLite 冲突
_db_write_lock = threading.Lock()

# JSON 归档写锁：防止多线程并发读写同一 JSON 文件导致数据丢失
_json_write_lock = threading.Lock()

# URL 去重时需要去除的跟踪/分享参数
_URL_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "wfr", "isappinstalled", "wxshare", "nsukey",
    "scene", "clicktime", "enterid", "abtest", "ref", "source_id",
}


def normalize_url(url: str) -> str:
    """
    URL 归一化：去跟踪参数、统一协议、去末尾斜杠。
    用于生成 url_hash 做去重。
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
        # 统一用 https
        scheme = "https"
        # 去跟踪参数
        query_params = parse_qs(parsed.query, keep_blank_values=False)
        clean_params = {
            k: v for k, v in query_params.items()
            if k.lower() not in _URL_TRACKING_PARAMS
        }
        # 排序参数保证一致性
        sorted_query = urlencode(clean_params, doseq=True)
        # 去末尾斜杠
        path = parsed.path.rstrip("/") if parsed.path != "/" else "/"
        clean_url = urlunparse((scheme, parsed.netloc.lower(), path,
                                parsed.params, sorted_query, ""))
        return clean_url
    except Exception:
        return url.strip()


def make_url_hash(url: str) -> str:
    """生成 URL 的 SHA256 哈希"""
    normalized = normalize_url(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# 标记 WAL 模式是否已设置（持久化配置，只需首次设置）
_wal_initialized = False


def _get_connection(readonly=False) -> sqlite3.Connection:
    """获取数据库连接，统一配置。isolation_level=None 使用手动事务管理。"""
    global _wal_initialized
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    if not _wal_initialized:
        conn.execute("PRAGMA journal_mode=WAL")
        _wal_initialized = True
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    return conn


def save_to_db(news_list: list) -> int:
    """
    批量写入 SQLite，URL 哈希去重，事务包裹。
    返回实际新增条数。
    """
    if not news_list:
        return 0

    inserted = 0

    with _db_write_lock:
        conn = _get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN")
            for item in news_list:
                url = item.get("url", "")
                if not url or not item.get("title", ""):
                    continue  # 跳过无效数据

                url_hash = make_url_hash(url)
                # 序列化媒体字段
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
                    else:
                        # INSERT OR IGNORE 跳过了 → URL 已存在
                        # 如果新数据有 content 而旧记录没有，则更新（回填正文）
                        new_content = item.get("content", "")
                        new_content_html = item.get("content_html", "")
                        new_summary = item.get("summary", "")
                        new_thumbnail = item.get("thumbnail", "")
                        new_author = item.get("author", "")
                        new_pub_time = item.get("pub_time", "")
                        # 有新内容、图片、视频、作者、时间等任意新数据时都应回填
                        new_images = item.get("images", [])
                        new_videos = item.get("videos", [])
                        has_new_data = (
                            (new_content and len(new_content) > 50) or
                            new_author or new_pub_time or
                            new_images or new_videos
                        )
                        if has_new_data:
                            # 当新数据包含视频而旧记录没有时，content_html 也需要更新
                            new_rank = item.get("rank", 0)
                            new_crawl_time = item.get("crawl_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                            if new_videos:
                                # 有新视频：允许覆盖旧 content_html（视频可能嵌入其中）
                                cursor.execute("""
                                    UPDATE news SET
                                        content = CASE WHEN (content IS NULL OR content = '' OR length(content) < 50) THEN ? ELSE content END,
                                        content_html = CASE WHEN (content_html IS NULL OR content_html = '' OR (videos IS NULL OR videos = '' OR videos = '[]')) THEN ? ELSE content_html END,
                                        summary = CASE WHEN (summary IS NULL OR summary = '') THEN ? ELSE summary END,
                                        images = CASE WHEN (images IS NULL OR images = '' OR images = '[]') THEN ? ELSE images END,
                                        videos = CASE WHEN (videos IS NULL OR videos = '' OR videos = '[]') THEN ? ELSE videos END,
                                        thumbnail = CASE WHEN (thumbnail IS NULL OR thumbnail = '') THEN ? ELSE thumbnail END,
                                        author = CASE WHEN (author IS NULL OR author = '') THEN ? ELSE author END,
                                        pub_time = CASE WHEN (pub_time IS NULL OR pub_time = '') THEN ? ELSE pub_time END,
                                        rank = CASE WHEN ? > 0 THEN ? ELSE rank END,
                                        crawl_time = ?
                                    WHERE url_hash = ?
                                """, (
                                    new_content, new_content_html, new_summary,
                                    images_json, videos_json, new_thumbnail,
                                    new_author, new_pub_time,
                                    new_rank, new_rank, new_crawl_time,
                                    url_hash,
                                ))
                            else:
                                # 无新视频：content_html 仅在旧值为空时回填
                                cursor.execute("""
                                    UPDATE news SET
                                        content = CASE WHEN (content IS NULL OR content = '' OR length(content) < 50) THEN ? ELSE content END,
                                        content_html = CASE WHEN (content_html IS NULL OR content_html = '') THEN ? ELSE content_html END,
                                        summary = CASE WHEN (summary IS NULL OR summary = '') THEN ? ELSE summary END,
                                        images = CASE WHEN (images IS NULL OR images = '' OR images = '[]') THEN ? ELSE images END,
                                        videos = CASE WHEN (videos IS NULL OR videos = '' OR videos = '[]') THEN ? ELSE videos END,
                                        thumbnail = CASE WHEN (thumbnail IS NULL OR thumbnail = '') THEN ? ELSE thumbnail END,
                                        author = CASE WHEN (author IS NULL OR author = '') THEN ? ELSE author END,
                                        pub_time = CASE WHEN (pub_time IS NULL OR pub_time = '') THEN ? ELSE pub_time END,
                                        rank = CASE WHEN ? > 0 THEN ? ELSE rank END,
                                        crawl_time = ?
                                    WHERE url_hash = ?
                                """, (
                                    new_content, new_content_html, new_summary,
                                    images_json, videos_json, new_thumbnail,
                                    new_author, new_pub_time,
                                    new_rank, new_rank, new_crawl_time,
                                    url_hash,
                                ))
                except sqlite3.IntegrityError:
                    # UNIQUE 冲突（去重），正常跳过
                    pass
                except sqlite3.Error as e:
                    logger.warning(f"写入失败: {e} | {item.get('title', '')[:30]}")

            conn.commit()
        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()

    return inserted


def save_to_json(news_list: list, crawl_time: str = "") -> str:
    """
    按日期/小时归档 JSON 文件（原子写入 + 线程安全）。
    目录结构: data/json/2026-03-11/14_00.json
    返回文件路径。
    """
    if not news_list:
        return ""

    now = datetime.now()
    if not crawl_time:
        crawl_time = now.strftime("%Y-%m-%d %H:%M:%S")

    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H_%M")

    day_dir = os.path.join(JSON_DIR, date_str)
    os.makedirs(day_dir, exist_ok=True)

    file_path = os.path.join(day_dir, f"{time_str}.json")

    # 加锁：读取→合并→写入 整体原子化，防止多线程竞争
    with _json_write_lock:
        # 如果同一时间文件已存在，追加合并
        existing = []
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # 兼容新旧格式
                    if isinstance(data, list):
                        existing = data
                    elif isinstance(data, dict):
                        existing = data.get("items", [])
            except (json.JSONDecodeError, IOError):
                existing = []

        # URL 去重合并
        existing_urls = {normalize_url(item.get("url", "")) for item in existing}
        for item in news_list:
            norm_url = normalize_url(item.get("url", ""))
            if norm_url and norm_url not in existing_urls:
                existing.append(item)
                existing_urls.add(norm_url)

        # 带元数据的归档格式
        archive_data = {
            "archive_time": crawl_time,
            "crawler_version": "1.1.0",
            "total_count": len(existing),
            "items": existing,
        }

        # 原子写入：先写临时文件，再 rename
        tmp_path = ""
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(dir=day_dir, suffix=".tmp")
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(archive_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, file_path)
        except Exception:
            # 回退到直接写入
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(archive_data, f, ensure_ascii=False, indent=2)

    return file_path


def get_news(source=None, language=None, keyword=None,
             start_time=None, end_time=None,
             limit=100, offset=0) -> tuple:
    """
    从 SQLite 查询新闻，支持按来源/语言/关键词/时间范围筛选。
    返回 (news_list, total_count) 元组，total_count 用于分页计算。
    """
    conn = _get_connection(readonly=True)
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
            # 转义 LIKE 通配符，防止用户输入 % 或 _ 导致误匹配
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            kw_pattern = f"%{escaped}%"
            where += (" AND (title LIKE ? ESCAPE '\\'"
                      " OR summary LIKE ? ESCAPE '\\'"
                      " OR content LIKE ? ESCAPE '\\')")
            params.extend([kw_pattern, kw_pattern, kw_pattern])
        if start_time:
            where += " AND crawl_time >= ?"
            params.append(start_time)
        if end_time:
            # 日期格式 "2026-03-11" 补全为 "2026-03-11 23:59:59"
            # 否则 "2026-03-11 14:00:00" > "2026-03-11" 导致当天数据被排除
            where += " AND crawl_time <= ?"
            if len(end_time) == 10 and " " not in end_time:
                end_time = end_time + " 23:59:59"
            params.append(end_time)

        # 先查总数（用于精确分页）
        cursor.execute(f"SELECT COUNT(*) FROM news {where}", params)
        total = cursor.fetchone()[0]

        # 再查分页数据
        query = f"SELECT * FROM news {where} ORDER BY crawl_time DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor.execute(query, params)
        rows = cursor.fetchall()

        results = [_deserialize_row(row) for row in rows]
        return results, total
    finally:
        conn.close()


def _deserialize_row(row) -> dict:
    """将 SQLite Row 转为 dict，并反序列化 JSON 字段"""
    item = dict(row)
    for json_field in ("images", "videos"):
        raw = item.get(json_field, "[]")
        if isinstance(raw, str):
            try:
                item[json_field] = json.loads(raw) if raw else []
            except (json.JSONDecodeError, TypeError):
                item[json_field] = []
    return item


def check_urls_have_content(urls: list) -> set:
    """
    批量检查哪些 URL 在 DB 中已完整抓取（正文 + 图片 + 作者都有）。
    返回已完整抓取的 URL 集合。用于优化：跳过详情页抓取。
    只有正文、图片、作者全部都有的才跳过，否则需要重新抓取补全。
    """
    if not urls:
        return set()

    conn = _get_connection(readonly=True)
    try:
        cursor = conn.cursor()
        # 用 url_hash 查找
        hashes = [(make_url_hash(u), u) for u in urls]
        found = set()
        # SQLite IN 参数上限 999，分批查
        batch_size = 500
        for i in range(0, len(hashes), batch_size):
            batch = hashes[i:i + batch_size]
            placeholders = ",".join(["?"] * len(batch))
            hash_values = [h for h, _ in batch]
            cursor.execute(f"""
                SELECT url_hash FROM news
                WHERE url_hash IN ({placeholders})
                AND content IS NOT NULL AND content != ''
                AND length(content) > 50
                AND images IS NOT NULL AND images != '' AND images != '[]'
                AND author IS NOT NULL AND author != ''
            """, hash_values)
            found_hashes = {row[0] for row in cursor.fetchall()}
            for h, u in batch:
                if h in found_hashes:
                    found.add(u)
        return found
    finally:
        conn.close()


def get_news_by_id(news_id: int) -> dict:
    """根据 ID 查询单条新闻（含媒体字段），用于详情页"""
    conn = _get_connection(readonly=True)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM news WHERE id = ?", (news_id,))
        row = cursor.fetchone()
        if row is None:
            return {}
        return _deserialize_row(row)
    finally:
        conn.close()


def get_stats() -> dict:
    """获取统计信息：各站点数量、总数、最近更新时间。"""
    conn = _get_connection(readonly=True)
    try:
        cursor = conn.cursor()

        # 总数
        cursor.execute("SELECT COUNT(*) FROM news")
        total = cursor.fetchone()[0]

        # 各站点数量
        cursor.execute("""
            SELECT source, source_name, COUNT(*) as count,
                   MAX(crawl_time) as last_crawl
            FROM news GROUP BY source ORDER BY count DESC
        """)
        sources = [
            {"source": r[0], "source_name": r[1], "count": r[2], "last_crawl": r[3]}
            for r in cursor.fetchall()
        ]

        # 最近更新
        cursor.execute("SELECT MAX(crawl_time) FROM news")
        last_update = cursor.fetchone()[0]

        return {"total": total, "sources": sources, "last_update": last_update}
    finally:
        conn.close()


# ========== 爬取轮次 ==========

def get_crawl_rounds(limit: int = 50) -> list:
    """
    获取最近的爬取轮次列表（按 crawl_time 去重分组）。
    返回 [{"crawl_time": "2026-03-11 14:56:14", "count": 120}, ...]
    用于前端"按轮次查看"功能。
    """
    conn = _get_connection(readonly=True)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT crawl_time, COUNT(*) as cnt
            FROM news
            WHERE crawl_time != ''
            GROUP BY crawl_time
            ORDER BY crawl_time DESC
            LIMIT ?
        """, (limit,))
        return [{"crawl_time": r[0], "count": r[1]} for r in cursor.fetchall()]
    finally:
        conn.close()


# ========== 爬取日志 ==========

def log_crawl_start(source: str, source_name: str = "") -> int:
    """记录爬取开始，返回 log_id"""
    with _db_write_lock:
        conn = _get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            cursor.execute("""
                INSERT INTO crawl_log (source, source_name, start_time, status)
                VALUES (?, ?, ?, 'running')
            """, (source, source_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            log_id = cursor.lastrowid or 0
            conn.commit()
            return log_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def log_crawl_end(log_id: int, status: str, news_count: int = 0,
                  error_msg: str = "", duration_ms: int = 0):
    """记录爬取结束"""
    with _db_write_lock:
        conn = _get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            cursor.execute("""
                UPDATE crawl_log
                SET end_time = ?, status = ?, news_count = ?,
                    error_msg = ?, duration_ms = ?
                WHERE id = ?
            """, (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                status, news_count, error_msg, duration_ms, log_id,
            ))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def get_crawl_health() -> list:
    """
    获取各爬虫最近 3 轮的健康状态。
    返回 [{"source": ..., "status": "healthy/warning/critical", "recent": [...]}]
    """
    conn = _get_connection(readonly=True)
    try:
        cursor = conn.cursor()

        # 获取所有源
        cursor.execute("SELECT DISTINCT source, source_name FROM crawl_log ORDER BY source")
        sources = cursor.fetchall()

        health = []
        for source, source_name in sources:
            cursor.execute("""
                SELECT status, news_count, start_time, error_msg
                FROM crawl_log
                WHERE source = ?
                ORDER BY start_time DESC
                LIMIT 3
            """, (source,))
            recent = [
                {"status": r[0], "count": r[1], "time": r[2], "error": r[3]}
                for r in cursor.fetchall()
            ]

            # 判定健康状态
            fail_count = sum(1 for r in recent if r["status"] == "failed")
            if fail_count >= 3:
                level = "critical"
            elif fail_count >= 1:
                level = "warning"
            else:
                level = "healthy"

            health.append({
                "source": source,
                "source_name": source_name,
                "level": level,
                "recent": recent,
            })

        return health
    finally:
        conn.close()


# ========== 数据清理 ==========

def cleanup(days: int = 0):
    """
    清理过期数据：
    1. 删除 N 天前的 SQLite 记录
    2. 删除 N 天前的 JSON 归档目录
    """
    if days <= 0:
        days = DATA_RETAIN_DAYS

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    # 1. 清理数据库（DELETE 在锁内，VACUUM 在锁外避免长时间阻塞）
    with _db_write_lock:
        conn = _get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            cursor.execute("DELETE FROM news WHERE crawl_time < ?", (cutoff,))
            deleted_news = cursor.rowcount
            cursor.execute("DELETE FROM crawl_log WHERE start_time < ?", (cutoff,))
            deleted_logs = cursor.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # VACUUM 放在锁外：可能较慢，但不阻塞其他写操作
    # WAL 模式下 VACUUM 只需短暂排他锁
    conn = None
    try:
        conn = _get_connection()
        conn.execute("VACUUM")
    except Exception as e:
        logger.warning(f"VACUUM 失败（不影响正常使用）: {e}")
    finally:
        if conn:
            conn.close()

    logger.info(f"数据库清理: 删除 {deleted_news} 条新闻, {deleted_logs} 条日志 (>{days}天)")

    # 2. 清理 JSON 归档
    deleted_dirs = 0
    if os.path.exists(JSON_DIR):
        cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        for dirname in os.listdir(JSON_DIR):
            dir_path = os.path.join(JSON_DIR, dirname)
            if os.path.isdir(dir_path) and dirname < cutoff_date:
                try:
                    shutil.rmtree(dir_path)
                    deleted_dirs += 1
                except OSError as e:
                    logger.warning(f"删除归档目录失败: {dir_path} | {e}")

    logger.info(f"JSON 归档清理: 删除 {deleted_dirs} 个日期目录 (>{days}天)")

    # 3. 清理过期的媒体图片（按日期目录）
    deleted_media = 0
    try:
        from config import MEDIA_IMAGE_DIR
        if os.path.exists(MEDIA_IMAGE_DIR):
            cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            for dirname in os.listdir(MEDIA_IMAGE_DIR):
                dir_path = os.path.join(MEDIA_IMAGE_DIR, dirname)
                # 只删除日期格式的目录且超过保留天数
                if (os.path.isdir(dir_path)
                        and re.match(r"\d{4}-\d{2}-\d{2}$", dirname)
                        and dirname < cutoff_date):
                    try:
                        shutil.rmtree(dir_path)
                        deleted_media += 1
                    except OSError as e:
                        logger.warning(f"删除媒体目录失败: {dir_path} | {e}")
    except ImportError:
        pass

    if deleted_media:
        logger.info(f"媒体文件清理: 删除 {deleted_media} 个日期目录 (>{days}天)")


def mark_read(news_id: int) -> bool:
    """标记新闻为已读，返回是否实际更新了记录"""
    with _db_write_lock:
        conn = _get_connection()
        try:
            conn.execute("BEGIN")
            cursor = conn.execute("UPDATE news SET is_read = 1 WHERE id = ?", (news_id,))
            conn.commit()
            return cursor.rowcount > 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
