#!/bin/bash
# ============================================
# 新闻爬虫 - 一键部署脚本
# 服务器: Alibaba Cloud Linux
# 端口: 8888 (Web + API)
# ============================================

set -e  # 出错立即停止

# ---------- 配置区（可按需修改） ----------
PROJECT_NAME="news-radar"
DEPLOY_DIR="/opt/news-radar"
WEB_PORT=8888
IMAGE_NAME="news-radar"
IMAGE_TAG="latest"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # 无颜色

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ============================================
# 第一步：检查环境
# ============================================
log_info "========== 第一步：检查环境 =========="

# 检查是否是 root 用户
if [ "$EUID" -ne 0 ]; then
    log_error "请使用 root 用户运行此脚本: sudo bash deploy.sh"
    exit 1
fi

# 检查 Docker
if ! command -v docker &> /dev/null; then
    log_error "Docker 未安装！请先安装 Docker:"
    echo "  curl -fsSL https://get.docker.com | bash"
    echo "  systemctl start docker && systemctl enable docker"
    exit 1
fi
log_info "Docker 已安装: $(docker --version)"

# 检查 Docker Compose (v2 plugin)
if docker compose version &> /dev/null; then
    COMPOSE_CMD="docker compose"
    log_info "Docker Compose (V2 plugin) 已安装"
elif command -v docker-compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
    log_info "Docker Compose (standalone) 已安装"
else
    log_warn "Docker Compose 未安装，正在安装..."
    # 安装 Docker Compose V2 插件
    mkdir -p /usr/local/lib/docker/cli-plugins
    curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
    COMPOSE_CMD="docker compose"
    log_info "Docker Compose 安装完成"
fi

# 检查端口是否被占用
if ss -tlnp | grep -q ":${WEB_PORT} "; then
    log_warn "端口 ${WEB_PORT} 已被占用！"
    ss -tlnp | grep ":${WEB_PORT} "
    echo ""
    read -p "是否更换端口？输入新端口号（直接回车使用 ${WEB_PORT}）: " NEW_PORT
    if [ -n "$NEW_PORT" ]; then
        WEB_PORT=$NEW_PORT
        log_info "已更换为端口: ${WEB_PORT}"
    else
        log_warn "将尝试继续使用端口 ${WEB_PORT}（可能需要先停止占用的服务）"
    fi
fi

# ============================================
# 第二步：创建部署目录
# ============================================
log_info "========== 第二步：创建部署目录 =========="

if [ -d "$DEPLOY_DIR" ]; then
    log_warn "部署目录已存在: ${DEPLOY_DIR}"
    read -p "是否覆盖更新？(y/N): " CONFIRM
    if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
        log_info "取消部署"
        exit 0
    fi
    # 先停止现有容器
    if [ -f "${DEPLOY_DIR}/docker-compose.yml" ]; then
        log_info "停止现有容器..."
        cd "$DEPLOY_DIR" && $COMPOSE_CMD down 2>/dev/null || true
    fi
fi

mkdir -p "$DEPLOY_DIR"
log_info "部署目录: ${DEPLOY_DIR}"

# ============================================
# 第三步：复制项目文件
# ============================================
log_info "========== 第三步：复制项目文件 =========="

# 获取脚本所在目录（即项目根目录）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 复制所有项目文件（排除不需要的）
rsync -av --exclude='.venv' \
          --exclude='venv' \
          --exclude='__pycache__' \
          --exclude='.git' \
          --exclude='data' \
          --exclude='logs' \
          --exclude='*.pyc' \
          --exclude='nul' \
          --exclude='news_crawler' \
          --exclude='*.bat' \
          --exclude='*.wav' \
          "$SCRIPT_DIR/" "$DEPLOY_DIR/"

log_info "项目文件已复制到 ${DEPLOY_DIR}"

# ============================================
# 第四步：创建环境配置文件
# ============================================
log_info "========== 第四步：创建环境配置 =========="

ENV_FILE="${DEPLOY_DIR}/.env"

# 如果 .env 已存在，询问是否覆盖
if [ -f "$ENV_FILE" ]; then
    log_warn ".env 已存在，跳过创建（保留现有配置）"
    log_info "如需修改请编辑: ${ENV_FILE}"
else
    # 交互式收集敏感配置
    echo ""
    echo "  请输入以下配置（直接回车跳过，后续可编辑 ${ENV_FILE} 修改）:"
    echo ""

    read -p "  PushPlus Token（微信推送，留空则关闭）: " INPUT_PUSH_TOKEN
    read -p "  远程同步服务器地址（如 http://1.2.3.4，留空则关闭同步）: " INPUT_SYNC_URL

    SYNC_ENABLED_VAL="false"
    INPUT_SYNC_USER=""
    INPUT_SYNC_PASS=""
    if [ -n "$INPUT_SYNC_URL" ]; then
        SYNC_ENABLED_VAL="true"
        read -p "  远程同步用户名: " INPUT_SYNC_USER
        read -s -p "  远程同步密码: " INPUT_SYNC_PASS
        echo ""
    fi

    cat > "$ENV_FILE" << EOF
