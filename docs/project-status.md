# globuy 项目状态

## 2026-07-22：固化全品类按需商品搜索与 AG-UI 进度目标方案

- 新增 `docs/商品搜索链路逻辑更新的具体实现方案.md`，将当前“主要检索耳机离线快照”的链路扩展为目标设计：Think/Planner 先形成结构化商品品类和搜索意图，ItemSearch 内部检查公共目录覆盖，不足时通过通用 Provider 适配层按需补充淘宝、京东和抖音候选，再经 MySQL、事务 Outbox、批量 Embedding 和 OpenSearch 执行现有 BM25 + BGE-M3 + 无权重 RRF 混合检索。
- 目标候选策略固定为三平台有效去重总数 60 条可用下限、100 条软目标、120 条硬上限；三个平台可受控并发、单平台按页串行，抖音连续传递 `searchId`。缓存命中不调用 Provider，单平台失败时允许其他平台以 `partial` 返回，100 条不是每次对话的阻塞条件。
- 方案保持九业务工具、MySQL 权威库、单一 OpenSearch 商品索引和既定向量模型不变；实时采集设计为 ItemSearch 下层应用服务，不让 LLM 直接写库或索引。新增目标包括 CatalogScope 新鲜度/覆盖、同范围 singleflight、Provider预算与幂等、`semantic_hash` 向量复用、批量写入及全路径取消传播。
- AG-UI 目标增加意图识别、缓存检查、三平台采集、标准化、持久化、索引和混合检索等脱敏 `CUSTOM` 进度事件；继续使用当前 EventBroker sequence、缓冲、重放和标准终态。本文仅为目标设计，尚未修改运行代码、数据库结构、OpenSearch Mapping或前端 reducer，也未调用真实付费 API；实施前仍需确认 Just One API 实际单价、长期持久化和面向用户展示授权。

## 2026-07-22：重整 GitHub clone 后的 README 启动入口

- README 现将可独立复现的“本地 MySQL 8 + mock 模型 + FastAPI + Vite”路径设为快速开始：包含 Conda/Node/Docker 前置条件、创建随机本地数据库账号、Alembic 迁移、双终端启动、健康检查、停止与清理命令。它不要求 DeepSeek、Tavily 或商品 Provider 密钥，适合登录、会话、任务与 WebSocket 链路演示。
- 明确 `npm run dev` 仅启动 Vite；其 `/healthz` 与 `/api/*` 会代理到 8000，因此必须单独启动 FastAPI。README 也明确当前 `compose.yaml` 只管理 OpenSearch/Redis，MySQL 需按快速开始单独创建或接入已有实例。
- 纠正 clone 可复现范围：1000 条商品 Candidate 快照、索引、模型缓存与密钥均不随 Git 发布，完整 ItemSearch 不能仅靠 clone 重建；README 将其作为需要获准数据包、OpenSearch/Redis 和首次 BGE-M3 建库的进阶路径，避免把离线快照表述为实时数据。
- 验证（未调用付费模型或外部商品 Provider）：在当前已配置 MySQL 上，`Database.ping()` 返回成功；临时启动 `python -m uvicorn app.api.server:app --host 127.0.0.1 --port 8000` 后，`GET /healthz` 返回 `status=ok` 与 `database=ok`，随后已停止该临时进程。Docker 状态确认现有 `mysql8` 映射 3307，项目 OpenSearch/Redis 均 healthy。尚未在全新机器完成端到端 clone 演练。

## 2026-07-21：修复推荐商品加入心愿库，并重构长期记忆中心

- 推荐结果在返回与持久化前会按 `item_id` 补齐稳定的 `product_id` / `offer_id`，历史结果也会在读取时获得相同补全，因此前端“加入心愿库”按钮不再因缺少报价标识而被禁用。
- 修复商品快照导入时 Product/Offer 缺少 ORM relationship 导致的 MySQL 外键写入顺序问题；已将已核验的本地 JSONL 快照幂等导入当前 MySQL，共 1000 条 Product/Offer/Observation，并通过临时隔离用户验证“创建默认心愿库 → 加入真实 Offer → 读取心愿库”完整链路，QA 数据随后已清理。
- 个人中心左侧已从心愿库改为长期记忆列表，保留分类、内容与删除操作；右侧只保留新增记忆表单，并加入“性价比党、精致党、中国风、传统风格、高端、INS 风”六个快速选择标签，点击后会填充可继续编辑的结构化名称与内容。
- 视觉延续现有暖白纸张、Newsreader / Noto Serif SC 标题与 DM Sans / Noto Sans SC 正文体系；桌面采用左右双卡片，移动端改为单列并重排账户头部，避免横向溢出。
- 验证：后端 134 项测试通过；前端 Vitest 4 文件 13 项测试、TypeScript 与 Vite 生产构建通过；本机 Google Chrome 在 1440×900 与 390×844 下通过真实认证/记忆接口交互检查，六个标签可点击填表且两种视口均无横向溢出。未调用付费模型或外部商品 Provider。

> 最后更新时间：2026-07-22
> 状态口径：本文同时记录参考目标、当前实现和已知差距；“存在文件”不等于“已接入主链路”。

## 2026-07-21：新增首个 Agent 阶段前的初始化状态事件

- `RunRegistry` 现于 `RUN_STARTED` 后、`TEXT_MESSAGE_START` 前发布可重放的 `CUSTOM/conversation_initializing` 事件，用户可见文案为“收到你的消息了~正在初始化本次对话”。它覆盖长期记忆召回及上下文准备等待，但不暴露记忆原文、查询、向量或 Prompt。
- 前端将该事件存为临时运行状态：运行轨迹与空内容流式气泡可展示该文案，首个 `STEP_STARTED` 或任一终态即清除；它不写入 `messages`，因而不会出现在持久化的 assistant 历史中。
- WebSocket v1 契约与 README 已同步补充。验证：`conda run -n globuy python -m pytest tests/test_api.py::test_thread_task_status_and_replayable_websocket -q` 通过；`frontend` 的 `npm.cmd run test -- --run src/state.test.ts`（4 项）与 `npm.cmd run build` 均通过，未调用付费模型或外部商品 Provider。

## 2026-07-21：修复登录后首条对话 HTTP 500

- 根因与注册故障同属无 ORM relationship 时的 MySQL flush 顺序问题：`create_run` 在同次 flush 中先插入引用 `run_id` 的 `messages`，再插入 `runs`，真实 MySQL 因外键错误 1216 回滚任务事务，前端只能收到 HTTP 500。
- `MySQLSessionStore.create_run` 现在先插入并 flush `runs` 父记录，再写首条用户消息和任务幂等记录；任务创建仍保持 `202 + thread_id/run_id` 后台执行与 WebSocket 订阅契约。
- 外键回归测试已扩展为“注册 → 登录 → 创建活动会话 → 提交任务”完整无付费链路。Ruff、compileall 和后端 133 项测试通过；`127.0.0.1:3307/globuy` 真实事务验证活动会话、run 与首条 message 均写入成功，临时 QA 数据已清理，未调用真实模型。

## 2026-07-21：修复 MySQL 注册成功误报与无法登录

- 根因是认证模型仅声明表级外键、未声明 ORM 关系时，SQLAlchemy 的同次 flush 在真实 MySQL 上先插入 `auth_sessions`，触发外键错误 1216；事务因此整体回滚，原错误恢复逻辑却把任意 `IntegrityError` 都误报成 `EMAIL_ALREADY_REGISTERED`。
- 注册事务现在先单独 flush `users` 父记录，再写入默认心愿库、认证会话和幂等记录；只有能查到既有邮箱时才返回 `EMAIL_ALREADY_REGISTERED`，其他子记录约束失败改为可重试的 `500 REGISTRATION_FAILED`。
- 新增开启 SQLite 外键约束的注册顺序回归测试，以及“子记录约束不得误报邮箱已存在”的错误分类测试；旧 API 测试夹具显式关闭真实数据库配置，避免本机 `.env` 污染 legacy SQLite 隔离测试。Ruff、compileall 通过，后端 133 项测试全部通过；当前 `127.0.0.1:3307/globuy` 真实链路验证注册 201、同密码登录 200，临时 QA 用户已清理。

## 2026-07-21：注册面板三处排版修正

- 根据用户标注放大注册表单标签、输入内容、密码规则、页签与提交按钮文字；删除标题下方的注册说明文案。
- 将“创建你的账号。”调整为更适合右栏宽度的响应式字号并锁定桌面单行，左侧插画、Globuy 超大字样及认证交互保持不变。
- 验证：TypeScript 编译通过，Vitest 4 文件 12 项通过，Vite 7.3.6 生产构建通过；本机 Chrome 在 2048×1080 注册态确认标题单行、说明不存在、无横向溢出，并生成全页及认证栏对照图。

## 2026-07-21：前端完成认证封面与独立心愿库 v1 适配

- 未登录入口已改为沿用原 Globuy 彩铅封面的登录/注册面板；注册包含显示名称、确认密码、显隐密码、8～16 位字母数字规则和同次提交复用 `Idempotency-Key`，注册/登录成功后再以 `/auth/me` 校验会话。
- 统一 API Client 继续使用 `credentials: include`，只为登录后的写请求集中注入 `globuy_csrf`，并新增 15 秒超时、结构化错误保留和 `401 AUTH_REQUIRED` 全局失效通知。
- 新增 `/wishlist` 独立页面及工作台右上角入口，接入默认心愿库真实 GET、心愿项 PATCH/DELETE、价格历史 GET 和商品 POST；支持空价格、上涨/下降/持平、陈旧数据、缺失链接、删除确认、购买/恢复状态和近七日有效价格 SVG 折线图。
- 心愿库沿用现有暖白、Newsreader/DM Sans/Noto Sans SC 与彩铅品牌语言，新增独立可替换的“价格观察员”和“心愿购物车”SVG IP；桌面、平板、移动端均有布局，并遵循 44px 触控区域与 `prefers-reduced-motion`。
- 验证：TypeScript `tsc -b --pretty false` 通过；Vitest 4 文件 12 项通过；Vite 7.3.6 生产构建通过。本机 Chrome 以隔离配置验证 1440×1000 认证封面、1440×1000 与 390×844 心愿库均无横向溢出。视觉 QA 使用 CDP 拦截的契约样例，仅验证布局；当时本机 `127.0.0.1:8000/healthz` 超时，因此真实 MySQL 登录、CSRF 写入和心愿库数据联调仍需后端恢复后执行。

