# globuy 会话归档、异步任务与购物工作台实施计划

> 状态：后端与 React + Vite 正式工作台均已于 2026-07-20 实施并通过无付费测试、生产构建和
> 三视口浏览器联调。
>
> 依据：飞书《Globuy项目整理》第 14 点、`docs/frontend-task-api-contract.md`、
> `docs/systematic-design-prompt.json` 以及当前 FastAPI、WebSocket、React + Vite 实现。
>
> 本文同时记录已上线的后端契约与正式购物工作台；完成事实以
> `docs/project-status.md` 和真实测试为准。

## 1. 目标与固定边界

本阶段把当前“WebSocket 内发送消息并同步运行 Agent”的调试链路升级为：

```text
活动会话提交问题
  -> POST /api/v1/tasks
  -> FastAPI 创建 run_id 和后台 asyncio.Task
  -> HTTP 立即返回 thread_id/run_id
  -> React 通过 WebSocket 订阅并重放 AG-UI 事件
  -> 消息、最终结果和产物写入 SQLite
  -> 用户新建对话
  -> 取消活动 run（如有）
  -> 归档旧 thread
  -> 创建唯一的新活动 thread
```

每个匿名 `user_id` 同时只能拥有一个可继续对话的活动会话，以及任意数量只能查看的归档会话。
一个活动会话可以顺序执行多个 run，但同一时刻最多一个 run。

固定边界：

- 首期输入只接受文本；当前 DeepSeek 配置不是多模态模型，不实现参考图上传。
- `thread_id` 表示会话，`run_id` 表示一次 Agent 执行，二者不能互换。
- WebSocket 只订阅、重放事件和维持心跳，不在连接内启动 AgentLoop。
- WebSocket 断开不取消后台任务。
- 归档只读由后端和数据库强制执行，不能只隐藏输入框。
- SQLite 归档与 LangGraph Checkpointer、长期偏好记忆、商品/品类索引分离。
- 只归档消息、run 终态、结构化结果和产物；完整 token、工具和 fork 事件仍是临时事件。
- 商品仍是离线快照，历史与当前页面都不能把价格、库存或运费描述为实时数据。
- `learned_preferences` 仍未持久化，不得显示为“已记住”。
- 本阶段不改变 ItemSearch、CategoryInsight、fork 深度或 ShoppingSummary 业务边界。

## 2. 会话和运行状态

### 2.1 Thread

```text
active   唯一可提交任务的会话
archived 只能查看的历史会话
```

- 同一 `user_id` 最多一个 `active` thread。
- 新建对话时先终结活动 run，再事务性归档旧 thread 并创建新 thread。
- 归档不可逆；首版不提供重新激活或“继续此对话”。
- 没有消息和 run 的空活动会话直接替换，不进入历史栏。
- 归档按 `archived_at` 倒序排列。

### 2.2 Run

```text
starting | running | cancelling |
succeeded | cancelled | failed | interrupted
```

- 每个 run 只能有一个终态。
- `interrupted` 表示服务重启等进程中断，不冒充正常取消或失败。
- thread 归档前必须不存在仍可写入的活动 run。

### 2.3 标题

- 空会话显示“新对话”。
- 首条用户问题提交后，合并连续空白并截取前 32 个字符作为标题。
- 不调用 LLM 生成标题。
- 首版不实现搜索、重命名、删除和自动过期。

## 3. SQLite 持久化

### 3.1 配置

新增 `aiosqlite`，默认数据库为：

```text
output/globuy-sessions.sqlite3
```

新增配置：

```text
GLOBUY_SESSION_DB_PATH=output/globuy-sessions.sqlite3
GLOBUY_ARCHIVE_PAGE_SIZE=20
GLOBUY_ARCHIVE_MAX_PAGE_SIZE=50
```

FastAPI lifespan 启动时打开连接、启用 WAL/Foreign Keys/Busy Timeout、执行版本化迁移并修复遗留
非终态 run；关闭时先取消和等待活动任务，再关闭数据库。共享连接和注册表挂到 `app.state`，避免
模块级可变状态污染测试应用。

### 3.2 表结构

`threads`：

