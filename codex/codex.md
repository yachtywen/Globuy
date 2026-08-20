# globuy 项目长期契约

## 1. 项目定位

`globuy` 是用户从零学习并独立实现的全球购物 AgentLoop 项目。参考来源是用户购买项目的
实现方案、课程截图和设计思路，用户没有参考项目源码。本仓库必须独立实现，并保持与参考项目
相近的概念命名、模块划分和学习颗粒度。

- 边实现边学习 LangChain、LangGraph、FastAPI、向量检索和 Agent 工程化。
- 每一步先形成可运行、可测试、可解释的最小闭环。
- 不伪造外部事实，不把参考目标冒充当前实现。
- 用户资料中的参考名 `globex`，在新代码和文档中统一写成 `globuy`。

主要技术栈：Python 3.12、Conda、FastAPI、Uvicorn、WebSocket、LangChain、LangGraph、
React、TypeScript、Vite 和 OpenSearch；Faiss 仅保留为实验性 ANN 能力。对话模型通过 OpenAI
兼容接口接入 DeepSeek。

## 2. 事实来源和严格程度

后续工作依次读取 `.codex/codex.md`、`docs/project-status.md`、
`docs/vector-infrastructure.md`、`docs/ToDo.md` 和 `README.md`。用户最新明确要求优先。
固定技术选型不得因为更熟悉或当前 Compose 已存在其他服务而擅自替换；变更必须先获得用户
明确批准，并同步更新决策文档。

## 3. AgentLoop 目标模型

九个工具不是九个子智能体，而是主 AgentLoop 循环中的行动选项：

| 工具 | 归属 | 目标触发时机 |
|---|---|---|
| `Planner` | 内部 | Think 判断需求复杂、需要拆解时 |
| `ChatFallback` | 内部 | Think 判断非购物意图、闲聊或需要澄清时 |
| `WebSearch` | 外部 | Think 判断需要外部事实补全时 |
| `CategoryInsight` | 外部 | Think 判断需要品类趋势、爆款或 RAG 知识时 |
| `ItemSearch` | 外部 | Think 判断需要跨平台商品检索时 |
| `ItemPicker` | 内部 | Reflect 判断召回商品需要二次筛选时 |
| `PriceCompare` | 外部 | Reflect 判断需要跨平台比价时 |
| `ShippingCalc` | 外部 | Act 估算关税、运费和到手成本时 |
| `ShoppingSummary` | 内部 | Reflect 判断信息足够，可以生成购物清单时 |

目标阶段：`Think` 理解和拆解；`Reflect` 检查、筛选、比较和收敛；`Act` 执行成本估算、
生成最终清单或触发明确外部动作。

## 4. AgentLoop 四属性与 fork

每个主 Agent 或同质 fork 子 Agent 都明确拥有 `thread_id`、`checkpoint`、`tool_set`、
`system_prompt`。同质 fork 的固定语义是：子 Agent 独立 `thread_id/checkpoint`，共享父 Agent
完整 `tool_set/system_prompt`。工具裁剪或专用 Prompt 必须另建异构专家 Agent 机制。

## 5. 向量基础设施固定选型

详细契约见 `docs/vector-infrastructure.md`，后续实现必须严格遵守：

- 当前项目不训练或微调检索模型，不建设人工评分标签、负样本或学习排序闭环。
- ItemSearch：单一 `OpenSearch` 商品索引，BM25 与冻结 `BAAI/bge-m3` Dense Vector 通过无权重
  RRF 融合；`platform` 在召回前限定单平台候选域，价格、评分、销量和属性只在 RRF 完成后通过
  `post_filter` 过滤，再从扩大候选池中截取 `top_k`。
- 商品向量固定为 1024 维归一化向量；OpenSearch 使用 Lucene HNSW + COSINE。
- ItemSearch 的旧“三塔 + Faiss + min_max 0.7/0.3”目标已经取消。Faiss 只保留为实验性
  ANN 基础设施，不得作为 OpenSearch 故障时的静默后备。
- 长期记忆接口：`LangGraph BaseStore`，PostgreSQL/pgvector 为事实与检索后端。
- 长期记忆使用独立事实表、候选确认、版本审计和软衰减生命周期，不能直接复用商品索引；CategoryInsight 继续使用独立 OpenSearch 索引。
- Qdrant、Redis Stack、Chroma、pgvector、Milvus 不得作为 ItemSearch 默认替代；更换模型、
  维度、归一化方式、距离或融合策略必须先获得用户批准。