## 2026-07-21：完成 v1 前后端接口文档

- 新增 `docs/globuy接口文档v1.md`，以当前 FastAPI 路由、请求模型、MySQL Repository、WebSocket 事件和前端 API Client 的实际实现为依据，覆盖系统、认证、会话、任务、产物、心愿库、价格历史、长期记忆及兼容接口。
- 文档明确服务端会话、CSRF、幂等、用户数据隔离、统一错误结构、WebSocket 断线续传和商品空值展示规则，并汇总当前前端仍需补齐的接口与字段。
- 本次仅新增和维护文档，未修改前后端运行逻辑。

## 1. 项目目标

`globuy` 是一个边学习边独立实现的全球购物 AgentLoop 项目。用户持有参考项目的实现方案、
课程截图和设计思路，但没有其源码。当前目标是在不复制源码的前提下，让本项目在概念命名、
模块边界和学习颗粒度上与参考项目对齐。

核心技术栈：FastAPI、Uvicorn、WebSocket、LangChain、LangGraph、React、TypeScript、Vite。
ItemSearch 检索栈固定为 OpenSearch Hybrid，Faiss 仅保留实验能力。开发环境采用 Conda
`globuy`，Python 3.12；对话模型使用 DeepSeek OpenAI 兼容接口。

当前项目范围明确排除自研检索模型的训练与微调，也不建设依赖人工评分标签、负样本和训练
闭环的学习型排序机制。原三塔目标中的自训练 Query/User/Item 编码不再作为当前项目的实施
方向。`ItemSearch` 的替代方案已确认并固化到
`docs/itemsearch-no-training-implementation-plan.md`：使用单一 OpenSearch 商品索引，将 BM25 与
冻结 BGE-M3 Dense Vector 通过无权重 RRF 融合。该方案已完成代码、真实建库和本地检索验收。

## 2. 当前主链路

```text
React 彩铅品牌封面 + 三栏购物工作台
    -> FastAPI HTTP 202 后台任务 / 只订阅 WebSocket
    -> 服务端 Cookie 鉴权 + MySQL 权威业务库 + RunRegistry + EventBroker
    -> OpenSearch 商品/长期记忆索引 + Redis 缓存/登录限流
    -> run_agent_stream() / run_agent()
    -> 显式 AgentState
    -> Think -> Act -> Observe -> Reflect
    -> 成功 ShoppingSummary / ChatFallback -> END
```

当前主图已具备：

- `ChatOpenAI` 统一模型创建、进程级缓存和 `mock` 模式；父 Loop 与 fork 共享模型实例。
- 显式 `AgentState`、Think/Act/Observe/Reflect 节点、阶段工具白名单和终结状态。
- `InMemorySaver` 按调用时传入的 `thread_id` 保存进程内短期消息。
- 九业务工具由统一工厂注册；ShoppingSummary 绑定共享模型，owner-aware DispatchTool 运行时追加。
- ToolNode 统一监控、工具结果压缩、6/4 循环检测和 fork/主循环超时与递归预算。
- Observe 后的 Cache Breakpoint 保留最近 3 个完整工具调用组，并用 RemoveMessage 替换旧历史。
- `astream_events(version="v2")` 流式入口、FastAPI 202 后台任务、事件重放/心跳和只订阅 WebSocket。
- SQLAlchemy 2/asyncmy/Alembic MySQL 持久化、唯一活动 thread、只读归档、
  run/message/result/artifact 持久化和重启修复；SQLite 仅保留显式 legacy 测试适配器。
- 邮箱密码注册登录、Argon2id、服务端可撤销会话、HttpOnly Cookie、CSRF 和 Redis 登录失败限流；
  正式业务接口不再信任前端 `user_id`。
- Product/Offer/SourceSnapshot/OfferObservation 权威表、幂等 JSONL 导入、稳定
  `product_id/offer_id` OpenSearch 投影、从 MySQL 重建索引的入口、商品 Outbox Worker，以及按北京时间
  03:00 调度、优先复用当日观测且不伪造价格的独立刷新 Worker。
- 单默认心愿库、相对加入时价格变化、历史观测 API；用户管理的长期记忆、版本审计、事务 Outbox、
  独立 OpenSearch 索引和 LangGraph BaseStore 适配已接入主 Agent 查询入口。
- React 正式 API client、会话 reducer、只订阅事件 Hook、游标重连、只读归档，以及带品牌封面、
  URL 恢复、注册登录、个人中心、心愿库、长期记忆、商品卡片/比较和响应式三栏布局的工作台。
- YAML Prompt 配置与 System Prompt 拼装，已固化循环、fork 判断和工具职责边界。

## 3. 九个工具：目标与当前实现

九个工具都是主 AgentLoop 的行动选项，不是九个子智能体。

| 工具 | 目标阶段/归属 | 当前实现 | 主要差距 |
|---|---|---|---|
| Planner | Think / 内部 | 确定性需求拆解，可直接调用 | 尚未建立显式 Think 状态与动态计划状态 |
| ChatFallback | Think / 内部 | 返回澄清问题 | 尚未完成购物意图分类路由 |
| WebSearch | Think / 外部 | Tavily 异步 `POST /search`；返回 URL/摘要/相关度/检索时间/request ID/credit，用量与错误脱敏；真实 `basic` 搜索已通过 | 不负责实时商品报价；对话中暴露过的本地 key 应在验收后轮换 |
| CategoryInsight | Think / 外部 | 独立 CategoryCard RAG：别名归一、Hybrid 召回、可降级精排、Redis 缓存和严格 JSON 提炼 | 首期仅覆盖耳机快照；本机 Reranker 端点未配置，DeepSeek 最终制卡发布需单独允许聚合数据外发 |
| ItemSearch | Think / 外部 | 异步单平台 BM25 + BGE-M3 + RRF，支持结构化过滤和 monitor | 数据为离线快照，尚未接实时 Provider |
| ItemPicker | Reflect / 内部 | 严格 Schema；有证据硬约束后按 retrieval rank、rating、price、输入顺序选择，最多 3 项；运行前会读取当前用户已确认的长期记忆并注入 Prompt | 偏好仍需用户通过记忆 CRUD 明确确认，不自动持久化 Agent 推断 |
| PriceCompare | Reflect / 外部 | 使用国内 CNY 费用契约比较完整报价；未知运费单列且不得胜出 | 未接入实时跨平台报价和规格对齐 |
| ShippingCalc | Act / 外部 | 国内单报价确定性计算：商品价 × 数量 + 已知运费；未知运费不按零处理 | 未接入平台实时运费来源 |
| ShoppingSummary | Reflect / 内部 | 工具内额外调用一次共享 LLM 生成严格 `final_text`；DeepSeek V4 使用非思考模式 function calling；最终商品按平台与商品 ID 从已验证本地快照确定性补齐缺失 `image_url`，成功结构化输出才终结 | 报告未持久化；额外调用需要真实模型配置 |

当前 LangGraph 已显式建模 `Think -> Act -> Observe -> Reflect`。Think 与 Reflect 分别绑定允许
工具集合，ToolNode 包装器再次执行阶段校验；信息不足时 Reflect 回到 Think，成功终结工具进入 END。
模型偶发生成的跨阶段工具调用会在写入消息历史前被剔除；Reflect 只请求 Think 工具时会安全回到
Think。同质 fork 在返回压缩发现后可以直接结束，不再要求调用仅应由父任务使用的终结工具。

## 4. AgentLoop 四属性与 fork 对齐情况

| 属性 | 参考目标 | 当前实现 | 对齐状态 |
|---|---|---|---|
| `thread_id` | Loop 实例的会话唯一标识 | `run()` 调用参数，不是 AgentLoop 实例属性 | 未对齐 |
| `checkpoint` | 独立检查点，支持断点恢复 | 每个 AgentLoop 默认拥有独立 `InMemorySaver` | 部分对齐，仅进程内 |
| `tool_set` | 同质 fork 与父 Loop 共享同一套工具 | `fork()` 复用完整业务工具；裁剪移入 `expert()` | 已对齐 |
| `system_prompt` | 同质 fork 与父 Loop 共享同一 Prompt | `fork()` 复用完全相同的 Prompt；专家 Prompt 独立 | 已对齐 |

参考项目补充的同质 fork 契约已经落地：子 Agent 获得独立 `thread_id/checkpoint`，同时共享父
Agent 的 `tool_set/system_prompt`：

- `fork()`：同质完整克隆，并由动态 `dispatch_tool` 调度。
- `expert()`：显式工具子集和专用 Prompt，不暴露嵌套 dispatch。

## 5. 其他模块状态

