"""
一次性工具：清除数据库中正文不足200字的记录的正文字段。
清除后下一轮爬取会重新抓取这些文章的详情页（含Playwright渲染）。

用法：python clear_short_content.py
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DB_PATH


def main():
    if not os.path.exists(DB_PATH):
        print(f"数据库不存在: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 统计当前状态
    cursor.execute("SELECT COUNT(*) FROM news")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM news WHERE content IS NOT NULL AND content != '' AND length(content) > 200")
    good = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM news WHERE content IS NULL OR content = '' OR length(content) <= 200")
    bad = cursor.fetchone()[0]

    print(f"数据库总记录: {total}")
    print(f"正文>=200字: {good} 条（保留）")
    print(f"正文<200字:  {bad} 条（将清除正文，下轮重新抓取）")
    print()

    if bad == 0:
        print("无需清除，所有记录正文都>=200字")
        return

    # 清除短正文（只清空 content 和 content_html，保留其他字段）
    cursor.execute("""
        UPDATE news
        SET content = '', content_html = ''
        WHERE content IS NULL OR content = '' OR length(content) <= 200
    """)
    affected = cursor.rowcount
    conn.commit()

    print(f"已清除 {affected} 条记录的正文字段")
    print("下一轮爬取将重新抓取这些文章的详情页")

    conn.close()


if __name__ == "__main__":
    main()
