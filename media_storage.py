"""
媒体文件本地存储 - 图片下载/压缩/路径映射
图片存储路径: data/media/images/{date}/{hash}.jpg
所有图片格式（PNG/WebP/BMP等）统一转换为 JPEG 保存，节省 80-95% 磁盘空间。
视频只存 URL，不下载。
"""
import os
import hashlib
import logging
import requests
import random
from datetime import datetime
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    MEDIA_IMAGE_DIR, MEDIA_MAX_IMAGES, MEDIA_IMAGE_MAX_WIDTH,
    MEDIA_DOWNLOAD_TIMEOUT, USER_AGENTS,
)

logger = logging.getLogger(__name__)

# 支持的图片格式
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _url_hash(url: str) -> str:
    """生成 URL 的短哈希（前16位 SHA256）"""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _ensure_dir(path: str):
    """确保目录存在"""
    os.makedirs(path, exist_ok=True)


_MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB 图片大小上限


def _is_private_ip(hostname: str) -> bool:
    """检查主机名是否解析到私有/保留 IP 地址（SSRF 防护）"""
    import ipaddress
    import socket
    try:
        for info in socket.getaddrinfo(hostname, None, socket.AF_UNSPEC):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
                return True
    except (socket.gaierror, ValueError):
        return True  # 无法解析视为不安全
    return False


def download_image(image_url: str, date_str: str = "") -> str:
    """
    下载单张图片到本地。
    返回本地相对路径（如 'images/2026-03-13/abc123.jpg'），失败返回空字符串。
    """
    if not image_url or not image_url.startswith(("http://", "https://")):
        return ""

    # SSRF 防护：拒绝内网/私有 IP
    from urllib.parse import urlparse as _urlparse
    hostname = _urlparse(image_url).hostname or ""
    if _is_private_ip(hostname):
        logger.warning(f"SSRF 拦截: {image_url}")
        return ""

    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    day_dir = os.path.join(MEDIA_IMAGE_DIR, date_str)
    _ensure_dir(day_dir)

    # 所有图片统一保存为 JPEG（避免 PNG 原始大图占用大量磁盘）
    file_hash = _url_hash(image_url)
    ext = ".jpg"
    filename = f"{file_hash}{ext}"
    filepath = os.path.join(day_dir, filename)

    # 如果文件已存在，直接返回（去重）
    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        return f"images/{date_str}/{filename}"

    try:
        resp = requests.get(
            image_url,
            timeout=MEDIA_DOWNLOAD_TIMEOUT,
            headers={"User-Agent": random.choice(USER_AGENTS)},
            stream=True,
        )
        resp.raise_for_status()

        # 检查 Content-Type
        content_type = resp.headers.get("Content-Type", "")
        if "image" not in content_type and "octet-stream" not in content_type:
            return ""

        # 流式读取，限制大小
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=8192):
            total += len(chunk)
            if total > _MAX_IMAGE_SIZE:
                logger.debug(f"图片超过大小限制({_MAX_IMAGE_SIZE//1024//1024}MB): {image_url[:60]}")
                resp.close()
                return ""
            chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) < 500:  # 太小的图片可能是追踪像素
            return ""

        # 压缩大图
        try:
            from PIL import Image
            img = Image.open(BytesIO(data))

            # 统一转 RGB（处理 RGBA/P/L 等非 RGB 模式，确保能保存为 JPEG）
            if img.mode != "RGB":
                img = img.convert("RGB")

            # 按比例缩放大图
            if img.width > MEDIA_IMAGE_MAX_WIDTH:
                ratio = MEDIA_IMAGE_MAX_WIDTH / img.width
                new_size = (MEDIA_IMAGE_MAX_WIDTH, int(img.height * ratio))
                img = img.resize(new_size, Image.LANCZOS)

            # 统一保存为 JPEG（PNG 原图可达 6MB，转 JPEG 后约 100-300KB，节省 80-95% 磁盘）
            img.save(filepath, "JPEG", quality=85, optimize=True)

        except ImportError:
            # Pillow 不可用，直接保存原始数据
            with open(filepath, "wb") as f:
                f.write(data)
        except Exception as e:
            # Pillow 处理失败，保存原始数据
            logger.debug(f"图片压缩失败，保存原始: {image_url} | {e}")
            with open(filepath, "wb") as f:
                f.write(data)

        # 验证文件确实已保存且非空
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            return f"images/{date_str}/{filename}"
        else:
            logger.debug(f"图片保存后文件不存在或为空: {filepath}")
            return ""

    except requests.RequestException as e:
        logger.debug(f"图片下载失败: {image_url} | {e}")
        return ""
    except Exception as e:
        logger.debug(f"图片处理失败: {image_url} | {e}")
        return ""


def download_images_for_news(images: list, max_count: int = 0) -> list:
    """
    为一篇新闻批量下载图片。
    输入: [{"url": "...", "caption": "..."}, ...]
    输出: [{"url": "原始URL", "local": "images/2026-03-13/abc.jpg", "caption": "..."}, ...]
    """
    if not images:
        return []

    if max_count <= 0:
        max_count = MEDIA_MAX_IMAGES

    images = images[:max_count]
    date_str = datetime.now().strftime("%Y-%m-%d")
    results = []

    for img in images:
        img_url = img.get("url", "")
        local_path = download_image(img_url, date_str)
        results.append({
            "url": img_url,
            "local": local_path,
            "caption": img.get("caption", ""),
        })

    return results


def download_thumbnail(thumbnail_url: str) -> str:
    """下载缩略图，返回本地路径"""
    if not thumbnail_url:
        return ""
    return download_image(thumbnail_url)


def get_local_image_path(relative_path: str) -> str:
    """将相对路径转为绝对路径"""
    if not relative_path:
        return ""
    from config import MEDIA_DIR
    return os.path.join(MEDIA_DIR, relative_path)
