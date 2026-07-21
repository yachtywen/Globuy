# globuy

`globuy` 是一个从零构建的 AgentLoop 学习项目，当前框架链路为：

`React 三栏购物工作台 -> FastAPI 202 后台任务/WebSocket 事件订阅 -> Think/Act/Observe/Reflect AgentLoop -> 工具/检索/压缩`

真实模型通过 LangChain 接入 DeepSeek 的 OpenAI 兼容接口。测试使用注入式假 Agent，
不会消耗真实 API Token。ItemSearch 已接入本地 OpenSearch 商品索引，使用 BM25、冻结 BGE-M3
向量和 RRF 检索 1000 条离线商品快照。CategoryInsight 使用另一套 CategoryCard 索引和加权
min-max Pipeline；两条检索链路互不复用索引或融合规则，也不会把快照描述为实时数据。

## 目录结构

```text
globuy/
├─ app/
│  ├─ agent/             # AgentLoop、fork、模型、工具分发与 system prompt
│  ├─ api/               # FastAPI、登录鉴权、后台任务、事件重放和安全产物
│  ├─ database/          # SQLAlchemy MySQL Schema、会话存储与业务事务服务
│  ├─ tools/             # 9 个核心购物工具
│  ├─ search/            # BGE-M3、OpenSearch 商品索引与 Hybrid/RRF 检索
│  ├─ category/          # CategoryCard ETL、独立索引、召回、精排、缓存与抽取
│  ├─ recall/            # 实验性编码与 Faiss ANN 基础设施（非 ItemSearch 主链路）
│  ├─ memory/            # MySQL 记忆权威数据、LangGraph BaseStore 与 OpenSearch Outbox
│  ├─ compress/          # 已接主图的 Cache Breakpoint 与确定性上下文压缩
│  ├─ eval/              # Rubric、Judge 与高分轨迹
│  ├─ prompt/            # YAML Prompt 配置
│  └─ utils/             # 安全路径与 Thread Context
├─ frontend/             # React + TypeScript + Vite 正式购物工作台
├─ docker/               # 容器配置说明
├─ datasets/             # 本地夹具与 OneBound 离线采集器
├─ examples/             # HTTP / WebSocket 调用示例
├─ tests/                # 后端测试
├─ output/               # 本地运行产物（不提交）
├─ uploaded/             # 用户上传文件（不提交）
├─ environment.yml       # Conda 环境定义
├─ pyproject.toml
└─ requirements.txt
```

## 手动测试：完整启动命令（PowerShell）

以下命令均从仓库根目录 `F:\agents-learning\globuy` 执行。要求已安装 Conda 和 Node.js 20.19
或更高版本；完整购物链路还需要 Docker Desktop。请不要把真实密钥写入命令、README 或 Git，
只写入已被忽略的本地 `.env`。

### 1. 首次安装

只在第一次运行项目时执行：

```powershell
Set-Location F:\agents-learning\globuy

# 首次创建 Conda 环境；如果 globuy 已存在，跳过这一行。
conda env create -f environment.yml
conda activate globuy

# environment.yml 已安装项目依赖；下面一行用于同步后续新增依赖。
python -m pip install -e ".[dev]"

# 只在 .env 不存在时复制，避免覆盖本机密钥。
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

Push-Location frontend
npm install
Pop-Location
```

环境已经存在时，日常只需：

```powershell
Set-Location F:\agents-learning\globuy
conda activate globuy
```

### 2A. 最短零付费烟雾测试

这条路径用于验证 React 页面、FastAPI、MySQL 会话、HTTP 202 后台任务、WebSocket 事件流、
取消和归档，不调用 DeepSeek 或 Tavily，也不要求启动 OpenSearch/Redis。
首次运行前必须按 [MySQL 持久化运行手册](docs/mysql-persistence.md) 为现有 `mysql8` 创建应用库和账号，并在 `.env`
配置 `GLOBUY_DATABASE_URL`，然后执行：

```powershell
alembic upgrade head
python -m app.products.import_snapshot
```

