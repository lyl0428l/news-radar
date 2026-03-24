# 新闻爬虫系统 - 完整链路时序图

## 1. 爬取主流程（定时调度 → 数据入库 → 推送通知）

```mermaid
sequenceDiagram
    autonumber
    participant S as Scheduler<br/>(scheduler.py)
    participant M as Main<br/>(main.py)
    participant TP as ThreadPool<br/>(并发池)
    participant C as Crawler<br/>(crawlers/*.py)
    participant W as Web<br/>(目标网站)
    participant MS as MediaStorage<br/>(media_storage.py)
    participant ST as Storage<br/>(storage.py)
    participant DB as SQLite<br/>(news.db)
    participant N as Notify<br/>(utils/notify.py)
    participant WX as WeChat<br/>(PushPlus)

    Note over S,WX: ===== 定时触发阶段 =====
    S->>S: 等待 CRAWL_INTERVAL_HOURS 小时
    S->>M: run_single_crawl()

    Note over M,WX: ===== 并发爬取阶段 =====
    M->>M: 获取启用站点列表 enabled_sites
    M->>TP: 创建线程池 (MAX_WORKERS=8)
    
    loop 每个启用的站点 (10个国内站)
        TP->>C: crawl_with_retry(name, mod)
        activate C
        C->>C: load_crawler(mod) 动态加载爬虫
        C->>W: HTTP请求 (带UA轮换+重试)
        W-->>C: 返回HTML/JSON
        C->>C: parse() 解析列表页
        C->>C: validate() 数据验证
        C->>C: 截断为 TOP 10
        C-->>TP: 返回 CrawlResult
        deactivate C
    end

    Note over C,WX: ===== 详情页抓取阶段 =====
    loop 每条新闻 (最多10条/站)
        TP->>C: fetch_detail(item)
        C->>W: HTTP请求详情页
        W-->>C: 返回详情HTML
        C->>C: extract_content() 提取正文/图片/视频
        C->>MS: download_image() 下载图片
        MS->>W: HTTP请求图片
        W-->>MS: 返回图片数据
        MS->>MS: 压缩/保存到 data/media/images/
        MS-->>C: 返回本地路径
    end

    Note over TP,WX: ===== 数据存储阶段 =====
    TP-->>M: 返回所有爬取结果 all_results
    M->>ST: save_to_db(all_results)
    ST->>ST: normalize_url() URL归一化
    ST->>ST: make_url_hash() 生成哈希
    ST->>DB: INSERT OR IGNORE (去重写入)
    DB-->>ST: 返回新增条数 new_count
    ST-->>M: 
    
    M->>ST: save_to_json(all_results)
    ST->>ST: 原子写入 data/json/{date}/{time}.json
    ST-->>M: 返回JSON路径

    Note over M,WX: ===== 推送通知阶段 =====
    M->>N: check_and_push_breaking(all_results)
    loop 命中关键词的重大新闻
        N->>N: 构建完整HTML正文
        N->>WX: PushPlus API 推送
        WX-->>N: 推送成功
    end
    
    M->>N: push_crawl_summary(all_results)
    N->>N: 按来源分组构建汇总
    N->>WX: PushPlus API 推送
    WX-->>N: 推送成功

    Note over S,WX: ===== 本轮完成，等待下轮 =====
    M-->>S: 爬取完成
```

## 2. Web 用户浏览流程

