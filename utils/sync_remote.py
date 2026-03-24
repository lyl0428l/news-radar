"""
远程数据同步模块 - 将本地爬取的新闻推送到 News Radar 后端

对接已有后端API：
  POST /api/login          → 登录获取JWT Token
  POST /api/v1/contents    → 逐条推送新闻

每轮爬取完成后自动调用。支持：自动登录、Token缓存、失败重试。
"""
import logging
import requests
import time

logger = logging.getLogger(__name__)

# 缓存Token，避免每轮都登录
_cached_token = ""
_token_expire_time = 0


def _login(server_url: str, username: str, password: str) -> str:
    """登录获取JWT Token"""
    global _cached_token, _token_expire_time

    # Token还没过期就复用
    if _cached_token and time.time() < _token_expire_time:
        return _cached_token

    login_url = server_url.rstrip("/") + "/api/login"
    try:
        resp = requests.post(
            login_url,
            json={"username": username, "password": password},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 0 and data.get("data", {}).get("token"):
            _cached_token = data["data"]["token"]
            # Token有效期设为23小时（实际可能24小时，留1小时余量）
            _token_expire_time = time.time() + 23 * 3600
            logger.info("[同步] 登录成功")
            return _cached_token
        else:
            logger.error(f"[同步] 登录失败: {data.get('message', '未知错误')}")
            return ""
    except Exception as e:
        logger.error(f"[同步] 登录请求异常: {e}")
        return ""


def _map_item_to_content(item: dict) -> dict:
    """
    将本地爬虫数据格式映射为后端 /api/v1/contents 的格式

    本地格式 → 后端格式：
      title       → title（必填）
      content     → body（必填，回退到summary）
      source      → source
      source_name → source_name
      category    → category
      url         → url
      summary     → summary
      pub_time    → pub_time
      language    → language
      thumbnail   → thumbnail
    """
    # body必填，优先用content，回退到summary，再回退到title
    body = item.get("content", "") or item.get("summary", "") or item.get("title", "")

    content_data = {
        "title": item.get("title", "").strip(),
        "body": body,
    }

    # 可选字段，有值才传
    optional_fields = {
        "source": item.get("source", ""),
        "source_name": item.get("source_name", ""),
        "category": item.get("category", ""),
        "url": item.get("url", ""),
        "summary": item.get("summary", ""),
        "pub_time": item.get("pub_time", ""),
        "language": item.get("language", ""),
        "thumbnail": item.get("thumbnail", ""),
    }
    for k, v in optional_fields.items():
        if v:
            content_data[k] = v

    return content_data


def sync_to_remote(news_list: list, server_url: str, api_token: str = "",
                   batch_size: int = 50, max_retries: int = 3,
                   username: str = "", password: str = "",
                   total_timeout: int = 180) -> dict:
    """
    将新闻数据推送到远程服务器的 /api/v1/contents 接口

    参数：
        news_list:     新闻列表
        server_url:    服务器地址，如 "http://8.162.9.143"
        api_token:     （已废弃，保留兼容）
        username:      后端登录用户名
        password:      后端登录密码
        max_retries:   每条最大重试次数
        total_timeout: 整个同步任务的总超时秒数（默认180s），防止服务器不可达时阻塞整轮爬取

    返回：
        {"ok": True/False, "total_sent": N, "total_inserted": N, "errors": [...]}
    """
    if not news_list:
        return {"ok": True, "total_sent": 0, "total_inserted": 0, "errors": []}

    if not server_url:
        logger.debug("[同步] 未配置远程服务器地址，跳过同步")
        return {"ok": False, "total_sent": 0, "total_inserted": 0, "errors": ["未配置服务器地址"]}

    _sync_start = time.time()

    # 登录获取Token
    token = _login(server_url, username, password)
    if not token:
        return {"ok": False, "total_sent": 0, "total_inserted": 0,
                "errors": ["登录失败，无法获取Token"]}

    push_url = server_url.rstrip("/") + "/api/v1/contents"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    total_sent = 0
    total_inserted = 0
    errors = []
    skipped = 0

    for idx, item in enumerate(news_list):
        # 总体超时检查：超时则中止剩余条目，避免阻塞整轮爬取调度
        elapsed = time.time() - _sync_start
        if elapsed >= total_timeout:
            logger.warning(
                f"[同步] 已超过总超时限制 {total_timeout}s（已耗时 {elapsed:.0f}s），"
                f"中止剩余 {len(news_list) - idx} 条，已推送 {total_sent} 条"
            )
            errors.append(f"总超时中止（{elapsed:.0f}s > {total_timeout}s）")
            break

        title = item.get("title", "").strip()
        if not title:
            skipped += 1
            continue

        content_data = _map_item_to_content(item)

        # 带重试的推送
        success = False
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(
                    push_url,
                    json=content_data,
                    headers=headers,
                    timeout=15,
                )

                if resp.status_code in (200, 201):
                    total_sent += 1
                    total_inserted += 1
                    success = True
                    break

                elif resp.status_code == 401:
                    # Token过期，重新登录
                    logger.warning("[同步] Token过期，重新登录...")
                    global _cached_token, _token_expire_time
                    _cached_token = ""
                    _token_expire_time = 0
                    token = _login(server_url, username, password)
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                        # 不计入重试次数，立即重试
                        continue
                    else:
                        errors.append("重新登录失败")
                        break

                else:
                    if attempt < max_retries:
                        time.sleep(1 * attempt)
                    else:
                        err_text = resp.text[:100]
                        errors.append(f"{title[:20]}: HTTP {resp.status_code} {err_text}")

            except requests.exceptions.Timeout:
                if attempt < max_retries:
                    time.sleep(2 * attempt)
                else:
                    errors.append(f"{title[:20]}: 超时")

            except requests.exceptions.ConnectionError:
                if attempt < max_retries:
                    time.sleep(3 * attempt)
                else:
                    errors.append(f"{title[:20]}: 连接失败")

            except Exception as e:
                errors.append(f"{title[:20]}: {str(e)[:50]}")
                break

        # 每推送20条输出一次进度
        if (idx + 1) % 20 == 0:
            logger.info(f"[同步] 进度: {idx + 1}/{len(news_list)}, 成功{total_sent}条")

    ok = total_sent > 0 and len(errors) <= len(news_list) * 0.1  # 错误率低于10%算成功
    level = "info" if ok else "warning"
    getattr(logger, level)(
        f"[同步] 完成: 推送{total_sent}/{len(news_list)}条"
        + (f", 跳过{skipped}条" if skipped else "")
        + (f", 错误{len(errors)}个" if errors else "")
    )

    return {
        "ok": ok,
        "total_sent": total_sent,
        "total_inserted": total_inserted,
        "errors": errors[:10],  # 最多返回10条错误信息
    }
