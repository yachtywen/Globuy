# globuy 向量与检索基础设施固定选型

> 决策状态：用户已于 2026-07-19 确认无训练方案、2026-07-20 授权实施，并于 2026-08-20 授权长期记忆改用 PostgreSQL/pgvector；ItemSearch 的 OpenSearch 契约不变。
> 本文记录当前有效契约；完成程度与实测结果以 `docs/project-status.md` 为准。

## 1. 当前项目边界

- 当前项目不训练或微调 Query/User/Item 编码模型。
- 不建设依赖人工评分标签、负样本、点击反馈或学习排序的训练闭环。
- ItemSearch 使用现成冻结模型做推理，检索质量依靠全文召回、通用语义召回、硬过滤和确定性
  规则，不把手写分数伪装成训练效果。
- 原“三塔 + Faiss ItemSearch + 学习权重”是已取消的历史目标，不再指导当前实现。

## 2. 当前有效架构

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
    -> DeepSeek 严格 JSON 提炼
    -> 仅缓存 status=ok 的结构化结果
```

三条 Category Pipeline 的 KNN/BM25 权重分别为 exact 0.5/0.5、balanced 0.7/0.3、
semantic 0.9/0.1；纯语义 query 可绕过融合直接走 KNN。这些权重只适用于知识卡片召回，不能
移植到商品 RRF，也不是商品评分。

## 3. 决策矩阵

| 范围 | 当前固定选择 | 不作为当前默认选择 | 原因 |
|---|---|---|---|
| ItemSearch 存储与检索 | OpenSearch Hybrid Query | Faiss、Milvus、Qdrant、Redis Stack | 支持 BM25、向量、平台召回域过滤和融合后结构化过滤 |
| Dense Embedding | 冻结 `BAAI/bge-m3`，1024 维，归一化 | 自训练三塔、在线付费 Embedding | 无训练边界下仍保留通用语义召回，且可本地推理 |
| 全文检索 | `title` 的 BM25，内置 `cjk` analyzer | 仅向量检索 | 型号、品牌和关键词精确命中更可靠 |
| 向量检索 | Lucene HNSW + `cosinesimil` | L2 | 与归一化文本向量匹配 |
| 融合 | `score-ranker-processor` 的无权重 RRF | 手工分数、`min_max + 0.7/0.3` | 不引入未经标注验证的业务权重，避免跨路原始分数不可比 |
| Faiss | 保留实验性 ANN 基础设施 | ItemSearch 主链路 | 现阶段过滤、全文和可运维性比单纯 ANN 更重要 |
| 长期记忆 | LangGraph BaseStore + PostgreSQL/pgvector + 关键词 RRF | OpenSearch 商品索引或本地 JSON 作为最终实现 | 与 ItemSearch 分离，采用用户确认、版本审计和软衰减生命周期 |
| CategoryInsight RAG | 独立 OpenSearch 索引；BGE-M3 + BM25 的 Category 专用 min-max Pipeline；按需冻结 Cross-Encoder 精排 | 复用商品索引或 ItemSearch RRF Pipeline | 知识卡片与商品候选的 Schema、分数和生命周期不同 |

Faiss 代码可用于学习、基准测试或未来经过明确论证的纯 ANN 场景，但不得在 OpenSearch 不可用
时静默接管 ItemSearch，也不得把其哈希演示向量用于真实语义结果。

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

## 5. 同质 fork 与结果边界

- 单平台请求由主 Agent 直接调用 ItemSearch。
- 明确的多平台比较由主 Agent 产生多个 `dispatch_tool` 调用；LangGraph 的并行工具调用能力负责
  并发执行。
- 每个子 Agent 与父 Agent 共享同一模型、完整业务工具集和完全相同的 System Prompt，但拥有
  独立 thread/checkpointer。
- fork 深度首版限制为 1；子 Agent 不得继续 dispatch。
- 回流主线程时每路 ItemSearch 候选最多保留 10 条，避免三个平台的完整工具消息挤占上下文。
- ItemPicker 只按 `retrieval_rank` 升序、评分降序、价格升序做确定性选择；缺少 rank 时保持输入
  顺序，不计算任意业务 score。

## 6. 故障与真实性边界

- OpenSearch、模型缓存或商品索引未准备好时，ItemSearch 返回 `not_configured`。
- 已配置资源上的运行时异常返回 `error` 与简短信息。
- 禁止生成占位商品、伪造实时价格或在失败时静默改用另一向量空间。
- 当前 1000 条商品来自离线快照，ItemSearch 返回的是快照数据，不代表实时库存、实时价格或
  平台官方推荐。
- 测试使用 Fake Encoder/Fake Client/Fake Agent，不调用付费模型或商品 Provider。

## 7. CategoryInsight 实施状态与其他向量应用

- 长期记忆已接入 LangGraph BaseStore + PostgreSQL/pgvector；黑名单全量优先，普通记忆使用向量与关键词无权重 RRF，并乘置信度和时间软衰减。
- CategoryInsight 已建立独立知识索引和 RAG。当前 `globuy-category` 指向包含 7 张耳机卡片的
  确定性验收索引，三条 Category Pipeline 和真实 BGE-M3 Hybrid Query 已通过本机验证。
- 生产 DeepSeek 制卡发布尚需对“向外部兼容端点发送聚合卡片草稿”进行知情授权；本机冻结
  `BAAI/bge-reranker-v2-m3` HTTP 端点也尚未配置。生产抽取缺失会返回 not_configured；需要精排
  且端点不可用时返回 partial，候选不足时旁路精排。任何路径都不得由模型常识静默补全。
- 长期记忆复用冻结 BGE-M3 模型实例，但使用 PostgreSQL 独立事实表、`vector(1024)` 投影、HNSW/COSINE、关键词 GIN 和独立生命周期；不得复用商品索引、ItemSearch RRF Pipeline 或 Category Pipeline。

## 8. 变更控制

以下变更仍需用户明确批准，并同步更新本文和项目状态：

- 恢复三塔或其他检索模型训练/微调。
- 引入依赖人工标签、点击数据或负样本的学习排序。
- 用 Faiss、Milvus、Qdrant、Redis Stack、Chroma、pgvector 等替代 ItemSearch 的 OpenSearch
  主链路。
- 更换 Embedding 模型、维度、归一化方式、语义文本版本、距离度量或 RRF 融合方式。
- 将离线快照结果描述为实时平台数据。
