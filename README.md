zzzzzzzzzz# globuy

`globuy` 是一个基于 FastAPI、LangGraph 和 React 的对话式购物 Agent。当前仓库已收敛为适合服务器部署的两套向量能力：

- 商品搜索：PostgreSQL 候选目录 + 请求内 FAISS `IndexFlatIP`。
- 长期记忆：LangGraph `BaseStore` + PostgreSQL 17 + pgvector。

项目不再依赖 OpenSearch，也不保留 CategoryInsight/OpenSearch、商品索引投影、持久化 FAISS 或三塔实验链路。未配置模型、商品 Provider 或网页搜索 Provider 时，相应能力返回 `not_configured`，不会伪造商品、价格、库存或来源。

## 运行架构

```text
React / HTTP 202 / WebSocket
            |
      FastAPI + LangGraph
            |
    PostgreSQL 17 + pgvector
       |                 |
Product / Offer      长期记忆当前态
       |                 |
请求内 BM25 + FAISS   pgvector + 关键词 RRF
       |
一次 LLM 精排 + ShoppingSummary
```

商品搜索的固定行为：

1. Planner 生成结构化 `ShoppingIntent`。
2. `exact_product` 使用可靠型号或平台商品 ID 做确定性匹配。
3. `category_explore` 从 PostgreSQL 读取各平台新鲜候选；目录不足且已配置 Provider 时按需补充。
4. 硬过滤和保守同款聚合后，使用冻结的 `BAAI/bge-small-zh-v1.5` 生成 512 维归一化向量，在单次请求内创建 FAISS `IndexFlatIP`。
5. BM25 与 FAISS 名次使用无权重 RRF 融合，最多保留 36 个商品组，再执行一次严格 JSON 的 LLM 精排。
6. FAISS 编码不可用时，只对同一批真实候选做 BM25 降级，不切换其他向量后端。

长期记忆固定使用 PostgreSQL/pgvector：

- LangGraph `BaseStore` 是 Agent 读接口。
- 当前态、审计、沉淀游标和 Outbox 都保存在 PostgreSQL。
- BGE-small 512 维编码同时服务长期记忆（pgvector）与商品候选（请求内 FAISS）；两个向量空间严格隔离，不互用、不混写。推理统一使用仓库内本地 ONNX INT8 产物，不在线下载模型。
- 向量与关键词两路使用无权重 RRF；向量元数据不匹配时显式降级到关键词召回。
- 记忆投影失败采用租约、退避和 dead-letter，失败不会改变本轮商品结果。

## 主要组件

- Python 3.12、FastAPI、Uvicorn
- LangChain、LangGraph、WebSocket 增量事件
- PostgreSQL 17、pgvector 0.8
- FAISS CPU、ONNX Runtime、SentenceTransformers
- React、TypeScript、Vite
- 可选 Redis，仅用于登录失败限流
- 可选 Kimi K2.6、阿里云 IQS、Just One Provider、LangFuse

当前七个业务工具：`Planner`、`ChatFallback`、`WebSearch`、`ItemSearch`、`ItemPicker`、`PriceCompare`、`ShoppingSummary`。`dispatch_tool` 是同质 fork 元工具，不计入业务工具。

## 本地启动

前置条件：Docker、Conda、Node.js 20+。

```cmd
conda env create -f environment.yml
conda activate globuy
pip install -e .
copy .env.example .env
docker compose up -d --wait postgres redis
alembic upgrade head
python -m uvicorn app.api.server:app --host 127.0.0.1 --port 8000
```

另开终端启动前端：

```cmd
cd frontend
npm ci
npm run dev
```

访问 `http://127.0.0.1:5173`。后端健康检查：`http://127.0.0.1:8000/healthz`。

零付费本地运行保持：

```dotenv
GLOBUY_MODEL_PROVIDER=mock
GLOBUY_PRODUCT_PROVIDER=none
GLOBUY_WEB_SEARCH_PROVIDER=none
GLOBUY_OBSERVABILITY_PROVIDER=none
```

## Docker Compose