## 6. 流式与 AG-UI

项目实现必须以 stream 为默认方向，逐步向前端或控制台推送运行开始、文本增量、工具开始、
工具结束、运行完成、取消和错误事件。新增功能不得只设计阻塞式最终返回。当前代码仍是
`ainvoke()` 完成后一次推送，必须持续标记为差距，直到 LangGraph 事件流真正接通前端。

## 7. 通用约束与文档更新

- 外部工具未配置时返回 `not_configured`；测试不调用真实付费服务。
- 默认使用 Conda `globuy` 和 Python 3.12；`.env` 不提交、不打印密钥。
- Prompt 长期配置优先放 `app/prompt/prompts.yml`。
- 短期会话状态、长期记忆、业务数据库必须分开。
- recall/memory/compress/eval 只有进入主图后才能标记“已集成”。
- 向量、ANN、RAG、长期记忆和商品召回工作必须先读向量基础设施契约。
- 每次有实质代码、架构、配置、依赖、测试、设计或参考契约变化，都在同一轮自动更新
  `docs/project-status.md` 并追加变更记录；启动方式变化同步更新 README。

## 8. 对话式购物闭环与任务执行契约

用户补充的飞书《Globuy项目整理》是目标实现细节的参考契约，不是当前代码已经完成的证明。
参考资料中的 `Globex` 在本仓库统一解释为 `globuy`；其中 Python 3.10、`uv` 等示例标签不
覆盖本仓库已经固定的 Conda `globuy`、Python 3.12 和现有配置体系。模型训练与评测闭环暂不
属于当前实现范围，除非用户之后重新开启。

目标用户闭环是：用户提交文字购物意图或商品参考图，系统拆解预算、品类、偏好和硬约束，
按需跨平台并行检索，统一候选商品与报价，比较价格、运费和关税，应用黑名单与偏好做二次
筛选，最后输出带选购理由、风险、来源和跨平台价格对照的购物清单。

项目运行时可以概括为“一个主 AgentLoop、按需创建的多个同质 fork、九个业务工具和基础
设施”。`dispatch_tool(demands)` 是主 Loop 触发同质 fork 的元工具，不计入九个业务工具：

- 主 Loop 使用 `Think -> Act -> Observe -> Reflect` 循环；信息不足时回到 Think，信息充分时
  才生成 `ShoppingSummary`。
- Think 决定当前工作由主 Loop 直接完成还是拆成可并行 demands 后触发 fork；具体 fork
  阈值和判定规则仍需在实现前定义，不从参考图臆造。
- 同质 fork 对主 Loop 透明，继承完整 `tool_set/system_prompt`，拥有独立
  `thread_id/checkpoint`，工具结果回流主 Loop 的 Observe/Reflect。
- `WebSearch`、`ItemSearch`、`ShippingCalc` 等外部工具仍受 `not_configured` 和来源真实性
  约束；并行不能成为伪造外部数据的理由。

## 9. thread、任务和工作目录生命周期

`thread_id` 是一次会话执行链路的路由键，至少贯穿 WebSocket 连接、后台任务表、
`session_dir`、AgentLoop checkpoint、事件和记忆的 `source_session`。`user_id` 是长期偏好的
所有者，不能用 `thread_id` 代替；一个用户可以有多个 thread。

`session_dir` 是当前任务的隔离工作目录。上传文件、工具中间产物、清单和报告只能写入经过
校验的当前目录。API 在任务入口创建 `thread_id/session_dir`，通过 `ContextVar` 绑定；主
Loop、fork、工具和 monitor 从上下文读取，不在多层函数间手工透传，也不得跨会话串写。

长任务的目标 API 生命周期是：

1. HTTP 启动任务后立即返回 `thread_id/run_id`，不等待购物任务完成。
2. 后端用受控的 `asyncio` Task 和 `active_tasks` 管理运行；同一 thread 同时最多有一个活跃
   run。默认语义是新 run 先取消旧 run、等待其清理后再启动；若以后改成拒绝或排队，必须先
   明确变更契约并补并发测试。
3. 前端通过该 thread 的 WebSocket 订阅增量事件；WebSocket 负责事件和心跳，不承担阻塞式
   Agent 调用。
4. 提供取消入口；取消要传递到 AgentLoop/fork/工具，产生取消事件并清理 `active_tasks`，
   不能只在 HTTP 层返回“已取消”。
5. 任务成功、失败、取消和客户端断开都必须走 `finally` 清理，避免悬挂任务、连接和文件。

