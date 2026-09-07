# Globuy 长期记忆 v2：延迟沉淀

## 1. 边界

- Thread：可跨多次使用、持续存在的对话线程。
- Run：一次用户输入触发的一次 Agent 执行。
- Session：同一 Thread 内相邻活动间隔不超过 15 分钟的一段活动窗口，只是调度概念，不建表、不提供 API。

每轮消息先作为 Thread 短期上下文持久化。成功 Run 只增加待沉淀计数；达到 10 个成功 Run、最后活动空闲 15 分钟或 Thread 被归档时，独立 Worker 才批量沉淀。失败和取消 Run 不进入提取窗口，但会把已有窗口的空闲截止时间推迟到本次活动后 15 分钟。

## 2. 沉淀管线

```text
Thread 消息
→ 10 个成功 Run / 空闲 15 分钟 / 归档
→ Worker 领取最多 10 个成功 Run 的 ordinal 快照
→ 携带游标前 4 条只读上下文提取长期事实和关键词
→ 每条事实无衰减混合召回最多 5 条旧记忆，合并上限 20
→ LLM 为每个 fact_index 选择 ADD / UPDATE / DELETE / NONE
→ 服务端校验临时 ID、用户归属、重复目标和动作完整性
→ 单事务写入当前态与审计，ADD/UPDATE 同事务写 Outbox
→ 独立 Outbox Worker 生成 BGE-M3 向量
```

只有新窗口中的用户消息可以成为事实来源；助手消息和前 4 条上下文只用于消歧。寒暄、当次预算/颜色、密钥、联系方式、Prompt 注入和工具指令会被 Prompt 与确定性规则共同过滤。任一动作非法时整批零写入，沉淀失败不改变原 Run 的成功状态。

提取结果为 `fact_index/memory/keywords`。LLM 最多提供 8 个关键词或别名，与确定性分词和静态领域别名合并，经 NFKC、casefold、长度和敏感内容校验后去重，最终最多保存 32 个；关键词不改写记忆原文，也不进入 Embedding 文本。

动作语义：

- `ADD`：新增事实，服务端生成 UUID。
- `UPDATE`：同一主题的新状态，原 ID 原位更新。
- `DELETE`：用户明确撤回且没有替代状态；写 DELETE 审计后物理删除当前态。
- `NONE + id`：语义重复，只刷新 `last_confirmed_at`，不增加版本或审计。
- `NONE + 无 id`：忽略。

## 3. 当前态、审计和幂等

`memory_entries` 保存 `memory_id/user_id/memory/content_hash/keywords/source/source_thread_id/source_run_id/version/created_at/updated_at/last_confirmed_at`。不再有 status、deleted_at、slot、category、key、结构化事实、confidence、reinforcement 或 supersedes。

规范化文本的 SHA-256 和用户级唯一约束是最终幂等屏障：它覆盖向量尚未生成、HNSW 近似漏召回和并发 ADD。精确重复 ADD 会转成确认并刷新 `last_confirmed_at`。语义近似仍交给 LLM，不由 Hash 合并。

`memory_history` 是内部审计，不提供恢复和撤销。运行时只新增 ADD/UPDATE/DELETE；旧 UNDO/LEGACY_IMPORT 记录保留至清理期限。历史每日清理超过 180 天的记录。

DELETE 与审计在同一事务提交，当前态物理删除；向量通过外键级联删除。事务失败时二者一起回滚，提交后没有业务恢复入口。

## 4. 召回与软衰减

正常 Agent 召回使用 BGE-M3/pgvector COSINE 与关键词 lane，按无权重 RRF 合并，再应用纯时间重排：

```text
age = min((now - last_confirmed_at).days, 180)
factor = 1 - 0.4 × age / 180
final_score = rrf_score × factor
```

factor 从 1.0 线性降到 0.6 后保持下限，只重排、不筛除。同分依次使用 `last_confirmed_at/updated_at/memory_id` 稳定排序。普通召回和 Prompt 注入不刷新时间；只有 ADD、UPDATE、语义重复 `NONE + id` 或精确 Hash 重复才更新确认时间。

写入冲突检索不应用衰减，以免旧但相同或冲突的事实退出 Top 5。向量元数据不匹配时只使用关键词 lane，绝不混用不同向量空间。

## 5. Worker、重试和可观测性

`memory_consolidation_states` 每 Thread 一行，记录处理游标、待处理成功 Run、due_at、租约、失败次数和 dead-letter。领取使用事务行锁与 `FOR UPDATE SKIP LOCKED`；租约过期后可由其他 Worker 恢复。成功、无事实和全部 NONE 都推进快照游标，新消息不会被旧快照误推进。

沉淀任务在单次执行内不立即重试；失败后分别等待 1、5、30 分钟重投，三次延迟重试仍失败则 dead。运维命令：

```powershell
python -m app.memory.consolidation_worker --requeue-thread <thread_id>
```

Embedding 使用 Transactional Outbox。失败只回滚本次投影，不回滚当前记忆；最多执行 8 次延迟重试（5、10、20、40、80、160、320、600 秒），之后进入 dead-letter。成功使用幂等 upsert。运维命令：

```powershell
python -m app.memory.outbox_worker --requeue-event <event_id>
```

不引入 Kafka/RabbitMQ：两类任务都已与 PostgreSQL 事务事实绑定，数据库表、租约和 Worker 足以满足当前规模，同时避免额外基础设施和双写一致性问题。

## 6. 对外产品边界

所有 `/api/v1/memories` 路由均已删除；用户不能查看、增加、修改、删除或撤销当前记忆。TaskResult 不返回 `memory_status` 或 `memory_changes`，用户事件流也不发布 `memory_processing_*` 或 `memory_changed`。账户页与商品结果页没有记忆管理、通知或撤销 UI。用户仍可在自然语言中明确撤回事实，由后台管线产生 DELETE。

## 7. 迁移与运行

Alembic head 为 `20260901_0007`。升级前必须备份并设置 `GLOBUY_MEMORY_MIGRATION_BACKUP_CONFIRMED=true`。迁移初始化 `last_confirmed_at=updated_at`，清理 deleted 当前态及其向量，移除软删除字段，创建 Thread 沉淀状态和 Outbox dead-letter 字段，并打印当前记忆、历史、向量迁移前后的数量与 SHA-256 manifest。迁移是单向的，回滚只能恢复升级前备份。

运行两个独立 Worker：

```powershell
python -m app.memory.consolidation_worker --serve
python -m app.memory.outbox_worker --serve
```