# ============================================================
# 新闻爬虫 - 环境变量配置
# 生成时间: $(date '+%Y-%m-%d %H:%M:%S')
# 修改方法: vi ${ENV_FILE}，然后运行 bash manage.sh restart
# ============================================================

# Web 服务对外端口
WEB_PORT=${WEB_PORT}

# Docker 镜像配置
DOCKER_IMAGE=${IMAGE_NAME}
IMAGE_TAG=${IMAGE_TAG}

# 时区
TZ=Asia/Shanghai

# 微信推送（PushPlus）
PUSH_ENABLED=$([ -n "$INPUT_PUSH_TOKEN" ] && echo "true" || echo "false")
PUSH_TOKEN=${INPUT_PUSH_TOKEN}
PUSH_TOPIC=

# 远程数据同步
SYNC_ENABLED=${SYNC_ENABLED_VAL}
SYNC_SERVER_URL=${INPUT_SYNC_URL}
SYNC_USERNAME=${INPUT_SYNC_USER}
SYNC_PASSWORD=${INPUT_SYNC_PASS}
EOF

    chmod 600 "$ENV_FILE"   # 仅 root 可读，防止密码泄露
    log_info "环境配置已创建: ${ENV_FILE}"
fi

# ============================================
# 第五步：构建 Docker 镜像
# ============================================
log_info "========== 第五步：构建 Docker 镜像 =========="

cd "$DEPLOY_DIR"
docker build -t "${IMAGE_NAME}:${IMAGE_TAG}" .

log_info "镜像构建完成: ${IMAGE_NAME}:${IMAGE_TAG}"

# ============================================
# 第六步：启动服务
# ============================================
log_info "========== 第六步：启动服务 =========="

cd "$DEPLOY_DIR"
$COMPOSE_CMD up -d

log_info "等待服务启动..."
sleep 10

# ============================================
# 第七步：检查服务状态
# ============================================
log_info "========== 第七步：检查服务状态 =========="

$COMPOSE_CMD ps

echo ""

# 检查 Web 服务是否可访问
if curl -sf "http://localhost:${WEB_PORT}/" > /dev/null 2>&1; then
    log_info "Web 服务启动成功!"
else
    log_warn "Web 服务可能还在启动中，请稍后检查"
fi

# 获取服务器外网IP
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || echo "8.162.9.143")

# ============================================
# 部署完成！
# ============================================
echo ""
echo "============================================"
echo -e "${GREEN} 部署完成！${NC}"
echo "============================================"
echo ""
echo "  Web 界面:  http://${SERVER_IP}:${WEB_PORT}"
echo "  API 接口:  http://${SERVER_IP}:${WEB_PORT}/api/news"
echo "  统计信息:  http://${SERVER_IP}:${WEB_PORT}/api/stats"
echo "  健康状态:  http://${SERVER_IP}:${WEB_PORT}/api/health"
echo ""
echo "  项目目录:  ${DEPLOY_DIR}"
echo "  数据存储:  Docker Volume (news-radar-data)"
echo "  日志存储:  Docker Volume (news-radar-logs)"
echo ""
echo "  常用命令:"
echo "    查看状态:  cd ${DEPLOY_DIR} && ${COMPOSE_CMD} ps"
echo "    查看日志:  cd ${DEPLOY_DIR} && ${COMPOSE_CMD} logs -f"
echo "    爬虫日志:  cd ${DEPLOY_DIR} && ${COMPOSE_CMD} logs -f crawler"
echo "    Web日志:   cd ${DEPLOY_DIR} && ${COMPOSE_CMD} logs -f web"
echo "    重启服务:  cd ${DEPLOY_DIR} && ${COMPOSE_CMD} restart"
echo "    停止服务:  cd ${DEPLOY_DIR} && ${COMPOSE_CMD} down"
echo "    启动服务:  cd ${DEPLOY_DIR} && ${COMPOSE_CMD} up -d"
echo ""
echo "  自动运行: 已配置 (Docker restart: unless-stopped)"
echo "    - 爬虫每 1 小时自动爬取一次"
echo "    - 每天凌晨 3 点自动清理 30 天前的旧数据"
echo "    - 服务异常自动重启"
echo "    - 服务器重启后 Docker 自动拉起容器"
echo ""
echo "  防火墙提醒:"
echo "    如果无法从外网访问，请确保已开放端口 ${WEB_PORT}:"
echo "    - 阿里云控制台 → 安全组 → 入方向 → 添加 ${WEB_PORT}/TCP"
echo "    - 或运行: firewall-cmd --permanent --add-port=${WEB_PORT}/tcp && firewall-cmd --reload"
echo "============================================"
