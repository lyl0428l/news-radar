"""
数据模型 - SQLite 表结构定义与初始化
"""
import sqlite3
import os
import hashlib
from config import DB_PATH, DATA_DIR
from storage import normalize_url


def init_db():
    """初始化数据库，创建表结构"""
    os.makedirs(DATA_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()

        # 开启 WAL 模式：允许读写并发，避免 database is locked
        cursor.execute("PRAGMA journal_mode=WAL")

        # ========== 主表：新闻条目 ==========
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS news (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url_hash    TEXT UNIQUE,
                title       TEXT NOT NULL,
                url         TEXT NOT NULL,
                source      TEXT NOT NULL,
                source_name TEXT,
                summary     TEXT DEFAULT '',
                content     TEXT DEFAULT '',
                category    TEXT DEFAULT '',
                rank        INTEGER DEFAULT 0,
                pub_time    TEXT DEFAULT '',
                crawl_time  TEXT NOT NULL,
                language    TEXT DEFAULT 'zh',
                is_read     INTEGER DEFAULT 0,
                extra_json  TEXT DEFAULT '{}',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ========== 爬取日志表 ==========
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crawl_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT NOT NULL,
                source_name TEXT DEFAULT '',
                start_time  TEXT NOT NULL,
                end_time    TEXT,
                status      TEXT NOT NULL DEFAULT 'running',
                news_count  INTEGER DEFAULT 0,
                error_msg   TEXT DEFAULT '',
                duration_ms INTEGER DEFAULT 0
            )
        """)

        # ========== 兼容旧数据库：逐列迁移 ==========
        _migrate_column(cursor, "news", "rank", "INTEGER DEFAULT 0")
        _migrate_column(cursor, "news", "content", "TEXT DEFAULT ''")
        _migrate_column(cursor, "news", "is_read", "INTEGER DEFAULT 0")
        _migrate_column(cursor, "news", "extra_json", "TEXT DEFAULT '{}'")
        _migrate_column(cursor, "news", "url_hash", "TEXT")

        # ========== 媒体字段迁移（图片/视频/缩略图/正文HTML） ==========
        _migrate_column(cursor, "news", "images", "TEXT DEFAULT '[]'")
        _migrate_column(cursor, "news", "videos", "TEXT DEFAULT '[]'")
        _migrate_column(cursor, "news", "thumbnail", "TEXT DEFAULT ''")
        _migrate_column(cursor, "news", "content_html", "TEXT DEFAULT ''")

        # ========== 作者/账号字段迁移 ==========
        _migrate_column(cursor, "news", "author", "TEXT DEFAULT ''")

        # ========== 回填旧数据的 url_hash ==========
        _backfill_url_hash(cursor)

        # ========== 修复：将 url_hash 普通索引升级为 UNIQUE 索引 ==========
        _upgrade_url_hash_unique(cursor)

        # ========== 索引 ==========
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_source ON news(source)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_crawl_time ON news(crawl_time)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_language ON news(language)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pub_time ON news(pub_time)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rank ON news(rank)")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_url_hash ON news(url_hash)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_is_read ON news(is_read)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_crawl_log_source ON crawl_log(source)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_crawl_log_time ON crawl_log(start_time)")

        conn.commit()
    finally:
        conn.close()
    print(f"[models] 数据库初始化完成: {DB_PATH}")


def _migrate_column(cursor, table: str, column: str, col_type: str):
    """安全添加列，已存在则跳过"""
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except sqlite3.OperationalError:
        pass  # 列已存在


def _upgrade_url_hash_unique(cursor):
    """
    将 idx_url_hash 从普通索引升级为 UNIQUE 索引。
    旧数据库中 url_hash 通过 ALTER TABLE ADD COLUMN 添加，无法带 UNIQUE 约束，
    导致 INSERT OR IGNORE 的去重依赖旧的 url TEXT UNIQUE 而非 url_hash。
    修复步骤：
    1. 检测 idx_url_hash 是否已经是 UNIQUE（若是则跳过）
    2. 清理已有的 url_hash 重复记录（保留 id 最小的）
    3. 删除旧的普通索引，创建 UNIQUE 索引
    """
    # 检查 idx_url_hash 是否已经是 UNIQUE
    cursor.execute("PRAGMA index_list(news)")
    for row in cursor.fetchall():
        if row[1] == "idx_url_hash" and row[2] == 1:  # row[2]=1 means unique
            return  # 已经是 UNIQUE，无需操作

    # 清理重复的 url_hash（保留 id 最小的记录）
    cursor.execute("""
        DELETE FROM news WHERE id IN (
            SELECT n.id FROM news n
            INNER JOIN (
                SELECT url_hash, MIN(id) as keep_id
                FROM news
                WHERE url_hash IS NOT NULL AND url_hash != ''
                GROUP BY url_hash
                HAVING COUNT(*) > 1
            ) d ON n.url_hash = d.url_hash AND n.id != d.keep_id
        )
    """)
    deleted = cursor.rowcount
    if deleted > 0:
        print(f"[models] 清理 url_hash 重复记录: {deleted} 条")

    # 删除旧的普通索引，创建 UNIQUE 索引
    cursor.execute("DROP INDEX IF EXISTS idx_url_hash")
    cursor.execute("CREATE UNIQUE INDEX idx_url_hash ON news(url_hash)")
    print("[models] idx_url_hash 已升级为 UNIQUE 索引")


def _backfill_url_hash(cursor):
    """
    回填旧数据的 url_hash。
    只处理 url_hash 为 NULL 的行，避免重复执行开销。
    使用 storage.normalize_url 确保与写入时的归一化逻辑一致。
    """
    cursor.execute("SELECT id, url FROM news WHERE url_hash IS NULL")
    rows = cursor.fetchall()
    if not rows:
        return

    for row_id, url in rows:
        normalized = normalize_url(url)
        url_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        try:
            cursor.execute(
                "UPDATE news SET url_hash = ? WHERE id = ?",
                (url_hash, row_id)
            )
        except sqlite3.IntegrityError:
            # 如果 hash 冲突（同一 URL 的旧重复行），删除这条重复数据
            cursor.execute("DELETE FROM news WHERE id = ?", (row_id,))

    print(f"[models] 回填 url_hash: {len(rows)} 行")


if __name__ == "__main__":
    init_db()
