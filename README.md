# globuy

`globuy` 是一个用于学习 Agent 工程化的全栈购物助手项目。它提供 React 工作台、FastAPI
后台任务、WebSocket 运行事件、MySQL 会话与用户数据、长期记忆和心愿库；Agent 主图按
`Think → Act → Observe → Reflect` 组织。

真实商品检索依赖本地 OpenSearch 索引和单独提供的离线数据包，见“完整检索模式”。

## 当前能力与边界

- 前端：React + TypeScript + Vite，包含注册/登录、三栏工作台、历史会话、心愿库与长期记忆管理。
- 后端：FastAPI、HTTP `202` 后台任务、只订阅 WebSocket、取消、事件重放与 MySQL 持久化。
- 演示模式：内置 `mock` 模型；不调用 DeepSeek、Tavily 或商品 Provider。
- 商品检索：固定使用 OpenSearch 的 BM25 + 冻结 BGE-M3 + 无权重 RRF；当前结果来自离线快照，
  不是实时价格或库存。
- 本仓库**不包含**商品快照、索引、模型缓存、`.env`、数据库卷或 API 密钥。普通 `git clone`
  后可运行演示模式；若要使用商品检索，需要额外取得合规的数据包并完成索引构建。

更多实现状态与已知差距见 [docs/project-status.md](docs/project-status.md)，接口见
[docs/globuy接口文档v1.md](docs/globuy接口文档v1.md)。
当前项目优化部分已做和未做还有还需要做的内容详细看[docs/ToDo.md](docs/ToDo.md)。

## 按需商品目录（默认无付费调用）

商品搜索现在支持“结构化购物意图 → MySQL 覆盖检查 → 可选三平台共享补库 → Outbox 批量投影 →
OpenSearch Hybrid 检索”。`GLOBUY_PRODUCT_PROVIDER` 默认是 `none`：已有本地目录仍可搜索，目录不足时返回
`not_configured`，不会静默请求 Just One 或任何付费模型。只有另行确认 API 计费、长期保存权和展示权后，才应在
本地 `.env` 中配置 `GLOBUY_PRODUCT_PROVIDER=justone` 与 Token。

数据库升级和 v2 索引生命周期命令：

```powershell
alembic upgrade head
python -m app.products.catalog_lifecycle stats
python -m app.products.catalog_lifecycle audit
python -m app.products.catalog_lifecycle cleanup
python -m app.products.catalog_lifecycle rebuild --value globuy-products-v2-20260819
```

`rebuild` 从 MySQL 的活动且有合法价格的 Offer 构建新物理索引，校验后原子切换
`globuy-products` 别名；活动索引不会执行 `force merge`。

## 快速开始：本地演示模式

这条路径适合课程演示、前后端联调和同学第一次运行。它会启动一个本地 MySQL 8 容器，使用
`mock` 模型，因此不需要任何第三方密钥，也不会产生付费调用。

### 前置条件