当前接口路径可按版本化 API 规范设计，不要求机械复制参考图的 `/api/task`；但“立即返回任务
标识 + WebSocket 持续推送 + 可取消”的语义不可丢失。

## 10. Cache Breakpoint 与长期记忆契约

上下文压缩不能只看总 token 后整段重写。目标策略是在历史消息与当前工作区之间设置 Cache
Breakpoint：尽量保持 System Prompt、工具声明和已稳定历史前缀不变以提高 Prompt Cache
命中率，只压缩越过阈值的旧历史，同时保留最近 K 轮/工具调用、当前用户约束以及尚未完成的
tool-call/tool-result 配对。压缩前后必须保持消息协议合法，摘要要标注其来源和边界。

长期记忆存储的是从对话中提炼出的结构化结论，不保存整段原始聊天。逻辑上的
`PreferenceEntry` 至少包含：

- `key`：用户内稳定唯一键；同 key 写入采用可审计的覆盖/版本策略。
- `category`：至少区分 `blacklist`、`preference`、`history`。
- `content`：可直接用于约束或 Prompt 注入的简洁结论。
- `source_session`、`created_at`、`confidence`：来源、时效和置信度。

存储能力应覆盖读取、写入/更新、用户主动撤回删除，以及按当前 query 读取相关 Top-K。
对外仍使用已经固定的 LangGraph BaseStore 契约，PostgreSQL/pgvector 是事实与检索后端；
黑名单和硬规则必须全量生效，普通偏好使用 pgvector COSINE 与关键词召回的无权重 RRF 取相关
Top-K，再乘置信度和时间软衰减。Agent 抽取只生成候选，必须经用户确认后才进入长期记忆。

只有用户明确表达或高置信度抽取得到的偏好才持久化。新偏好必须同时影响当前 Observe/筛选，
并带 `source_session` 写入 Store 供后续会话使用；推断性偏好要降低置信度，不能把一次浏览
自动升级为永久偏好。新会话/运行入口按 `user_id + query` 读取相关记忆，格式化后追加到稳定
System Prompt 的记忆区；只注入结构化结论和必要硬约束，不回放全部历史，且要控制 token
预算、去重并优先使用最新有效记录。

## 11. AG-UI 事件、monitor 与 WebSocket 契约

后端执行过程必须拆成可增量消费的事件流。飞书资料中的七类业务事件语义是：会话/运行创建、
Agent 阶段更新、工具开始、工具结束、任务结果、任务取消和错误。它们应映射到 AG-UI 的标准
运行、文本、工具、完成、取消和错误事件；参考图里的 `session_created`、`assistant_call`、
`tool_start` 等小写名称只是语义样例，不直接替换当前标准事件名。

统一事件信封至少包含 `type`、`thread_id`、`run_id`、`timestamp` 和事件专属 `data`，可增加
面向用户的简短 `message`。工具开始事件包含工具名和脱敏后的必要参数，工具结束事件包含状态、
耗时和结果摘要；任务结果携带最终回答引用或增量完成标记。不得通过事件暴露密钥、完整内部
对象、原始思维链或不必要的个人数据；Agent 阶段事件只发布可展示的状态/计划摘要。

monitor 是业务执行与传输层之间的统一适配器：从 `ContextVar` 读取 thread/run 上下文，将
LangGraph `astream_events`、模型 token、工具回调、取消和异常转换为统一事件，再交给
`ConnectionManager`。工具可以通过轻量 hook 上报开始/结束，但不应知道 WebSocket、序列化
或连接路由细节，也不能与 LangGraph 回调重复发同一事件。

`ConnectionManager` 维护 `thread_id -> 当前有效 WebSocket 集合`，支持同一 thread 的页面
刷新和重连。断开旧连接时只能移除对应的 WebSocket 对象，不能按 thread 盲删而误伤刚建立的
新连接；发送失败要清理陈旧连接，连接表访问要具备并发安全。前端只依赖稳定的事件契约和
`data`，不依赖后端具体如何生成事件。

## 12. 当前实现判定

上述第 8 至 11 节描述目标行为。是否已经实现必须以 `docs/project-status.md` 和真实代码、测试
为准。尤其是后台任务生命周期、同质 fork、显式 Think/Observe/Reflect 状态、Cache
Breakpoint 主图接入、BaseStore/PostgreSQL-pgvector 长期记忆、真正的 token/tool 事件流和取消传播，
在完成代码与无付费调用测试前都必须继续标记为差距。
