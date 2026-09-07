# globuy 向量与检索基础设施固定选型

> 决策状态：用户于 2026-09-05 明确要求商品搜索只保留 FAISS 链路，长期记忆只保留 PostgreSQL + pgvector。该要求替代此前保留 OpenSearch 基线的决定。
> 2026-09-06：用户批准把长期记忆编码从 BGE-M3 1024d 统一为与商品候选相同的冻结 BGE-small 512d（共用仓库内本地 ONNX INT8 产物）。商品与记忆的向量存储仍严格隔离：请求内 FAISS vs PostgreSQL/pgvector。
> 完成程度与实测结果以 `docs/project-status.md` 为准。

## 1. 当前边界

- 商品搜索唯一向量后端是请求内 FAISS；不再提供 OpenSearch 商品链、灰度切链或静默后备。
- 长期记忆唯一事实与向量后端是 PostgreSQL/pgvector；不使用 OpenSearch、本地 JSON 或 FAISS。
- 商品与记忆向量空间严格隔离；两者共用同一冻结 BGE-small 512d 编码模型，但向量只写入各自空间（请求内 FAISS / PostgreSQL pgvector），不互用、不混写。
- 当前项目不训练或微调 Query/User/Item 编码模型，不建设依赖人工标签、点击反馈、负样本或学习排序的训练闭环。
- 明确商品型号或平台商品 ID 使用确定性身份匹配，不为了“使用向量”牺牲精确命中。

## 2. 商品搜索链路

```text
ShoppingIntent
  -> goal_explore: 最多两轮澄清，未收敛时不搜索
  -> exact_product: PostgreSQL 候选 + 可靠身份字段确定性匹配
  -> category_explore:
       PostgreSQL 新鲜 Product/Offer 候选
       -> 可选真实 Provider 补充
       -> 硬过滤
       -> 同平台去重
       -> 强证据跨平台 ProductGroup
       -> BM25 + BGE-small 512d + 请求内 FAISS IndexFlatIP
       -> 无权重 RRF，最多 36 组
       -> 一次严格 JSON LLM 精排
       -> 确定性代表 Offer + Top 3
```

### 2.1 FAISS 契约

- 编码器：冻结 `BAAI/bge-small-zh-v1.5`。
- 维度：512。
- 文本：标题与有来源的稳定属性；价格、评分、销量、优惠、店铺和库存不得进入语义文本。
- 归一化：查询与候选都做 L2 归一化。
- 索引：`faiss.IndexFlatIP`，只在单次请求内存在，不落盘。
- 融合：BM25 与 FAISS 两路名次按 `k=60` 无权重 RRF；禁止融合原始 BM25 或余弦分数。
- 上限：融合后最多 36 个商品组进入一次 LLM 精排。
- 设备：CUDA 使用本地 FP16 模型；CPU 使用部署阶段准备的 ONNX INT8 模型。
- 在线链禁止下载、导出或量化模型。仓库当前包含部署所需 ONNX INT8 产物，Docker 镜像会复制该目录。

商品候选来自 PostgreSQL 权威目录。数据库没有新鲜覆盖且真实 Provider 未配置时返回 `not_configured`；不得从模型常识生成商品。

### 2.2 降级

候选编码器未准备、超时、OOM、维度错误或向量非法时不重试，只对同一批真实候选使用：

```text
BM25 -> source_rank -> 稳定输入顺序
```

降级必须标记 `candidate_faiss_degraded`。不得切换 OpenSearch、持久化 FAISS、pgvector 商品表或另一向量空间。

## 3. 长期记忆链路

```text
成功 Run 延迟沉淀
  -> LLM 提取纯文本长期事实与关键词
  -> pgvector + PostgreSQL GIN 关键词召回旧记忆
  -> 无权重 RRF
  -> ADD / UPDATE / DELETE / NONE 严格动作决策
  -> PostgreSQL 原子校验写入
  -> Outbox 异步生成 pgvector 投影
```

固定契约：

- 对外接口：LangGraph `BaseStore`。
- 数据库：PostgreSQL 17 + pgvector 0.8。
- 编码器：与商品候选统一，冻结 `BAAI/bge-small-zh-v1.5`，512 维归一化向量；CPU 推理复用仓库内本地 ONNX INT8 产物（`GLOBUY_CANDIDATE_EMBEDDING_ONNX_PATH`），请求与 Worker 不在线下载或导出模型。
- 距离：pgvector COSINE；索引使用 HNSW/COSINE。
- 关键词：PostgreSQL GIN lane。
- 融合：向量与关键词名次按 `k=60` 无权重 RRF。
- 语义文本：规范化后的纯文本 `memory`，关键词不拼入 Embedding 文本。
- 当前态、180 天内部审计、沉淀状态和 Outbox 全部保存在 PostgreSQL。
- Agent 正常召回在 RRF 后按 `last_confirmed_at` 做 180 天内 1.0 到 0.6 的线性软衰减，只重排不筛除；冲突检索不衰减。
- 删除立即退出当前态和两条召回 lane；向量元数据不匹配时显式标记并只使用关键词 lane。
- 投影 Worker 使用租约、指数退避和 dead-letter；失败 fail-open，不改变本轮商品结果或终态事件。

## 4. 隔离矩阵

| 范围 | 商品搜索 | 长期记忆 |
|---|---|---|
| 权威事实 | PostgreSQL Product/Offer | PostgreSQL memory entries |
| 向量后端 | 请求内 FAISS | PostgreSQL/pgvector |
| 模型 | BGE-small 512d | BGE-small 512d（共用冻结模型与本地 ONNX INT8 产物） |
| 生命周期 | 单次请求 | 持久化当前态 + Outbox |
| 融合 | BM25 + FAISS RRF | pgvector + GIN 关键词 RRF |
| 允许互用 | 否 | 否 |

## 5. 已删除的历史能力

以下能力不再属于仓库运行时：

- OpenSearch 商品索引、RRF Pipeline、蓝绿索引与商品投影 Worker。
- CategoryInsight 的 OpenSearch 知识卡片索引、Reranker 与 Redis 缓存。
- `hybrid/direct_llm/intent_routed/progressive` 多策略配置与灰度切链。
- 三塔哈希编码、持久化 `FaissHNSWIndex` 和相关示例。

Redis 如启用，仅服务登录失败限流，不参与商品检索、短期记忆或长期记忆。

## 6. 变更控制

以下变更需要用户再次明确批准，并同步更新本文和项目状态：

- 恢复 OpenSearch、引入其他商品向量数据库或持久化商品 FAISS 索引。
- 把商品候选写入 pgvector，或让长期记忆复用商品 FAISS 空间。
- 更换任一 Embedding 模型、维度、归一化方式、语义文本版本、距离或 RRF 规则。
- 恢复三塔、学习排序、人工标签或训练闭环。
- 将离线/缓存商品描述为实时库存或平台官方推荐。