- Windows PowerShell（下方命令按 PowerShell 编写）
- [Miniconda 或 Anaconda](https://docs.conda.io/projects/conda/en/latest/user-guide/install/)
- Node.js **20.19+**（`frontend/package-lock.json` 锁定了前端依赖）
- Docker Desktop 已启动，并可运行 Linux 容器

确认版本：

```powershell
conda --version
node --version
docker version
```

### 1. 克隆并安装依赖

```powershell
git clone <你的 GitHub 仓库地址> globuy
Set-Location globuy

# 首次创建 Conda 环境；environment.yml 会以可编辑方式安装项目和开发依赖。
conda env create -f environment.yml
conda activate globuy

Push-Location frontend
npm ci
Pop-Location
```

已存在 `globuy` 环境时，进入仓库后运行 `conda activate globuy` 即可；Python 依赖有更新时执行
`python -m pip install -e ".[dev]"`。

### 2. 创建仅供本机演示的 MySQL 8

项目的正式用户、会话和心愿库都依赖 MySQL；未配置数据库时 FastAPI 会拒绝启动，这是有意的
安全约束。以下命令创建名为 `globuy-mysql` 的本地容器，并映射到宿主机 `3307`。
请在**同一个 PowerShell 窗口**连续完成第 2、3 步：随机生成的本地密码只保存在该窗口的变量中。

```powershell
# 生成仅保存在当前 PowerShell 会话中的本地随机密码。
$rootPassword = python -c "import secrets; print(secrets.token_urlsafe(24))"
$appPassword = python -c "import secrets; print(secrets.token_urlsafe(24))"

docker run -d --name globuy-mysql --restart unless-stopped `
  -e "MYSQL_ROOT_PASSWORD=$rootPassword" `
  -p 3307:3306 `
  -v globuy-mysql-data:/var/lib/mysql `
  mysql:8.4

# 等待 MySQL 就绪；首次拉取镜像和初始化通常需要几十秒。
docker logs -f globuy-mysql
```

当日志出现 `ready for connections` 后按 `Ctrl+C` 退出日志跟随（容器不会停止），再创建项目账号：

```powershell
$sql = @"
CREATE DATABASE IF NOT EXISTS globuy CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS 'globuy_app'@'%' IDENTIFIED BY '$appPassword';
ALTER USER 'globuy_app'@'%' IDENTIFIED BY '$appPassword';
GRANT ALL PRIVILEGES ON globuy.* TO 'globuy_app'@'%';
FLUSH PRIVILEGES;
"@
$sql | docker exec -i -e "MYSQL_PWD=$rootPassword" globuy-mysql mysql -uroot
```

如果 `3307` 已被占用，请先停止占用该端口的 MySQL，或在以上命令和下面 `.env` 的连接串中同时改用
另一个空闲端口。不要把本地 `.env` 或任何密码提交到 Git。

再次使用同一套本地数据时无需重新执行 `docker run`；使用 `docker start globuy-mysql` 后直接从第 3 步继续。

### 3. 创建本地配置并迁移数据库

```powershell
Copy-Item .env.example .env

$databaseUrl = "mysql+asyncmy://globuy_app:$appPassword@127.0.0.1:3307/globuy?charset=utf8mb4"
$envFile = Get-Content .env -Raw
$envFile = $envFile -replace '(?m)^GLOBUY_DATABASE_URL=.*$', "GLOBUY_DATABASE_URL=$databaseUrl"
$envFile = $envFile -replace '(?m)^GLOBUY_MODEL_PROVIDER=.*$', 'GLOBUY_MODEL_PROVIDER=mock'
$envFile = $envFile -replace '(?m)^GLOBUY_WEB_SEARCH_PROVIDER=.*$', 'GLOBUY_WEB_SEARCH_PROVIDER=none'
Set-Content .env $envFile -Encoding utf8

alembic upgrade head
```

`mock` 仅用于演示 Agent 任务和 WebSocket 生命周期，不会提供真实商品推荐。它不会读取或发送
DeepSeek、Tavily 等第三方服务的凭据。

### 4. 在两个终端启动服务

终端 A（后端）：

```powershell
Set-Location <globuy 仓库目录>
conda activate globuy
python -m uvicorn app.api.server:app --host 127.0.0.1 --port 8000 --reload
```

终端 B（前端）：

```powershell
Set-Location <globuy 仓库目录>\frontend
npm run dev -- --host 127.0.0.1
```

浏览器打开 <http://127.0.0.1:5173>，注册一个本地测试账号后即可创建会话并提交消息。

> `npm run dev` **只启动 Vite**，不会启动 FastAPI。前端会把 `/healthz` 和 `/api/*` 代理到
> `127.0.0.1:8000`；若 8000 未启动，页面会显示 500。这是代理连接失败，不是 Agent 返回的
> `INTERNAL_ERROR`。

### 5. 验证与停止

在第三个 PowerShell 窗口执行：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
Get-NetTCPConnection -LocalPort 5173,8000 -State Listen
```

`/healthz` 应至少返回 `status: ok`、`model_provider: mock` 和 `database: ok`。

停止前端与后端时，在各自终端按 `Ctrl+C`。保留本地数据库以便下次使用：

```powershell
docker stop globuy-mysql
```

如需彻底删除**本地演示数据**（不可恢复），再执行：

```powershell
docker rm globuy-mysql
docker volume rm globuy-mysql-data
```

## 完整检索模式（可选）

完整购物检索还需要 OpenSearch、Redis、BGE-M3 模型缓存和商品 Candidate 数据包。商品数据包不随
Git 仓库发布，原因是它来自离线采集快照；请向仓库维护者获取获准分发的数据，或使用你自己有权
使用的数据源。不要将 API Token、原始响应或未获授权的商品数据提交到 GitHub。

在已完成“快速开始”并拿到数据包后：

```powershell
Set-Location <globuy 仓库目录>
conda activate globuy

# 启动固定的 OpenSearch 与 Redis；不会启动 MySQL。
docker compose up -d --wait --wait-timeout 300 opensearch redis
docker compose ps

# 数据包应提供以下 Candidate 文件；没有该文件时不要运行导入和建索引命令。
Test-Path datasets/headphones_1000/structured/itemsearch_candidates.jsonl

# 将 Candidate 导入 MySQL，然后构建商品索引。
python -m app.products.import_snapshot
python -m app.search.build_index

# 可选：从已有 Candidate 数据生成确定性品类卡片索引。
python -m app.category.build_index --deterministic
```

首次运行 `app.search.build_index` 会下载约 2.3 GB 的 `BAAI/bge-m3` 并构建向量；请预留磁盘空间
并等待完成。商品检索结果是导入快照，不代表实时库存、价格或官方推荐。

若要启用真实模型或网页搜索，复制 `.env.example` 中相应变量到本地 `.env` 并填写你自己的密钥。
真实模型和 Tavily 调用可能产生费用；未配置时对应能力应返回 `not_configured`。详细的 MySQL 运维与
Worker 启动方式见 [docs/mysql-persistence.md](docs/mysql-persistence.md)。

## 常见问题

### 页面立即显示 500

先区分是 Vite 代理失败还是后端错误：

```powershell
Get-NetTCPConnection -LocalPort 5173,8000 -State Listen -ErrorAction SilentlyContinue
Invoke-RestMethod http://127.0.0.1:8000/healthz
```

- 5173 有监听而 8000 没有：按“终端 A”启动 FastAPI，并查看该终端的异常日志。
- Uvicorn 报 MySQL 连接失败：确认 `globuy-mysql` 正在运行、端口为 3307，且 `.env` 中的
  `GLOBUY_DATABASE_URL` 与创建的应用账号一致。
- 8000 返回 200 而浏览器仍报 500：停止并重启 Vite，确认浏览器访问的是它实际输出的地址。

### `docker` 无法连接 Docker Desktop

先打开 Docker Desktop，等待其状态变为 Running，再执行 `docker version`。项目的 `compose.yaml`
只管理 OpenSearch 和 Redis；MySQL 由上面的 `globuy-mysql` 命令单独创建。

### `npm ci` 或前端启动失败

确认 `node --version` 至少为 20.19。删除 `frontend/node_modules` 后再次执行 `npm ci`；不要提交
`node_modules` 或 `frontend/dist`。

### 数据库迁移失败

确认使用的是 Conda `globuy` 环境，并检查：

```powershell
docker ps --filter name=globuy-mysql
Get-NetTCPConnection -LocalPort 3307 -State Listen
```

迁移成功后可用 `alembic current` 查看当前版本。

## 测试与代码质量

后端测试使用 Fake/Mock，不应调用真实付费模型或商品 Provider：

```powershell
Set-Location <globuy 仓库目录>
conda activate globuy
python -m ruff check app datasets tests
python -m compileall -q app datasets tests
python -m pytest -q
```

前端测试与生产构建：

```powershell
Set-Location <globuy 仓库目录>\frontend
npm run test
npm run build
```

### 双层评测

默认离线评测只使用仓库内合成夹具，不调用真实模型、WebSearch、商品 Provider 或向量服务：

```powershell
python scripts/eval_regression.py --suite offline
```

真实质量评测通过正式认证、HTTP 202、WebSocket replay 和 MySQL 商品事实执行，必须使用专用评测
账号并显式增加 `--allow-model-calls`。独立 LLM Judge 还需要配置 `GLOBUY_EVAL_JUDGE_*` 并增加
`--judge`；若服务端外部工具已配置，脚本会拒绝运行，除非再明确增加
`--allow-external-tools`。完整契约与产物说明见 [评测系统文档](docs/evaluation-system.md)。

## 项目结构

```text
app/                 FastAPI、AgentLoop、工具、数据库、检索与长期记忆
frontend/            React + TypeScript + Vite 工作台
alembic/             MySQL Schema 迁移
datasets/            采集器、夹具和数据包说明（真实快照不提交）
docs/                接口、MySQL 运维、实现状态和架构契约
compose.yaml         OpenSearch 与 Redis 的本地基础服务
```

## 发布前检查

推送到 GitHub 前请确认：

- `.env`、数据库卷、`output/`、`uploaded/`、模型缓存和真实数据快照没有被加入暂存区。
- `git status` 中的变更都确实属于本次提交；不要覆盖或删除他人的未提交文件。
- 已在干净环境至少走完“快速开始”的数据库迁移、`/healthz` 与前端登录验证。
- 若希望同学体验完整商品检索，提供经过授权的数据包及其校验说明；仅有 Git 仓库不足以重建当前索引。
