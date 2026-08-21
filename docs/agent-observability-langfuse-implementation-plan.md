# Globuy Agent 全链路可观测体系实施计划

> 计划状态：代码实施完成，待真实 Cloud 凭据烟雾验收
>
> 确认日期：2026-08-20；实施日期：2026-08-21
> 首期选择：LangFuse Cloud 日本区、默认脱敏摘要、生产可用版

## 1. 目标与总体方案

在不替换现有 AG-UI、Monitor、WebSocket 和双层 Eval 的前提下，接入 LangFuse Cloud，形成：

- 一次 `run_id` 对应一棵完整 Trace。
- 主 Agent、Think/Reflect、九个业务工具、ShoppingSummary 模型调用和同质 fork 都能看到父子关系。
- 统计每次模型调用的输入、输出、缓存及总 Token。
- 统计 Run、阶段、工具、模型和 fork 的 RT、状态及错误类型。
- Eval 结果可以反向关联到真实 Trace。
- 默认只上传脱敏摘要，不上传完整 Prompt、商品数组、用户身份或密钥。
- LangFuse 故障时 fail-open，不影响购物任务执行。

采用 LangFuse Python SDK v4 和 LangChain `CallbackHandler`。LangFuse 作为现有运行事件体系的旁路
观测端，不取代 AG-UI 事件、WebSocket 重放、本地评测报告或业务数据库。

预计工作量为 9～12 人日，单人合理工期约 2～3 周。

### 1.1 当前落地状态

已完成 `app/observability/`、LangFuse v4 依赖、RunRegistry 根 `agent` observation、LangGraph/LangChain
request-scoped Callback、ContextVar fork 继承、确定性 Trace ID、HMAC 用户标识、三档采集、OTEL
export-stage 二次脱敏、健康检查、进程关闭 flush、流式 usage 请求、live Eval Trace ID 和显式 Score
发布。`POST /api/v1/tasks`、run status、`RUN_STARTED` 与 `RUN_FINISHED` 都提供同一个 `trace_id`。

本次没有真实 LangFuse 项目 Key，因此没有连接 Cloud、创建远端看板或执行付费 live Eval。Cloud 烟雾
与五分钟定位仍是部署验收项，不能把“代码接入完成”写成“生产数据已经验证”。

## 2. 可观测适配层

新增独立 `app/observability/` 模块，避免 LangFuse 代码散落在业务工具中：

- `ObservabilityProvider`：支持 `none | langfuse`，默认 `none`。
- `RunObservation`：管理根 Trace、LangChain Handler、关闭和异常降级。
- ContextVar 运行上下文：保存当前 Callback 与 `trace_id`，主图和 fork 重建 config 时继续继承。
- `sanitize_observation()`：统一脱敏、裁剪和摘要生成。
- ChatOpenAI `stream_usage` + LangFuse Callback：读取模型返回的标准 usage，不推测缺失 Token。
- 根 observation 终态：记录 `succeeded/cancelled/failed`、RT 和异常类型，不上传异常正文。

新增配置：

```dotenv
GLOBUY_OBSERVABILITY_PROVIDER=none
GLOBUY_LANGFUSE_BASE_URL=https://jp.cloud.langfuse.com
GLOBUY_LANGFUSE_PUBLIC_KEY=
GLOBUY_LANGFUSE_SECRET_KEY=
GLOBUY_LANGFUSE_CAPTURE_MODE=summary
GLOBUY_LANGFUSE_SAMPLE_RATE=1.0
GLOBUY_OBSERVABILITY_HASH_SALT=
GLOBUY_OBSERVABILITY_FLUSH_TIMEOUT_SECONDS=2
```

配置不完整时，应用正常启动；`/healthz` 只返回 provider、是否配置和采集模式，不返回 Key、Salt
或完整凭据。观测状态标为 `not_configured`，业务链路继续运行。

## 3. Trace 与 Span 层级

在 `RunRegistry._execute()` 外围建立根 observation：

