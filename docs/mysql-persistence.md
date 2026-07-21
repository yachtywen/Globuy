# MySQL 持久化部署与运维

Globuy 使用 MySQL 8 保存用户、服务端会话、对话任务、商品主数据、心愿库、价格历史和长期记忆。
OpenSearch 继续承担商品和长期记忆检索；Redis 继续承担缓存和登录失败限流。

## 1. 初始化现有 `mysql8`

宿主机已通过 `127.0.0.1:3307` 暴露 MySQL。使用容器当前管理员凭据进入 MySQL 后执行：

```sql
CREATE DATABASE globuy CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'globuy_app'@'%' IDENTIFIED BY '请替换为随机强密码';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP, REFERENCES
ON globuy.* TO 'globuy_app'@'%';
FLUSH PRIVILEGES;
```

真实密码只写入 Git 已忽略的 `.env`：

```dotenv
GLOBUY_DATABASE_URL=mysql+asyncmy://globuy_app:URL编码后的密码@127.0.0.1:3307/globuy?charset=utf8mb4
GLOBUY_DOCKER_DATABASE_URL=mysql+asyncmy://globuy_app:URL编码后的密码@host.docker.internal:3307/globuy?charset=utf8mb4
GLOBUY_AUTH_COOKIE_SECURE=false
```

生产 HTTPS 环境必须将 `GLOBUY_AUTH_COOKIE_SECURE` 设为 `true`。
未配置 `GLOBUY_DATABASE_URL` 时 FastAPI 会拒绝启动，不会静默使用 SQLite。仅维护旧测试或只读诊断时
才可显式设置 `GLOBUY_LEGACY_SQLITE_ENABLED=true`。

## 2. 建表与首次商品导入

```powershell
conda activate globuy
alembic upgrade head
python -m app.products.import_snapshot
python -m app.search.build_index
```

JSONL 导入以文件 SHA-256、商品来源 ID和观测 ID做幂等控制，可以安全重跑。商品索引文档会同时携带
兼容的 `item_id` 与稳定的 `product_id/offer_id`。执行商品索引命令前仍需确保 OpenSearch 已健康。

本次选择全新 MySQL 空库，不导入 `output/globuy-sessions.sqlite3`。旧 SQLite 文件只作为历史只读
备份保留，不要删除。

## 3. 启动应用与 Worker

宿主机开发：

```powershell
python -m uvicorn app.api.server:app --host 127.0.0.1 --port 8000 --reload
python -m app.products.price_worker --serve
python -m app.products.outbox_worker --serve
python -m app.memory.outbox_worker --serve
```

Docker：

```powershell
docker compose up -d --build api price-worker product-outbox-worker memory-outbox-worker redis opensearch
```

`price-worker` 只调用已经注册的平台详情适配器。当前没有正式详情适配器的平台会记录
`provider_unavailable` 并保留上一次有效价格，不会写入 0 或伪造实时价格。
心愿商品默认安排在北京时间每天 03:00；Worker 先复用当日非离线采集任务的有效观测，未覆盖时再调用
详情适配器。领取使用短租约与 `SKIP LOCKED`，进程中断后任务会重新到期。

## 4. 安全与故障语义

- 正式业务接口从 HttpOnly Cookie 获取用户身份，不接受前端自行声明的 `user_id`。
- 注册请求使用 `Idempotency-Key`；同键重试只恢复同一用户，并重新签发服务端会话。
- 所有写接口校验 CSRF Cookie 与 `X-CSRF-Token`；密码使用 Argon2id。
- 登录失败计数只在 Redis 保存经过 SHA-256 处理的邮箱键；Redis 不可用时不影响已有数据库事实。
- 商品或记忆写入与 Outbox 事件在同一 MySQL 事务提交；OpenSearch 故障时由 Worker 重试。
- 价格刷新失败只更新失败状态和下次重试时间，不覆盖 `offers.current_price`。
- `/healthz` 在 MySQL 不可用时返回 `status=degraded`，不会输出连接串或凭据。

## 5. 验证命令

```powershell
python -m ruff check app tests
python -m compileall -q app tests
python -m pytest -q
cd frontend
npm run test
npm run build
```

真实 MySQL 验收还应执行：

```powershell
alembic current
python -m app.products.import_snapshot
```

随后注册两个测试账号，确认双方无法读取对方的 thread、run、产物、心愿库和长期记忆。