| 模块 | 当前能力 | 是否进入主 AgentLoop |
|---|---|---|
| API / WebSocket | 会话/归档 API、HTTP 202 后台任务、run-aware 取消、统一事件信封、sequence 重放、心跳、多订阅和安全产物下载；兼容 `/chat`/`files` 保留 | 是 |
| Recall | User/Query/Item 演示编码与融合排序；Faiss HNSW/IP 索引支持新增、检索、保存和加载 | 否 |
| Memory | MySQL 为权威库，提供用户级 CRUD、版本历史与事务 Outbox；LangGraph BaseStore 使用独立 OpenSearch 记忆索引检索，并在运行前注入相关条目 | 是（读取）；写入只接受用户确认后的 API 操作 |
| Compress | Cache Breakpoint 已在 Observe 后进入主图，保留最近 3 个完整工具调用组 | 是 |
| Eval | 动态 Rubric、确定性 Judge、JSONL TraceLogger | 否 |
| Frontend | React + TypeScript + Vite；注册登录门禁、`/assistant/:thread_id` 恢复、三栏工作台、HTTP 202 创建任务、只订阅 WS、游标重放、只读归档、默认心愿库、价格变化和长期记忆管理 | 是 |
| OpenSearch / Redis | OpenSearch 3.7.0 分别承载商品、品类和长期记忆索引；Redis 7 承担 CategoryInsight 缓存与登录失败限流 | ItemSearch、CategoryInsight 和 Memory 检索已接入；MySQL 仍是业务权威库 |

正式任务由 `POST /api/v1/tasks` 创建并立即返回，`RunRegistry` 管理每 thread 唯一后台协程、
替换/归档前取消等待和对象身份清理。WebSocket 只订阅指定 thread/run，EventBroker 把
LangGraph v2、Monitor、文本和终态统一封装为 `monitor_event`，分配 sequence 后缓冲并广播，
支持 replay gap、独立订阅队列和心跳。MySQL 持久化 thread/run/message/result/artifact manifest；
归档强制只读，重启将遗留 run 标为 interrupted，并归档有内容的活动 thread。`/chat` 与 `/files`
仅保留兼容调试。React 正式 reducer/Hook 以活动/查看会话分离建模，按 message_id 拼接文本并按
sequence 去重；45 秒陈旧连接检测、1/2/4/8/15 秒抖动重连、replay gap 状态恢复、终态同步、
归档只读、新对话原子切换和真实产物下载均已接入。

### 5.1 飞书《Globuy项目整理》补充的目标契约

2026-07-17 已完整读取用户指定的“项目实现细节的整理”正文及其中 29 张有效图片；按文档明确
标记跳过了开头无需阅读的 Agent 范式问答。该资料补充的是目标实现细节，不代表仓库已完成：

- 端到端目标：文字/图片购物意图 -> 结构化约束 -> 按需同质 fork 并行跨平台检索 ->
  到手成本比较 -> 偏好/黑名单筛选 -> 带理由、来源和风险的购物清单。
- 主循环目标：`Think -> Act -> Observe -> Reflect`，信息不足继续循环，充分后才调用
  `ShoppingSummary`；`dispatch_tool(demands)` 是 fork 元工具，不属于九个业务工具。
- 任务生命周期目标：HTTP 立即返回任务标识，`active_tasks` 管理后台协程，WebSocket 持续
  推送，支持心跳、同 thread 新任务取消旧任务、取消传播和全路径清理。
- 上下文目标：`thread_id` 贯穿连接、任务、checkpoint、事件、`session_dir` 和记忆来源；
  `user_id` 独立表示长期记忆所有者；两者通过 `ContextVar` 向 Loop、fork、工具和 monitor
  透明传播。
- 压缩目标：Cache Breakpoint 保护稳定 Prompt 前缀和最近 K 轮/工具调用，只压缩旧历史并
  保持 tool-call/tool-result 配对合法。
- 记忆目标：结构化条目包含 `key/category/content/source_session/created_at/confidence`，
  支持读、写/覆盖、撤回删除和相关 Top-K；显式偏好同时影响当前筛选并持久化，新会话仅注入
  相关且最新的紧凑记忆。实现仍必须遵守 BaseStore + OpenSearch 固定选型。
- 事件目标：把运行创建、阶段状态、工具开始/结束、结果、取消、错误映射为 AG-UI 标准事件；
  monitor 统一消费 LangGraph/模型/工具事件，ConnectionManager 做重连安全的 thread 路由，
  前端只依赖稳定事件信封，不展示原始思维链。

当前正式 API 已完成后台任务、精确取消、心跳、事件重放和全路径 finally 清理；
`thread_id/run_id/user_id/session_dir` 通过 ContextVar 贯穿主图、fork、工具和 monitor。
仍未完成的是持久化 checkpoint、跨进程任务/事件共享和长期记忆 BaseStore。Cache Breakpoint
已在 Observe 后接入主图，ShoppingSummary 内部模型增量可进入事件流；前端已经消费正式生命周期。

### 5.2 基础模块与模型配置实现（2026-07-17）

已重新读取飞书文档最新的“基础模块与模型配置”正文和新增实现截图，并按仓库固定技术决策
完成以下落地：

- `.env.example` 与 `Settings` 包含模型温度、压缩边界、WebSocket 心跳、Faiss 路径和
  OpenSearch 配置；三塔 HTTP 端点已在无训练 ItemSearch 实施时移除。对话模型仍使用 DeepSeek
  OpenAI 兼容接口，不复制参考 Qwen 配置。
- `thread_ctx.py` 现在管理 `thread_id/run_id/user_id/session_dir`，提供自动 reset 的
  `thread_scope()` 和继承会话目录的 `fork_scope()`；HTTP/WS 运行已绑定 `run_id`。
- `Monitor` 通过注入 publisher 发送标准工具事件和 `CUSTOM` fork/task 事件，工具不依赖
  WebSocket 或 ConnectionManager；主图事件流适配仍待后续接入，以免重复上报。
- 路径工具区分 session/upload 根目录并保持清洗和越界保护。
- 模型构造增加进程级缓存与可配置 temperature；现有 `AgentLoop.fork()` 复用同一模型对象。
- Prompt 固化 Think/Act/Observe/Reflect 工作协议、三条 fork 条件、工具职责、Planner 字段和
  Summary 上限，并禁止暴露原始思维链。
- 新增 `FaissHNSWIndex`，使用归一化向量上的 Inner Product 实现余弦候选召回，并支持磁盘
  round-trip；新增 OpenSearch 客户端工厂。
- Conda `globuy` 已安装 `faiss-cpu 1.14.3` 和 `opensearch-py 3.2.0`；Compose 已移除
  Qdrant，改为固定版本 OpenSearch 3.7.0；OpenSearch 与 Redis 容器均已启动并通过健康检查，
  Python 客户端 `ping()` 返回成功。

该阶段留下的 Query/User/Item 哈希编码仍只是实验代码，不具备真实语义能力，也未进入主链路。
ItemSearch 后续完成情况见 5.4；LangGraph BaseStore 适配仍属于后续阶段。

### 5.3 原项目 ItemSearch 与跨平台 fork 参考契约（2026-07-19）

已重新导出飞书《Globuy项目整理》并逐张读取新增的 ItemSearch 章节 image_49～image_63。以下是
参考目标，不代表当前代码已经实现：

- 工具签名为单平台检索：`query + platform + top_k + optional user_id`；`top_k` 默认 20、最大
  50。返回固定 Pydantic 结构 `platform/candidates/total_recall/truncated`。
- `Candidate` 固定包含 `item_id/platform/title/price/currency/rating/sales/image_url/attributes`；
  `attributes` 保持嵌套字典，避免把大量长尾属性全部展开给主 Loop。
- 原项目内部使用两个并行通道：Query 向量语义召回始终执行；传入 `user_id` 时再将 User 与
  Query 向量按示例 `0.6/0.4` 融合进行个性化召回；两路结果按 `item_id` 去重并加权重排。
- ANN 示例使用一个索引多取 `top_k * 3` 后按 `platform` 元数据过滤；Query/User 编码通过 HTTP
  TowerClient 调用，Item 向量离线写入 ANN。该三塔与学习权重部分和当前“不训练、不微调”边界
  冲突，只作为理解原设计的证据，不再直接照搬。
- 工具入口负责限制 `top_k`、上报 monitor 开始/结束事件、把 ANN 元数据转成 Candidate，并通过
  `total_recall/truncated` 告诉后续 PriceCompare 是否仍有更多候选。
- 多平台检索是同质 fork 的典型场景：主 Loop 在一次回复中产生多个 `dispatch_tool` 调用，
  LangGraph 并发执行；每个子 Loop 继承完整工具集和 System Prompt、使用独立 thread，随后在
  Think 后调用指定平台的 ItemSearch。单平台单关键词则由主 Loop 直接调用，不 fork。
- 参考图使用 Amazon/Shopee/AliExpress/eBay 四个平台。当前项目已调整为淘宝、京东、抖音三个
  同质子 Agent；最终采用单一 OpenSearch 索引加 `platform` 硬过滤，不再按平台拆分 Faiss。
- 四路长候选结果会增加主 Loop 上下文；参考方案依赖下一轮 Think 前的 Cache Breakpoint 压缩。
  当前主图已在 Observe 后接入 Cache Breakpoint，并保留最近 3 个完整工具调用组；每路候选回流
  仍限制为 10 条。

该节描述原项目参考契约。当前仓库已用无需训练的 OpenSearch Hybrid 方案替代三塔/ANN 内部
实现，同时保留单平台工具契约、结构化输出、monitor 和同质 fork 行为。

### 5.4 ItemSearch 无训练混合检索实现（2026-07-20）

- 新增 `app/search/`：Candidate/SearchFilters Schema、稳定语义文本构造、BGE-M3 懒加载编码、
  OpenSearch mapping/Pipeline/Hybrid Query Builder、索引管理和检索服务。
- 模型固定为冻结 `BAAI/bge-m3` 的 1024 维归一化 Dense Embedding。加载时优先本地缓存，缓存
  缺失才联网下载；`device=auto` 支持 CUDA/CPU 回退。当前实测环境为 PyTorch 2.13.0 CPU 版。
- 商品索引 `globuy-products-v1` 已真实写入 1000 条，平台计数为淘宝 350、京东 333、抖音 317；
  总数与聚合校验后发布别名 `globuy-products`。Pipeline `globuy-products-rrf` 使用无权重 RRF。