正式启动未配置 `GLOBUY_DATABASE_URL` 时会直接失败，不会静默回退 SQLite。只有维护旧测试或只读诊断时，
才可显式设置 `GLOBUY_LEGACY_SQLITE_ENABLED=true`；SQLite 历史文件不会导入 MySQL。

终端 1——启动后端：

```powershell
Set-Location F:\agents-learning\globuy
conda activate globuy
$env:GLOBUY_MODEL_PROVIDER = "mock"
$env:GLOBUY_WEB_SEARCH_PROVIDER = "none"
python -m uvicorn app.api.server:app --host 127.0.0.1 --port 8000 --reload
```

终端 2——启动前端：

```powershell
Set-Location F:\agents-learning\globuy\frontend
npm run dev -- --host 127.0.0.1
```

浏览器打开 <http://127.0.0.1:5173>。未登录时根路径显示注册/登录入口；登录后可进入品牌封面、
购物工作台和个人中心。
Mock 模式会返回固定回答，用来确认完整通信与界面链路，
不代表真实购物推理质量。

### 2B. 完整购物链路测试

首次运行完整链路时，先在终端 1 启动基础设施并构建索引：

```powershell
Set-Location F:\agents-learning\globuy
conda activate globuy

docker compose up -d opensearch redis
docker compose ps

# 等待 OpenSearch 完成启动，最长约 5 分钟。
$clusterHealth = $null
for ($attempt = 1; $attempt -le 60; $attempt++) {
    try {
        $clusterHealth = Invoke-RestMethod http://127.0.0.1:9200/_cluster/health
        break
    } catch {
        Start-Sleep -Seconds 5
    }
}
if ($null -eq $clusterHealth) { throw "OpenSearch 未在 5 分钟内就绪，请检查 docker compose logs opensearch" }
$clusterHealth

# 首次准备 1000 条离线商品快照并发布 globuy-products 索引别名。
python -m datasets.justone_headphones.repair_snapshot_text --root datasets/headphones_1000 --write
python -m datasets.justone_headphones.structure_itemsearch_dataset
python -m app.search.build_index

# 本机无付费验收：确定性生成品类卡片并发布 globuy-category 别名。
python -m app.category.build_index --deterministic
```

`python -m app.search.build_index` 首次会下载约 2.3 GB 的 `BAAI/bge-m3` 模型并生成商品向量，
商品文档从已导入的 MySQL `Product/Offer` 权威表读取，不再直接把 JSONL 当作索引数据源。
之后无需在每次启动时重新执行。`--deterministic` 适合本地功能测试；若要使用 DeepSeek 生成正式
品类卡片，在确认允许把聚合卡片草稿发送给外部模型后，改为执行：

```powershell
python -m app.category.build_index
```

然后编辑本地 `.env`，至少配置真实对话模型：

```dotenv
GLOBUY_MODEL_PROVIDER=openai-compatible
GLOBUY_LLM_MODEL=你的模型名
GLOBUY_LLM_API_KEY=你的 DeepSeek 或兼容服务密钥
GLOBUY_LLM_BASE_URL=https://api.deepseek.com
```

如果需要验证 WebSearch，再额外配置 Tavily；它会消耗真实 credit：

```dotenv
GLOBUY_WEB_SEARCH_PROVIDER=tavily
GLOBUY_TAVILY_API_KEY=你的 Tavily 密钥
```

不测试 WebSearch 时可设置 `GLOBUY_WEB_SEARCH_PROVIDER=none`。首次索引初始化完成后，以后每次启动
完整购物链路都按下面三终端方式执行。首版 RunRegistry/EventBroker 是单进程实现，不要增加
`--workers`。

终端 1——先确保 OpenSearch/Redis 健康，再启动后端：

```powershell
Set-Location F:\agents-learning\globuy
conda activate globuy

# 该命令可重复执行；已有容器和数据卷会继续复用，不会重建索引。
docker compose up -d --wait --wait-timeout 300 opensearch redis
docker compose ps

# 清除当前 PowerShell 会话中的临时覆盖值，让后端读取项目根目录的 .env。
Remove-Item Env:GLOBUY_MODEL_PROVIDER -ErrorAction SilentlyContinue
Remove-Item Env:GLOBUY_WEB_SEARCH_PROVIDER -ErrorAction SilentlyContinue
python -m uvicorn app.api.server:app --host 127.0.0.1 --port 8000 --reload
```

