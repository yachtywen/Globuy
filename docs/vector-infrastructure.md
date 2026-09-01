# globuy 向量与检索基础设施固定选型

> 决策状态：用户已于 2026-08-29 授权三级意图路由与临时 FAISS Hybrid；OpenSearch Hybrid 完整保留为基线和人工回退策略。
> 本文记录当前有效契约；完成程度与实测结果以 `docs/project-status.md` 为准。

## 1. 当前项目边界

- 当前项目不训练或微调 Query/User/Item 编码模型。
- 不建设依赖人工评分标签、负样本、点击反馈或学习排序的训练闭环。
- `intent_routed` 先区分 `exact_product/category_explore/goal_explore`。只有品类探索且硬过滤、
  同款聚合后超过 36 组时，才执行临时候选 Hybrid；两条线上链都不使用手写综合分。
- 原“三塔 + Faiss ItemSearch + 学习权重”是已取消的历史目标，不再指导当前实现。

## 2. 当前有效架构

在线策略由 `GLOBUY_ITEM_SEARCH_STRATEGY=hybrid|direct_llm|intent_routed|progressive` 控制。
`progressive` 按用户 ID（缺失时 thread ID）的稳定 SHA-256 桶把流量灰度到 `intent_routed`，
不在普通请求中双跑 Provider。

```text
direct_llm:
ShoppingIntent -> 三平台并行 ItemSearch -> PostgreSQL 新鲜 Candidate（每平台 <=15）
  -> 硬过滤 -> 同平台去重 -> 强证据跨平台 ProductGroup -> 最多 36 组
  -> 一次 LLM 严格 JSON 精排 -> 确定性代表 Offer -> Top 3 + alternative_offers

后台：Product/Offer -> Transactional Outbox -> Embedding/OpenSearch 投影

intent_routed:
ShoppingIntent
  -> exact_product -> 三平台精确召回 -> 硬过滤/同款聚合 -> 确定性 Offer（无 Embedding/LLM）
  -> category_explore -> 三平台宽召回 -> 硬过滤/同款聚合
       -> <=36 组：全部进入一次 LLM 精排
       -> >36 组：BM25 + BGE-small 512d + 临时 IndexFlatIP + RRF -> 36 -> 一次 LLM 精排
  -> goal_explore -> 每轮一个澄清问题、最多两轮 -> 单一品类或 insufficient_intent
```

LLM 只能引用输入的 `product_group_id` 和已有证据字段；重复/未知 ID、非法结构、超时或未配置
均不重试，并按 `source_rank/retrieval_rank -> 标准化评分 -> 价格 -> 稳定输入顺序` 降级。
自动同款合并只接受已验证 GTIN 完全一致，或规范化品牌、型号和完整关键变体完全一致；标题/图片/LLM
相似只产生疑似重复提示。

```text
离线商品快照（淘宝 / 京东 / 抖音）
    -> 统一 Candidate 文档
    -> 冻结 BAAI/bge-m3 生成 1024 维归一化向量
    -> 单一 OpenSearch 商品索引

ItemSearch(query, platform, filters)
    -> platform 召回前过滤
    -> BM25(title)
    -> Lucene HNSW / COSINE(content_vector)
    -> OpenSearch Search Pipeline 无权重 RRF
    -> price / currency / rating / sales / attribute post_filter
    -> 从扩大后的融合候选池截取 top_k
    -> Candidate[] + retrieval_rank
```

商品索引使用别名 `globuy-products` 指向物理索引。只有文档总数、各平台计数和模型元数据校验
通过后才发布别名。淘宝、京东、抖音共用一个索引，以 `platform` 作为强制过滤条件；不维护三份
Faiss 文件。

CategoryInsight 使用第二条独立链路：

```text
耳机 Candidate 快照 + 人工品类别名
    -> 确定性统计与 CategoryCard 严格门禁
    -> 冻结 BGE-M3 生成 category-card-v1 向量
    -> 版本化 globuy-category-v1-* 物理索引
    -> 数量/类型/抽样校验后切换 globuy-category 别名

CategoryInsight(query, depth)
    -> category_key + card_type 前置过滤
    -> KNN(content_vector) + BM25(category^2, summary)
    -> query-sensitive min_max / arithmetic_mean Pipeline
    -> 可选本机 BGE-Reranker-v2-m3 Top-30 -> Top-8/15
    -> Kimi K2.6 严格 JSON 提炼
    -> 仅缓存 status=ok 的结构化结果
```

三条 Category Pipeline 的 KNN/BM25 权重分别为 exact 0.5/0.5、balanced 0.7/0.3、
semantic 0.9/0.1；纯语义 query 可绕过融合直接走 KNN。这些权重只适用于知识卡片召回，不能
移植到商品 RRF，也不是商品评分。

## 3. 决策矩阵

