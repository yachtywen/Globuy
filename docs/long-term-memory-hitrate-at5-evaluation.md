# 长期记忆 HitRate@5：200 条固定评测集

## 1. 文件与用途

评测数据位于 `docs/long-term-memory-hitrate-at5-200.json`，用于衡量长期记忆混合检索在前 5 条普通记忆中是否至少找回一条与当前查询相关的已确认记忆。

这份数据集是独立的合成检索基准，不替代 `eval/memory-cases.yaml` 的 50 条集成与安全回归用例。当前文件只提供评测输入和标准答案，尚未运行生产 PostgreSQL/pgvector 检索，也不包含预设分数。

## 2. 数据集结构

- 20 个隔离的合成用户画像，每个画像有 12 条结构化记忆。
- 每个画像有 10 条查询，共 200 条评测 case。
- 每个记忆池同时包含 active 偏好、active 黑名单、history、归档旧版本、跨品类干扰项和全局策略。
- 查询覆盖直接关键词、同义改写、隐式偏好、数值约束、作用域、历史记录、跨会话、冲突替代、归档旧值、英文表达和多记忆组合。
- 语言分布固定为 180 条中文查询和 20 条英文查询。

每条 case 的主要字段：

| 字段 | 含义 |
|---|---|
| `case_id` | 全局唯一用例 ID |
| `profile_id` | 对应的隔离用户画像 |
| `query` | 本次检索查询 |
| `expected.relevant_memory_ids` | 可使该查询命中的标准普通记忆 |
| `expected.required_hard_rule_ids` | 必须全量生效的 active 黑名单 |
| `expected.forbidden_memory_ids` | 不应进入 Top 5 的归档旧值或跨作用域干扰项 |
| `tags` / `difficulty` | 分层统计标签和难度 |
| `rationale` | 标注理由 |

## 3. HitRate@5 计算

对每条 case，将对应画像的记忆写入隔离用户空间，然后按数据中的生命周期状态建立投影。检索时只把普通 active 记忆的前 5 条作为 `top5_memory_ids`；active 黑名单走现有全量硬规则通道，单独记录为 `returned_hard_rule_ids`。

单条是否命中：

```text
hit_i = 1，若 top5_memory_ids 与 relevant_memory_ids 至少有一个交集
hit_i = 0，否则
```

总指标：

```text
HitRate@5 = sum(hit_i) / 200
```

例如 200 条中有 186 条命中：

```text
HitRate@5 = 186 / 200 = 0.93 = 93%
```

93% 只能在真实执行得到 186 条命中后声明，不能把数据集条数、集成测试通过数或人工预期当成检索成绩。

## 4. 辅助门禁

仅看 HitRate@5 会掩盖黑名单和旧版本泄漏，因此建议同时报告：

```text
HardRuleCoverage = 所有 required_hard_rule_ids 均返回的 case 数 / 200
ForbiddenLeakage@5 = Top 5 命中任一 forbidden_memory_id 的 case 数 / 200
```

目标方向：`HardRuleCoverage` 越高越好，`ForbiddenLeakage@5` 越低越好。多标准答案 case 还可以补充 `Recall@5`：

```text
Recall@5_i = |top5_memory_ids ∩ relevant_memory_ids| / |relevant_memory_ids|
```

## 5. 预测结果格式

评测程序可为每条 case 输出一行 JSON：

```json
{"case_id":"mem-001","top5_memory_ids":["p01-m01","p01-m07"],"returned_hard_rule_ids":["p01-m04"]}
```

必须保持检索参数固定：Top-K 为 5、同一 Embedding 模型与 revision、同一归一化方式、同一 RRF 配方和同一生命周期参考时间。报告还应记录代码版本、数据库迁移版本、模型指纹、运行时间及失败 case ID。

## 6. 使用边界

- 数据集当前标记为 `source=synthetic_curated` 和 `result_status=dataset_only_not_executed`。
- 在调参前应进行一次独立人工复核，然后将 `frozen_for_tuning` 固定为 `true`；正式测试集不得同时用于选择 RRF、衰减或过滤参数。
- 如果需要调参，应从 200 条之外另建开发集，避免测试集泄漏。
- 任何对外成绩都必须来自保存的逐 case 预测、失败清单和可复现运行配置。