```text
globuy.agent_run                         agent
├── memory_recall                       retrieval
├── think#1                             chain
│   └── coordinator_llm                 generation
├── planner                             tool
├── dispatch_tool                       tool
│   ├── taobao-fork                     agent
│   │   ├── fork_coordinator_llm        generation
│   │   └── item_search                 tool
│   ├── jingdong-fork                   agent
│   └── douyin-fork                     agent
├── reflect#N                           chain
│   └── coordinator_llm                 generation
├── item_picker                         tool
└── shopping_summary                    tool
    └── summary_llm                     generation
```

具体约定：

- 将 `run_id` 稳定转换为 32 位 W3C `trace_id`，无需新增数据库映射表。
- `thread_id` 映射为 LangFuse `session_id`。
- `user_id` 使用带 Salt 的 HMAC 后再作为观测用户标识。
- 根调用创建 request-scoped Handler，通过 `RunnableConfig.callbacks` 注入 LangGraph。
- `_config()` 从 ContextVar 读取同一个 Handler；fork 创建新 config 时继续继承，不能产生孤立 Trace。
- fork 写入 `child_thread_id`、`parent_thread_id`、`fork_depth` 和目标平台。
- 继续保留现有 Monitor 工具事件；Monitor 不重复创建 LangFuse tool span。
- 自动检查一个 `tool_call_id` 在 LangFuse 中只能对应一个 tool observation。

## 4. 指标与归因口径

### 4.1 根 Trace

记录 `run_id`、脱敏 session/user、环境、版本、最终状态、总耗时、迭代数、fork 数量，以及是否发生
压缩、循环、取消或降级。评测运行额外写入 Eval ID 和 Case ID。

### 4.2 工具 observation

记录 `tool_name`、`phase`、`fork_depth`、平台、状态、`duration_ms`、结果数量、
`partial/not_configured/degraded_reason`、压缩后结果的估算 Token 和脱敏错误分类。

### 4.3 模型 generation

记录模型、`model_role=coordinator|shopping_summary|extractor|judge`、阶段、输入/输出/总量/缓存/
推理 Token、TTFT、总 RT 和成本。若模型不能由 LangFuse 自动匹配，只能配置经过核验的自定义单价。

### 4.4 Token 归因

- 模型实际 Token 归属于 generation。
- 工具内部嵌套的 generation 汇总到最近的工具祖先。
- Coordinator 决策 Token 单列为 Think/Reflect，不平均摊给多个工具。
- 工具结果进入后续上下文的大小记录为 `tool_result_estimated_tokens`，不得冒充计费 Token。

### 4.5 故障分类

```text
not_configured
timeout
cancelled
authentication
quota
provider_error
validation_error
phase_rejected
loop_detected
persistence_error
index_error
unknown
```

原始异常、响应体和密钥不上传。

## 5. 默认脱敏策略

- 用户输入：保存清理后的最多 200 字摘要、字符数和 SHA-256，不上传未经处理的原文。
- System Prompt：只保存 `prompt_sha256` 和版本，不上传正文。
- 模型输出：保存最多 500 字脱敏摘要、状态和商品数量。
- `items/picks/results/offers`：只保存数量、平台分布和稳定 ID 哈希。
- 工具参数：沿用当前 `_safe_arguments()` 白名单；候选数组只记录数量。
- 删除邮箱、手机号、Cookie、CSRF、Authorization、API Key、Token、数据库 URL、用户记忆原文和
  `reasoning_content`。
- 商品来源 URL 只保存域名及 URL 哈希。
- 所有字符串设置长度上限，防止 Trace 体积失控。
- LangFuse 发送异常只写本地脱敏警告，不进入用户回答和 AG-UI 错误事件。

## 6. Eval 与 LangFuse 联动

扩展 live 评测，offline fixture 保持不变：

- `CaseEvidence` 增加 `trace_ids`，支持多轮 case。
- 报告为每轮真实运行记录可在 LangFuse 搜索的 Trace ID。
- 增加显式参数 `--publish-langfuse-scores`；未指定时只生成本地报告。
- 启用后写入 `eval.overall`、`eval.p0_pass`、`eval.verdict` 和逐项 `eval.<criterion_id>`。
- Score 通过稳定 `trace_id` 关联；上传失败不改变本地 verdict。
- accepted trace 继续保持 `training_use=false`，不建立自动训练闭环。

