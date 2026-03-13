"""
媒体文件本地存储 - 图片下载/压缩/路径映射
图片存储路径: data/media/images/{date}/{hash}.jpg
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


def download_image(image_url: str, date_str: str = "") -> str:
    """
    下载单张图片到本地。
    返回本地相对路径（如 'images/2026-03-13/abc123.jpg'），失败返回空字符串。
    """
    if not image_url or not image_url.startswith(("http://", "https://")):
        return ""

    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    day_dir = os.path.join(MEDIA_IMAGE_DIR, date_str)
    _ensure_dir(day_dir)

    # 生成文件名
    file_hash = _url_hash(image_url)
    ext = ".jpg"  # 默认扩展名
    # 尝试从 URL 推断扩展名
    from urllib.parse import urlparse
    path = urlparse(image_url).path.lower()
    for e in _IMAGE_EXTENSIONS:
        if path.endswith(e):
            ext = e
            break

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

        # 读取内容
        data = resp.content
        if len(data) < 500:  # 太小的图片可能是追踪像素
            return ""

        # 压缩大图
        try:
            from PIL import Image
            img = Image.open(BytesIO(data))
            # 转 RGB（处理 RGBA/P 模式）
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
                ext = ".jpg"
                filename = f"{file_hash}{ext}"
                filepath = os.path.join(day_dir, filename)

            # 按比例缩放大图
            if img.width > MEDIA_IMAGE_MAX_WIDTH:
                ratio = MEDIA_IMAGE_MAX_WIDTH / img.width
                new_size = (MEDIA_IMAGE_MAX_WIDTH, int(img.height * ratio))
                img = img.resize(new_size, Image.LANCZOS)

            # 保存
            if ext in (".jpg", ".jpeg"):
                img.save(filepath, "JPEG", quality=85, optimize=True)
            elif ext == ".png":
                img.save(filepath, "PNG", optimize=True)
            elif ext == ".webp":
                img.save(filepath, "WEBP", quality=85)
            else:
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

        return f"images/{date_str}/{filename}"

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