| 范围 | 当前固定选择 | 不作为当前默认选择 | 原因 |
|---|---|---|---|
| ItemSearch 在线策略 | `intent_routed` 灰度；OpenSearch Hybrid 保留基线 | 静默自动切链 | 按意图避开不必要的向量化，同时保留冻结查询集对照 |
| Dense Embedding | 临时候选 `bge-small-zh-v1.5` 512 维；OpenSearch `bge-m3` 1024 维，严格隔离 | 自训练三塔、在线付费 Embedding | 小模型只筛短生命周期候选，长期投影契约不变 |
| 全文检索 | `title` 的 BM25，内置 `cjk` analyzer | 仅向量检索 | 型号、品牌和关键词精确命中更可靠 |
| 向量检索 | Lucene HNSW + `cosinesimil` | L2 | 与归一化文本向量匹配 |
| 融合 | `score-ranker-processor` 的无权重 RRF | 手工分数、`min_max + 0.7/0.3` | 不引入未经标注验证的业务权重，避免跨路原始分数不可比 |
| Faiss | 请求内 `IndexFlatIP` 精确余弦；仅在分组数 >36 时启用 | 持久化候选索引、OpenSearch 故障后备 | 数十到百级候选无需近似索引，生命周期与请求一致 |
| 长期记忆 | LangGraph BaseStore + PostgreSQL/pgvector + 关键词 RRF | OpenSearch 商品索引或本地 JSON 作为最终实现 | 与 ItemSearch 分离，采用用户确认、版本审计和软衰减生命周期 |
| CategoryInsight RAG | 独立 OpenSearch 索引；BGE-M3 + BM25 的 Category 专用 min-max Pipeline；按需冻结 Cross-Encoder 精排 | 复用商品索引或 ItemSearch RRF Pipeline | 知识卡片与商品候选的 Schema、分数和生命周期不同 |

临时 FAISS 不持久化、不使用 HNSW/IVF/PQ，也不得在 OpenSearch 不可用时静默接管旧 `hybrid`
链。既有 `FaissHNSWIndex` 仍仅是实验能力，禁止与 `TransientFaissFlatIndex` 混用。

## 4. ItemSearch 索引契约

### 4.1 文档字段

- 标识与来源：`item_id`、`platform`、`product_url`。
- 展示字段：`title`、`price`、`currency`、`rating`、`sales`、`image_url`、`attributes`。
- 过滤字段：上述标量字段与扁平化的 `attribute_terms`。
- 语义字段：`content_vector`。

Embedding 文本只包含标题和稳定属性白名单，例如品牌、型号、类目、佩戴方式、连接方式、降噪
能力和使用场景。价格、销量、评分、店铺名和促销信息不得写入向量文本；它们只用于返回、过滤
或后续确定性选择。

索引 `_meta` 必须记录：

- `embedding_model`
- `embedding_revision`
- `embedding_dimensions`
- `embedding_normalized`
- `semantic_text_version`

查询时这些元数据与当前编码器不一致就返回 `not_configured`，不得混用向量空间。

### 4.2 Hybrid Query 与过滤

每次 ItemSearch 必须只接受一个平台。过滤分为两个明确阶段：

- `platform` 必填，放在 `hybrid.filter`，在评分前限定 BM25 与 KNN 共享的单平台召回域。
- `min_price` / `max_price`、`currency`、`min_rating` / `min_sales`、`attribute_equals` 放在请求
  顶层 `post_filter`，只在 BM25 与 KNN 完成 RRF 融合后过滤结果，不改变召回路由。

候选池按 `max(60, top_k * 3)` 计算，上限 150；工具对外 `top_k` 默认 20、最大 50。Pipeline
只用 RRF 名次融合，不配置业务权重。OpenSearch 先对扩大候选池执行 RRF 和 `post_filter`，服务
再截取 `top_k`；过滤后不足 `top_k` 时如实返回较少结果，不用不相关商品补齐。返回顺序写入
一基 `retrieval_rank`，不向上层暴露或依赖 OpenSearch 原始 `_score`。

### 4.3 模型加载与运行设备

- 模型按进程懒加载并缓存，多个同质 fork 共用同一实例。
- 优先使用本地 Hugging Face 缓存；缓存不存在时才联网下载，因此首次建库后查询不依赖外网。
- `device=auto` 时有可用 CUDA 就使用 CUDA，否则回退 CPU；CUDA 使用半精度，CPU 使用全精度。
- 当前索引构建命令为 `python -m app.search.build_index`。

### 4.4 临时候选 Hybrid 契约

- 仅当 `category_explore` 在硬过滤和 ProductGroup 聚合后超过 36 组时启用；查询向量一次、未命中缓存的组向量一次批量编码。
- BM25 固定 `k1=1.2/b=0.75`，中文单字+双字、英文/数字/型号完整 token；BGE-small 文本只含标题和有证据的稳定属性。
- 候选编码器固定 `BAAI/bge-small-zh-v1.5`、512 维、最大长度 128；缓存键包含模型、解析 revision、语义文本版本和内容哈希。
- FAISS 固定归一化 `IndexFlatIP`，两路名次以 `k=60` 的无权重 RRF 融合，再按平台桶轮转到 36 组。禁止融合原始 BM25/余弦分数。
- CUDA 使用进程级本地 FP16 模型；CPU 只加载部署阶段准备的 ONNX INT8。准备命令是 `python scripts/prepare_candidate_embedding.py`，在线链禁止下载、导出或量化。