```text
thread_id, user_id, title, status,
created_at, updated_at, archived_at, archive_reason, last_run_id
```

`runs`：

```text
run_id, thread_id, status, query,
created_at, started_at, finished_at,
final_text, result_json, error_code, error_message
```

`messages`：

```text
message_id, thread_id, run_id, role,
content, is_partial, ordinal, created_at
```

`artifacts`：

```text
file_id, thread_id, run_id, filename, kind,
media_type, size, relative_path, created_at
```

`idempotency_keys`：

```text
user_id, client_request_id, operation,
response_json, created_at
```

数据库使用部分唯一索引保证同一 `user_id` 最多一个 `status='active'` 的 thread。所有外键开启
约束；run/message/artifact 必须属于真实 thread。

### 3.3 写入规则

- run 创建时在同一事务中写入用户消息和 run 记录。
- assistant 消息完成后写入完整文本。
- 取消或失败时，如已有部分 assistant 文本，以 `is_partial=true` 保存。
- run 终结时持久化真实状态、最终回答、结构化结果和真实产物 manifest。
- Think/Act、token、工具和 fork 事件不写 SQLite。
- 不存在真实文件时产物列表必须为空，不生成占位下载项。
- 写入失败必须形成明确错误，不能向浏览器报告“已保存”或“已归档”。

## 4. 会话接口

### 4.1 列表

```text
GET /api/v1/threads
  ?user_id={user_id}
  &status=active|archived
  &cursor={cursor}
  &limit=20
```

列表项返回 `thread_id/title/status/created_at/updated_at/archived_at/last_run_status/message_count`。
活动会话置顶；归档使用包含排序时间和 `thread_id` 的稳定游标分页，默认 20、最大 50。

### 4.2 新建并自动归档

```text
POST /api/v1/threads
```

```json
{
  "user_id": "anonymous_x",
  "current_thread_id": "thread_old",
  "client_request_id": "request_x"
}
```

处理顺序：

1. 获取 `user_id` 对应的进程内异步锁。
2. 验证 `current_thread_id` 是该用户当前活动会话。
3. 如有活动 run，先取消并等待其 `finally` 完成。
4. 开启 SQLite `BEGIN IMMEDIATE`。
5. 重新检查活动 thread 未被其他请求替换。
6. 归档旧 thread，创建新 `active` thread。
7. 提交事务后返回新旧 thread 标识。
8. 向旧连接发布 `CUSTOM/thread_archived`。

相同 `client_request_id` 的重试返回同一结果。并发请求最终只能产生一个活动 thread；活动会话已被
其他标签页替换时返回 `409 ACTIVE_THREAD_CHANGED` 和真实活动 thread 摘要。取消或归档任一步失败
都不能创建新 thread 或让前端切换为空白页面。

### 4.3 详情

```text
GET /api/v1/threads/{thread_id}?user_id={user_id}
```

返回 thread 元数据、`read_only`、按 ordinal 排序的消息、run 摘要、结构化结果和产物。归档会话
只通过该接口读取，不建立 WebSocket。

### 4.4 只读强制

归档 thread 上的任务创建、消息写入、标题更新、产物注册和继续执行全部返回
`409 THREAD_ARCHIVED`。thread 不属于传入 user 时统一返回 404，避免通过响应差异枚举会话。

匿名 `user_id` 不是认证机制；首版适用于本地单用户学习环境，公开部署前必须增加服务端认证。

## 5. 异步任务与取消

### 5.1 正式接口

```text
POST /api/v1/tasks
GET  /api/v1/threads/{thread_id}/runs/{run_id}
POST /api/v1/threads/{thread_id}/runs/{run_id}/cancel
WS   /api/v1/ws/{thread_id}?run_id={run_id}&after={sequence}
```

`POST /tasks` 请求固定为：

```json
{
  "query": "帮我筛选 500 元以内的降噪耳机",
  "thread_id": "thread_active",
  "user_id": "anonymous_x",
  "client_request_id": "run_request_x"
}
```

