# Globuy 接口文档 v1

> 文档版本：v1  
> 更新时间：2026-08-21
> 适用范围：当前仓库实现的 Web 前端、FastAPI 后端与 WebSocket 实时任务流  
> HTTP 接口前缀：`/api/v1`

本文档以当前后端路由、Pydantic 请求模型、PostgreSQL Repository、WebSocket 事件模型和前端 API Client 的实际实现为准，用于后续前端开发、联调与验收。示例中的 ID、邮箱、商品名称和时间均为占位数据，不代表真实业务数据。

## 1. 当前接口范围与部署前提

当前版本已实现服务状态、注册登录、会话、Agent 任务、运行结果、WebSocket 实时事件、运行产物、默认心愿库、价格历史和长期记忆接口。除注册、登录、根信息和健康检查外，核心业务接口均要求登录。业务数据按当前登录用户隔离，前端不得通过传入 `user_id` 指定身份。

领域接口依赖 PostgreSQL 17。正式启动时必须配置 `GLOBUY_DATABASE_URL`；未配置时生产启动会失败。代码与 Alembic 迁移已经实现，但接口可访问不代表当前部署已经完成建库、商品导入和 Worker 启动。

应用启动后可访问：

| 地址 | 用途 |
| --- | --- |
| `/docs` | Swagger UI |
| `/redoc` | ReDoc |
| `/openapi.json` | OpenAPI JSON |

WebSocket 不包含在 OpenAPI 中，以本文档第 8 节为准。

## 2. 通用约定

### 2.1 请求、ID、时间和金额

- HTTP API 默认使用 UTF-8 JSON；上传使用 `multipart/form-data`，下载返回二进制。
- 浏览器请求必须使用 `credentials: "include"`，否则不会发送服务端会话 Cookie。
- 跨域部署时，前端来源必须加入后端 CORS 允许列表；服务端按携带凭证模式处理跨域请求，不能使用通配来源替代明确来源。
- 除明确说明外，登录后的 `POST`、`PATCH`、`DELETE` 均要求 CSRF 请求头。
- 请求模型禁止未声明的额外字段。
- 路由 ID 仅允许字母、数字、下划线和连字符，长度 1～128。
- 时间使用 ISO 8601 UTC，例如 `2026-07-21T08:30:00.123Z`。
- 价格在 PostgreSQL 中为 `NUMERIC(18,2)`，JSON 中为数字或 `null`，必须结合 `currency` 使用。
- 分页游标是不透明字符串，前端不得解析或自行构造。

推荐封装：