## 7. 看板与排障流程

### 7.1 Run Health

- Run 数、成功率、取消率和错误率。
- 总 RT 的 P50/P95。
- 按环境、模型、版本和最终状态筛选。

### 7.2 Tool Reliability

- 九工具调用量、P50/P95 RT 和失败率。
- `not_configured/partial/timeout` 分布。
- fork 平台与主/子 Agent 对照。

### 7.3 Token & Cost

- Coordinator、ShoppingSummary 和其他嵌套模型的 Token/成本。
- 按 Think/Reflect、工具祖先、模型和版本分组。
- 高 Token、长 RT 和循环 Trace 排名。

### 7.4 badcase 排障顺序

1. 从 Eval 失败项打开 Trace。
2. 检查根状态、总耗时和故障分类。
3. 查看 Think/Reflect 是否循环。
4. 查看失败或最慢的工具/fork。
5. 查看 generation 的 Token、TTFT 和模型错误。
6. 对照结构化商品事实与 Eval criterion。
7. 记录根因及修复位置。

本期不开发自有观测前端，也不把 LangFuse 数据复制进 Globuy 业务数据库。

## 8. 测试与验收

### 8.1 自动化测试

已完成的自动化覆盖使用 Fake LangFuse Client，不调用真实模型或 Cloud：

- provider 关闭、配置缺失和 SDK 异常时业务结果完全不变。
- 根 Trace ID 稳定且不同 run 不冲突。
- Callback/Trace Context 在作用域内存在、退出后清理，初始化失败不逃逸到业务链路。
- API 任务响应、状态和事件使用同一个确定性 Trace ID。
- streaming usage 参数开启；Token 仅使用模型响应，不把工具估算值混入 generation usage。
- 邮箱、密钥、Cookie、URL、用户记忆、Prompt 和商品数组不出现在发送载荷。
- `/healthz` 不泄露凭据。
- 现有 AgentLoop/fork、AG-UI/API 和六个 offline Eval 定向测试不回归。

### 8.2 Cloud 烟雾验收

- 一次闲聊或单平台任务：验证基础 Trace、模型 Token 和工具 RT。
- 一次淘宝/京东/抖音比较：验证三个 fork 属于同一个根 Trace。
- 确认相同 `tool_call_id` 不产生重复 observation，ShoppingSummary generation 归属正确。
- 一次取消或超时任务：验证 observation 正确结束且错误分类准确。
- 一次 live Eval：验证本地报告、LangFuse Trace 和 Scores 能互相对应。
- 在 LangFuse 搜索完整用户邮箱、密钥片段和原始商品数组，结果必须为零。

### 8.3 五分钟定位验收

准备模型超时、Provider 部分失败、非法阶段调用、fork 超时、循环检测或 Summary 输出失败五类故障。
实施前先测量当前日志的实际定位时间，不能直接声称现状为 30 分钟。实施后由未参与埋点开发的人员
仅使用 LangFuse 排障：

- 五个 case 根因必须全部判断正确。
- 定位时间中位数不超过 5 分钟。
- 单个 case 不超过 8 分钟。
- 形成一页排障 Runbook 和验收记录。

## 9. 交付顺序

1. 第 1～2 天：配置、Fake Client、脱敏器、稳定 Trace ID 和 fail-open 生命周期。
2. 第 3～5 天：根 Trace、LangChain Handler、模型/工具/阶段/fork 层级与 Token 采集。
3. 第 6～7 天：Eval Scores、Trace 关联、指标字段和三类看板。
4. 第 8～9 天：故障注入、Cloud 烟雾测试和五分钟定位验收。
5. 第 10～12 天缓冲：处理 Token 兼容、异步上下文丢失、重复 Span 或 Cloud 网络问题。

## 10. 实施约束