- query trim 后 1～20000 字符。
- thread 必须是该 user 当前活动会话。
- 相同 client_request_id 返回同一 run。
- 成功返回 202、thread_id、run_id、ws_url 和 status_url。
- 旧阻塞式 `/api/v1/chat`、WS 内发送用户消息和正式前端上传入口被正式任务流替代。

### 5.2 RunRegistry

```text
active_tasks: thread_id -> RunHandle
runs: (thread_id, run_id) -> RunRecord
thread_locks: thread_id -> asyncio.Lock
user_locks: user_id -> asyncio.Lock
```

- 同一 thread 最多一个活动 Task。
- 新 run 先取消并等待旧 run。
- 旧 task 的 finally 必须比较 RunHandle 对象身份，不能直接 `pop(thread_id)`。
- 取消清理宽限期默认 5 秒；超时返回 `409 RUN_REPLACEMENT_TIMEOUT`，不允许两个主 run 重叠。
- lifespan 关闭时 cancel 并 await 全部活动任务。

### 5.3 取消

- 运行中转为 `cancelling` 并注入 `CancelledError`。
- 已 cancelling 时保持幂等。
- 已终结时返回真实终态。
- run 不属于 thread 时返回 404，绝不取消后来建立的新 run。
- 取消传播到 AgentLoop、fork、工具和 ShoppingSummary 内部 LLM。
- 前端保持 cancelling，直到 `TASK_CANCELLED` 或状态查询确认。

## 6. AG-UI 事件、重放与心跳

### 6.1 信封

```ts
type MonitorEvent = {
  type: "monitor_event";
  schema_version: "1.0";
  event: MonitorEventName;
  event_id: string;
  sequence: number | null;
  thread_id: string;
  run_id: string;
  timestamp: string;
  message?: string;
  data: Record<string, unknown>;
};
```

事件目录：

- `RUN_STARTED`
- `STEP_STARTED` / `STEP_FINISHED`
- `TOOL_CALL_START` / `TOOL_CALL_ARGS` / `TOOL_CALL_RESULT` / `TOOL_CALL_END`
- `TEXT_MESSAGE_START` / `TEXT_MESSAGE_CONTENT` / `TEXT_MESSAGE_END`
- `RUN_FINISHED` / `RUN_ERROR` / `TASK_CANCELLED`
- `CUSTOM/agent_fork` / `CUSTOM/thread_archived`
- 连接级 `CUSTOM/stream_ready` / `CUSTOM/heartbeat` / `CUSTOM/replay_gap`

工具参数和结果必须脱敏、截断；不得发布密钥、完整 Prompt、向量、原始 RAG 证据或思维链。

### 6.2 EventBroker

- 每个 run 默认保留最近 2000 条业务事件，终态后保留 30 分钟。
- publisher 按“分配 sequence/event_id -> 写入缓冲 -> 广播”执行。
- 每连接使用独立队列和单一 writer，确保历史重放在实时事件之前。
- 队列默认 256；慢消费者关闭后携带游标重连，不静默丢事件。
- after 早于缓冲起点时发 `replay_gap`，前端通过状态接口恢复终态。
- 同一 run 支持多标签页；断开时只移除准确 WebSocket。
- WebSocket 断开不取消任务。

### 6.3 心跳与文本

- 服务端每 20 秒发送 heartbeat；前端 45 秒无任何消息时主动关闭并重连。
- heartbeat/stream_ready/replay_gap 不进入业务事件列表。
- 非终态异常按最后 sequence 指数退避重连；终态后停止重连。
- 文本严格按 message_id 拼接 START -> CONTENT delta -> END。
- sequence 小于等于已处理游标的事件视为重复，不再次触发 reducer。

## 7. 服务重启

首版仍使用 `InMemorySaver`。SQLite 能恢复可查看内容，但不能恢复 Agent checkpoint。

启动时：

1. 遗留 `starting/running/cancelling` run 更新为 `interrupted`。
2. 有消息或 run 的旧活动 thread 归档，`archive_reason=server_restart`。
3. 空活动 thread 废弃，不加入历史栏。
4. 浏览器创建新的活动 thread。

前端显示“因服务重启中断并归档”，不能声称 Agent 上下文已恢复。持久化 LangGraph Checkpointer
不属于本阶段。

