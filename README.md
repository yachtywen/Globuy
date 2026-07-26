# Globuy

Globuy is a shopping assistant built with React, FastAPI, MySQL, OpenSearch,
Redis, and an Agent workflow. It supports conversation-based product discovery,
three-platform realtime candidate retrieval, preference confirmation, and a
wishlist.

The application has two startup paths:

- **Demo mode**: registration, sessions, UI, and Agent workflow using the mock
  model. No paid API calls are made.
- **Realtime mode**: searches Taobao, JD, and Douyin through Just One, then
  applies OpenSearch BM25 + BGE-M3 + RRF hybrid retrieval.

## Prerequisites

- Windows PowerShell
- Docker Desktop running
- Conda or Miniconda
- Node.js 20.19 or newer

Run every command below from the repository root:

```powershell
Set-Location "C:\Users\Lenovo\Desktop\模板例子\code\Globuy"
```

## 1. Install dependencies

```powershell
conda env create -f environment.yml
conda activate globuy
Push-Location frontend
npm ci
Pop-Location
```

If the `globuy` environment already exists, use `conda activate globuy` and
run `python -m pip install -e ".[dev]"` only when Python dependencies change.

## 2. Create MySQL once

The FastAPI application requires MySQL. Skip this section when a working
`globuy-mysql` container already exists.

```powershell
$rootPassword = python -c "import secrets; print(secrets.token_urlsafe(24))"
$appPassword = python -c "import secrets; print(secrets.token_urlsafe(24))"

docker run -d --name globuy-mysql --restart unless-stopped `
  -e "MYSQL_ROOT_PASSWORD=$rootPassword" `
  -p 3307:3306 `
  -v globuy-mysql-data:/var/lib/mysql `
  mysql:8.4
```

Wait until `docker logs globuy-mysql` contains `ready for connections`, then
create the application database:

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

## 3. Configure the local environment

Create `.env` only once:

```powershell
Copy-Item .env.example .env
```

Set the MySQL connection string to the password generated above, then migrate:

```powershell
$databaseUrl = "mysql+asyncmy://globuy_app:$appPassword@127.0.0.1:3307/globuy?charset=utf8mb4"
$envFile = Get-Content .env -Raw
$envFile = $envFile -replace '(?m)^GLOBUY_DATABASE_URL=.*$', "GLOBUY_DATABASE_URL=$databaseUrl"
Set-Content .env $envFile -Encoding utf8
alembic upgrade head
```

For demo mode, retain `GLOBUY_MODEL_PROVIDER=mock` and leave the realtime
provider disabled.

## 4. Start OpenSearch and Redis

Realtime mode requires both services. Run this from the directory containing
`compose.yaml`, not from `C:\Windows\System32`.

```powershell
docker compose up -d --wait --wait-timeout 600 opensearch redis
docker compose ps
curl.exe http://127.0.0.1:9200
```

OpenSearch must report version `2.19.1` and Redis must be healthy. The host
backend configuration must be:

```env
GLOBUY_OPENSEARCH_URL=http://127.0.0.1:9200
```

If OpenSearch fails after switching from the earlier 3.x image, its old data
volume is incompatible. This removes only the local search index, not MySQL
users or conversations:

```powershell
docker compose down
docker volume rm globuy_opensearch-data
docker compose up -d --wait --wait-timeout 600 opensearch redis
```

## 5. Enable realtime product search (optional)

Add valid private credentials to `.env`. Never commit this file.

```env
GLOBUY_REALTIME_PRODUCT_PROVIDER=justone
GLOBUY_JUSTONE_API_TOKEN=<your Just One token>

GLOBUY_MODEL_PROVIDER=openai-compatible
GLOBUY_LLM_MODEL=<model name>
GLOBUY_LLM_API_KEY=<provider key>
GLOBUY_LLM_BASE_URL=<OpenAI-compatible URL>
```

The first realtime query loads BGE-M3 locally and can take longer than later
queries. Validate the three platform path before opening the UI:

```powershell
conda run -n globuy python scripts/check_realtime_product_search.py --query "降噪耳机"
```

Expected result: Taobao, JD, and Douyin each report `status=ok` with candidates.
See [docs/realtime-search-check.md](docs/realtime-search-check.md) for output
interpretation.

## 6. Start backend and frontend

Use two separate PowerShell windows and keep both running.

Backend:

```powershell
Set-Location "C:\Users\Lenovo\Desktop\Globuy"
conda activate globuy
python -m uvicorn app.api.server:app --host 127.0.0.1 --port 8000 --reload
```

Frontend:

```powershell
Set-Location "C:\Users\Lenovo\Desktop\Globuy\frontend"
cmd /c npm run dev -- --host 127.0.0.1
```

Open `http://127.0.0.1:5173`, register a local user, and start a conversation.

## Product review lookup (optional)

To answer requests such as `了解 Sony WH-1000XM6` or `看看这个型号的口碑和测评`, configure
Tavily in the private `.env` file:

```env
GLOBUY_WEB_SEARCH_PROVIDER=tavily
GLOBUY_TAVILY_API_KEY=<your Tavily key>
```

The Agent routes product-review intent to `web_search(search_mode=product_reviews)`. This mode
searches only Xiaohongshu and Zhihu, removes duplicate or off-domain links, and returns at most
three cited results. Every retained title or snippet must also match the requested product phrase,
model identifier, or required generation number. If Tavily can retrieve fewer than three relevant
target-platform pages, Globuy
returns the smaller verified set instead of filling it with other websites. These snippets are
external content evidence, not realtime product prices or inventory.

如果小红书和知乎没有相关页面，且当前会话之前已经检索到带有可信评分、好评率或评论数的淘宝、京东、抖音候选，系统会补充这些购物平台的聚合消费者反馈。它们会标记为“聚合信号”，不会伪装成消费者评价原文；没有可验证字段时仍会如实提示未找到。后续继续对话或刷新历史时，最新回答会更新，但最近一次非空商品推荐仍保留在商品卡片中，收藏来源继续使用原推荐 run。

## Verify and troubleshoot

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
Get-NetTCPConnection -LocalPort 5173,8000 -State Listen -ErrorAction SilentlyContinue
```

- Vite `ECONNREFUSED 127.0.0.1:8000`: the FastAPI backend is not running.
  Start it with the backend command above and inspect its terminal for errors.
- `docker compose` says no configuration file: change directory to the project
  root first.
- A product image is unavailable: external marketplace CDNs can reject an
  image. Globuy uses an allowlisted backend image proxy and falls back to a
  placeholder when a source image is unavailable.
- A realtime result can be added to the wishlist only after its `Product`,
  `Offer`, snapshot, and observation transaction commits. If persistence is
  unavailable, the card remains usable for comparison/source navigation but
  its wishlist button stays disabled instead of returning `OFFER_NOT_FOUND`.

## Tests

```powershell
conda activate globuy
python -m compileall -q app tests scripts
python -m pytest -q

Set-Location frontend
cmd /c npm run test
cmd /c npm run build
```

## Project status

Read [docs/ToDo.md](docs/ToDo.md) for implementation handoff and priorities,
and [docs/project-status.md](docs/project-status.md) for dated implementation
notes. Secrets, database volumes, model caches, and provider responses must not
be committed.
