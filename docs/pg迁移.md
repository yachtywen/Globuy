# Globuy PostgreSQL 与 pgvector 迁移实施方案

> 状态：核心代码与容器基础设施已完成；真实 MySQL 数据搬运需在提供有效源库连接并进入维护窗口后执行。
> 最后更新：2026-08-20

## 1. 目标与边界

本次迁移将 PostgreSQL 17 作为用户、认证、会话、任务、商品、报价、价格观测、心愿库、长期记忆和 Outbox 的唯一关系型事实库。长期记忆的向量投影与检索由同一 PostgreSQL 实例中的 pgvector 承担；商品检索和 CategoryInsight 继续使用 OpenSearch，不改变既有 BM25、BGE-M3、RRF 与品类 Pipeline 契约。

迁移采用短停机切换，不做长期 MySQL/PostgreSQL 双写。MySQL 仅作为一次性迁移源，迁移后保留只读备份和回滚窗口，不再承接线上写入。

## 2. 目标架构

```text
FastAPI / Worker
    -> SQLAlchemy async + Psycopg
    -> PostgreSQL 17（全部关系型事实）
       -> memory_entries（结构化记忆事实）
       -> memory_candidates（待用户确认候选）
       -> memory_embeddings vector(1024)（检索投影）
       -> outbox_events（事务内投影任务）

Long-term Memory BaseStore
    -> 黑名单全量硬约束
    -> pgvector COSINE 语义召回
    -> PostgreSQL 关键词召回
    -> 无权重 RRF
    -> confidence × 时间软衰减
    -> Top-K 注入 Agent 上下文

ItemSearch / CategoryInsight
    -> OpenSearch（保持现状）
```

## 3. 已落地的代码改造

### 3.1 PostgreSQL 基础设施

- `compose.yaml` 增加 `pgvector/pgvector:0.8.6-pg17-bookworm`，暴露主机端口 `5433`，配置健康检查与独立数据卷。
- API、价格 Worker、商品 Outbox Worker、长期记忆 Worker 默认连接 Compose 内的 PostgreSQL。
- 正式运行依赖改为 `psycopg[binary,pool]` 与 `pgvector`；`asyncmy` 仅作为一次性 MySQL 迁移工具的可选依赖。
- Windows 入口统一使用 Selector Event Loop，兼容 Psycopg 异步连接。

### 3.2 Schema 与 Alembic

Alembic 会先启用 `vector` 扩展，再创建完整关系模型。长期记忆新增：

| 表/字段 | 用途 |
|---|---|
| `memory_entries.keywords` | 本地确定性关键词，用于关键词召回 |
| `memory_entries.lifecycle_status` | `active / archived / deleted` 生命周期 |
| `last_reinforced_at` | 衰减起点，确认或编辑时重置 |
| `reinforcement_count` | 用户重复确认次数 |
| `archived_at / purge_after` | 归档与最终清理时间 |
| `memory_embeddings.embedding vector(1024)` | BGE-M3 归一化向量投影 |
| `memory_embeddings.*metadata` | 模型、revision、维度、归一化和文本版本校验 |
| `memory_candidates` | Agent 抽取但尚未获得用户确认的候选记忆 |

关键索引包括：`memory_embeddings` 的 HNSW + `vector_cosine_ops`、`memory_entries.keywords` 的 GIN、候选状态/过期时间索引，以及原有业务唯一约束与外键。

### 3.3 候选确认闭环

Agent 运行完成后可以产生 `learned_preferences`，但不会直接写入永久记忆。系统将其保存为 `pending` 候选并发送 `CUSTOM/memory_candidate_created` 增量事件。个人中心展示待确认候选，用户可以确认、编辑后确认或拒绝：

- `GET /api/memory-candidates`
- `POST /api/memory-candidates/{candidate_id}/confirm`
- `POST /api/memory-candidates/{candidate_id}/reject`

只有确认成功后才写入 `memory_entries`、版本审计和事务 Outbox。重复确认同一稳定 key 会增强并更新原记录，而不是无限新增重复事实。

### 3.4 pgvector 混合检索

对外保持 LangGraph `BaseStore` 契约，namespace 固定为 `("users", user_id, "memories")`：

1. 全量读取当前用户的 active 黑名单，始终置于返回结果前部，不参与时间衰减。
2. 使用当前查询的 1024 维归一化向量执行 pgvector COSINE 召回。
3. 使用本地确定性关键词执行关键词召回，不调用额外模型或外部服务。
4. 两路候选用 `k=60` 的无权重 RRF 融合。
5. 普通偏好和历史记录乘以置信度与时间衰减因子；最终截取 Top-K。
6. 向量模型 ID、revision、维度或 normalized 元数据不一致时，不混用向量空间。

软衰减公式：

```text
decay(age) = 2 ^ (-age_days / half_life_days)
effective_score = rrf_score × confidence × decay(age)
```

平衡档参数：