终端 2——启动前端：

```powershell
Set-Location F:\agents-learning\globuy\frontend
npm run dev -- --host 127.0.0.1
```

### 3. 启动后检查

在终端 3 执行：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
Invoke-RestMethod http://127.0.0.1:9200/_cluster/health  # 完整链路模式
docker compose exec redis redis-cli ping                 # 应返回 PONG
```

可访问地址：

- 前端工作台：<http://127.0.0.1:5173>
- FastAPI Swagger：<http://127.0.0.1:8000/docs>
- 后端健康检查：<http://127.0.0.1:8000/healthz>

#### 页面打开后立即显示 500

前端开发服务会把 `/healthz` 和 `/api/*` 代理到 `127.0.0.1:8000`。如果只启动了 Vite，
没有启动 FastAPI，Vite 代理会返回一个非结构化的 HTTP 500，工作台通常显示
`请求失败（500）`。这不是 Agent 执行失败，也不是 FastAPI 返回的
`INTERNAL_ERROR`；它表示前端代理无法连接后端。

先分别检查前后端端口：

```powershell
Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
Invoke-RestMethod http://127.0.0.1:8000/healthz
```

- 5173 有监听、8000 无监听：按上文“终端 1”命令启动 FastAPI。
- 8000 有监听，但直接请求 `/healthz` 仍返回 500：查看 Uvicorn 终端的异常日志。
- 8000 的 `/healthz` 返回 200，但 5173 仍返回 500：停止并重启 Vite，同时确认
  `frontend/vite.config.ts` 仍代理到 `http://127.0.0.1:8000`。
- Vite 因为 5173 被占用而自动改用 5174 时，应以终端打印的实际地址为准；
  这不会改变后端仍必须监听 8000 的要求。

### 4. 无付费自动测试

后端测试使用 Fake/Mock，不会请求真实 DeepSeek、Tavily 或商品 Provider：

```powershell
Set-Location F:\agents-learning\globuy
conda activate globuy
python -m ruff check app datasets tests
python -m compileall -q app datasets tests
python -m pytest -q
```

前端测试和生产构建：

```powershell
Set-Location F:\agents-learning\globuy\frontend
npm run test
npm run build
```

### 5. 停止服务

在后端和前端终端分别按 `Ctrl+C`，然后停止基础设施：

```powershell
Set-Location F:\agents-learning\globuy
docker compose stop opensearch redis
```

`docker compose stop` 会保留 OpenSearch 数据卷和已构建索引。除非明确要清空并重新建库，否则不要
执行 `docker compose down -v`。

## 基础设施与索引细节

项目采用独立的 `globuy` Conda 环境：

```powershell
conda activate globuy
```

项目依赖发生变化后，可以运行：

```powershell
python -m pip install -e ".[dev]"
```

启动本地基础设施（OpenSearch 承载商品与品类两个独立索引，Redis 缓存品类查询结果）：

```powershell
docker compose up -d opensearch redis
docker compose ps
```

首次使用 ItemSearch 时，先修复已缓存快照中的文本、生成统一候选文件并构建索引：

```powershell
python -m datasets.justone_headphones.repair_snapshot_text --root datasets/headphones_1000 --write
python -m datasets.justone_headphones.structure_itemsearch_dataset
python -m app.search.build_index
```

最后一条命令首次运行会下载约 2.3 GB 的 `BAAI/bge-m3` 模型，随后为 1000 条商品生成向量、
校验淘宝 350 / 京东 333 / 抖音 317 条平台计数，并发布 `globuy-products` 别名。模型优先使用
本地缓存；有可用 CUDA 时自动使用 GPU，否则回退 CPU。Faiss 仍作为 Python 实验依赖保留，
但不进入 ItemSearch 主链路。

### 给 CategoryInsight 提供卡片

当前项目采用“提供商品快照，由离线任务生成卡片”的方式，不要求在线请求直接拼写卡片。
默认输入是 `datasets/headphones_1000/structured/itemsearch_candidates.jsonl`；每行至少需要
`item_id/platform/title/price/currency`，销量、评分和属性用于增强证据。快照时间来自同级数据包的
`state/collection_state.json.updated_at`，规范品类及别名维护在
`app/category/category_aliases.yml`。

先做不调用付费模型、也不发布索引的本地审计：

```powershell
python -m app.category.build_index --deterministic --no-publish
```

产物位于 `output/category/{build_id}/`：`category_cards.jsonl` 是最终卡片列表，
`audit_manifest.json` 记录源文件哈希、行数、拒绝数、卡片类型计数、抽取器与向量元数据。
单张卡片固定包含：

```json
{
  "card_id": "稳定 ID",
  "category": "耳机",
  "card_type": "bestseller | attribute | price_range",
  "summary": "检索与提炼使用的短摘要",
  "raw_evidence": ["1～3 条、每条不超过 80 字的可反查证据"],
  "last_updated": "带时区的快照时间",
  "confidence": 0.9
}
```

生产构建使用项目已配置的 DeepSeek 对确定性统计结果做严格 JSON 压缩，并在全部门禁通过后创建
版本化物理索引、校验数量与类型，再原子切换 `globuy-category` 别名：

```powershell
python -m app.category.build_index
```

该命令会把聚合后的卡片草稿发送到 `.env` 指向的外部模型端点；执行前应确认数据允许外发。
`--deterministic` 仅用于测试、本机验收和故障排查，不代替生产抽取门禁。若要切换数据集、品类或
别名文件，分别配置 `GLOBUY_CATEGORY_DATASET_PATH`、`GLOBUY_CATEGORY_SOURCE_CATEGORY` 和
`GLOBUY_CATEGORY_ALIASES_PATH`。

在线召回默认先取 30 张卡片，再按 quick=8、deep=15 截取。可选的本机 Cross-Encoder 服务需
实现以下固定接口，并通过 `GLOBUY_RERANKER_ENDPOINT=http://127.0.0.1:8010/rerank` 接入：

```text
POST /rerank
input:  {"query": "...", "candidates": ["..."]}
output: {"scores": [0.1, 0.9]}
```

目标模型是冻结的 `BAAI/bge-reranker-v2-m3`。当粗排候选多于目标 Top-K、确实需要精排时，端点
未配置或超时会保留 Hybrid 粗排并把结果标为 `partial/hybrid_without_rerank`；候选不足时直接
旁路精排。Redis 不可用只会跳过缓存，不改变事实结果。

后端地址：<http://127.0.0.1:8000>，Swagger：<http://127.0.0.1:8000/docs>。

另开终端启动前端：

```powershell
cd frontend
npm install
npm run dev
```

前端地址：<http://127.0.0.1:5173>。

前端路由：

- `/`：未登录时在 Globuy 彩铅品牌封面完成注册或登录；已登录时显示品牌入口。
- `/assistant`：进入或创建当前活动会话。
- `/assistant/{thread_id}`：恢复指定活动或归档会话；归档仍由后端强制只读。
- `/wishlist`：查看默认心愿库、价格变化、近七日价格历史并管理心愿商品。
- `/account`：查看个人长期记忆及现有个人数据入口。

品牌封面和工作台共用生成后经过压缩的 WebP 资产：彩铅地球购物车主视觉、Globuy 小型品牌标记
和商品图片失败占位。工作台继续使用现有正式 API client、reducer 和 WebSocket Hook；新增的会话
搜索只过滤当前已加载归档页，商品比较只比较真实 `result.picks` 字段，不生成匹配度、原价或库存。
完整前后端差异见 `docs/frontend-backend-gap.md`。

正式工作台不提供游客模式。注册或登录后，后端从 HttpOnly Cookie 会话确定用户身份并创建唯一活动会话；
前端不再提交 `user_id`。发送需求使用 `POST /api/v1/tasks`，WebSocket 也校验 Cookie、会话和运行归属，
只负责订阅、重放事件和心跳；刷新页面不会取消后台任务。桌面
显示历史/对话/运行轨迹三栏，平板和手机使用侧边抽屉；归档会话强制只读。运行轨迹只展示阶段、
工具和 fork 摘要，不展示思维链。结构化结果明确标记为离线数据快照，`learned_preferences` 只显示
为“本次识别、尚未持久化”。

前端验证命令：

```powershell
cd frontend
npm run test
npm run build
```

## 接入真实模型

项目使用 `ChatOpenAI` 接入 OpenAI 或兼容 OpenAI Chat Completions 的服务。修改 `.env`：

```dotenv
GLOBUY_MODEL_PROVIDER=openai-compatible
GLOBUY_LLM_MODEL=你的模型名
GLOBUY_LLM_API_KEY=你的密钥
GLOBUY_LLM_BASE_URL=https://你的兼容接口
```

使用 OpenAI 官方接口时，`GLOBUY_LLM_BASE_URL` 可留空。所有项目环境变量均使用
`GLOBUY_` 前缀，以免与系统或其他项目的同名变量冲突。修改环境变量后需要重启后端。

### 配置 Tavily WebSearch

`web_search` 已通过 Tavily 官方 `POST /search` 接入真实网页检索。真实 key 只写入被 Git 忽略的
本地 `.env`，不要提交到仓库或粘贴进日志：

```dotenv
GLOBUY_WEB_SEARCH_PROVIDER=tavily
GLOBUY_TAVILY_API_KEY=轮换后的新 key
GLOBUY_TAVILY_SEARCH_DEPTH=basic
```

默认 `basic` 每次搜索消耗 1 Tavily credit，最多返回 10 条；实现不会自动重试，也不请求
Tavily 生成答案，只向 Agent 返回标题、HTTP(S) URL、截断摘要、相关度、抓取时间、request ID
和实际 credit 用量。未配置 key 时返回 `not_configured`；认证、限流、额度和服务异常会返回脱敏的
结构化错误。网页摘要是不可信外部数据，不能作为 Prompt 指令执行，也不会在线写入 Category 索引。

## 正式任务与会话接口

- `POST /api/v1/threads`：创建首个活动会话，或原子归档当前会话并创建新会话
- `GET /api/v1/threads`：按用户列出活动或归档会话，归档支持稳定游标分页
- `GET /api/v1/threads/{thread_id}`：读取消息、run、结构化结果和产物
- `POST /api/v1/tasks`：创建后台 Agent run，立即返回 `202 + thread_id + run_id`
- `GET /api/v1/threads/{thread_id}/runs/{run_id}`：查询运行或持久化终态
- `POST /api/v1/threads/{thread_id}/runs/{run_id}/cancel`：幂等取消准确 run
- `WS /api/v1/ws/{thread_id}?run_id=...&after=...`：只订阅、重放事件和接收心跳
- `GET /api/v1/threads/{thread_id}/runs/{run_id}/files`：列出真实产物
- `GET /api/v1/threads/{thread_id}/runs/{run_id}/files/{file_id}`：安全下载产物

正式 WebSocket 不再接收用户消息，也不会在连接内启动 Agent。所有消息使用
`type="monitor_event"`、`schema_version="1.0"`、`event`、`sequence`、`thread_id`、`run_id`
组成的统一信封；首次连接和重连使用 `after` 补发事件，服务端每 20 秒发送应用层 heartbeat。
任务开始后会先发送一条临时初始化状态“收到你的消息了~正在初始化本次对话”；它用于覆盖长期记忆
召回等首个 Agent 阶段前的等待，不会保存为 assistant 对话消息。
`/api/v1/chat` 和 `/api/v1/files` 仅保留为本地兼容调试入口，不属于正式前端流程。

会话、消息、run 终态和产物 manifest 默认持久化到
`output/globuy-sessions.sqlite3`。首版任务表和事件缓冲仍是单进程实现，因此 Uvicorn 必须使用
一个 worker；刷新或 WebSocket 断开不会取消后台任务，但进程重启不能恢复 LangGraph
checkpoint。重启时遗留 run 会标记为 `interrupted`，有内容的活动会话会自动归档。

ShoppingSummary 会在工具内部使用共享对话模型额外调用一次 LLM，因此一次完整购物任务可能比
普通工具循环多一次模型调用。该调用默认超时 60 秒、不自动重试，并继承取消与事件回调。
长期记忆目前只预留 BaseStore 接口，不会读取或写入本地 JSON/OpenSearch；具体边界见
`docs/itempicker-agentloop-10-14-implementation.md`。

## 错误码与具体情况

### 1. 如何判断错误来自哪一层

FastAPI 的 HTTP 非 2xx 响应使用统一结构：

```json
{
  "error": {
    "code": "RUN_NOT_FOUND",
    "message": "指定任务不存在或已过期",
    "retryable": false,
    "details": {}
  }
}
```

- 有上述 JSON 信封：请以 `error.code` 和 `retryable` 为准，不要只看 HTTP 数字。
- 只有纯文本/HTML 500，没有 `error` 字段：开发环境中通常是 Vite 代理连不到
  8000 端口，参见上文“页面打开后立即显示 500”。
- HTTP 已返回 `202`，但随后页面提示任务失败：这是后台 run 的异步错误，查看
  WebSocket `RUN_ERROR` 或 run 状态接口，不属于该 POST 的 HTTP 500。
- `not_configured`、`partial`、`insufficient_data` 和 `error` 也可以是工具结果状态，
  不等于 HTTP 错误码。

### 2. HTTP API 错误码

| HTTP | `error.code` | 何时出现 | 是否建议原样重试 | 处理方式 |
|---|---|---|---|---|
| 401 | `INVALID_CREDENTIALS` | 登录邮箱不存在、账号不可用或密码不匹配 | 否 | 核对邮箱和密码；不要据此判断邮箱是否存在 |
| 404 | `THREAD_NOT_FOUND` | `thread_id` 不存在，或不属于当前登录用户 | 否 | 刷新会话列表，回到当前活动会话 |
| 404 | `RUN_NOT_FOUND` | `thread_id/run_id` 组合不存在、不匹配或已过期；精确取消时也会检查 | 否 | 重新读取 thread 详情，不要对旧 `run_id` 继续操作 |
| 404 | `FILE_NOT_FOUND` | 产物不存在、不属于该 run，或文件路径/符号链接未通过安全检查 | 否 | 重新请求产物列表，不要自行拼接磁盘路径 |
| 404 | `NOT_FOUND` | 请求了没有注册的 HTTP 路由 | 否 | 核对 `/api/v1` 前缀、路径和端口 |
| 409 | `EMAIL_ALREADY_REGISTERED` | 注册邮箱已经存在 | 否 | 切换到登录；该错误不再用于其他注册事务约束失败 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 注册幂等键已用于不一致的请求 | 否 | 修改注册内容后生成新的幂等键 |
| 409 | `ACTIVE_THREAD_CHANGED` | 多标签页/并发请求已替换活动会话，客户端携带的旧 thread 不再是当前活动会话 | 否 | 重新拉取活动会话后再操作 |
| 409 | `THREAD_ARCHIVED` | 尝试在只读归档会话中创建任务或注册产物 | 否 | 返回当前活动会话 |
| 409 | `RUN_REPLACEMENT_TIMEOUT` | 同一 thread 的旧 run 在取消宽限内未清理完，后端拒绝让新旧 run 重叠 | 是 | 稍后重试；同时检查工具/模型是否响应取消 |
| 409 | `RUN_CANCELLATION_FAILED` | 旧 run 取消后仍未进入终态，因此不允许继续替换或归档 | 是 | 先查询 run 状态和 Uvicorn 日志，确认旧任务已清理再重试 |
| 422 | `VALIDATION_ERROR` | 缺少必填字段、存在未允许字段、长度/枚举/ID 格式不合法、`query` 去空白后为空，或 `limit` 超过最大值 | 否 | 根据 `details.errors` 修正请求 |
| 422 | `INVALID_CURSOR` | 归档游标损坏/伪造，或在活动会话列表上使用归档游标 | 否 | 丢弃本地游标，从第一页重新加载 |
| 422 | `INVALID_ARTIFACT_PATH` | 后端产物注册尝试使用绝对路径或 `..` 越界；这是内部安全门禁 | 否 | 修正产物生成器；后台 run 中触发时通常最终表现为 `AGENT_RUN_FAILED` |
| 500 | `REGISTRATION_FAILED` | 邮箱不存在，但注册事务中的关联记录约束失败并已整体回滚 | 是 | 保留用户输入，稍后重试并检查 Uvicorn/MySQL 日志 |
| 500 | `INTERNAL_ERROR` | FastAPI 路由出现未捕获异常 | 是 | 记录请求路径和时间，查看 Uvicorn 日志；响应不会暴露堆栈 |
| 503 | `SERVICE_SHUTTING_DOWN` | FastAPI lifespan 正在关闭时仍尝试创建新 run | 是 | 等待服务重启完成后重试 |
| 保留原 HTTP 状态（常见 405） | `HTTP_ERROR` | Starlette 产生的非 404 HTTP 错误，例如用错请求方法 | 否 | 核对 Swagger 中的 method 和接口契约 |

正式业务接口使用服务端 Cookie 会话，并会对未登录和 CSRF 校验失败分别返回 HTTP 401/403；不在
`GLOBUY_CORS_ORIGINS` 中的页面如果绕过 Vite 代理直连 8000，浏览器可能报 CORS 错误。
此时 JavaScript 通常只能观察到网络失败，而不是可读的 API JSON。

### 3. 前端本地错误码

这些是 `ApiClientError` 在浏览器内生成的诊断码，不是后端 HTTP 响应体中的业务码。

| 前端 code / status | 具体情况 | 处理方式 |
|---|---|---|
| `NETWORK_ERROR` / `0` | `fetch` 未取得 HTTP 响应：服务未启动、连接被拒绝、DNS/TLS/CORS 阻断等 | 检查 5173/8000 端口、`/healthz`、浏览器 Network 面板和 Uvicorn 日志 |
| `SERVICE_UNAVAILABLE` / 实际 HTTP 状态 | 封面的 `/healthz` 请求收到非 2xx；包括 Vite 代理无法连接 FastAPI 时的 500 | 先直接请求 `http://127.0.0.1:8000/healthz` 区分代理错误与后端错误 |
| `REQUEST_FAILED` / 实际 HTTP 状态 | API 返回非 2xx，但响应不是约定的 `{"error": ...}` JSON；前端会显示 `请求失败（状态码）` | 查看响应体；开发环境 500 优先检查 Vite 代理和 8000 端口 |
| 后端 `error.code` / 实际 HTTP 状态 | API 返回了合法结构化错误；前端保留后端的 code/message/retryable/details | 按上一表处理 |

### 4. WebSocket 关闭码和控制事件

| 关闭码/事件 | 具体情况 | 客户端行为 |
|---|---|---|
| `4401` | WebSocket 未携带有效登录 Cookie，或当前用户不拥有该 thread/run | 重新登录；不要对无权访问的资源重连 |
| `4400` | `after` 不是非负整数，或 `thread_id/run_id` 缺失、格式无效 | 修正订阅 URL，不应对原 URL 无限重连 |
| `4404` | thread/run 不存在，或该 thread 已归档而不再建立事件流 | 调用会话/run 状态接口，必要时返回活动会话 |
| `4408` | 该订阅者消费太慢，有界队列已满 | 使用最后成功处理的 `sequence` 重连 |
| `1000` | 客户端在 run 终态或组件卸载时正常关闭 | 终态不重连 |
| `1006` | 浏览器没有收到关闭帧，常见于后端停止、代理不可达或网络中断；这不是服务端显式发出的码 | 非终态时按 1/2/4/8/15 秒退避重连 |
| `CUSTOM/replay_gap` | `after` 早于事件缓冲中仍保留的最早序号；这不是关闭码 | 调用 run 状态和 thread 详情恢复终态/消息 |

### 5. 异步 run 错误码

`POST /api/v1/tasks` 返回 202 只代表任务已受理。执行期错误通过 WebSocket 和
`GET /api/v1/threads/{thread_id}/runs/{run_id}` 暴露：

| run `error.code` | 终态 | 具体情况 | 处理方式 |
|---|---|---|---|
| `AGENT_RUN_FAILED` | `failed` | AgentLoop、模型、工具或持久化在后台执行中抛出未恢复异常；同时发送 `RUN_ERROR`，`retryable=true` | 查看 run 状态与 Uvicorn 日志，确认外部服务/配置后可新建 run 重试 |
| `SERVER_RESTART` | `interrupted` | FastAPI 启动时发现 MySQL 中仍为 `starting/running/cancelling` 的旧 run；首版不恢复进程内 checkpoint | 打开自动归档的历史查看部分结果，在新活动会话重新发起需求 |

`TASK_CANCELLED` 表示用户取消的 `cancelled` 终态，不是故障错误码；如果已经
产生部分文本，该消息会以 `is_partial=true` 持久化。

### 6. 工具状态与 Tavily WebSearch 错误

工具失败优先以结构化结果返回 AgentLoop，因此不会直接变成 HTTP 4xx/5xx：

| 状态 | 含义 |
|---|---|
| `not_configured` | API key、本地模型、OpenSearch 索引或必要端点未配置；必须如实降级，不伪造结果 |
| `partial` | 有可用结果，但运费、精排或部分数据缺失 |
| `insufficient_data` / `incomplete` | 证据或候选不足，不能生成完整结论 |
| `error` | 已配置工具运行失败；AgentLoop 可以收敛为部分结果，未捕获时才会变成 `AGENT_RUN_FAILED` |

Tavily 的 `web_search` 在 `status="error"` 时还会返回以下脱敏子错误码：

| Tavily `error.code` | 具体情况 | `retryable` |
|---|---|---|
| `invalid_query` | 搜索词去空白后为空 | `false` |
| `authentication_failed` | Tavily 返回 401/403，API key 无效、被撤销或无权限 | `false` |
| `rate_limited` | Tavily 返回 429 | `true` |
| `usage_limit` | Tavily 返回 432/433，额度或套餐限制 | `false` |
| `timeout` | Tavily 请求超时 | `true` |
| `provider_unavailable` | HTTP 传输/网络层错误 | `true` |
| `invalid_response` | Tavily 返回非 JSON，或 JSON 缺少 `results` 列表 | `true` |
| `provider_error` | 其他 Tavily 非 200 响应 | 仅上游 HTTP 5xx 为 `true` |

WebSearch Provider 不是 `tavily`、或 key 缺失时返回 `status="not_configured"`，而不是
`authentication_failed`。响应不会包含 API key、完整上游错误体或堆栈。

## 测试

```powershell
python -m pytest -q
```

## OneBound 耳机数据采集

独立采集器位于 `datasets/onebound_headphones/`。真实凭据只写入被 Git 忽略的 `.env`，生成的
原始响应、标准化 JSONL/CSV、请求账本和断点状态也不会提交。执行前先检查预算：

```powershell
python -m datasets.onebound_headphones.collect dry-run
python -m datasets.onebound_headphones.collect collect --smoke-only
python -m datasets.onebound_headphones.collect resume
```

默认不并发、不自动重试，最多 70 次搜索、20 次详情，估算费用硬上限 2 元。供应商额度或接口
权限发生变化后，按该目录 README 的说明使用显式单次重试开关。

示例 01、02 需要先启动后端；其余示例可以直接运行：

```powershell
python examples/01_chat_http.py
python examples/02_chat_ws.py
python examples/03_planner_tool.py
python examples/04_compare_pipeline.py
python examples/05_three_tower_recall.py
python examples/06_long_term_memory.py
python examples/07_context_compression.py
python examples/08_evaluation.py
```

## Docker 基础服务

`docker compose up -d opensearch redis` 会启动固定选型 OpenSearch 3.7.0 和 Redis 7。
商品索引使用 Lucene HNSW/COSINE、`cjk` 全文分析和无权重 RRF Search Pipeline；品类卡片索引
使用独立的 0.5/0.5、0.7/0.3、0.9/0.1 min-max Pipeline。当前项目不训练或微调检索模型；
开发 compose 关闭了 OpenSearch Security 插件，仅限本机开发使用。