## 5. 同质 fork 与结果边界

- 单平台请求由主 Agent 直接调用 ItemSearch。
- 明确的多平台比较由主 Agent 产生多个 `dispatch_tool` 调用；LangGraph 的并行工具调用能力负责
  并发执行。
- 每个子 Agent 与父 Agent 共享同一模型、完整业务工具集和完全相同的 System Prompt，但拥有
  独立 thread/checkpointer。
- fork 深度首版限制为 1；子 Agent 不得继续 dispatch。
- `direct_llm/intent_routed` 回流主线程时每路最多 15 条；三级路由先完整分组，再按需筛到 36 个商品组。
- 三个子 Agent 只调用 ItemSearch；父 Agent 汇总后只调用一次 ItemPicker。ItemPicker 排商品组，
  Top 3 不会被强证据同款重复占位；组内按价格、来源顺位和稳定输入顺序选择展示 Offer。

## 6. 故障与真实性边界

- `hybrid` 所需 OpenSearch、模型缓存或商品索引未准备好时返回 `not_configured`；`direct_llm/intent_routed`
  在 Provider 未配置且 PostgreSQL 没有新鲜范围缓存时同样返回 `not_configured`，不得静默切 Hybrid。
- 临时候选编码器未准备、超时、OOM、维度错误或向量非法时不重试，使用同一真实候选集的
  `BM25 -> source_rank -> 输入顺序` 筛到 36，并标记 `candidate_hybrid_degraded`。
- 已配置资源上的运行时异常返回 `error` 与简短信息。
- 禁止生成占位商品、伪造实时价格或在失败时静默改用另一向量空间。
- 当前 1000 条商品来自离线快照，ItemSearch 返回的是快照数据，不代表实时库存、实时价格或
  平台官方推荐。
- 测试使用 Fake Encoder/Fake Client/Fake Agent，不调用付费模型或商品 Provider。

## 7. CategoryInsight 实施状态与其他向量应用

- 长期记忆已接入 LangGraph BaseStore + PostgreSQL/pgvector；黑名单全量优先，普通记忆使用向量与关键词无权重 RRF，并乘置信度和时间软衰减。
- CategoryInsight 已建立独立知识索引和 RAG。当前 `globuy-category` 指向包含 7 张耳机卡片的
  确定性验收索引，三条 Category Pipeline 和真实 BGE-M3 Hybrid Query 已通过本机验证。
- 生产 Kimi K2.6 制卡发布尚需配置 Moonshot API Key，并对“向外部兼容端点发送聚合卡片草稿”进行知情授权；本机冻结
  `BAAI/bge-reranker-v2-m3` HTTP 端点也尚未配置。生产抽取缺失会返回 not_configured；需要精排
  且端点不可用时返回 partial，候选不足时旁路精排。任何路径都不得由模型常识静默补全。
- 长期记忆复用冻结 BGE-M3 模型实例，但使用 PostgreSQL 独立事实表、`vector(1024)` 投影、HNSW/COSINE、关键词 GIN 和独立生命周期；不得复用商品索引、ItemSearch RRF Pipeline 或 Category Pipeline。
- 长期记忆 v2 在保留 `key/category/content` 的同时增加 `subject/predicate/value_json/polarity/scope/evidence/fact_slot`。普通偏好按 `user_id + fact_slot` 去重或替代；黑名单与普通偏好冲突时返回硬冲突，不允许静默覆盖。
- v2 召回仍使用无权重 RRF，再乘 `confidence × time_decay`；只增加确定性的作用域过滤与注入顺序，不引入人工相关性权重。active 黑名单全量返回且不计入普通记忆 Prompt 预算；普通记忆默认最多 10 条、估算 1200 Token。
- 投影元数据不兼容时不得混用向量空间：向量 lane 显式标记 `vector_metadata_mismatch` 并降级到关键词 lane。关闭 `GLOBUY_MEMORY_RETRIEVAL_V2_ENABLED` 可恢复旧 `key/content + RRF` 读取路径，新字段与版本数据保留。

## 8. 变更控制

以下变更仍需用户明确批准，并同步更新本文和项目状态：

- 恢复三塔或其他检索模型训练/微调。
- 引入依赖人工标签、点击数据或负样本的学习排序。
- 用其他向量库替代 OpenSearch 长期商品投影基线，或把临时候选 FAISS 改为持久化索引。
- 更换 Embedding 模型、维度、归一化方式、语义文本版本、距离度量或 RRF 融合方式。
- 将离线快照结果描述为实时平台数据。