- 使用 LangFuse Cloud 日本区，交付生产可用版本，默认采集脱敏摘要。
- 开启后首期采样率为 100%，观察一周数据量后再决定是否降采样。
- LangFuse 是非关键旁路，任何故障都不得阻断 Agent。
- 暂不接入外部告警、自托管 LangFuse、Prompt 管理或自动训练。
- 不改变九工具、AgentLoop 阶段、AG-UI 事件或当前 Eval 评分规则。
- 保护工作区现有 PostgreSQL/pgvector 迁移改动，不覆盖或回滚用户已有修改。
- 完成后同步更新 `README.md`、`docs/evaluation-system.md` 和 `docs/project-status.md`。

## 11. 官方参考

- [LangFuse SDK Overview](https://langfuse.com/docs/observability/sdk/overview)
- [LangFuse Instrumentation](https://langfuse.com/docs/observability/sdk/instrumentation)
- [Observation Types](https://langfuse.com/docs/observability/features/observation-types)
- [Token & Cost Tracking](https://langfuse.com/docs/observability/features/token-and-cost-tracking)
- [Scores via SDK](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk)

## 12. 启用与运维 Runbook

### 12.1 Cloud 启用

1. 在 `https://jp.cloud.langfuse.com` 建立项目并创建 ingestion API Key。
2. 将 Public/Secret Key 和随机生成的高熵 Salt 写入部署平台 Secret 或本地 `.env`；不得提交仓库。
3. 设置 `GLOBUY_OBSERVABILITY_PROVIDER=langfuse`，首期保持 `CAPTURE_MODE=summary` 和
   `SAMPLE_RATE=1.0`，重启 API。
4. 检查 `/healthz`：`observability_configured=true`、`observability_enabled=true`、
   `observability_status=ready`。这些字段只代表 SDK 已配置，不验证远端 Key 权限。
5. 发起一条 mock 之外的隔离测试任务，用任务响应的 `trace_id` 在 LangFuse 搜索；确认根节点、
   Think/Act/Observe/Reflect、模型 generation、工具与 fork 层级存在。
6. 搜索测试邮箱、Secret Key 片段、完整 Prompt 与原始商品标题数组，必须零命中；否则立即关闭 provider。

### 12.2 建议看板

在 LangFuse UI 保存三组视图即可，无需改业务数据库：

- 稳定性：按 `globuy.agent_run` 过滤，展示 trace count、ERROR 比例、P50/P95 latency，并按 status、
  environment 分组。
- 工具瓶颈：Observation type=tool，按 name 展示 count、error rate、P50/P95 duration；重点关注
  `dispatch_tool`、`item_search`、`web_search`、`shopping_summary`。
- Token/质量：Observation type=generation，按 model/name 展示 input/output/total tokens 和 cost；叠加
  `globuy.eval.score`、`globuy.eval.p0_pass`，定位高成本低分 Trace。成本只采用 SDK/模型可核验值。

### 12.3 五分钟 badcase 排查顺序

1. 从 API、评测报告或用户反馈拿到 `trace_id`，先确认根 Trace 的 status、总 RT 和错误级别。
2. 沿最慢或首个 ERROR 子节点下钻，判断停在模型、工具还是 fork；不要先通读完整日志。
3. 模型节点检查 Token 突增、首包/总耗时和结束原因；工具节点检查输入结构摘要、RT 与异常类型。
4. 多平台问题按 `dispatch_tool` 子树比较各 fork，区分单平台失败、超时和父 Agent 汇总失败。
5. 对照同 case 的 Eval P0、总分与本地 `events.jsonl`；LangFuse 不替代 PostgreSQL 事实和 AG-UI 事件。

### 12.4 降级与关闭

- 紧急关闭：设置 `GLOBUY_OBSERVABILITY_PROVIDER=none` 并重启；Agent、HTTP/WS 和本地 Eval 不受影响。
- 降采样：只调整 `GLOBUY_LANGFUSE_SAMPLE_RATE`；Trace ID 仍稳定返回，未采样 Trace 在 Cloud 中不存在。
- 隐私收紧：改为 `CAPTURE_MODE=none`，输入输出属性在 export 阶段删除，名称、层级、时长仍保留。
- SDK/网络故障只产生本地 warning；不得转成用户可见 `RUN_ERROR`。应用关闭时最多等待配置的 flush 超时。
