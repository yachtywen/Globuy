# Globuy 双层评测系统

## 1. 目标与边界

评测系统用于验证当前 AgentLoop、工具、异步任务协议和最终购物回答是否发生回归。它借鉴
`globex-agent` 的 YAML case、P0/P1/P2 Rubric、独立 Judge 和 Markdown 报告，但不复制其同步接口、
静态种子商品或“全部交给 LLM 判断”的事实校验方式。

- `offline` 是默认路径，只读取仓库内合成夹具并运行确定性评分，不访问模型、Provider、数据库、
  OpenSearch 或 Redis。
- `live` 使用正式认证、HTTP 202、WebSocket replay、任务状态与 MySQL 商品事实，必须显式允许模型
  调用；外部工具已配置时还需要第二次明确允许。
- P0 只由代码判断，覆盖终态、工具、预算、结构化商品、MySQL 事实和来源链接；LLM Judge 只能判断
  P1 行为命中与 P2 表达，不能覆盖 P0 失败。
- 评测只生成回归和审计产物，不写训练标签、不修改检索权重，也不建立自动训练闭环。

## 2. 用例与评分契约

`eval/cases.yaml` 使用 `schema_version: "1.0"`。每个 case 独立声明 suite、查询轮次、能力要求、
可选记忆 setup、前置事实和三档 Rubric。criterion ID 在 case 内必须唯一；P0 必须使用
`deterministic`，P1/P2 可选择 `deterministic` 或 `llm`。离线 case 必须携带明确标记为合成测试
数据的 fixture，真实 case 不硬编码固定商品答案。

评分权重为 P0 0.50、P1 0.35、P2 0.15。只有运行完成、P0 全过且总分不低于 0.70 才是 PASS：

- `FAIL`：评测完成但质量门禁未通过。
- `ERROR`：被测系统或 Judge 执行错误。
- `TIMEOUT`：任务超时且已请求取消。
- `PARTIAL`：需要 LLM Judge，但未配置或未执行。
- `SKIP`：预留给后续能力缺失时的显式跳过；首版运行器不会把错误自动改写为 SKIP。

确定性事实校验使用最终结构化 `picks` 和该次运行后读取的 MySQL `Product + Offer`。价格容差为
0.01 CNY；标题、平台、币种和来源链接必须相等。回答中的来源 URL 必须属于已验证商品。动态目录
的具体商品不写入 live case，因此目录变化不会因固定 ID 自身导致误报。

## 3. 运行方式

默认零外部调用：

```powershell
conda activate globuy
python scripts/eval_regression.py --suite offline
```

真实层要求后端已经运行，并使用专用评测账号。账号凭据和 Judge 配置只通过环境变量传入，不写入
用例或报告：

```powershell
$env:GLOBUY_EVAL_EMAIL='<专用评测账号>'
$env:GLOBUY_EVAL_PASSWORD='<密码>'
python scripts/eval_regression.py --suite live --allow-model-calls
```

启用独立 Judge 时还需配置 `GLOBUY_EVAL_JUDGE_MODEL`、`GLOBUY_EVAL_JUDGE_BASE_URL` 和
`GLOBUY_EVAL_JUDGE_API_KEY`，然后增加 `--judge`。Judge 不会隐式复用主模型凭据。服务端若报告
Tavily 或商品 Provider 已配置，运行器默认拒绝启动；只有明确增加 `--allow-external-tools` 才会继续。

可用参数：`--cases` 指定用例文件，`--only` 选择单 case，`--base-url` 指定服务，`--output`
指定本轮产物目录。真实 case 按顺序执行，但每个 case 建立独立 thread，记忆条目使用 `eval_` 前缀并在
case 结束后通过正式 API 删除，所以不依赖另一 case 的副作用。

## 4. 产物、可复现性与安全

每轮写入 `output/eval/<evaluation_id>/`：

- `manifest.json`：Git、Prompt、cases 哈希，主模型/Judge、索引别名、Embedding 契约和安全能力开关。
- `events.jsonl`：从正式 WebSocket replay 得到的脱敏 AG-UI 事件。
- `case-results.json`：逐 criterion 的通过状态、理由、得分、耗时和错误。
- `report.md`：按 offline/live 分开的汇总、失败证据和折叠对话。

P0 全过且得分至少 0.90 的合成/专用评测轨迹会追加到
`output/eval/accepted-traces.jsonl`，并明确写入 `training_use=false`。API Key、Cookie、密码、CSRF、
数据库 URL、Token 和 `reasoning_content` 会递归脱敏。普通用户会话不进入该轨迹文件。

当前限制：首版没有评测数据库、后台 API 或前端看板；冷缓存、故障注入和 Provider 费用场景只应在
隔离环境执行；WebSocket 事件缓冲仍是单进程内存实现，所以 live 评测继续要求单 worker。