| 类型 | 半衰期 | 最早归档时间 | 归档阈值 | 归档后清理 |
|---|---:|---:|---:|---:|
| `blacklist` | 不衰减 | 不自动归档 | 不适用 | 用户主动删除 |
| `preference` | 180 天 | 730 天 | 有效置信度 ≤ 0.10 | 730 天后 |
| `history` | 30 天 | 180 天 | 有效置信度 ≤ 0.05 | 365 天后 |

归档记录不会参与默认召回，但可以在个人中心查看并恢复；恢复会重置衰减起点并重新生成向量。待确认候选 30 天过期，拒绝或过期候选 90 天后清理。

## 4. MySQL 到 PostgreSQL 数据迁移

迁移工具为 `python -m app.database.migrate_mysql_to_postgres`，具有以下保护：

- 源连接必须是 MySQL，目标连接必须是 PostgreSQL。
- 目标 PostgreSQL 非空时拒绝运行，避免覆盖现有数据。
- 按外键拓扑顺序、分批复制所有源库中存在的业务表。
- 仅复制源表和目标表共有字段；旧记忆自动初始化关键词和生命周期字段。
- 每张表比较行数与按主键排序后的 SHA-256 规范摘要，任一不一致则整笔事务回滚。
- 为所有 active 记忆生成幂等 Outbox 请求，切换后由 Worker 补齐 pgvector 投影。
- 将不含凭据的核验清单写入 `output/migrations/mysql-to-postgres.json`。

维护窗口执行顺序：

```powershell
# 1. 停止 API 与所有写入 Worker，MySQL 保持只读可用。
# 2. 对 MySQL 做一致性备份，并记录备份校验值。
# 3. 启动 PostgreSQL 并建库。
docker compose up -d --wait postgres
alembic upgrade head

# 4. 仅在当前终端注入真实连接，不写入仓库或命令日志。
python -m pip install -e ".[migration]"
$env:GLOBUY_MIGRATION_SOURCE_URL = '<MYSQL_SQLALCHEMY_URL>'
$env:GLOBUY_DATABASE_URL = '<POSTGRESQL_SQLALCHEMY_URL>'
python -m app.database.migrate_mysql_to_postgres

# 5. 核对 manifest，启动记忆和商品 Outbox Worker，等待 backlog 清零。
python -m app.memory.outbox_worker --serve

# 6. 将 API/Worker 的 GLOBUY_DATABASE_URL 切换为 PostgreSQL，先启动 Worker，再启动 API。
# 7. 执行健康检查、登录、会话、心愿库、价格历史、记忆确认与检索冒烟测试。
```

## 5. 切换验收与回滚

必须同时满足以下条件才可解除维护窗口：

- Alembic 版本为 `20260820_0003`，`vector` 扩展和 HNSW/GIN 索引存在。
- 所有迁移表的 `source_count == target_count` 且摘要一致。
- API `/healthz` 返回数据库正常；注册、登录、CSRF、会话创建与 WebSocket replay 正常。
- Product/Offer、心愿库、价格历史和长期记忆 CRUD 可读写。
- 候选记忆在确认前不可召回，确认后 Outbox 能生成向量并被 pgvector 命中。
- 黑名单优先且不衰减；历史/偏好衰减、归档、恢复和清理符合配置。
- Outbox 无持续失败，日志中不包含数据库凭据或记忆原文。

回滚触发条件包括摘要不一致、核心 API 冒烟失败、持续 Outbox 积压或检索元数据不匹配。回滚时停止 PostgreSQL 写入端，将应用连接恢复到维护窗口前的只读保护后的 MySQL 快照版本；禁止把已在 PostgreSQL 产生的新写入盲目反灌 MySQL。若切换后已接受用户写入，应进入人工数据对账流程，而不是自动回滚。

## 6. 运维与观测

- 数据库：连接池占用、慢查询、锁等待、磁盘、WAL、备份恢复演练。
- pgvector：HNSW 索引大小、查询 P95/P99、候选池大小、向量模型元数据不匹配数。
- 记忆：候选确认率、拒绝率、过期数、active/archived 数量、日归档/清理量。
- Outbox：未发布数量、最老事件年龄、失败次数和重试率。
- 安全：数据库凭据只保存在本地 `.env` 或部署密钥系统；迁移 manifest、日志与 API 事件均不记录凭据和完整敏感记忆内容。

## 7. 当前完成度

已完成 PostgreSQL 容器、依赖、配置、ORM、Alembic、pgvector BaseStore、关键词 + 向量 RRF、候选确认 API、前端确认面板、软衰减 Worker、MySQL 一次性迁移工具和自动化测试。真实 pgvector 容器已经通过扩展、迁移、向量写入与召回验证。

当前环境没有有效 MySQL 源连接且没有运行中的 MySQL 容器，因此尚未执行真实历史业务数据的停机搬运。上线前还需要由运维提供只读迁移源连接、备份位置和维护窗口，并按第 4～5 节完成一次真实数据演练与正式切换。