- `item_search` 已成为异步单平台工具：`platform` 在 Hybrid 召回前限定候选域；价格、币种、
  最低评分、最低销量和属性等值条件在 RRF 后通过 `post_filter` 执行。OpenSearch/模型/索引不可用
  时返回 `not_configured`，运行失败返回 `error`，不伪造候选。
- `item_picker` 不再计算任意 score，使用 `retrieval_rank`、评分和价格做确定性选择。
- `AgentLoop.fork()` 已改为严格同质克隆，原裁剪能力移入 `expert()`；运行时动态
  `dispatch_tool` 建立独立子 thread/checkpointer、限制深度为 1，并将 fork 与 ItemSearch monitor
  事件统一路由到根 WebSocket。回流时每路 ItemSearch 最多保留 10 条候选。
- 实测三平台查询都严格平台隔离；真实京东查询确认请求体只把平台放入召回前过滤，把
  `max_price=500/min_sales=20` 放入 RRF 后的 `post_filter`，返回 5 条均满足条件且 rank 为 1～5。
  三路并发 dispatch、聚合、线程隔离和深度限制通过 Fake Agent 自动测试，未调用付费 DeepSeek。

### 5.5 CategoryInsight 卡片知识库实现（2026-07-20）

- 新增 `app/category/`，实现人工维护的品类别名、快照标准化、确定性爆款/属性/价格聚合、
  DeepSeek 严格 JSON 制卡与在线提炼、Pydantic 入库门禁、audit manifest 和版本化索引发布 CLI。
- `CategoryCard` 固定为 `card_id/category/card_type/summary/raw_evidence/last_updated/confidence`；
  证据限制为 1～3 条且单条不超过 80 字，时间必须带时区且不能来自未来，未知字段拒绝入库。
- Category 索引与商品索引完全分离。已注册 exact=0.5/0.5、balanced=0.7/0.3、
  semantic=0.9/0.1 三条 `min_max + arithmetic_mean` Pipeline；KNN 子查询固定在 BM25 之前。
- 在线 `category_insight` 支持 quick/deep、Top-30 粗召回、Top-8/15 精排、索引与编码器元数据
  校验、BM25-only/无 Reranker 分层降级及 Redis 一小时缓存。工具只返回提炼后的结构化结论，
  不向主 Loop 暴露 `raw_evidence`、向量或内部提示词。
- `ItemPicker` 可接收品类上下文并添加价格档位、典型属性与组件覆盖标注，但基础排序仍固定为
  retrieval rank、评分、价格，不产生新的综合分数。Dispatch 回流同时压缩 ItemSearch 和
  CategoryInsight 结果；Prompt/Planner 已明确工具分工、未知运费和冷启动边界。
- 本机已使用无付费的确定性路径生成并发布 7 张耳机卡片：attribute 4、bestseller 2、
  price_range 1；物理索引为 `globuy-category-v1-20260720111400-94c03755`，稳定别名为
  `globuy-category`。三条 Pipeline、7 条文档及真实 Hybrid 查询均已验证。
- 默认生产命令仍使用项目现有 DeepSeek 对聚合草稿做 JSON 压缩。本次执行因外部端点数据外发
  需要知情后的单独授权而未完成，因此当前别名指向确定性验收版本；可选本机
  `BAAI/bge-reranker-v2-m3` HTTP 端点也尚未配置；当候选多于目标 Top-K、需要精排时，在线路径
  会显式标为 partial，而不是伪装成功，候选不足则按契约旁路精排。

## 6. 向量基础设施固定决策

详细技术契约见 `docs/vector-infrastructure.md`。后续实现必须遵守：

- ItemSearch 固定为单一 OpenSearch 商品索引上的 BM25 + 冻结 BGE-M3 + 无权重 RRF；
  `platform` 是两路召回共享的前置过滤；价格、币种、评分、销量和属性在 RRF 后执行
  `post_filter`，再从 60～150 条融合候选中截取 `top_k`。
- BGE-M3 使用 1024 维归一化向量；OpenSearch 使用 Lucene HNSW + COSINE。
- Faiss HNSW/IP 只保留为实验能力，不接 ItemSearch，也不作为 OpenSearch 故障时的静默后备。
- 长期记忆当前只预留 `LangGraph BaseStore` 对外接口；未来实现仍固定以 OpenSearch 为后端，
  本轮没有创建索引，也没有执行读取、写入或撤回。
- 当前项目不训练或微调检索模型，不建立标签、负样本和学习排序闭环；原三塔与
  `min_max + 0.7/0.3` ItemSearch 契约已经被本方案取代。
- 长期记忆将使用独立 OpenSearch 索引；CategoryInsight 已使用独立索引和专用融合配方，二者都
  不得直接复用商品索引。
- Qdrant、Redis Stack、Chroma、pgvector、Milvus 不作为 ItemSearch 默认替代，L2 不作为当前
  商品向量距离。

当前未对齐点：Memory 仍是本地 JSON；CategoryInsight 的本机 Cross-Encoder 端点与生产
DeepSeek 制卡版本尚未完成外部验收。Faiss/三塔演示代码仍存在但不进入 ItemSearch，Compose 的
Qdrant 历史预留已经移除。

### 6.1 商品数据源调研结论

平台 API 的详细调研、申请步骤、字段覆盖和合规边界见 `docs/平台api调研.md`。当前固定的接入
顺序是：

- P0：淘宝/天猫联盟、京东联盟；两者均允许个人申请，但最终可用权限取决于媒体备案和平台审核。
- P1：拼多多/多多进宝；已确认 `pdd.ddk.goods.search` 是正确的商品候选入口，后续配合
  `pdd.ddk.goods.detail`；只有个人账号在控制台取得对应权限包后才实现。应用审核计划使用自有
  域名的 HTTPS 官网和独立 OAuth 回调路径，先部署最小审核站点，再部署完整购物 Agent。
- 官方直连接入暂缓：得物、抖音、快手；当前没有普通个人可自助使用的全站消费者商品搜索
  API。该结论不否定 Just One API 等第三方 Provider，其中抖音电商搜索已由用户人工实测可用。

万邦 OneBound 已作为候选第三方统一商品数据 Provider 完成第一轮调研，接口、公开价目、
1000 条耳机数据预算和小样本验收方案见 `docs/万邦API调研.md`。当前只确认淘宝、京东、
拼多多均可采用 `item_search + item_get` 的最小组合；万邦公开价目注明拼多多需单独充值计费，
5 元账户余额的赠送规则、拼多多实际单价、数据持久化/公开展示授权仍需书面确认。万邦尚未被
确定为最终 Provider。

2026-07-17 已在 `datasets/onebound_headphones/` 落地独立、可续传、零自动重试的采集器，真实
凭据仅位于 Git 忽略的 `.env`，原始响应、标准化 JSONL/CSV、请求账本和状态文件也全部忽略。
主、备用两个账号的真实验证结果为：33 次搜索请求、2 次详情请求、按调研单价估算 0.772 元；
保留淘宝 500 条去重候选，其中 1 条完成详情增强，重复 ID 为 0，标题、价格、图片和来源链接
覆盖率均为 100%。成功详情响应具有品牌、类目、`props_list/props_name` 和 SKU 容器，可映射为
设计中的 `attributes`；搜索响应不提供销量和评分，因此对应字段保持 `null`，没有推断或伪造。

两个账号随后都触发调用额度限制；两个账号的京东 `item_search` 均返回接口未开通。按官方
readme 补齐 `cat/start_price/end_price/seller_info` 等完整参数后的额外复测返回 4013 额度超限，
证明原调用地址和参数格式没有偏离文档，但仍无法绕过账号权限/额度。累计另有 7 次淘宝搜索
返回 `data error`。22 个成功搜索响应各返回 48 条，共 1056 条原始候选；符合过滤
条件的唯一 ID 为 506，重复出现率 52.08%，最终按目标截断为 500 条。因此总计 1000 条、京东
至少 450 条以及每平台 10 条详情仍未完成，当前数据状态必须标为 `partial`。补充额度并开通
`jd.item_search`、`jd.item_get` 后可续传；普通数据错误仍不会重试。ItemSearch、Faiss 和图片
下载均未接入本阶段采集链路。

万邦同类第三方商品 API 的详细对比见 `docs/第三方商品API平台对比调研.md`。京东万象已从
候选和测试短名单中移除：京东云官方公告确认其数据服务商城已于 2023 年 3 月 18 日终止运营，
当前仍可访问的商品/API 分类和价格页面只是历史页面，不能购买，也不能证明接口仍可调用。
当前第三方测试顺序调整为先验证万邦，再把 Just One API 作为淘宝/京东及 1688、海外平台的
备用候选；拼多多仍依赖万邦候选或获批后的官方 DDK。聚推客、大淘客和好单库只考虑补充 CPS
Offer/榜单数据；维易明确禁止纯采集和比价，当前不得接入。

2026-07-17 已使用试用 Token 对 Just One API 的“耳机”第一页搜索做真实烟雾测试，共 6 次
请求且未调用详情：淘宝 1 次成功并返回 10 条，已确认商品 ID、标题、价格、主图、评论数、
评分信号、支付用户数和总数分页字段；京东 1 次成功并返回 48 条，已确认商品 ID、标题、价格、
主图、销量、月销量、结构化属性容器和总数分页字段。抖音电商两次均返回业务码 301
`COLLECT FAILED`，得物两次均返回 HTTP 403 且无 JSON 错误体。用户随后人工实测确认抖音电商
搜索接口可正常使用，因此两次 301 只保留为瞬时采集失败证据，不再把抖音判定为接口不可用；
其 Candidate 字段覆盖仍需脱敏响应样本确认。淘宝和京东已通过搜索层最小 Candidate 结构验证，
结构化商品属性仍需详情验证；得物保持未确认。完整脱敏结论见
`docs/第三方商品API平台对比调研.md`，Token、完整请求 URL 和原始响应未写入仓库。由于未保留
商品 ID，下一轮需要淘宝/京东各重取 1 页并各查 1 条详情，共 4 次成功调用上限。