## 8. React + Vite 工作台

### 8.1 布局

桌面 12 栏：

```text
左侧 3 栏：历史会话栏
中间 6 栏：购物对话、结果和输入区
右侧 3 栏：当前 run 的阶段、工具和 fork 轨迹
```

历史栏分“当前会话”和“历史归档”；归档项显示标题、归档时间和最后终态，并支持“加载更多”。

- `>1200px`：3/6/3 三栏。
- `768～1200px`：历史 3 栏、对话 9 栏，运行轨迹使用抽屉。
- `<768px`：单栏，历史与运行轨迹分别使用可访问抽屉。

### 8.2 活动与查看分离

```ts
activeThreadId: string;
viewingThreadId: string;
viewMode: "active" | "archived";
```

- localStorage 保存匿名 user_id 和 activeThreadId。
- 点击归档只改变 viewingThreadId，不能覆盖活动 thread。
- 打开归档时不建 WS、不显示输入、发送或取消按钮。
- 归档顶部显示“已归档 · 只读”，右侧只显示持久化 run 摘要，不伪造事件轨迹。
- 提供“返回当前会话”，不提供“继续此对话”。
- window focus 时重新查询活动 thread，处理其他标签页的归档操作。

### 8.3 新对话确认

```text
开启新对话？

开启新对话后，当前对话将自动归档。
归档后的对话只能查看，不能继续发送消息。

取消 | 归档并开启新对话
```

任务仍运行时追加：“当前任务仍在运行。继续后将先取消该任务，再归档当前对话。”

确认后进入 `archiving`，禁用重复确认，等待后端取消、清理和事务提交，再关闭旧 WS、保存新
activeThreadId、清空活动视图并刷新历史栏。失败时保留旧会话和页面内容，不产生半归档状态。

### 8.4 Reducer 与 Hook

状态至少包含：

```text
threads, archiveCursor, activeThreadId, viewingThreadId, viewMode,
threadLoading, archiveCreating, connectionStatus, taskStatus,
lastSequence, messages, toolCalls, forks, result, artifacts, error
```

操作至少包含：

```text
loadThreads(), loadMoreArchivedThreads(), openArchivedThread(),
returnToActiveThread(), createNewThread(), startTask(), cancelTask(), reconnectNow()
```

- 任务与连接状态分开，不继续使用单一 running 布尔值。
- WS 回调从 ref 读取最新 thread/run/status/sequence，避免陈旧闭包。
- 旧 thread/run 的迟到事件不得写入新会话或归档会话。
- 重连按 1、2、4、8、15 秒封顶并加入抖动。
- 组件卸载只关连接，不自动取消任务。
- Markdown 使用 `react-markdown + remark-gfm`，不启用原始 HTML。

## 9. 视觉、可访问性与产物

根据 `systematic-design-prompt.json`：

- 8px 间距基线、桌面 48px 页边距、24px gutter。
- `#F0EFE9`/白色为背景；`#3A6B5A` 表示活动/成功；`#2E4A7A` 表示运行中；
  `#F4793E` 只用于取消、归档确认和警告。
- 历史栏依靠排版、细边框和留白，不堆叠厚重卡片。
- 默认扁平，仅 hover/活动面板使用轻阴影。
- 动画只用 opacity/transform，并尊重 `prefers-reduced-motion`。
- 不实现装饰性准星鼠标。
- 确认弹窗支持焦点锁定、Escape 和键盘操作。
- 状态必须有文字，文本与控件满足 WCAG AA 和可见焦点要求。

产物接口：

```text
GET /api/v1/threads/{thread_id}/runs/{run_id}/files
GET /api/v1/threads/{thread_id}/runs/{run_id}/files/{file_id}
```

归档仍可下载真实产物。服务端用 manifest 将 file_id 映射到安全相对路径，拒绝路径穿越、绝对
路径、错误 run、未知 file_id 和符号链接越界；不实现参考图或通用附件上传。

## 10. 测试与验收

### 10.1 SQLite 与会话

