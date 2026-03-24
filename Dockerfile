# ============================================
# 新闻爬虫 Docker 镜像 - 多阶段构建
# ============================================

# ---------- 基础镜像 ----------
FROM python:3.11-slim AS base

# 设置环境变量：不生成 .pyc 文件、不缓冲输出（实时看到日志）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ---------- 依赖安装阶段 ----------
FROM base AS deps

# 安装系统依赖（lxml 编译需要）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        libxml2-dev \
        libxslt1-dev \
        libjpeg62-turbo-dev \
        zlib1g-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------- 最终运行阶段 ----------
FROM base AS runtime

# 安装运行时所需的共享库（不需要编译工具）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libxml2 \
        libxslt1.1 \
        libjpeg62-turbo \
        zlib1g \
        curl && \
    rm -rf /var/lib/apt/lists/*

# 从依赖阶段复制已安装的 Python 包
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# 复制项目源码
COPY . .

# 创建数据和日志目录
RUN mkdir -p data/json data/media/images logs

# 暴露 Web 服务端口
EXPOSE 5000

# 健康检查：每 30 秒检测 Web 服务是否存活
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

# 默认启动调度器（爬虫定时任务）
CMD ["python", "scheduler.py"]
