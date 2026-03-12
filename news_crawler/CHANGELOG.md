# 变更日志 (CHANGELOG)

## v1.2.0 - 2026-03-11

### 新增功能
- **数据导出**: Web UI 支持导出 CSV（UTF-8 BOM 兼容 Excel）和 JSON 格式
- **全文搜索**: 搜索范围从 标题+摘要 扩展到 标题+摘要+正文
- **错误通知横幅**: 首页顶部显示爬虫异常警告，链接到健康状态详情页
- **SIGTERM 优雅退出**: scheduler.py 注册 SIGTERM 信号处理（Linux/Mac 兼容）
- **手动触发爬取**: Web UI "立即爬取" 按钮，异步执行+轮询状态+完成自动刷新
- **自动刷新提示**: 前端每 5 分钟检测新数据，绿色提示条提醒刷新
- **爬取轮次浏览**: 新增"爬取轮次"下拉框，可按某次爬取时间点查看快照
- **精确时间筛选**: 日期选择器升级为 datetime-local，精确到小时分钟
- **轮次与时间互斥**: 选爬取轮次自动清空时间范围，反之亦然

### Bug 修复（严重）
1. **BUG-1 重试机制失效**: `base.py run()` 不再吞掉异常，异常正确传播到 `crawl_with_retry()` 实现指数退避重试
2. **BUG-2 时间解析缺失**: CNN/BBC/NYT 的 RSS `published` 字段现在通过 `parse_time()` 解析 RFC 2822 格式；Sina 备选 API 解析 `ctime` 时间戳
3. **BUG-3 CCTV 死 API**: 改进空结果检测逻辑，死 API 快速跳过，不再浪费重试时间
4. **BUG-4 腾讯垃圾数据**: 过滤热榜第一条页面描述条目（无效 URL、站名标题），rank 独立计数
5. **BUG-5 DB 连接泄漏**: storage.py 全部 7 个 DB 函数加 `try/finally` 确保连接关闭

### Bug 修复（中等）
6. **BUG-6 分页偏移**: `get_news` 返回 `(list, total)` 元组，Web UI 用精确 `total_pages` 计算分页
7. **BUG-7 LIKE 通配符注入**: 搜索关键词中的 `%` 和 `_` 被正确转义
8. **BUG-8 JSON 写入竞争**: 新增 `_json_write_lock`，读取→合并→写入整体加锁
9. **BUG-9 bat 硬编码路径**: 三个 `.bat` 改为自动检测 Python 路径，优先 `D:\Python`，回退 PATH

### Bug 修复（二次审计）
10. **手动爬取竞争条件**: `_crawl_running` 检查+设置放入同一把锁内
11. **指数退避注释错误**: 修正注释为 30s, 60s（与代码一致）
12. **SQLite 事务冲突**: `isolation_level=None` + 显式 `BEGIN/COMMIT`，消除自动事务冲突
13. **空结果误报失败**: 返回 0 条结果的站点不再计入失败列表
14. **URL 归一化重复**: `models.py` 改为 `from storage import normalize_url`，消除代码重复
15. **Playwright 浏览器泄漏**: 腾讯爬虫 Playwright 备选方案加 `try/finally` 确保浏览器关闭

---

## v1.1.0 - 2026-03-11

### 重大重构
- **TOP 10 热榜**: 全部 15 个爬虫从"批量抓取"重构为"热度排名 TOP 10"模式
- **rank 字段**: 数据库、基类、存储、Web UI 全面支持排名（1=最热）
- **Web UI 中文化**: 所有界面文字翻译为中文
- **综合审计**: 深度代码审查发现 28 个 bug + 10 个缺失功能

---

## v1.0.0 - 2026-03-11

### 初始版本
- 15 个新闻站点爬虫（10 国内 + 5 国外）
- SQLite 存储 + JSON 归档
- Flask Web 浏览界面
- APScheduler 定时调度（每小时）
- 多策略爬取（API → HTML → RSS）
- URL 归一化去重
- 爬取健康监控面板
- Windows `.bat` 启动脚本