1. 同一 user 无法同时拥有两个活动 thread。
2. 新建对话原子归档旧 thread 并创建新 thread。
3. 空会话不进入历史栏。
4. 归档会话重启后仍可查看且不可写。
5. 消息顺序、run 结果和产物完整恢复。
6. 历史游标分页无重复或遗漏。
7. 多标签页并发新建最终只有一个活动 thread。
8. 相同 client_request_id 不重复创建 thread/run。
9. SQLite 失败不产生半归档状态。

### 10.2 任务、事件和归档

1. Fake Agent 未完成时 POST 立即返回 202。
2. 同 thread 新 run 取消并等待旧 run。
3. 旧 finally 不删除新 RunHandle。
4. 取消传播到 AgentLoop、fork、工具和 Summary LLM。
5. 运行中确认新对话先取消再归档；取消失败不归档。
6. 归档后迟到事件不写入新 thread。
7. HTTP 返回前的事件可在首次 WS 连接后重放。
8. 多 WS 断开一个不影响其他连接。
9. replay gap 后状态接口仍可恢复终态。

### 10.3 重启与前端

1. 遗留 run 变为 interrupted，有内容的活动会话自动归档。
2. 历史消息、结果和产物仍可读取，UI 不声称恢复 Agent 上下文。
3. 历史栏正确区分当前/归档；归档只读且不建 WS。
4. 返回当前会话恢复输入和活动状态。
5. reducer 按 message_id 拼接、按 sequence 去重。
6. 新对话弹窗通过键盘、焦点和 Escape 测试。
7. Markdown XSS 不能执行，外链带安全 rel。
8. 1440px、1024px、390px 布局可用。

最终验证：

```text
python -m ruff check app tests
python -m pytest -q
npm run test
npm run build
```

自动测试只使用 Fake Agent/Fake Model，不请求真实 DeepSeek 或商品 Provider。

## 11. 推荐实施顺序

1. ~~SQLite schema、迁移、ThreadRepository 和 RunRepository。~~ 已完成。
2. ~~会话列表、详情、新建并自动归档接口。~~ 已完成。
3. ~~RunRegistry、HTTP 202 后台任务、run-aware cancel 和 lifespan 清理。~~ 已完成。
4. ~~Monitor/EventBroker、sequence 缓冲、重放、心跳和只订阅 WS。~~ 已完成。
5. ~~消息、结果、产物写入与安全下载。~~ 接口和门禁已完成；Agent 尚不生成真实文件。
6. ~~前端会话 API、reducer、`useGlobuyTask` 和重连。~~ 已完成。
7. ~~三栏工作台、只读历史栏、确认弹窗和响应式布局。~~ 已完成。
8. ~~前端 reducer、XSS、弹窗、三视口浏览器联调和构建验收。~~ 已完成；完整多标签页和服务
   重启场景仍由后端契约测试覆盖，后续可增加浏览器 E2E 自动化。

## 12. 当前未实施项

后端本阶段仍未实现：持久化 LangGraph Checkpointer、多进程/多实例共享任务与事件基础设施、
服务重启后的过程事件恢复、真实报告文件生成器、服务端认证，以及长期偏好 BaseStore。
这些能力不属于本阶段首版 FastAPI 闭环，不能由 SQLite 会话历史冒充。

前端首版已实现本文约定的 API client、reducer、`useGlobuyTask`、游标重连、三栏历史/对话/轨迹
工作台、只读归档、新对话确认、安全 Markdown、ArtifactList、响应式和键盘可访问性。当前 Agent
尚不生成真实报告文件，因此 ArtifactList 只在后端返回真实 manifest 时显示；浏览器端自动化目前
覆盖 reducer、XSS、弹窗与三视口烟雾联调，尚未建立持续运行的完整多标签页 E2E 套件。

## 13. 参考资料

- `docs/frontend-task-api-contract.md`
- `docs/systematic-design-prompt.json`
- `docs/project-status.md`
- AG-UI Core Architecture：<https://docs.ag-ui.com/concepts/architecture>
- AG-UI Events：<https://docs.ag-ui.com/sdk/python/core/events>
- FastAPI Lifespan：<https://fastapi.tiangolo.com/advanced/events/>