用户充值后已新增 `datasets/justone_headphones/` 自包含、可续传的 Just One API 搜索采集器，
生成文件和 Token 均由 Git 忽略。淘宝 36 次成功搜索形成 350 条去重候选，京东 20 次成功搜索
形成 333 条去重候选；导入用户提供的 1 份抖音成功响应后，又完成 21 次抖音成功搜索，形成
317 条去重候选。最后以淘宝补充 16 条，最终为淘宝 350、京东 333、抖音 317，共 1000 条且
重复商品 ID 为 0。标题、价格、主图和来源链接覆盖率均为 100%；
京东评分缺失、销量覆盖约 41.14%，抖音好评率覆盖约 85.53%、销量覆盖 100%，没有推断或
伪造。累计 125 次尝试、77 次成功搜索，48 次业务码 301 未计入成功调用；按未验证的 0.10 元/
成功调用假设估算 7.70 元，实际扣费以控制台为准。抖音真实响应已用于修正嵌套价格“分→元”
和主图、来源链接、销量、好评率、店铺、类目映射；该批数据现已修复并注入 ItemSearch 的
OpenSearch 商品索引，未注入 Faiss。

为后续离线结构化处理，已把本批数据快照单独归档到 `datasets/headphones_1000/`：主入口为
`normalized/headphones.jsonl` 和 `normalized/headphones.csv`，同时保留已脱敏的 `raw/`、
`reports/` 与 `state/` 用于字段追溯和复现。该目录不包含采集代码或 Token；实际数据文件已
Git 忽略，原始采集工作目录 `datasets/justone_headphones/` 保持不动。

已基于该快照生成 ItemSearch Candidate v2：`datasets/headphones_1000/structured/` 中的
`itemsearch_candidates.jsonl`、`itemsearch_candidates.csv` 均为 1000 条。每条包含
`item_id`、`platform`、`title`、`price`、`currency`、`rating`、`sales`、`image_url`、
`attributes`、`product_url`；`jd` 已统一为 `jingdong`，商品链接覆盖率 100%。新增的只读修复器
从已缓存成功原始响应中找回 1000/1000 条标题与属性，不调用 Provider，且在任何目标 ID 缺失
时拒绝写入。修复后的标题不再含历史乱码，商品 ID 重复数为 0，并已用于真实 OpenSearch 建库。

2026-07-18 已对上述 1000 个 `image_url` 执行只读 HEAD/Range 连通性审计：全部收到 2xx 响应，
996 条声明标准 `image/*` 内容类型；其余 4 条均为抖音图片 URL，响应为
`application/octet-stream`，因此标为“可访问、MIME 未确认”。其中京东 333 条和抖音 317 条均在
严格 TLS 校验下可访问；淘宝 350 条仅在兼容模式下可访问，当前环境的严格 TLS 校验因阿里 CDN
证书域名不匹配失败，已在逐条审计文件中保留 `tls_fallback_used=true`，不能将其表述为严格 HTTPS
校验通过。审计未下载或人工目视完整图片，产物为 `structured/image_url_audit.jsonl` 和
`structured/dataset_audit_report.json`。标题关键词和价格覆盖显示：低价（<100 元）513 条、
100–299 元 280 条、300–999 元 132 条、1000–2999 元 49 条、3000 元以上 26 条；入耳/真无线
552、头戴 113、开放式/骨传导 354、蓝牙/无线 827、有线 145、降噪 502、游戏/电竞 210。数据集
覆盖高低价和主要形态，但整体明显偏低价无线；3000 元以上和头戴式占比低，高价主要来自京东，
后续离线检索评测应按价格带和形态分层抽样，并将 0.01 元等促销价作为异常值复核。

- 测试数据采用“真实获批联盟 API + 明确标注的本地夹具”，不得使用 App 抓包或非官方私有接口。

商品数据模型目标仍分为稳定的 `Product` 和易变的 `Offer`：OpenSearch 的 Embedding 文本只含
标题、品牌、型号、类目和稳定规格；平台、店铺、价格、优惠、运费和采集时间属于展示或报价
层。当前 ItemSearch 使用离线快照；实时联盟账号和 Provider 尚未接入主链路。

## 7. 环境、配置与安全

- Conda 环境：`globuy`
- Python：3.12
- 环境定义：`environment.yml`
- Python 依赖：`pyproject.toml`、`requirements.txt`
- 本地真实配置：`.env`，已由 Git 忽略
- 示例配置：`.env.example`，不得包含真实密钥
- Prompt：`app/prompt/prompts.yml`
- 运行输出：`output/`
- 上传文件：`uploaded/`

## 8. 当前验证基线

最近一次已验证结果（2026-07-21）：

- `python -m compileall -q app datasets tests`：通过。
- `python -m ruff check app datasets tests`：通过。
- `alembic upgrade head`：在全新临时 SQLAlchemy 数据库完成首次升级，随后重复执行安全无变更；
  MySQL DDL 编译测试确认时间列为 `DATETIME(6)`。
- `python -m pytest -q`：131 passed；除既有任务、工具和检索测试外，新增覆盖 SQLAlchemy 空库建表、
  JSONL 商品幂等导入、未登录 401、Argon2id 注册/服务端 Cookie/CSRF、MySQL 会话、心愿库空值与
  价格变化、长期记忆 CRUD 和退出登录；既有 SQLite 测试仅验证显式 legacy 适配器。其余覆盖
  重启修复/归档分页、唯一活动会话、
  HTTP 202、同 thread 替换、run-aware 取消、旧 finally 身份保护、运行中归档、事件重放/
  replay gap、多 WebSocket 隔离、统一信封、幂等创建和产物路径安全；Tavily 请求边界、来源映射、
  无 key 降级、认证/限流/额度/服务错误和取消传播也使用 MockTransport 验证；另覆盖跨阶段工具调用
  归一、同质 fork 正常返回和 DeepSeek V4 Summary 非思考模式。
- Just One API 采集器本地夹具测试：13 passed；未调用付费 API。
- `examples/03` 至 `examples/08`：全部运行成功。
- `npm run test`：Vitest 4 个测试文件、12 项测试通过，覆盖事件拼接/去重、工具聚合、Markdown XSS、
  登录门禁，以及评分/销量空值隐藏、心愿库交互和商品卡片不展示运费。
- `npm run build`：当前 TypeScript 与 Vite 生产构建成功；三张 WebP 品牌资产合计约 397 KB，构建后
  主 JS gzip 约 135.79 KB、CSS gzip 约 88.16 KB。
- 旧版工作台曾由 Playwright 在 1440×1000、1024×900、390×844 三视口通过真实 FastAPI/Vite
  烟雾联调。本轮品牌封面和商品卡片视觉改造尚未重新做浏览器截图对比：Codex 内置浏览器当前
  不可用，直接使用项目 Playwright 仍在等待用户允许，因此不能沿用旧版截图冒充本轮视觉验收。
- 已知非阻塞项：FastAPI/Starlette TestClient 存在一条上游弃用警告。

自动测试使用 Fake Encoder/OpenSearch/Reranker/Redis/Extractor，不请求真实 DeepSeek API。
另已验证 OpenSearch Category 别名指向 7 条文档、三条 Pipeline 存在、真实 BGE-M3 Hybrid 查询
返回三类卡片，Redis `PING` 成功。经用户明确授权，已完成真实 DeepSeek V4 Flash、Tavily 和正式
FastAPI/WebSocket 任务验收：Prompt“帮我挑一个不超过1000元的头戴式耳机”依次完成
CategoryInsight、淘宝/京东/抖音 ItemSearch、ItemPicker 和 ShoppingSummary，收到 `RUN_FINISHED`，
任务状态为 `succeeded`，返回 3 个带来源链接的离线快照候选。另一次 Tavily `basic` 搜索返回
`status=ok` 和 3 条网页结果；供应商响应未提供 credit 数值。测试调用会产生实际模型用量和 Tavily
credit。生产 Category 卡片别名仍未切换，当前 7 张确定性验收卡继续有效。

## 9. 后续优先级

### P0：对齐 AgentLoop 核心语义

1. 把进程内 Checkpointer 升级为可恢复的本地或外部持久化实现。
2. 将当前单进程 `RunRegistry/EventBroker` 保持为首版边界；若启用多 worker 或多实例，先迁移到
   共享任务注册、事件缓冲和广播基础设施，不得直接横向扩容。
3. 让 `thread_id/run_id/session_dir/user_id` 的边界继续贯穿持久化 checkpoint、真实工具产物和
   记忆来源。
4. 为已上线 React 工作台补充端到端自动化测试和真实报告产物预览（当前 reducer、XSS、弹窗、
   三视口浏览器联调与生产构建已覆盖）。

### P1：形成真实购物闭环

1. 为现有 `Product`、`Offer`、`SourceSnapshot`、`OfferObservation` MySQL Schema 补充真实平台
   Provider 验收，并持续量化快照新鲜度。
2. 按淘宝/天猫联盟、京东联盟、获批后的多多进宝顺序接入真实商品数据源；实时 Provider 与
   当前离线 ItemSearch 索引必须保持同一对外 Candidate 契约。
3. 在不改变统一 `Product + Offer` Schema 的前提下，先定义供应商无关 `ProductProvider`。
   Just One API 已完成淘宝 350 条、京东 333 条、抖音 317 条搜索候选；下一步从三平台样本中
   验证稳定属性、字段非空率和实际扣费。得物
   保持未确认。取得数据使用边界和详情证据后才确定首个第三方实现；不实现已停运的
   `JdWanxiangProvider`。