Compose 包含 `api`、`postgres`、`redis`、`price-worker`、`memory-consolidation-worker` 和 `memory-outbox-worker`。商品 FAISS 使用仓库内预准备的 ONNX INT8 模型，不需要独立容器。镜像内也包含 Alembic 文件，可在发布前执行：

```cmd
docker compose build
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head
docker compose up -d
docker compose ps
```

生产环境必须修改 PostgreSQL 密码、Cookie 安全选项、CORS 域名和外部 Provider 凭据。`.env` 已被 Git 忽略，禁止提交或写入镜像。

全新空数据库的首次迁移请走仓库设计的基线路径（直接 `alembic upgrade head` 会在 `20260823_0004` 处因当前 ORM 不再定义中间表 `memory_candidates` 而失败）：

```cmd
python -m alembic upgrade 20260721_0001
psql -h 127.0.0.1 -p 5433 -U globuy_app -d globuy -c "CREATE INDEX IF NOT EXISTS ix_memory_entries_keywords_gin ON memory_entries USING gin (keywords);" -c "CREATE INDEX IF NOT EXISTS ix_memory_embeddings_hnsw_cosine ON memory_embeddings USING hnsw (embedding vector_cosine_ops);"
python -m alembic stamp 20260901_0007
```

已有数据库升级仍使用普通 `alembic upgrade head`。

阿里云部署建议使用单实例 API。当前 `RunRegistry`、事件缓冲与 LangGraph checkpointer 仍是进程内实现，不能直接把 API 横向扩容为多个 worker；如需多实例，必须先迁移到共享任务、事件和 checkpoint 基础设施。

当前服务器对外访问端口固定为 `6412`（`.env` 已设 `GLOBUY_HOST=0.0.0.0` / `GLOBUY_PORT=6412`，`compose.yaml` 的 api 服务同步）。前端已按生产构建托管在同一端口：浏览器访问 `http://<IP>:6412` 即打开 `globuy Agent Console`（UI 与 `/api/v1`、WebSocket 同源同端口）。公网访问需在云安全组放行 TCP 6412；PostgreSQL（5433）与 Redis（6379）保持内网使用，后续部署其他项目时自行分配端口即可。

修改前端后重新发布：先在本机（或服务器）执行 `cd frontend && npm run build`，再重启 API。

## 配置重点

```dotenv
GLOBUY_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/globuy
GLOBUY_PRODUCT_SEARCH_BACKEND=faiss
GLOBUY_CANDIDATE_EMBEDDING_BACKEND=onnx
GLOBUY_CANDIDATE_EMBEDDING_ONNX_PATH=data/models/bge-small-zh-v1.5-onnx-int8
GLOBUY_MEMORY_STORE_BACKEND=pgvector
```

真实能力按需配置：Kimi、Just One、阿里云 IQS 和 LangFuse。所有凭据只放在服务器 `.env` 或密钥管理服务中。

## 验证

自动测试不得访问付费模型或真实商品 Provider：

```cmd
python -m ruff check app tests scripts
python -m compileall -q app tests scripts
python -m pytest -q
cd frontend
npm run test -- --run
npm run build
```

FAISS 候选链的离线评测与基准：

```cmd
python scripts/evaluate_candidate_hybrid.py eval/candidate-hybrid-retention.json
python scripts/benchmark_candidate_hybrid.py candidate-groups.json --query "通勤降噪耳机"
```

## 目录

```text
app/agent/       LangGraph AgentLoop、fork 与收敛保护
app/api/         FastAPI、任务、WebSocket 与事件重放
app/products/    Product/Offer、Provider、目录与价格刷新
app/recall/      请求内 BM25 + FAISS 候选选择
app/search/      候选 Schema、商品/记忆 Embedding 编码器
app/memory/      PostgreSQL/pgvector BaseStore、沉淀与 Outbox
app/database/    SQLAlchemy、PostgreSQL 与业务服务
frontend/        React 工作台
alembic/         PostgreSQL 数据库迁移
docs/            当前契约、状态和运维说明
```

当前实现、已验证结果和剩余部署差距以 [项目状态](docs/project-status.md) 与 [向量基础设施契约](docs/vector-infrastructure.md) 为准。