```ts
async function apiFetch(path: string, init: RequestInit = {}) {
  return fetch(`/api/v1${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...init.headers,
    },
  });
}
```

### 2.2 商品空值与展示规则

- `rating === null`：完全不展示评分区域，不显示“暂无”或占位符。
- `sales === null`：完全不展示销量区域，不显示“暂无”或占位符。
- 所有商品卡片、对比区域和心愿库商品统一不展示运费。
- `price === null` 不得转换为 `0`。
- `price_change === null` 不得显示上涨、下降或持平。

`rating` 的上游含义可能是星级评分，也可能是好评比例。当前结果没有稳定提供评分尺度，前端不得固定标注“满分 5 分”。`sales` 是上游可获得的销量数，但统计周期可能未知；没有口径字段时仅建议显示“销量”。

### 2.3 幂等

- 注册：请求头 `Idempotency-Key`，长度 8～128。
- 创建会话、任务、加入心愿库：请求体 `client_request_id`。
- 同一次用户操作的网络重试必须复用原键；新操作必须使用新键，建议 UUID。
- 同一个键对应不同请求时返回 `409 IDEMPOTENCY_KEY_REUSED`。

### 2.4 统一错误结构

```json
{
  "error": {
    "code": "THREAD_NOT_FOUND",
    "message": "Thread not found",
    "retryable": false,
    "details": {}
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | `string` | 稳定业务错误码，前端逻辑优先判断它 |
| `message` | `string` | 开发与兜底展示信息 |
| `retryable` | `boolean` | 服务端是否建议重试 |
| `details` | `object` | 可选上下文；参数错误位于 `details.errors` |

前端不得只按 HTTP 状态码区分业务错误。未匹配路由返回 `404 NOT_FOUND`，参数校验返回 `422 VALIDATION_ERROR`，未处理异常返回 `500 INTERNAL_ERROR`。

## 3. 认证与安全

### 3.1 Cookie 与 CSRF

注册或登录成功后，服务端默认设置：

| Cookie | 属性 | 用途 |
| --- | --- | --- |
| `globuy_session` | HttpOnly、SameSite=Lax | 服务端会话；JavaScript 不可读取 |
| `globuy_csrf` | SameSite=Lax | 前端读取并放入 CSRF 请求头 |

生产环境应启用 Secure Cookie。默认会话有效期为 30 天，实际值可配置。

写请求头：

```http
X-CSRF-Token: <globuy_csrf Cookie 的值>
```

注册和登录不要求 CSRF；退出登录要求。缺失或不匹配返回 `403 CSRF_FAILED`。

```ts
function readCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`;
  const part = document.cookie.split("; ").find(v => v.startsWith(prefix));
  return part ? decodeURIComponent(part.slice(prefix.length)) : null;
}
```

### 3.2 身份约定

- 当前用户统一通过 `GET /api/v1/auth/me` 获取。
- 服务端从会话解析用户并校验会话、运行、文件、心愿项和记忆所有权。
- 部分请求 Schema 仍有可选 `user_id`，仅供遗留 SQLite 测试；正式前端禁止传入。
- 未登录访问保护接口返回 `401 AUTH_REQUIRED`。

## 4. 系统接口

### 4.1 服务信息

`GET /`

无需登录。响应 `200`：

```json
{"name":"globuy-api","version":"1.0.0","docs":"/docs"}
```

### 4.2 健康检查

`GET /healthz`

无需登录。健康响应 `200`：

```json
{
  "status":"ok",
  "model_provider":"openai-compatible",
  "database":"ok",
  "observability_provider":"langfuse",
  "observability_configured":true,
  "observability_enabled":true,
  "observability_status":"ready",
  "observability_capture_mode":"summary"
}
```

数据库探测失败时当前实现仍返回 HTTP `200`：

```json
{"status":"degraded","model_provider":"openai","database":"unavailable"}
```

监控和状态页必须判断响应体 `status`，不能只判断 HTTP 状态码。非生产诊断模式未配置数据库时，响应可能不含 `database`。
观测字段不包含 Public/Secret Key 或 Hash Salt；`ready` 只表示本地 SDK 配置完成，不代表远端凭据已通过网络验证。

## 5. 认证接口

### 5.1 注册

`POST /api/v1/auth/register`

请求头必须有 `Idempotency-Key`。请求体：

```json
{
  "email": "user@example.com",
  "password": "example-password",
  "display_name": "示例用户"
}
```

| 字段 | 约束 |
| --- | --- |
| `email` | 必填，3～320 字符，标准化后唯一 |
| `password` | 必填，8～256 字符，服务端以 Argon2id 哈希保存 |
| `display_name` | 必填，1～100 字符 |

响应 `201` 并设置 Cookie：

```json
{
  "user": {
    "user_id": "usr_example",
    "email": "user@example.com",
    "display_name": "示例用户"
  },
  "csrf_token": "<csrf-token>"
}
```

错误：`409 EMAIL_ALREADY_REGISTERED`、`409 IDEMPOTENCY_KEY_REUSED`、`422 VALIDATION_ERROR`。
若邮箱并不存在，但创建默认心愿库、认证会话或幂等记录时发生关系约束失败，则返回可重试的
`500 REGISTRATION_FAILED`，不得误报为邮箱已注册。

### 5.2 登录

`POST /api/v1/auth/login`

```json
{"email":"user@example.com","password":"example-password"}
```

响应 `200`，结构与注册一致并设置 Cookie。错误：`401 INVALID_CREDENTIALS`、`429 LOGIN_RATE_LIMITED`。

### 5.3 当前用户

`GET /api/v1/auth/me`

要求登录。响应 `200`：

```json
{
  "user": {
    "user_id": "usr_example",
    "email": "user@example.com",
    "display_name": "示例用户"
  }
}
```

前端启动时可调用本接口恢复登录状态；收到 `401` 后应清空本地用户状态并跳转登录页。

### 5.4 退出

`POST /api/v1/auth/logout`

要求登录和 CSRF。响应 `204 No Content`，服务端撤销会话并删除认证 Cookie。

## 6. 会话接口

### 6.1 会话摘要

```ts
interface ThreadSummary {
  thread_id: string;
  user_id: string;
  title: string;
  status: "active" | "archived";
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  archive_reason: string | null;
  last_run_id: string | null;
  message_count: number;
  last_run_status: RunStatus | null;
}
```

服务端会返回 `user_id`，前端通常可忽略，不能据此越权查询。

### 6.2 查询会话列表

`GET /api/v1/threads`

查询参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `status` | `archived` | `active` 或 `archived` |
| `cursor` | 无 | 仅历史会话分页使用 |
| `limit` | `20` | 当前最大 `50` |

响应：

```json
{
  "items": [{
    "thread_id":"thr_example","user_id":"usr_example",
    "title":"寻找适合通勤的耳机","status":"archived",
    "created_at":"2026-07-20T08:00:00.000Z",
    "updated_at":"2026-07-20T08:05:00.000Z",
    "archived_at":"2026-07-21T01:00:00.000Z",
    "archive_reason":"replaced","last_run_id":"run_example",
    "message_count":2,"last_run_status":"succeeded"
  }],
  "next_cursor": null
}
```

活动会话每位用户最多一个。历史列表用 `next_cursor` 继续加载，`null` 表示结束。

### 6.3 创建新会话

`POST /api/v1/threads`

要求 CSRF。

```json
{"current_thread_id":"thr_current","client_request_id":"req_example"}
```

`current_thread_id` 可为 `null`；`client_request_id` 必填。正式前端不要传遗留字段 `user_id`。

响应 `201`：

```json
{
  "thread_id":"thr_new","status":"active","title":"新对话",
  "created_at":"2026-07-21T08:30:00.000Z",
  "archived_thread_id":"thr_current","archived_run_id":null
}
```

服务端原子处理旧活动会话：空会话可清理，有内容的会话会归档，运行中的任务会进入取消流程。`409 ACTIVE_THREAD_CHANGED` 表示客户端持有的活动会话已过期，应重新获取活动会话。

### 6.4 会话详情

`GET /api/v1/threads/{thread_id}`

响应为 `ThreadSummary` 加：

```ts
interface ThreadDetail extends ThreadSummary {
  read_only: boolean;
  messages: Array<{
    message_id: string;
    run_id: string | null;
    role: "user" | "assistant";
    content: string;
    is_partial: boolean;
    ordinal: number;
    created_at: string;
  }>;
  runs: Array<{
    run_id: string;
    status: RunStatus;
    query: string;
    created_at: string;
    started_at: string | null;
    finished_at: string | null;
    final_text: string | null;
    result: TaskResult | null;
    error_code: string | null;
    error_message: string | null;
    artifacts: ArtifactItem[];
  }>;
}
```

历史会话 `read_only === true`，前端应禁用发送、取消等写操作，也不要为其建立任务 WebSocket。

## 7. Agent 任务、状态与产物

### 7.1 创建任务

`POST /api/v1/tasks`

要求 CSRF。

```json
{
  "query":"预算 1000 元以内，推荐适合通勤的降噪耳机",
  "thread_id":"thr_example",
  "client_request_id":"req_example"
}
```

`query` 去除首尾空白后为 1～20000 字符。正式前端不要传遗留 `user_id`。

响应 `202 Accepted`：

```json
{
  "status":"starting","thread_id":"thr_example","run_id":"run_example",
  "trace_id":"0123456789abcdef0123456789abcdef",
  "replaced_run_id":null,"created_at":"2026-07-21T08:30:00.000Z",
  "ws_url":"/api/v1/ws/thr_example?run_id=run_example&after=0",
  "status_url":"/api/v1/threads/thr_example/runs/run_example"
}
```

同一会话只保留一个活动运行。新任务可能替换旧运行，旧 ID 通过 `replaced_run_id` 返回。前端收到响应后立即连接 `ws_url`，保留 `status_url` 作为恢复兜底。

### 7.2 状态枚举

```ts
type RunStatus =
  | "starting" | "running" | "cancelling"
  | "succeeded" | "cancelled" | "failed" | "interrupted";
```

终态：`succeeded`、`cancelled`、`failed`、`interrupted`。

### 7.3 查询运行状态

`GET /api/v1/threads/{thread_id}/runs/{run_id}`

```json
{
  "thread_id":"thr_example","run_id":"run_example",
  "trace_id":"0123456789abcdef0123456789abcdef","status":"succeeded",
  "created_at":"2026-07-21T08:30:00.000Z",
  "started_at":"2026-07-21T08:30:01.000Z",
  "finished_at":"2026-07-21T08:30:10.000Z",
  "last_sequence":18,"earliest_available_sequence":1,
  "terminal_event":"RUN_FINISHED","result":{},"artifacts":[],
  "memory_status":"not_requested","error":null
}
```

失败时 `error` 为 `{ "code": "AGENT_RUN_FAILED", "message": "..." }`。刷新页面、断线或收到 `replay_gap` 时，用本接口恢复终态和结果。

### 7.4 任务结果与商品结构

```ts
interface TaskResult {
  status: "complete" | "incomplete" | "not_configured" | "error";
  final_text: string;
  picks: ProductPick[];
  unresolved: string[];
  learned_preferences: unknown[];
  memory_status: string;
  source_kind: "offline_snapshot";
  artifacts: unknown[];
}

interface ProductPick {
  item_id: string;
  product_id?: string;
  offer_id?: string;
  platform: string;
  title: string;
  price: number | null;
  currency: string;
  rating: number | null;
  sales: number | null;
  image_url?: string | null;
  product_url?: string | null;
  attributes?: Record<string, unknown>;
  reasons?: string[];
  flags?: string[];
  retrieval_rank?: number | null;
}
```

- 加入心愿库必须使用稳定 `offer_id`，不能只提交兼容 `item_id`。
- `product_id` 是归一化商品；`offer_id` 是具体平台报价，心愿库按 Offer 跟踪。
- `rating`、`sales`、`price` 可能为 `null`，不可转成 `0`。
- 运行状态响应顶层 `artifacts` 是产物清单的权威入口。

### 7.5 取消任务

`POST /api/v1/threads/{thread_id}/runs/{run_id}/cancel`

要求 CSRF，无请求体。取消处理中响应 `202`：

```json
{
  "status":"cancelling","thread_id":"thr_example","run_id":"run_example",
  "requested_at":"2026-07-21T08:31:00.000Z","terminal":false
}
```

若已结束则响应 `200`，返回实际终态且 `terminal: true`。

### 7.6 产物清单

`GET /api/v1/threads/{thread_id}/runs/{run_id}/files`

```json
{
  "items": [{
    "file_id":"file_example","filename":"report.md","kind":"report",
    "media_type":"text/markdown","size":1024,
    "created_at":"2026-07-21T08:30:10.000Z",
    "download_url":"/api/v1/threads/thr_example/runs/run_example/files/file_example"
  }]
}
```

### 7.7 下载产物

`GET /api/v1/threads/{thread_id}/runs/{run_id}/files/{file_id}`

返回文件二进制，并带安全的 `Content-Disposition` 和 `X-Content-Type-Options: nosniff`。前端必须使用 `download_url`，不得拼接或暴露本地文件路径。

## 8. WebSocket 实时事件

### 8.1 建立连接

```text
ws://<host>/api/v1/ws/{thread_id}?run_id={run_id}&after={lastSequence}
```

HTTPS 使用 `wss://`。浏览器自动随同源握手发送会话 Cookie。首次 `after=0`；重连时传已经成功处理的最大事件序号。

事件信封：

```json
{
  "type":"monitor_event","schema_version":"1.0",
  "event":"TEXT_MESSAGE_CONTENT","event_id":"run_example:8","sequence":8,
  "thread_id":"thr_example","run_id":"run_example",
  "timestamp":"2026-07-21T08:30:05.000Z","message":null,"data":{}
}
```

`sequence` 在单个运行内从 1 单调递增；控制事件可能为 `null`，不可写入续传游标。

### 8.2 事件类型

| `event` | 主要数据 | 前端处理 |
| --- | --- | --- |
| `RUN_STARTED` | `started_at`, `trace_id` | 标记开始并提供可观测关联 ID |
| `STEP_STARTED` / `STEP_FINISHED` | 步骤信息 | 展示步骤进度 |
| `TOOL_CALL_START` | `tool_call_id`, `tool_name` | 工具开始 |
| `TOOL_CALL_ARGS` | `tool_call_id`, `arguments` | 可选调试展示 |
| `TOOL_CALL_RESULT` | `tool_call_id`, `result` | 可选结果展示 |
| `TOOL_CALL_END` | `tool_call_id`, `status`, `duration_ms` | 工具结束 |
| `TEXT_MESSAGE_START` | `message_id`, `role` | 创建流式助手消息 |
| `TEXT_MESSAGE_CONTENT` | `message_id`, `delta` | 追加增量文本 |
| `TEXT_MESSAGE_END` | `message_id`, `partial` | 结束消息 |
| `CUSTOM` | 见下文 | 结果与控制消息 |
| `RUN_FINISHED` | `duration_ms`, `trace_id`, `metadata` | 成功终态 |
| `RUN_ERROR` | `code`, `message`, `retryable` | 失败终态 |
| `TASK_CANCELLED` | `cancelled_at`, `reason` | 取消终态 |

主要 `CUSTOM`：

- `{name: "conversation_initializing", phase: "preparing"}`：紧随 `RUN_STARTED` 发送的临时准备状态；`message` 固定为“收到你的消息了~正在初始化本次对话”。前端将其展示为实时状态，不写入或持久化为 assistant 对话消息，并在首个 Agent 阶段开始后清除。
- `{name: "task_result", result: TaskResult}`：最终结构化结果。
- `{name: "agent_fork", child_thread_id, reason, tool_names}`：Agent 分支信息。
- `{name: "stream_ready", replayed_through, earliest_available_sequence}`：连接和重放准备完成。
- `{name: "replay_gap", requested_after, earliest_available_sequence}`：游标过旧，调用运行状态和会话详情恢复。
- `{name: "heartbeat", server_time}`：保活，不展示。
- `{name: "thread_archived", thread_id, new_thread_id}`：当前会话归档，切换只读并跟随新会话。
- 商品目录临时状态：`shopping_intent_resolved`、`catalog_cache_checked`、`catalog_fetch_started`、
  `catalog_fetch_progress`、`catalog_fetch_finished`、`catalog_normalization_progress`、
  `catalog_persistence_progress`、`catalog_index_progress`、`hybrid_retrieval_progress`。这些事件只包含品类名、平台、
  是否有预算、阶段状态和候选计数等白名单字段；不得包含 Token、游标、Provider 原文、原始业务码或异常堆栈。
  前端按 `sequence` 幂等合并平台快照，不把事件转换成 assistant 消息，并在终态、取消、错误、新 run、切换会话或
  `replay_gap` 状态同步后清理临时进度。

ItemSearch 仍保持一次调用只搜索一个平台。内部调用可携带同一份已验证结构化购物意图，返回状态为
`ok | partial | not_configured | error | cancelled`，并可包含 `catalog_status`、`catalog_candidate_count`、
`captured_at` 和 `provider_status`。Provider 默认关闭时仍搜索已有本地目录；目录不足不会触发真实网络请求，而返回
`not_configured`。候选不新增无法验证的库存、运费或综合评分字段。

### 8.3 重连规则

1. 仅记录已成功处理的最大非空 `sequence`。
2. 以该值作为新连接 `after`，按 `event_id` 去重。
3. 建议 1、2、4、8、15 秒封顶的指数退避。
4. `replay_gap` 后调用运行状态与会话详情重建页面。
5. 运行终态后停止自动重连。

当前默认缓冲约 2000 条、保留约 1800 秒，但这是部署配置，不是永久契约。

### 8.4 关闭码

| 关闭码 | 含义 | 处理 |
| --- | --- | --- |
| `1000` | 正常关闭 | 终态时结束 |
| `4400` | ID 或 `after` 非法 | 修正参数，不自动重试 |
| `4401` | 未登录/会话失效 | 跳转登录 |
| `4404` | 无此会话/运行、无权访问或已归档 | 重取会话状态 |
| `4408` | 消费过慢、队列溢出 | 用状态接口恢复后重连 |
| `1006` | 浏览器观察到异常断开 | 按退避策略重连 |

## 9. 默认心愿库

首版每位用户只有一个默认心愿库，不提供多清单管理界面。

### 9.1 心愿项结构与价格变化

```ts
interface WishlistItem {
  wishlist_item_id: string;
  offer_id: string;
  product_id: string;
  item_id: string;
  platform: string;
  title: string;
  image_url: string | null;
  product_url: string | null;
  added_price: number | null;
  current_price: number | null;
  currency: string;
  price_change: number | null;
  price_change_percent: number | null;
  rating: number | null;
  sales: number | null;
  status: "active" | "removed" | "purchased";
  target_price: number | null;
  note: string | null;
  added_at: string;
  last_checked_at: string | null;
  next_check_at: string | null;
  failure_count: number;
  last_error_code: string | null;
}
```

```text
price_change = current_price - added_price
price_change_percent = price_change / added_price × 100
```

只有加入价和最新有效价均非空且币种一致时才计算变化；加入价为 0 时百分比为空。刷新失败保留旧有效价格，不伪造 `0`。

独立价格 Worker 默认每天北京时间 03:00 刷新到期的活跃心愿商品。`last_checked_at` 表示最近检查时间，`next_check_at` 表示计划下次检查时间；`last_error_code` 非空时，前端可提示数据可能陈旧，但仍应保留上一次有效价格。

### 9.2 查询默认心愿库

`GET /api/v1/wishlists/default`

```json
{"wishlist_id":"wsh_example","name":"默认心愿库","items":[]}
```

当前只返回有效心愿项。

### 9.3 添加心愿商品

`POST /api/v1/wishlists/default/items`

要求 CSRF。

```json
{
  "offer_id":"offer_example","source_thread_id":"thr_example",
  "source_run_id":"run_example","client_request_id":"req_example"
}
```

`offer_id` 和 `client_request_id` 必填；来源会话和运行可选，但若提供必须属于当前用户。响应 `201`，返回完整 `WishlistItem`。

### 9.4 修改心愿项

`PATCH /api/v1/wishlists/default/items/{item_id}`

要求 CSRF。所有字段可选：

```json
{"status":"active","target_price":799.00,"note":"降价时提醒我"}
```

- `status`：`active`、`removed`、`purchased`。
- `target_price`：大于等于 0 或 `null`。
- `note`：最长 1000 字符或 `null`。
- 省略字段表示不修改，显式 `null` 表示清空。

前端应至少提交一个真实变化字段。响应 `200` 返回更新后的完整心愿项。

### 9.5 删除心愿项

`DELETE /api/v1/wishlists/default/items/{item_id}`

要求 CSRF。响应 `204`。当前为软删除，随后不再出现在默认列表。

### 9.6 价格历史

`GET /api/v1/wishlists/default/items/{item_id}/price-history`

```json
{
  "wishlist_item_id":"wsi_example","added_price":899.00,"currency":"CNY",
  "items":[{
    "observation_id":"obs_example","observed_at":"2026-07-21T03:00:00.000Z",
    "price":859.00,"currency":"CNY"
  }]
}
```

观测按时间升序。`price` 可为空；绘图时跳过无有效价格的点，不显示为 0。

## 10. 长期记忆

管理页面直接读 PostgreSQL API。OpenSearch 通过 Outbox 异步同步，搜索索引可短暂滞后，CRUD 响应是权威状态。Agent 识别的偏好不能自动持久化，必须经用户确认后调用创建或修改接口。

### 10.1 数据结构

```ts
interface MemoryEntry {
  memory_id: string;
  user_id: string;
  category: "blacklist" | "preference" | "history";
  key: string;
  content: string;
  confidence: number;
  source: string;
  status: "active" | "deleted";
  source_thread_id: string | null;
  source_run_id: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}
```

### 10.2 查询

`GET /api/v1/memories`

响应 `{ "items": MemoryEntry[] }`。当前仅返回有效记忆，按更新时间倒序。

### 10.3 创建

`POST /api/v1/memories`

要求 CSRF。

```json
{
  "category":"preference","key":"headphone_preference",
  "content":"偏好轻量、主动降噪的头戴式耳机","confidence":1.0,
  "source_thread_id":"thr_example","source_run_id":"run_example"
}
```

| 字段 | 约束 |
| --- | --- |
| `category` | 必填：`blacklist`、`preference`、`history` |
| `key` | 必填，1～128 字符，同一用户内唯一 |
| `content` | 必填，1～4000 字符 |
| `confidence` | 可选，0～1，默认 1 |
| 来源 ID | 可选，若提供必须属于当前用户 |

响应 `201`，返回完整 `MemoryEntry`。

### 10.4 修改

`PATCH /api/v1/memories/{memory_id}`

要求 CSRF。可修改 `category`、`content`、`confidence`；`key` 不可修改。每次更新递增 `version` 并保存历史。前端应至少提交一个真实变化字段。响应 `200`。

### 10.5 删除

`DELETE /api/v1/memories/{memory_id}`

要求 CSRF。响应 `204`。当前为软删除并记录版本及搜索同步事件，首版无恢复接口。

## 11. 兼容接口

以下接口用于旧版或本地诊断，不建议新 Web 前端依赖。

### 11.1 同步聊天

`POST /api/v1/chat`

数据库模式要求登录和 CSRF。请求 `{ "message": "...", "thread_id": "..." }`；响应 `{thread_id, run_id, message, metadata}`。该接口等待任务完整结束，不具备流式体验。新前端应使用 `/tasks`、WebSocket 和运行状态接口。

### 11.2 临时文件上传

`POST /api/v1/files`

使用 `multipart/form-data`，文件字段名必须为 `uploaded`，可选头 `X-Thread-Id`；数据库模式要求登录和 CSRF。响应：

```json
{"thread_id":"thr_example","file_id":"file_example","filename":"example.pdf","size":1024}
```

它不等同于正式运行产物生命周期。若后续需要对话附件，应单独补充产品契约。

## 12. 错误码索引

| HTTP | 错误码 | 前端处理 |
| --- | --- | --- |
| 401 | `AUTH_REQUIRED` | 会话失效，跳转登录 |
| 401 | `INVALID_CREDENTIALS` | 邮箱或密码错误 |
| 403 | `CSRF_FAILED` | 刷新登录状态后再由用户重试 |
| 404 | `THREAD_NOT_FOUND` | 会话不存在或无权访问 |
| 404 | `RUN_NOT_FOUND` | 运行不存在或无权访问 |
| 404 | `FILE_NOT_FOUND` | 产物不存在或无权访问 |
| 404 | `OFFER_NOT_FOUND` | Offer 不存在 |
| 404 | `WISHLIST_NOT_FOUND` | 默认心愿库不存在 |
| 404 | `WISHLIST_ITEM_NOT_FOUND` | 心愿项不存在 |
| 404 | `MEMORY_NOT_FOUND` | 记忆不存在 |
| 409 | `EMAIL_ALREADY_REGISTERED` | 引导已有用户登录 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 键对应不同请求，不自动重放 |
| 409 | `MEMORY_KEY_EXISTS` | 相同记忆键已存在 |
| 409 | `THREAD_ARCHIVED` | 切换只读或新建会话 |
| 409 | `ACTIVE_THREAD_CHANGED` | 重取活动会话 |
| 409 | `RUN_REPLACEMENT_TIMEOUT` | 稍后查询状态或重试 |
| 409 | `RUN_CANCELLATION_FAILED` | 重查运行状态 |
| 422 | `VALIDATION_ERROR` | 读取 `details.errors` |
| 422 | `INVALID_CURSOR` | 清空游标重新加载 |
| 429 | `LOGIN_RATE_LIMITED` | 等待后再登录 |
| 500 | `REGISTRATION_FAILED` | 注册事务失败；保留输入并提示稍后重试 |
| 503 | `DATABASE_NOT_CONFIGURED` | 部署未完成数据库配置 |
| 503 | `SERVICE_SHUTTING_DOWN` | 稍后重试 |
| 500 | `INTERNAL_ERROR` | 展示通用错误并记录上下文 |

内部产物流程还可能返回 `ARTIFACT_EXISTS`、`INVALID_ARTIFACT_PATH`。前端需保留未知错误码兜底。

## 13. 前端更新清单

1. API 统一启用 `credentials: "include"`，写请求统一注入 CSRF。
2. 注册的一次提交生命周期内复用同一 `Idempotency-Key`，自动重试时不要重新生成。
3. 健康检查类型补充可选 `database`，识别 `status: "degraded"`。
4. 补齐心愿项 `PATCH`、`DELETE`、价格历史 Client，以及 `target_price`、`note`、`next_check_at`、`failure_count` 字段。
5. 补齐记忆 `PATCH`、`DELETE` Client，以及来源、状态、版本和删除时间字段。
6. 商品用 `offer_id` 加入心愿库；没有 `offer_id` 时禁用操作并给出可恢复提示。
7. 严格按空值契约渲染评分、销量和价格变化，移除所有运费展示。
8. WebSocket 保存最后成功处理的非空 `sequence`，实现事件去重、断线续传和 `replay_gap` 恢复。
9. 历史会话按 `read_only` 禁止写操作。
10. 对 `401`、`403`、`409`、`429`、`503` 建立统一且可区分的交互。

## 14. 联调验收

- 注册后无需再次登录即可调用 `/auth/me`；退出后原会话不可访问核心接口。
- 不同用户不能查看彼此的会话、运行、文件、心愿项和记忆。
- 同一幂等请求重试不会创建重复会话、任务或心愿项。
- WebSocket 重连不会重复文本或商品卡片，页面刷新后可恢复任务。
- `rating`、`sales`、价格和价格变化的 `null` 均不会显示为 0 或“暂无”。
- 任意商品展示区域都不出现运费。
- 心愿库上涨、下降、持平、空价格、币种不一致和刷新失败正确展示。
- 修改记忆后版本递增，删除后不再出现在有效记忆列表。

## 15. 版本维护

本文档描述 v1 当前契约。新增响应字段应保持向后兼容；删除字段、改变语义、改变空值行为或修改 WebSocket 结构时，必须同步更新本文档并说明迁移方式。前端不得依赖未声明的内部字段、数据库 ID 生成规则或本地文件路径。
