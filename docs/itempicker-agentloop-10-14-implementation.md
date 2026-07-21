# 飞书实现细节第 10～14 点：当前实现契约

> 状态：已实施，验证结果以 `docs/project-status.md` 为准。
> 依据：飞书《Globuy项目整理》的 ItemPicker/ShoppingSummary、工具注册、fork 防护、
> 主 AgentLoop 组装和 Prompt 补充；参考代码只作为目标契约，不代表原样复制。

## 1. 实现边界

- ItemSearch 继续使用既定 OpenSearch BM25 + 冻结 BGE-M3 + 无权重 RRF，不改变检索链路。
- ItemPicker 不计算综合分：先执行有证据的显式硬约束，再按
  `retrieval_rank → rating → price → input order` 选择，最多返回 3 项。
- `dispatch_tool` 是 owner-aware 元工具，不计入九业务工具；fork 深度固定为 1。
- ShoppingSummary 在工具内部使用与主 Agent/fork 相同的模型实例额外调用一次 LLM。该模型只
  生成 `final_text`，不能改写已验证 picks 或生成偏好；超时 60 秒且不自动重试。
- Cache Breakpoint 已进入主图；长期记忆没有集成。本轮只有可选 `BaseStore` 构造参数和
  `learned_preferences` 交接结构，不创建记忆索引、不读取、不写入，也不回退本地 JSON Store。

## 2. 工具与运行契约

- 九业务工具由统一工厂构建，ShoppingSummary 在构建时绑定共享模型；AgentLoop 再追加动态
  DispatchTool。父子 Loop 共享业务工具对象和完整 System Prompt，子 Loop 使用独立 checkpoint。
- Think 允许 Planner、ChatFallback、WebSearch、CategoryInsight、ItemSearch、DispatchTool；
  Reflect 允许 CategoryInsight、PriceCompare、ShippingCalc、ItemPicker、ShoppingSummary、
  ChatFallback；ToolNode 包装器拒绝跨阶段调用。
- ShoppingSummary 只有在 picks 非空、来源链接完整、嵌套 LLM 结构化输出有效时返回
  `status=complete, terminal=true`。未配置、超时、错误或证据不完整均不终结，也不生成占位清单。
- ToolNode 是标准工具事件的唯一发布点。工具结果进入消息前执行结构化压缩：ItemSearch 每路
  最多 10 条，CategoryInsight 移除私有证据/向量，通用上限约 4000 tokens，并保留截断标志。

## 3. AgentLoop 与防护

主图使用显式 `AgentState` 和 `Think → Act → Observe → Reflect` 节点。Observe 识别终结工具、
记录最近工具签名和结果摘要；信息不足时 Reflect 返回 Think，成功 ShoppingSummary 或
ChatFallback 进入 END。

- 主循环默认递归 24、超时 300 秒；子循环递归 12、超时 90 秒。
- 循环检测窗口为最近 6 次，相同“工具名 + 参数 + 结果”出现 4 次才触发；不同平台参数不会互相
  误判。深度、超时和单路错误均返回结构化状态，取消继续向嵌套模型和子协程传播。
- Observe 后执行 Cache Breakpoint：超过 token 阈值时只压缩旧的完整消息组，保留最近 3 个
  tool-call/tool-result 组，使用 LangGraph RemoveMessage 语义替换历史。
- `AgentLoop.astream()` 与 `run_agent_stream()` 暴露 LangGraph v2 事件。ShoppingSummary 内部
  模型通过继承 RunnableConfig 标记 `model_role=shopping_summary`；WebSocket 可增量转发该调用。

## 4. 明确未完成项

- `BaseStore` 当前只是接口位；长期记忆的 OpenSearch 适配、相关 Top-K、运行前注入、写回、撤回
  和版本审计均未实施。
- Checkpointer 仍是进程内 `InMemorySaver`。
- HTTP 立即返回任务、`active_tasks`、任务取消 API、心跳和完整断线重连生命周期不属于本次实施。
- 当前商品仍是离线快照；没有实时库存、实时运费或实时平台推荐。
