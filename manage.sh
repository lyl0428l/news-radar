#!/bin/bash
# ============================================
# 新闻爬虫 - 运维管理脚本
# 用法: bash manage.sh [命令]
# ============================================

DEPLOY_DIR="/opt/news-radar"
cd "$DEPLOY_DIR" 2>/dev/null || { echo "部署目录不存在: $DEPLOY_DIR"; exit 1; }

# Docker Compose 命令
if docker compose version &> /dev/null; then
    DC="docker compose"
else
    DC="docker-compose"
fi

case "$1" in
    status|ps)
        echo "=== 容器状态 ==="
        $DC ps
        echo ""
        echo "=== 数据卷 ==="
        docker volume ls | grep news-radar
        ;;
    logs)
        # 默认显示最近 100 行并跟踪
        $DC logs -f --tail=100 ${2:-}
        ;;
    restart)
        echo "重启服务..."
        $DC restart
        echo "重启完成"
        $DC ps
        ;;
    stop)
        echo "停止服务..."
        $DC down
        echo "服务已停止"
        ;;
    start)
        echo "启动服务..."
        $DC up -d
        echo "服务已启动"
        $DC ps
        ;;
    rebuild)
        echo "重新构建并启动..."
        $DC down
        docker build -t news-radar:latest .
        $DC up -d
        echo "重建完成"
        $DC ps
        ;;
    update)
        echo "更新项目文件并重建..."
        # 从指定目录同步文件（需要先把新文件上传到服务器）
        $DC down
        docker build -t news-radar:latest .
        $DC up -d
        echo "更新完成"
        $DC ps
        ;;
    db-backup)
        # 备份数据库
        BACKUP_DIR="/opt/news-radar/backups"
        mkdir -p "$BACKUP_DIR"
        TIMESTAMP=$(date +%Y%m%d_%H%M%S)
        docker cp news-radar-crawler:/app/data/news.db "${BACKUP_DIR}/news_${TIMESTAMP}.db"
        echo "数据库已备份到: ${BACKUP_DIR}/news_${TIMESTAMP}.db"
        # 保留最近 7 个备份
        ls -t "${BACKUP_DIR}"/news_*.db | tail -n +8 | xargs rm -f 2>/dev/null
        echo "旧备份已清理（保留最近 7 个）"
        ;;
    db-size)
        echo "=== 数据库大小 ==="
        docker exec news-radar-crawler ls -lh /app/data/news.db 2>/dev/null || echo "容器未运行"
        echo ""
        echo "=== 记录统计 ==="
        docker exec news-radar-crawler python -c "
import sqlite3
conn = sqlite3.connect('data/news.db')
c = conn.cursor()
c.execute('SELECT COUNT(*) FROM news')
print(f'  新闻总数: {c.fetchone()[0]} 条')
c.execute('SELECT source_name, COUNT(*) FROM news GROUP BY source ORDER BY COUNT(*) DESC')
for name, cnt in c.fetchall():
    print(f'  {name}: {cnt} 条')
conn.close()
" 2>/dev/null || echo "容器未运行"
        ;;
    crawl)
        echo "手动触发一轮爬取..."
        docker exec news-radar-crawler python main.py
        echo "爬取完成"
        ;;
    port)
        # 显示当前端口
        echo "当前端口映射:"
        docker port news-radar-web 2>/dev/null || echo "Web 容器未运行"
        ;;
    *)
        echo "============================================"
        echo " 新闻爬虫管理脚本"
        echo "============================================"
        echo ""
        echo "用法: bash manage.sh <命令>"
        echo ""
        echo "  status    查看容器状态"
        echo "  logs      查看日志 (可选: logs crawler / logs web)"
        echo "  start     启动服务"
        echo "  stop      停止服务"
        echo "  restart   重启服务"
        echo "  rebuild   重新构建镜像并启动"
        echo "  crawl     手动触发一轮爬取"
        echo "  db-backup 备份数据库"
        echo "  db-size   查看数据库大小和统计"
        echo "  port      查看端口映射"
        echo ""
        ;;
esac