4. 接入汇率与跨平台规格对齐；前端已按当前产品决策统一取消运费展示，未知运费不得推断为 0。
5. 明确外部工具失败、超时、重试和部分结果策略。
6. 为 CategoryInsight 配置并验收冻结 `BAAI/bge-reranker-v2-m3` 本机 HTTP 端点；在明确允许将
   聚合卡片草稿发往当前 DeepSeek 兼容端点后，执行生产制卡并切换 `globuy-category` 别名。

### P2：接入增强模块

1. 对已接入的 LangGraph BaseStore + 独立 OpenSearch 记忆索引补充真实 OpenSearch 故障补偿、
   用户确认偏好和恢复演练。
2. 在最终回答后接入 Eval 和高分轨迹采集。

## 10. 已确认设计决策

- 九个工具属于主 AgentLoop 行动空间，不是九个子 Agent。
- 工具调用与 Think/Reflect/Act 阶段存在目标对应关系。
- 同质 fork 应独立 `thread_id/checkpoint`，共享 `tool_set/system_prompt`。
- 未接入的外部能力必须明确返回 `not_configured`。
- Conda `globuy` 是默认 Python 环境，不重新创建 `.venv`。
- 新代码和文档把参考名称 `globex` 统一替换为项目名称 `globuy`。
- Agent/API 以流式增量事件为固定方向，为 AG-UI 提供运行和工具可观察性。
- ItemSearch 固定使用 OpenSearch BM25 + 冻结 BGE-M3 + Lucene HNSW/COSINE + 无权重 RRF；
  Faiss 只保留实验能力。
- 长期记忆接口固定使用 LangGraph BaseStore；当前项目不训练或微调检索模型，也不建设依赖
  训练标签的学习型评分机制；三塔 Embedding 与学习排序已取消。
- 商品目录目标仍拆分为 `Product + Offer`；价格、优惠、库存、销量等易变字段不得进入
  Embedding 文本。当前 Candidate v2 是离线检索快照，不冒充实时 Offer。
- 首批真实商品 Provider 固定为淘宝/天猫联盟和京东联盟；拼多多仅在个人 DDK 权限获批后接入。
- `docs/project-status.md` 是当前实现状态的事实来源，有实质变更时自动更新。

## 11. 变更记录

- 2026-07-22：新增 `docs/商品搜索链路逻辑更新的具体实现方案.md`，固化“先识别品类、缓存优先、
  三平台按需补充60～100条且最多120条、MySQL→Outbox→OpenSearch、语义哈希复用向量、全链路
  AG-UI进度”的目标方案。当前仍是设计文档，未实施Provider主链路、Mapping或前端状态变更，
  未调用真实付费服务。

- 2026-07-22：重写面向 GitHub 使用者的 README。快速开始改为可在无付费密钥条件下使用本地 MySQL 8 与 mock 模型完成注册、登录、任务和事件链路；将 MySQL 的 3307 端口、独立 FastAPI 启动、Vite 代理 500 诊断、验证与清理步骤集中说明。完整商品检索改为明确依赖获准离线数据包、OpenSearch/Redis 和 BGE-M3 建库的可选路径，不再暗示普通 clone 包含商品数据或可直接复现当前索引。当前 MySQL ping 与临时 FastAPI `/healthz` 已验证；全新 checkout 演练仍待后续环境验证。

- 2026-07-21：完成 MySQL 8 权威持久化首版：引入异步 SQLAlchemy 2、asyncmy 与 Alembic，建立用户、
  登录会话、thread/run/message/result/artifact、幂等键、Product/Offer/Snapshot/Observation、默认心愿库、
  价格刷新、长期记忆版本和 Outbox 共 19 张业务表。所有时间列在 MySQL 使用 UTC `DATETIME(6)`；
  会话存储已切换为 MySQL Repository，正式启动缺少
  `GLOBUY_DATABASE_URL` 时直接失败，SQLite 仅保留显式 legacy 适配器且不迁移历史。新增 Argon2id 注册登录、
  HttpOnly Cookie、CSRF、注册幂等、Redis 登录限流、资源归属校验、默认心愿库/价格历史、记忆 CRUD/BaseStore、
  商品 JSONL 幂等导入、从 MySQL 重建 OpenSearch，以及独立价格、商品 Outbox 和记忆 Outbox Worker；
  价格任务默认北京时间 03:00 执行，使用短租约恢复中断任务并优先复用当日有效采集观测。前端移除匿名用户流程，增加登录页和个人中心，
  商品卡继续在 `rating/sales` 为 null 时隐藏字段并彻底取消运费展示。Ruff、compileall、后端 131 项、
  前端 12 项测试和 Vite 构建均通过；Alembic 已在空 SQLAlchemy 测试库验证建表。当前本机 `.env` 尚未配置
  `GLOBUY_DATABASE_URL`；`mysql8` 容器状态已验证为 running，但没有擅自读取其管理员密码，因此尚未对该容器执行真实建库、迁移、
  JSONL 导入或 OpenSearch 重建；下一步按 `docs/mysql-persistence.md` 配置最小权限账号后执行验收命令。

- 2026-07-21：按当前商品数据不提供运费事实的产品规则，彻底取消结果页的运费展示与“待确认”占位；
  同时移除结果区“离线数据快照”徽标、统一数据说明、运费提示和底部快照复核说明。前端会在渲染时
  清洗历史会话 Markdown，并过滤商品卡片 `flags` 与终态 `unresolved` 中的相关旧字段；新结果在
  ShoppingSummary、终态持久化与 Prompt 三层约束中同样不再生成这些内容。内部费用工具和原始数据
  结构仍保留，不把未知运费推断为 0。相关后端 34 项、前端 12 项测试、Ruff 与 Vite 生产构建通过；
  本机 Chrome 复核 3 张商品卡片正常，运费/数据说明/快照徽标/底部声明检查均为 false。全量后端
  测试另有 12 个既有 API 隔离失败：前序状态污染后创建会话返回 422，而单独运行 API 测试通过。
- 2026-07-21：前端商品卡片继续透传终态 `picks` 中的 `rating`、`sales`，并将其定义为可空出参；
  只有字段实际非 `null` 时才显示对应指标，缺失值不显示“未提供”等占位内容。当前商品快照基本
  没有可核验运费，所有前端商品展示均已取消 `shipping_fee` 与 `total_cost`，离线快照提示也不再
  声称包含运费；后端费用工具仍保留未知运费不按 0 元推断的契约。新增前端组件回归测试和
  ShoppingSummary 出参断言；前端 Vitest 单 worker 10 项、生产构建，以及后端
  `tests/test_completion_loop.py` 16 项均已通过，未调用付费模型或商品 Provider。

- 2026-07-21：修复活动会话输入区上浮与商品卡片统一显示手绘 mock 图。会话主面板从依赖可选顶部节点数量的三行 Grid 改为纵向 Flex，滚动消息区占据剩余高度，输入区稳定贴住面板底部。新增本地商品图片目录解析器，按规范化平台别名与商品 ID 从
  `datasets/justone_headphones/normalized/headphones.jsonl` 为缺失 `image_url` 的结果做确定性补齐；ShoppingSummary、新任务终态、任务状态接口和历史会话读取均接入，因此既修复新结果，也能无损呈现已存储的旧结果。前端继续只消费接口 URL，并为确实不可访问的外部图片保留降级图。Chrome 实测输入区与面板底部仅相差 1 px 边框；用户截图中的 Q45、Space One Pro、BOSE 三个京东 URL 均加载为 350×350 图片。后端全量 126 项、前端 9 项测试及生产构建通过，未调用付费模型或商品 Provider。

- 2026-07-21：确认“打开前端即显示 500”的当前原因是 Vite 在 5173 监听，但 FastAPI
  未在 8000 监听；实测 `http://127.0.0.1:5173/healthz` 由开发代理返回非结构化 500，
  不是 FastAPI `INTERNAL_ERROR` 或 Agent run 失败。README 已新增端口诊断命令，并按 HTTP API、
  前端本地错误、WebSocket 关闭码/控制事件、异步 run 和 Tavily 工具分层记录当前实现的
  全部公开错误语义、可重试性与处理方式。`tests/test_api.py` 16 项全部通过，
  未调用付费模型或 Provider。随后已使用 Conda `globuy` 环境实际启动 FastAPI，
  直连 `8000/healthz`、Vite 代理 `5173/healthz`、Swagger 和代理后的只读会话 API 均返回 200，
  确认当前 500 已消失，且后端未存在代码或配置导致的启动崩溃。
- 2026-07-21：修复购物需求回车提交后的 React 白屏。根因是对话自动滚动的 `useEffect`
  直接返回 `scrollIntoView()` 运行时结果，消息乐观更新后的 effect 清理阶段抛出
  `TypeError: destroy is not a function`，导致 React 根节点卸载；后端任务仍会持久化，所以刷新后
  能重新显示结果。现改为带函数体且无返回值的 `useConversationAutoScroll`，并新增回归测试模拟
  `scrollIntoView` 返回非函数值，覆盖消息更新与卸载两次清理边界。Vitest 3 个测试文件、8 条
  用例全部通过，前端 TypeScript 与 Vite 生产构建通过。

- 2026-07-21：修正封面右侧主标题在窄栏视口中的二次断行。将标题拆成两个禁止内部换行的语义行，分别为
  “找到更适合”和“你的商品。”，并把响应式字号调整为 `clamp(2.375rem, 2.75vw, 4rem)`，保留现有
  Newsreader/Noto Serif SC 字体、常规字重和编辑式字距。使用本机 Google Chrome 在 1414×1266 视口完成
  全页及标题聚焦合并对照，标题稳定显示为两行；前端 Vitest 3 文件 7 项与 TypeScript/Vite 生产构建通过，
  `design-qa.md` 最终结果为 passed。
