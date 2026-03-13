"""
Flask Web 服务 - 本地新闻浏览界面
"""
import sys
import os
import time as _time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import csv
import io
import json
import threading
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, Response
from config import WEB_HOST, WEB_PORT, WEB_DEBUG, SITES, MEDIA_DIR
from storage import get_news, get_news_by_id, get_stats, get_crawl_health, get_crawl_rounds, mark_read
from models import init_db

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True


# ---------- 简易短时缓存（减少首页重复 DB 查询） ----------
_cache: dict = {}
_CACHE_TTL = 30  # 秒


def _cached(key: str, fn, ttl: int = _CACHE_TTL):
    """获取缓存值，过期则重新计算"""
    now = _time.monotonic()
    entry = _cache.get(key)
    if entry and now - entry[0] < ttl:
        return entry[1]
    value = fn()
    _cache[key] = (now, value)
    return value


# ============================================================
#  首页：只显示最新一轮 + 最近 1 小时内的新闻
# ============================================================

@app.route("/")
def index():
    """首页 - 最新新闻（最近 1 小时）"""
    source = request.args.get("source", "")
    language = request.args.get("language", "")
    keyword = request.args.get("keyword", "")
    try:
        page = int(request.args.get("page", 1))
    except (ValueError, TypeError):
        page = 1
    per_page = 50

    # 默认只查最近 1 小时内的数据
    one_hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    news_list, total = get_news(
        source=source or None,
        language=language or None,
        keyword=keyword or None,
        start_time=one_hour_ago,
        end_time=None,
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    stats = _cached("stats", get_stats)
    sites = [(s.display_name, s.module) for s in SITES]

    # 检查爬虫异常
    alerts = []
    try:
        health = get_crawl_health()
        for h in health:
            if h["level"] == "critical":
                alerts.append(f"{h['source_name'] or h['source']} 连续失败")
            elif h["level"] == "warning":
                alerts.append(f"{h['source_name'] or h['source']} 近期有失败")
    except Exception:
        pass

    return render_template(
        "index.html",
        news_list=news_list,
        stats=stats,
        sites=sites,
        current_source=source,
        current_language=language,
        current_keyword=keyword,
        current_page=page,
        total_pages=total_pages,
        total_count=total,
        alerts=alerts,
    )


# ============================================================
#  历史新闻：完整筛选 + 分页 + 导出
# ============================================================

@app.route("/archive")
def archive():
    """历史新闻 - 全量数据浏览"""
    source = request.args.get("source", "")
    language = request.args.get("language", "")
    keyword = request.args.get("keyword", "")
    start_time = request.args.get("start_time", "")
    end_time = request.args.get("end_time", "")
    crawl_round = request.args.get("crawl_round", "")
    try:
        page = int(request.args.get("page", 1))
    except (ValueError, TypeError):
        page = 1
    per_page = 50

    # datetime-local 提交格式 "2026-03-11T14:00" → "2026-03-11 14:00"
    if start_time and "T" in start_time:
        start_time = start_time.replace("T", " ")
    if end_time and "T" in end_time:
        end_time = end_time.replace("T", " ")

    # 如果选了爬取轮次，用精确的 crawl_time 覆盖时间范围
    effective_start = start_time or None
    effective_end = end_time or None
    if crawl_round:
        effective_start = crawl_round
        effective_end = crawl_round

    news_list, total = get_news(
        source=source or None,
        language=language or None,
        keyword=keyword or None,
        start_time=effective_start,
        end_time=effective_end,
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    stats = _cached("stats", get_stats)
    sites = [(s.display_name, s.module) for s in SITES]
    rounds = _cached("rounds", lambda: get_crawl_rounds(limit=50))

    return render_template(
        "archive.html",
        news_list=news_list,
        stats=stats,
        sites=sites,
        rounds=rounds,
        current_source=source,
        current_language=language,
        current_keyword=keyword,
        current_start_time=start_time,
        current_end_time=end_time,
        current_crawl_round=crawl_round,
        current_page=page,
        total_pages=total_pages,
        total_count=total,
    )


# ============================================================
#  API 接口
# ============================================================

@app.route("/api/news")
def api_news():
    """API 接口 - 返回 JSON 数据"""
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

    news_list, total = get_news(
        source=source or None,
        language=language or None,
        keyword=keyword or None,
        start_time=start_time or None,
        end_time=end_time or None,
        limit=limit,
        offset=offset,
    )
    return jsonify({"data": news_list, "count": len(news_list), "total": total})


@app.route("/api/stats")
def api_stats():
    """API 接口 - 统计信息"""
    return jsonify(get_stats())


@app.route("/api/health")
def api_health():
    """API 接口 - 爬虫健康状态"""
    return jsonify(get_crawl_health())


@app.route("/api/mark_read/<int:news_id>", methods=["POST"])
def api_mark_read(news_id):
    """标记新闻为已读"""
    updated = mark_read(news_id)
    if not updated:
        return jsonify({"ok": False, "msg": "新闻不存在"}), 404
    return jsonify({"ok": True})


@app.route("/health")
def health_page():
    """爬虫健康状态面板"""
    health = get_crawl_health()
    return render_template("health.html", health=health)


# ============================================================
#  新闻详情页
# ============================================================

@app.route("/news/<int:news_id>")
def news_detail(news_id):
    """新闻详情页 - 显示正文、图片、视频"""
    item = get_news_by_id(news_id)
    if not item:
        return render_template("detail.html", item=None), 404
    return render_template("detail.html", item=item)


# ============================================================
#  媒体文件路由（本地图片访问）
# ============================================================

@app.route("/media/<path:filename>")
def serve_media(filename):
    """提供本地媒体文件访问"""
    from flask import send_from_directory, abort
    # 安全检查：只允许访问 media 目录下的文件
    if ".." in filename:
        abort(403)
    return send_from_directory(MEDIA_DIR, filename)


@app.route("/api/export")
def api_export():
    """导出新闻数据为 CSV 或 JSON（遵循当前筛选条件）"""
    fmt = request.args.get("format", "csv")
    source = request.args.get("source", "")
    language = request.args.get("language", "")
    keyword = request.args.get("keyword", "")
    start_time = request.args.get("start_time", "")
    end_time = request.args.get("end_time", "")

    # 导出最多 5000 条
    news_list, _ = get_news(
        source=source or None,
        language=language or None,
        keyword=keyword or None,
        start_time=start_time or None,
        end_time=end_time or None,
        limit=5000,
        offset=0,
    )

    if fmt == "json":
        data = json.dumps(news_list, ensure_ascii=False, indent=2)
        return Response(
            data,
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=news_export.json"},
        )

    # 默认 CSV
    fields = ["id", "title", "url", "source", "source_name", "category",
              "rank", "pub_time", "crawl_time", "language", "summary"]
    output = io.StringIO()
    # 写 BOM 头让 Excel 正确识别 UTF-8
    output.write("\ufeff")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for item in news_list:
        writer.writerow(item)

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=news_export.csv"},
    )


# 手动爬取状态
_crawl_lock = threading.Lock()
_crawl_running = False


@app.route("/api/crawl", methods=["POST"])
def api_trigger_crawl():
    """手动触发一轮爬取（异步执行，防止阻塞 Web 请求）"""
    global _crawl_running
    with _crawl_lock:
        if _crawl_running:
            return jsonify({"ok": False, "msg": "爬取正在进行中，请稍后再试"}), 409
        _crawl_running = True

    def do_crawl():
        global _crawl_running
        try:
            from main import run_single_crawl
            run_single_crawl()
        finally:
            with _crawl_lock:
                _crawl_running = False

    t = threading.Thread(target=do_crawl, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "爬取任务已启动，请稍后刷新页面查看结果"})


@app.route("/api/crawl/status")
def api_crawl_status():
    """查询手动爬取是否正在运行"""
    return jsonify({"running": _crawl_running})


def main():
    init_db()
    print(f"Web 服务启动: http://{WEB_HOST}:{WEB_PORT}")
    # use_reloader=False：防止与 APScheduler 同进程时调度器启动两次
    app.run(host=WEB_HOST, port=WEB_PORT, debug=WEB_DEBUG, use_reloader=False)


if __name__ == "__main__":
    main()