```mermaid
sequenceDiagram
    autonumber
    participant U as User<br/>(浏览器)
    participant FL as Flask<br/>(web/app.py)
    participant ST as Storage<br/>(storage.py)
    participant DB as SQLite<br/>(news.db)
    participant FS as FileSystem<br/>(media/)

    Note over U,FS: ===== 首页浏览 =====
    U->>FL: GET /
    FL->>ST: get_news(start_time=1小时前)
    ST->>DB: SELECT * FROM news WHERE crawl_time >= ?
    DB-->>ST: 返回新闻列表
    ST-->>FL: (news_list, total)
    FL->>FL: _enrich_thumbnails() 补充本地缩略图
    FL-->>U: 渲染 index.html

    Note over U,FS: ===== 查看详情 =====
    U->>FL: GET /news/{id}
    FL->>ST: get_news_by_id(news_id)
    ST->>DB: SELECT * FROM news WHERE id = ?
    DB-->>ST: 返回单条新闻
    ST-->>FL: item (含content_html, images)
    FL->>FL: _rewrite_content_images() 重写图片路径
    FL->>ST: mark_read(news_id)
    ST->>DB: UPDATE news SET is_read = 1
    FL-->>U: 渲染 detail.html

    Note over U,FS: ===== 加载本地图片 =====
    U->>FL: GET /media/images/{date}/{hash}.jpg
    FL->>FS: send_from_directory()
    FS-->>FL: 返回图片文件
    FL-->>U: 图片数据

    Note over U,FS: ===== 筛选/导出 =====
    U->>FL: GET /archive?source=xxx&keyword=xxx
    FL->>ST: get_news(source, keyword, page)
    ST->>DB: SELECT ... WHERE source=? AND title LIKE ?
    DB-->>ST: 返回筛选结果
    ST-->>FL: (news_list, total)
    FL-->>U: 渲染 archive.html

    U->>FL: GET /api/export?format=csv
    FL->>ST: get_news(limit=5000)
    ST->>DB: SELECT ...
    DB-->>ST: 
    ST-->>FL: news_list
    FL-->>U: 下载 news_export.csv

    Note over U,FS: ===== 手动触发爬取 =====
    U->>FL: POST /api/crawl
    FL->>FL: 启动后台线程
    FL-->>U: {"ok": true, "msg": "爬取任务已启动"}
    FL->>FL: import run_single_crawl()
    Note right of FL: 异步执行爬取流程
```

## 3. 模块依赖关系图

```mermaid
graph TB
    subgraph "入口层"
        SCH[scheduler.py<br/>定时调度]
        MAIN[main.py<br/>单轮爬取]
        WEB[web/app.py<br/>Web服务]
        SVC[news_service.py<br/>Windows服务]
    end

    subgraph "爬虫层"
        BASE[base.py<br/>爬虫基类]
        SINA[sina.py]
        NETEASE[netease.py]
        TENCENT[tencent.py]
        OTHER[...其他7个站点]
    end

    subgraph "工具层"
        EXTRACTOR[content_extractor.py<br/>正文提取]
        MEDIA[media_storage.py<br/>图片下载]
        NOTIFY[notify.py<br/>微信推送]
        SYNC[sync_remote.py<br/>远程同步]
    end

    subgraph "存储层"
        STORAGE[storage.py<br/>SQLite+JSON]
        MODELS[models.py<br/>数据模型]
    end

    subgraph "配置层"
        CONFIG[config.py<br/>全局配置]
        LOG_CONFIG[logging_config.py<br/>日志配置]
    end

    subgraph "数据存储"
        DB[(news.db<br/>SQLite)]
        JSON[data/json/<br/>JSON归档]
        IMAGES[data/media/images/<br/>本地图片]
    end

    SCH --> MAIN
    SVC --> SCH
    SVC --> WEB
    MAIN --> BASE
    MAIN --> STORAGE
    MAIN --> NOTIFY
    
    WEB --> STORAGE
    WEB --> CONFIG
    
    BASE --> SINA
    BASE --> NETEASE
    BASE --> TENCENT
    BASE --> OTHER
    
    BASE --> EXTRACTOR
    BASE --> MEDIA
    BASE --> CONFIG
    
    STORAGE --> MODELS
    STORAGE --> DB
    STORAGE --> JSON
    
    MEDIA --> IMAGES
    NOTIFY --> CONFIG
    SYNC --> CONFIG

    style SCH fill:#e1f5ff
    style MAIN fill:#e1f5ff
    style WEB fill:#e1f5ff
    style BASE fill:#fff4e1
    style STORAGE fill:#e8f5e9
    style CONFIG fill:#f3e5f5
```

## 如何使用

### 方式1: 在 Typora 中查看
直接用 Typora 打开此文件即可渲染

### 方式2: 在 VS Code 中查看
安装扩展: `Markdown Preview Mermaid Support`

### 方式3: 在线查看
1. 打开 https://mermaid.live
2. 粘贴上面 ```mermaid ``` 中的代码
3. 可导出 PNG/SVG/PDF

### 方式4: GitHub
直接在 GitHub 的 Markdown 文件中会自动渲染