- 2026-07-21：统一前端字体系统。标题接入本地打包的 Newsreader Variable，并以 Noto Serif SC Variable
  补齐中文标题字形；英文 UI 使用 DM Sans Variable，中文 UI 使用 Noto Sans SC Variable。封面右侧主标题采用
  轻字重、紧凑行高与编辑式字距，并按现有窄栏宽度约束响应式字号；封面左下超大 `Globuy` 保留原 Trebuchet MS
  字体不变。对 `globuy-hero.webp` 外缘取样后，将封面左侧画布统一为 `#FDF8EA`，同时移除插画 multiply
  混合模式以避免矩形色差。前端 Vitest 3 文件 7 项通过，TypeScript/Vite 生产构建通过；使用本机 Google Chrome
  在参考视口完成落地页、工作台和聚焦字体的前后合并对照，字体加载、计算样式、横向溢出与运行时异常检查均通过，
  `design-qa.md` 最终结果为 passed。
- 2026-07-21：按用户两张标注截图精简品牌封面与购物工作台。封面删除右下服务状态和版本信息，
  右侧标题、说明及操作按钮整体上移；工作台删除顶部当前视图、左侧品牌标题与重复新建按钮、
  当前会话标题栏及左下 SQLite 提示，同时保留顶部新建会话入口和移动端可达性。前端 Vitest
  3 文件 7 项及生产构建通过；使用本机 Google Chrome 隔离配置按参考图有效视口完成两组截图、
  合并对照和 DOM/横向溢出检查，`design-qa.md` 最终结果为 passed。离线预览仍显示现有 API 500
  错误态及 favicon 404，但未发现 JavaScript 运行时异常，两者均非本次布局变更引入。
- 2026-07-21：复核当前 FastAPI、Vite、Compose 和 `.env` 加载方式，确认原后端/前端命令有效；
  README 将完整链路的日常启动命令集中为“OpenSearch/Redis 健康等待 → 单 worker 开发后端 →
  Vite 前端 → 后端/OpenSearch/Redis 检查”。基础设施改用本机已验证支持的
  `docker compose up -d --wait --wait-timeout 300`，明确重复启动会复用数据卷和现有索引，避免
  只启动 Uvicorn 后误以为 ItemSearch/CategoryInsight 可用。本次仅修正文档，未改运行代码。
- 2026-07-20：补齐本机 `.env` 的 80 项配置且保持 Git 忽略，写入用户授权的 Tavily 凭据并把本机
  DeepSeek 模型名修正为官方 `deepseek-v4-flash`；主图递归预算从 24 提升到 48。修复同质 fork
  无终止出口、DeepSeek V4 不支持 `json_schema`/思考模式强制 tool choice、Reflect 跨阶段工具调用
  污染历史以及有效终结工具仍触发递归上限的问题；运行异常日志只记录脱敏 thread/run 标识。
  全量 123 项后端测试、Ruff 和 compileall 通过。真实 FastAPI + 只订阅 WebSocket 任务以“不超过
  1000 元的头戴式耳机”为输入成功完成三平台检索、筛选和总结；独立真实 Tavily `basic` 搜索也
  返回 3 条结果。没有把任何密钥写入文档或受版本控制文件。
- 2026-07-20：根据用户提供的移动购物助手布局参考和彩铅绘本风格参考，完成 Product Design
  参考图到代码改造。新增 `/` 品牌封面和 `/assistant/:thread_id` URL 恢复；使用 Image Generation
  生成原创地球购物车主视觉、品牌标记和商品缺图占位，并压缩为 3 张 WebP。工作台沿用正式
  API/reducer/WebSocket Hook，新增当前页归档搜索、真实 Candidate 图文卡片、2～4 件客户端比较、
  Enter/Shift+Enter 输入约定和 Phosphor 图标；不补造匹配度、原价、库存或缺失规格。新增
  `docs/frontend-backend-gap.md`，当前 Vitest 3 文件 7 项与生产构建通过；本轮浏览器视觉 QA 尚因
  内置浏览器不可用且 Playwright 等待授权而未完成。
- 2026-07-20：重写 README 手动测试入口，按首次安装、零付费 Mock 烟雾测试、完整购物链路、
  OpenSearch/Redis 与两类索引初始化、真实模型/Tavily 可选配置、健康检查、无付费测试和保留数据卷
  的停止方式给出可复制 PowerShell 命令；同时明确首版 Uvicorn 只能单 worker，避免误用部署参数。
- 2026-07-20：按 `systematic-design-prompt.json` 和会话归档实施计划完成 React + Vite 正式购物
  工作台。新增版本化 TypeScript 契约、session API client、`workbenchReducer` 和
  `useGlobuyTask`；任务通过 HTTP 202 创建，WebSocket 仅订阅并按 sequence 重放，具备 45 秒陈旧
  检测、指数退避抖动重连、replay-gap 状态恢复和终态持久化同步。页面采用 12 栏 3/6/3 布局，
  平板/手机抽屉、活动与查看会话分离、只读归档、新对话焦点锁定弹窗、安全 GFM Markdown、离线
  快照标识、候选结果、偏好免责声明、运行轨迹与 Artifact 下载全部接入。Vitest 6 项测试、Vite
  构建以及 1440/1024/390 Playwright 浏览器联调通过，三个视口均无 console/page error。
- 2026-07-20：将 WebSearch 从占位工具升级为 Tavily 官方 Search API 异步适配器。新增
  `GLOBUY_WEB_SEARCH_PROVIDER/TAVILY_*` 配置，默认固定 basic 深度、最多 10 条、不请求生成答案、
  不自动重试；返回可核验 URL、截断摘要、相关度、published_date、检索时间、request ID 和 credit。
  认证、限流、额度、超时、网络和响应错误均结构化脱敏，未配置 key 继续返回 not_configured；
  Prompt 明确网页摘要为不可信数据且不得在线写 Category 索引。7 项 MockTransport 测试通过，
  未使用用户粘贴的 key、未发起真实 Tavily 请求；全量测试更新为 120 passed。
- 2026-07-20：按 `docs/session-archive-task-ui-implementation-plan.md` 完成首版后端最终闭环。
  新增 aiosqlite WAL 会话库和版本化 schema，持久化 thread/run/message/result/artifact/idempotency；
  实现唯一活动会话、空会话替换、原子归档、稳定游标、重启 interrupted 修复和归档只读门禁。
  新增 RunRegistry 的 HTTP 202 后台任务、user/thread 锁、同 thread 取消等待、run-aware 幂等取消、
  对象身份 finally 清理和 lifespan 关闭；EventBroker 提供统一 `monitor_event` 信封、严格 sequence、
  2000 条有界缓冲、30 分钟保留、replay gap、独立订阅队列、心跳和多 WebSocket 隔离。正式 WS
  改为只订阅，新增状态/产物列表与安全下载接口；`/chat`/`files` 仅兼容保留。当时全量 113 项测试与
  Ruff/compileall 通过，未调用真实 DeepSeek 或商品 Provider；React 正式工作台和持久化
  Checkpointer 仍未实现。
- 2026-07-20：重新核对飞书《Globuy项目整理》第 14 点 image_126～140、当前 FastAPI/WebSocket/
  React 代码和 `systematic-design-prompt.json`，新增 `docs/frontend-task-api-contract.md`。文档将
  参考骨架收敛为版本化目标契约：`thread_id + run_id`、HTTP 202 后台任务、run-aware 幂等取消、
  per-thread 串行替换、`monitor_event + sequence` 缓冲重放、心跳、多连接安全、终态查询、产物
  manifest/下载、React reducer/Hook、EventStream 和 Markdown XSS 防护。当前代码尚未实现这些
  新端点、active_tasks、取消、重放和正式前端状态机，状态仍按差距记录。
- 2026-07-20：按飞书实现细节第 10～14 点完成 ItemPicker、ShoppingSummary、工具注册、fork 防护、
  主 AgentLoop 和 Prompt 收尾。ItemPicker 使用严格 Schema、显式硬约束和固定无综合分排序；
  ShoppingSummary 在工具内以共享模型执行一次结构化 LLM 调用，60 秒超时、不重试且继承取消/
  回调。主图改为 Think/Act/Observe/Reflect，增加阶段白名单、ToolNode 统一事件与结果压缩、6/4
  循环检测、主/子运行预算、Cache Breakpoint 和 LangGraph v2 流式入口。长期记忆只保留可选
  BaseStore 接口及 learned_preferences 交接，未读写、未建索引且不回退本地 JSON；完整边界见
  `docs/itempicker-agentloop-10-14-implementation.md`。
- 2026-07-20：按已确认计划完成 PriceCompare、国内 ShippingCalc 和 CategoryInsight RAG 实现。
  新增独立 CategoryCard ETL、严格 Schema/audit manifest、版本化 OpenSearch 索引、三条加权
  min-max Pipeline、BGE-M3 Hybrid 召回、可选 HTTP Reranker、Redis 缓存、quick/deep 提炼及
  分层降级；同步 ItemPicker、Planner、Prompt、Dispatch 回流与 monitor。国内运费未知时不再按零
  处理，PriceCompare 只从完整报价中选最优。无付费路径已发布 7 张耳机卡片并验证真实检索与
  Redis；生产 DeepSeek 制卡因外部数据发送需单独知情授权而未发布。全量测试、Ruff、compileall
  均通过；自动测试未调用付费模型。
- 2026-07-20：重新读取飞书《Globuy项目整理》“RAG 知识库的构建实现具体思路”及
  image_91～105，细化 `docs/pricecompare-dispatch-categoryinsight-implementation-plan.md`，并同步
  `docs/vector-infrastructure.md` 的 CategoryInsight 目标检索契约。
  新方案补齐人工维护的品类归一、确定性聚合 + DeepSeek 严格 JSON 制卡、CategoryCard 入库门禁
  与 audit manifest、独立 Category OpenSearch 版本化索引，以及 Category 专用
  `min_max + arithmetic_mean` 三套 Hybrid Pipeline；新增冻结 BGE-Reranker-v2-m3 的
  Top-30→Top-8/15 精排契约、Redis 查询缓存、冷启动、分层降级和 monitor 字段。该加权 Pipeline
  只用于知识卡片召回，不改变 ItemSearch 的无权重 RRF，也不构成商品综合评分。当时仅优化设计
  文档，随后已按本节上方记录完成实现。
