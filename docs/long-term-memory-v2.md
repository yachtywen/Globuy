# 长期记忆 v2 实现契约

## 写入边界

Agent 的 `learned_preferences` 使用 `memory-fact-v2` 结构：`category/key/content` 加 `subject/predicate/value_json/polarity/scope_type/scope_value/evidence_type/confidence`。`session_only`、密钥/Cookie 模式和外部指令注入会在进入候选表前被确定性拒绝；explicit 与 inferred 都必须经用户确认后才能进入正式记忆。

数据表保留旧字段和旧记录，并通过 Alembic `20260823_0004` 增量增加结构化字段。旧记录无法可靠推断结构时保持空值，以 `imported/legacy-v1` 标记；版本快照只保存结构化结论，不保存原始聊天全文。

## 去重、冲突与生命周期

- 普通偏好和黑名单以规范化 `user_id + fact_slot` 定位；同值重复确认只强化原记录。
- 普通偏好同槽不同值会在同一事务内归档旧记录、写版本、建立 `supersedes_memory_id`、创建新记录和 Outbox 事件。
- active 黑名单不能被普通偏好覆盖，确认返回 `409 MEMORY_HARD_RULE_CONFLICT`。
- history 不参与槽位替代，继续按稳定 key/content 候选去重。
- history/preference/blacklist 的半衰期和归档规则仍分别为 30/180/不衰减；归档立即退出召回并删除投影，恢复后由 Outbox 重建。

三个独立开关为 `GLOBUY_MEMORY_STRUCTURED_FACTS_ENABLED`、`GLOBUY_MEMORY_CONFLICT_RESOLUTION_ENABLED` 和 `GLOBUY_MEMORY_RETRIEVAL_V2_ENABLED`。关闭 v2 读取可恢复旧 RRF 切片语义；Schema 与审计数据不做破坏性回滚。

## 召回与安全观测

召回按用户和 active 生命周期隔离，黑名单全量优先；普通记忆由 pgvector COSINE 与关键词 GIN 无权重 RRF 融合，再乘置信度和时间衰减。匹配 category/brand/product scope 的偏好优先于 global，history 最后补充。普通记忆注入默认最多 10 条和估算 1200 Token，黑名单不占该预算。

召回诊断只包含向量/关键词命中数、融合数、最终数、硬规则数、估算 Token 和降级原因，不包含记忆原文、完整 query、向量或内部 Prompt。

## 验证入口

```powershell
python scripts/eval_regression.py --suite offline --domain memory
$env:GLOBUY_TEST_POSTGRES_URL='<隔离 PostgreSQL URL>'
python scripts/eval_regression.py --suite offline --domain memory
python scripts/test_all.py --output output/test-runs/latest
```

真实 DeepSeek/Provider 验收必须通过 live CLI 同时显式启用 `--allow-model-calls --allow-external-tools`；P0 失败不能被 Judge 覆盖。
