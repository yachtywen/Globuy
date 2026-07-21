# globuy 前后端能力差异报告

> 核对日期：2026-07-20  
> 核对范围：当前 React 工作台、`app/api/server.py`、正式 HTTP 202 / WebSocket 会话链路。  
> 真实性边界：商品来自离线快照；前端不补造价格、库存、运费、评分或匹配度。

## 能力矩阵

| 前端功能 | 所需接口 | 当前接口 | 当前字段 | 缺失字段 | 临时方案 | 后端建议 |
|---|---|---|---|---|---|---|
| 创建首个/新会话 | 创建并返回会话 | `POST /api/v1/threads` | `thread_id/title/status/created_at/archived_thread_id` | 无核心缺口 | 已真实接入；新建时由后端原子归档旧会话 | 保持当前幂等和唯一活动会话契约 |
| 获取会话列表 | 活动/归档分页 | `GET /api/v1/threads` | 会话摘要、最后运行状态、消息数、稳定游标 | 服务端关键词搜索、时间分组字段 | 当前页在浏览器内搜索；归档仍按服务端游标加载 | 数据量增大后增加可选 `query` 与日期分组字段 |
| 查询历史消息 | 会话详情 | `GET /api/v1/threads/{thread_id}` | 消息、run 摘要、结果、产物、只读状态 | 临时 Agent 事件历史 | 归档页只展示真实持久化 run 摘要，不伪造时间轴 | 如需完整复盘，增加脱敏事件持久化/分页接口 |
| URL 恢复会话 | 根据 ID 读取详情 | 同上 | 合法 `thread_id`、会话状态 | 无核心缺口 | `/assistant/:conversationId` 映射到真实 thread | 可在未来增加服务端认证后继续沿用 |
| 发送购物需求 | 创建后台任务 | `POST /api/v1/tasks` | `query/thread_id/user_id/client_request_id`，返回 `run_id/ws_url/status_url` | 图片输入 | 文本需求真实接入；不提供伪图片上传 | 多模态模型确定后再设计图片 manifest，不复用兼容上传接口 |
| 流式回复 | 可重放增量事件 | `WS /api/v1/ws/{thread_id}` | `TEXT_MESSAGE_START/CONTENT/END`、sequence、heartbeat、replay gap | SSE | 已真实接入 WebSocket；无需 SSE 双栈 | 除非部署环境明确阻断 WS，否则不必重复建设 SSE |
| Agent 阶段 | 用户可见阶段事件 | 同一 WebSocket | `STEP_STARTED/STEP_FINISHED`、phase、iteration | 参考稿中的固定 00～08 业务阶段、阶段说明/耗时 | 只展示真实 Think/Act/Observe/Reflect 阶段与状态 | 若产品需要固定 9 阶段，后端应新增稳定的展示层 stage schema |
| 工具与 fork | 工具和并行分支事件 | 同一 WebSocket | 工具名、脱敏参数/结果摘要、耗时；fork 摘要 | 归档后的完整工具/fork 轨迹 | 活动任务显示真实轨迹；归档不伪造 | 如需审计，持久化脱敏摘要而非原始内部对象 |
| 中止任务 | run-aware 幂等取消 | `POST /api/v1/threads/{thread_id}/runs/{run_id}/cancel` | `status/terminal/thread_id/run_id` | 无核心缺口 | 已真实接入并等待终态事件/状态同步 | 保持取消向 AgentLoop、fork、工具传播 |
| 任务状态恢复 | 查询终态和事件游标 | `GET /api/v1/threads/{thread_id}/runs/{run_id}` | 状态、序列、终态事件、结果、产物、错误 | 无核心缺口 | replay gap 或刷新时真实同步 | 多实例前先迁移共享 registry/broker |
| 商品结果 | 结构化候选 | 任务结果内的 `result.picks` | `item_id/title/image_url/product_url/platform/price/currency/rating/sales/attributes/reasons/flags` | 原价、实时库存、促销、统一 Offer、稳定匹配度 | 用 Adapter 只显示存在字段；缺失明确写“未提供/待核验” | 按既定 `Product + Offer + SourceSnapshot` 目标补齐来源时间和规格对齐 |
| 商品图片 | 候选图片 URL | `result.picks[].image_url` | 真实快照图片 URL，可为空 | 本地代理/图片刷新时间 | 失败时使用项目内 Globuy 手绘占位图，不请求随机占位服务 | 如跨域或防盗链不稳定，增加受控图片代理与来源时间 |
| 商品来源与购买 | 候选来源链接 | `result.picks[].product_url/platform` | 平台、HTTP(S) 来源链接 | 链接可用性/库存时效保证 | 新标签页安全打开并明确“来源” | Provider 接入后增加采集时间和可用性状态 |
| 推荐理由与风险 | 结构化选择依据 | `reasons/flags` + `final_text` | 最多 3 条确定性理由、风险标记、Markdown 总结 | 标准化优势/劣势/适用人群 | 卡片展示真实 reasons/flags；其他内容留在回答中 | 若需要矩阵比较，新增证据化 `pros/cons/audience`，禁止模型常识补齐 |
| 商品匹配度 | 可解释的匹配指标 | 无 | 无 | `score/match_percent` | 不显示假百分比，不从 retrieval rank 换算 | 只有在定义可审计规则或评测方案后再增加 |
| 商品比较 | 2～4 件结构化对比 | 无独立接口；复用当前 `result.picks` | 价格、平台、评分、销量、理由、风险、属性 | 跨会话收藏、规格归一、持久化比较清单 | 当前页面内选择 2～4 件并比较真实字段 | 后端完成 Product/Offer 规格归一后再提供持久化 compare resource |
| 会话重命名/删除 | 写会话元数据 | 无 | 无 | rename/delete API | 首版不渲染无效操作 | 增加明确的重命名和可恢复删除契约；不得绕过归档只读 |
| 清空/导出会话 | clear/export | 无 | 产物下载仅限真实 manifest | 清空、Markdown/PDF 导出任务 | 首版不渲染无效操作；真实产物出现时可下载 | 采用后台 artifact 任务，不在浏览器伪造“服务端报告” |
| 服务状态 | 健康检查 | `GET /healthz` | `status/model_provider` | 依赖项细分状态、版本 | 封面显示连接/离线；不把 health=ok 等同于商品源就绪 | 可增加脱敏 capability health，不返回配置秘密 |

## 当前 Mock 边界

- 正式前端没有用静态商品冒充后端结果；商品卡片和比较只消费真实 `result.picks`。
- 开发者可按 README 将 `GLOBUY_MODEL_PROVIDER=mock`、`GLOBUY_WEB_SEARCH_PROVIDER=none`，以零付费方式验证会话、任务、WebSocket、取消和归档链路。
- Mock 模型的固定回答只用于通信测试，不代表真实购物推理，也不会被前端包装成真实推荐清单。
- 图片缺失时使用本地品牌占位资产；这是界面 fallback，不是商品数据 Mock。

## 结论

当前后端已覆盖正式前端的核心闭环：会话、历史、任务、增量消息、Agent 事件、取消、结果与产物。
主要缺口集中在产品增强能力：图片输入、服务端历史搜索、重命名/删除/导出、归档事件复盘、统一
Product/Offer、规格归一和有证据的匹配度。前端对这些缺口采取“隐藏无效操作、明确未提供、保留
真实字段”的策略，没有修改后端业务语义。