- 2026-07-20：根据飞书《Globuy 项目整理》第 7、8、9 节和用户最终选择，新增
  `docs/pricecompare-dispatch-categoryinsight-implementation-plan.md`，固化 PriceCompare、国内版
  ShippingCalc、通用 DispatchTool 与 CategoryInsight RAG 的实施方案。首期 CategoryInsight 仅使用现有
  1000 条三平台耳机快照，运行时由现有 DeepSeek 做严格 JSON 抽取；继续遵守不训练、不微调和不建立
  综合评分机制的边界。相对原文档的技术调整已在计划中逐项记录；本次只保存计划，代码和类别索引尚未实施。
- 2026-07-20：按用户确认调整 ItemSearch 过滤顺序。`platform` 继续放在 `hybrid.filter`，只负责
  单平台召回域；价格、币种、评分、销量和属性迁移到 RRF 后的 OpenSearch 顶层 `post_filter`。
  服务继续先取 60～150 条融合候选，再过滤并截取 `top_k`。真实京东查询验证价格/销量过滤及
  5 条返回结果，完整 62 项测试与 Ruff 通过。
- 2026-07-20：完整实施 ItemSearch 无训练方案。无付费调用地从缓存原始响应修复 1000/1000 条
  标题与属性，生成含 `product_url` 的 Candidate v2；新增 BGE-M3 本地缓存优先编码器、
  OpenSearch 商品 mapping、无权重 RRF Pipeline、Hybrid Query、索引管理和异步 ItemSearch。
  真实索引共 1000 条（淘宝 350、京东 333、抖音 317），三平台隔离和价格过滤实测通过。
  同时完成确定性 ItemPicker、严格同质 `fork()`、独立 `expert()`、动态 `dispatch_tool`、深度
  限制、根 WebSocket monitor 路由和候选压缩；当时全量 61 项测试与 Ruff 通过，未调用付费 LLM。
- 2026-07-19：用户确认 ItemSearch 无训练完整实施方案，并保存为
  `docs/itemsearch-no-training-implementation-plan.md`。目标主链路调整为单一 OpenSearch 商品
  索引上的 `BM25 + 冻结 BGE-M3 + RRF`，保留 Faiss 作为非主链路实验能力；首版使用现有
  1000 条离线候选，包含结构化过滤、商品链接、检索排名、三平台同质 fork 和 monitor 接入。
  当日只固化计划，后续已于 2026-07-20 完成实施与验收。
- 2026-07-19：重新导出并逐张读取飞书《Globuy项目整理》新增的 ItemSearch 章节 image_49～63；
  记录单平台工具签名、Candidate/ItemSearchOutput、语义与个性化双通道、ANN 平台过滤、monitor、
  `total_recall/truncated` 及跨平台同质 fork 参考行为。用户当前设想为淘宝/京东/抖音三路 fork
  和分平台 Faiss，但索引拆分尚未固化；原三塔内部实现仍受“不训练、不微调”边界约束。
- 2026-07-18：完成一次项目工作区清理：删除 pytest/ruff 缓存、构建元数据、11 个可删除的
  pytest 临时目录、飞书导出临时素材（约 7 MB）以及 `output/`、`uploaded/` 下的示例和测试
  运行残留；保留三份真实耳机数据包、采集脚本、源码和文档。4 个历史 pytest 临时目录受宿主
  ACL 锁定无法由当前会话删除，已用 `/.pytest-*/` 纳入 Git 忽略，不影响运行或版本状态。
- 2026-07-18：新增 `audit_structured_dataset.py`，对结构化候选的 1000 个图片 URL 执行只读
  HEAD/Range 审计，并输出逐条结果和数据集覆盖报告。全部 URL 收到 2xx；996 条为标准图片 MIME，
  4 条抖音 URL 的 MIME 未确认。淘宝 350 条仅通过兼容 TLS 模式访问，严格校验的 CDN 证书域名
  不匹配已显式记录；价格带、头戴/入耳/开放式、无线/有线等覆盖统计已写入本地报告。新增 3 项
  离线审计测试，通过；未调用商品搜索 API 或下载图片内容。
- 2026-07-18：新增 `structure_itemsearch_dataset.py`，从独立耳机快照生成严格 ItemSearch
  Candidate 签名的结构化数据；1000 条均通过字段白名单、平台值和重复 ID 校验，`jd` 映射为
  `jingdong`，仅保留 `image_url`。新增 3 项离线映射测试，通过；未调用外部 API。
- 2026-07-18：为后续结构化处理新增独立交付包 `datasets/headphones_1000/`；已校验 JSONL、CSV
  均为 1000 行、1000 个唯一 ID，主数据哈希与原采集目录一致。包内包含标准化数据、已脱敏原始
  响应、报告和状态快照，不含 Token；原采集工作目录保留以支持复现。
- 2026-07-18：完成 Just One API 耳机候选采集：淘宝 350 条、京东 333 条、抖音 317 条，共
  1000 条且重复 ID 为 0。抖音真实成功响应用于修正嵌套字段映射；因间歇性业务码 301，最后
  16 条由淘宝补齐。累计 77 次成功搜索、48 次 301，按未验证单价估算 7.70 元，实际扣费以
  控制台为准；数据与 Token 均保持 Git 忽略。
- 2026-07-17：用户明确收缩项目边界：当前项目不训练或微调检索模型，也不建设依赖人工评分、
  负样本和训练闭环的学习型排序机制；原三塔真实编码服务停止作为实施目标，`ItemSearch` 的
  无训练替代方案待本轮方案评估后由用户确认，Faiss/OpenSearch 等其他固定选型暂不变。
- 2026-07-17：根据用户随后对 Just One API 抖音电商搜索的人工成功复测，修正此前自动测试
  结论：连续业务码 301 仅代表当时瞬时采集失败，不再据此判定接口不可用；抖音改为“接口
  可用、字段待脱敏样本审计”，得物仍待考证。
- 2026-07-17：完成 Just One API 淘宝、京东、抖音电商、得物搜索接口的 6 次低成本真实烟雾
  测试；淘宝返回 10 条、京东返回 48 条且具备 Candidate 基础字段，抖音连续业务码 301、得物
  连续 HTTP 403；未调用详情、未保存 Token 或原始响应，下一步缩小为淘宝/京东各重取 1 页
  并各查 1 条详情，共 4 次成功调用上限。
- 2026-07-17：重新读取飞书最新“基础模块与模型配置”；实现共享模型缓存、四维 ContextVar
  scope、上下文感知 Monitor、安全路径、完整 Prompt 契约、Faiss HNSW/IP 持久化索引和
  OpenSearch 客户端；安装 Faiss/OpenSearch Python 依赖；Compose 用 OpenSearch 3.7.0 替换
  Qdrant，并完成容器健康检查；17 项后端测试通过。
- 2026-07-17：读取飞书《Globuy项目整理》的实现细节正文及 29 张有效图片；把购物闭环、
  `dispatch_tool` 元工具、Think/Act/Observe/Reflect、异步任务与取消、thread/session 上下文、
  Cache Breakpoint、结构化长期记忆和 AG-UI/monitor/重连安全契约补入 `.codex/codex.md`；同步
  记录这些目标与当前阻塞式 API、本地记忆、未接主图压缩和伪流式事件之间的差距。
- 2026-07-17：根据京东云官方停止运营公告复核京东万象状态；确认其已于 2023-03-18 终止运营、2023-02-16 起产品不可购买；从第三方 Provider 候选、成本对照、测试矩阵和代码骨架中移除京东万象，测试顺序调整为万邦优先、Just One API 备用。
- 2026-07-16：新增 `docs/第三方商品API平台对比调研.md`；对比万邦、京东万象、Just One API、聚推客、大淘客、好单库、维易和 TikHub 的平台覆盖、字段、公开价格、最低门槛与用途限制；形成万邦/京东万象同样本测试短名单，尚未改变最终 Provider 决策。
- 2026-07-16：新增 `docs/万邦API调研.md`；记录淘宝、京东、拼多多的 OneBound 最小接口组合、公开计费基线、1000 条数据预算、5 元余额边界和先搜索去重再补详情的推荐采集方案；OneBound 仍为未验证候选 Provider。
- 2026-07-16：明确中国内地服务器不能通过公网 IP HTTP 访问规避 ICP 备案；拼多多审核正式地址仍采用可控域名和 HTTPS，香港/海外免中国内地 ICP 不代表免平台审核。
- 2026-07-16：记录拼多多应用认证所需官网/回调 URL 边界与阿里云分阶段部署方案；完整服务尚未部署，域名、ICP备案和平台审核结果仍待实际验证。
- 2026-07-16：补充拼多多官方 API 边界；确认 globuy 使用多多进宝 `pdd.ddk.goods.search` + `pdd.ddk.goods.detail`，不使用商家新增、编辑、删除商品接口，并记录推荐、PID、推广链接等可选能力。
- 2026-07-15：新增 `docs/平台api调研.md`；确定淘宝/天猫联盟与京东联盟为首批商品源、拼多多为条件接入，并固定 `Product + Offer` 数据边界和耳机小样本建库方案。
- 2026-07-15：迁移正式协作规范到 `.codex/`；新增流式/AG-UI 和命名约束；记录 Faiss/OpenSearch 双栈、BaseStore、三塔 Query 塔、IP/COSINE 及 Hybrid Query 固定选型。
- 2026-07-15：创建状态文档；记录九工具阶段归属、AgentLoop 四属性、同质 fork 契约、当前实现差距和自动更新规则。
