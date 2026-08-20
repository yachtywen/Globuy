# Docker 基础设施

`compose.yaml` 管理以下本地服务：

- PostgreSQL 17 + pgvector 0.8：全部关系型业务事实和长期记忆向量检索。
- OpenSearch 3.7：ItemSearch 商品索引与 CategoryInsight 知识索引。
- Redis 7：普通缓存与登录限流，不承担长期记忆存储。
- FastAPI、价格刷新、商品 Outbox、记忆 Outbox Worker。

最小数据库启动：

```powershell
docker compose up -d --wait postgres
alembic upgrade head
```

完整基础设施：

```powershell
docker compose up -d --wait --wait-timeout 300 postgres opensearch redis
docker compose ps
```

首次启动前复制 `.env.example` 为 `.env`，为 PostgreSQL 设置随机本地密码，并同时更新主机与 Compose 数据库 URL。不要提交 `.env`、数据库卷、Token 或真实 Provider 响应。

MySQL 历史数据的一次性迁移、短停机切换、验收与回滚步骤见 [`docs/pg迁移.md`](../docs/pg迁移.md)。
