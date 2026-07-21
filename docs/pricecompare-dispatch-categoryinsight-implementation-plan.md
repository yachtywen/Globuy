# PriceCompare、ShippingCalc、DispatchTool、CategoryInsight 实现计划

> 状态：主体实现完成；生产 DeepSeek 制卡与本机 Reranker 端点待外部验收
> 确认日期：2026-07-20
> 依据：飞书《Globuy 项目整理》实现细节第 7、8、9 节，以及当前仓库既有边界与向量基础设施契约。
> RAG 细化依据：飞书“RAG 知识库的构建实现具体思路”及其 Search Pipeline、Rerank、冷启动、
> 缓存和异常兜底补充内容（image_91～105）。

## 一、已确认的实现决策

根据用户确认，本阶段采用以下选择：

- CategoryInsight 首期使用现有 1000 条三平台耳机真实快照建库。
- CategoryInsight 离线制卡和在线提炼均复用现有 DeepSeek 做严格 JSON 抽取；统计事实由确定性
  代码生成，不训练或微调模型。
- CategoryCard 召回使用独立的 OpenSearch Hybrid Pipeline，并按需使用冻结
  BGE-Reranker-v2-m3 精排；这不改变 ItemSearch 的固定 RRF 链路。
- Redis 只承担一小时查询缓存，OpenSearch 已发布索引仍是 CategoryInsight 的唯一在线事实源。
- ShippingCalc 不删除、不停用，改造成国内运费工具。
- PriceCompare 负责多个商品报价比较；ShippingCalc 负责单个报价的费用计算，两者共享同一套底层计算函数。

项目继续遵守以下边界：

- 不训练、不微调任何模型。
- 不建立学习型或人工加权的商品综合评分机制。
- 不虚构运费、属性、销量、类别知识或外部榜单数据。
- Category Hybrid 权重只融合知识卡片召回分数，不用于给商品质量或购买价值打分。
- ItemSearch 现有“平台预过滤 → BM25/BGE-M3 → RRF → post-filter”流程保持不变。

## 二、PriceCompare 与国内 ShippingCalc

### 2.1 国内 ShippingCalc

接口调整为：

```text
shipping_calc(
    item_price: float,
    quantity: int = 1,
    shipping_fee: float | null = null,
    currency: "CNY" = "CNY"
)
```

输出：

```text
status: ok | insufficient_data
currency
merchandise_cost
shipping_fee
total_cost
formula
message
```

计算公式固定为：

```text
merchandise_cost = item_price × quantity
total_cost = merchandise_cost + shipping_fee
```

约束：

- 删除 `duty_rate`、`estimated_duty`、`extra_fees`。
- 不计算关税、清关费和跨境税费。
- `shipping_fee=null` 时返回 `insufficient_data`，不能默认按零元计算。
- 只有数据源明确提供 `shipping_fee=0` 才表示包邮。
- 只接受非负价格、正整数数量和 CNY。

### 2.2 PriceCompare

保留工具名和主要调用形式：

```text
price_compare(items, currency="CNY")
```

每个商品报价支持：

```text
item_id
platform
title
price
quantity = 1
shipping_fee = null
currency = "CNY"
product_url = null
retrieval_rank = null
```

处理规则：

- 调用与 ShippingCalc 相同的纯函数计算每个报价。
- 有完整运费的报价进入 `offers` 并按 `total_cost` 升序排列。
- 运费未知的报价进入 `incomplete_offers`，不参与最低总价判断。
- 并列时依次按 `retrieval_rank`、平台、`item_id` 稳定排序。
- 部分报价不可比较时返回 `partial`；全部不可比较时返回 `insufficient_data`。
- 遗留的 `tax`、`duty` 等字段作为非法输入处理，不能静默参与或绕过计算。

ItemSearch 的 `Candidate` 增加可选 `shipping_fee` 字段。现有耳机快照没有可靠运费，统一写为 `null`。

职责边界：

- ShippingCalc：计算一个商品报价。
- PriceCompare：比较多个已经获得商品价和运费的报价。
- 主 Agent 不需要先逐个调用 ShippingCalc 再调用 PriceCompare；PriceCompare 内部复用计算函数，避免额外工具调用。

## 三、DispatchTool

保持公开接口不变：

```text
dispatch_tool(demand: str) -> str
```

复用现有同质子 Agent 实现，补齐以下行为：

- 子 Agent 继承主 Agent 的模型、系统提示、会话目录和业务工具。
- 每个子 Agent 使用唯一 `thread_id` 和独立检查点。
- 子 Agent 最大派生深度固定为 1，不允许再次调用 DispatchTool。
- 三平台检索时，主 Agent 必须在同一个模型响应中发出三个 DispatchTool 调用，由 ToolNode 真正并行执行。
- 单个平台失败只影响该平台结果，不取消其他并行任务。
- `CategoryInsight(depth="deep")` 在上下文较大时也可以通过 DispatchTool 隔离执行。

统一紧凑返回结构：

```text
status
child_thread_id
answer
tool_results
message
```

压缩和监控规则：

- ItemSearch 只回传约定数量的候选商品。
- CategoryInsight 回传完整结构化结论，但不回传原始卡片和证据正文。
- 不回传向量、模型提示或内部推理。
- 监控记录父子线程、demand、实际工具、耗时、状态和结果数量。

## 四、CategoryInsight RAG 知识库构建

本节把飞书文档中的“数据来源 → 三步 ETL → CategoryCard → OpenSearch”落实为可编码、可审计、
可回滚的离线流程。知识库不直接灌入商品详情或长篇文章；最小知识单元始终是结构化
`CategoryCard`。

### 4.1 数据来源与首期边界

飞书方案允许四路原始数据，并给出了不同刷新节奏：

| 原始数据 | 目标卡片 | 参考刷新节奏 | 首期可用性 |
|---|---|---|---|
| 内部销售榜 | `bestseller` | 周 | 无内部成交数据，不接入 |
| 平台公开榜单 | `bestseller` | 周 | 无独立榜单快照，不冒充已有 |
| 商品库属性聚合 | `attribute` | 月 | 使用现有耳机快照 |
| 历史成交价分位 | `price_range` | 月 | 无成交价；仅使用挂牌价并明确标注 |

首期唯一事实源固定为：

```text
datasets/headphones_1000/structured/itemsearch_candidates.jsonl
```

因此首期只覆盖标准品类“耳机”。淘宝、京东、抖音的销量和评分口径不同，只能在平台内部形成
候选证据；价格卡片只能描述“当前离线快照的 CNY 挂牌价分布”。其他品类、真实平台榜单、历史
成交价和实时趋势均属于未配置数据，不得由模型常识补齐。

当前快照更新时整批重建三类卡片。以后接入独立数据源后，再按“爆款周更、属性/月度价格卡片
月更”拆成增量任务；在线 `category_insight` 永远只读，不写知识库。

### 4.2 Step 1：字段标准化与品类归一

新增 `app/category/normalization.py` 和受版本控制的品类别名表。别名表由人维护，是入库
ground truth，不让 LLM 在运行时猜品类。例如：

```text
耳机 / 蓝牙耳机 / 无线耳机 / 头戴式耳机 / 入耳式耳机 -> 耳机
```

首期数据包本身已声明为耳机数据集，规范品类来自数据包 manifest；单条商品标题只用于提取显式
属性，不反向决定品类。标准化阶段生成内部 `NormalizedEvidence`，至少包含：

```text
canonical_category
source_kind
source_snapshot
platform
item_id
title
price_cny
rating
sales
attributes
observed_at
```

标准化规则：

- 平台名沿用 `taobao | jingdong | douyin`，币种必须为 CNY。
- 价格必须是有限的正数；异常促销价不静默删除，而是进入审计报告并保留其对挂牌价分布的影响。
- 缺失销量、评分或属性保持 `null/unknown`，不做均值填充。
- 属性只从标题和原始 `attributes` 的明确证据中映射到白名单维度，首期包括连接方式、佩戴形态、
  降噪和使用场景。
- 每条标准化证据保留 `platform + item_id + source_snapshot`，保证卡片可以追溯到原始快照。

### 4.3 Step 2：确定性聚合与小模型卡片抽取

先用确定性代码计算事实，再让现有 DeepSeek 仅负责把事实压缩成约定格式。模型不得计算分位点、
百分比、排名或置信度，也不得添加输入中不存在的品牌、组件和流行原因。

三类卡片的事实生成规则如下：

- `price_range`：对所有有效 CNY 挂牌价计算 `q33/q67`，形成
  `[min,q33] / (q33,q67] / (q67,max]` 三档。若分位点重合到无法形成三档，则该卡片拒绝入库。
- `bestseller`：每个平台分别取“有销量证据的销量 Top-N”和“有评分证据的评分 Top-N”，合并后
  按 `item_id` 去重；不把缺失值当零，也不跨平台比较绝对销量。`why_popular` 只能引用平台内名次、
  明确评分、挂牌价和显式属性。
- `attribute`：每个白名单维度单独聚合；分母是该品类全部样本，无法识别的商品计入 `unknown`，
  所有分布项之和必须在浮点容差内等于 1。
- `components`：它不是独立卡片类型，只能从套装类 `bestseller` 卡片的明确组成证据中提取。
  首期“耳机”不是套装知识库，预期输出为空列表。

离线抽取使用和在线工具相同的 OpenAI 兼容 DeepSeek 配置，不新建训练任务。输入是确定性聚合
结果，输出必须是单个 `CategoryCard` JSON。提示词固定以下格式约束：

```text
bestseller:  "{category}: {候选1} / {候选2} / ..."
attribute:   "{维度}: {值1} {比例1} / {值2} {比例2} / unknown {比例}"
price_range:"便宜款 {min}-{q33} / 中档 {q33}-{q67} / 高端 {q67}-{max}"
```

`raw_evidence` 只允许 1～3 条，每条不超过 80 个字符。抽取后立即用严格 Pydantic Schema 校验；
非法 JSON、越界数字、额外字段或格式不匹配直接拒绝该卡片，不进入下一步。测试使用 Fake
Extractor，不调用付费模型。

### 4.4 Step 3：入库门禁与审计

`CategoryCard` 公共 Schema 保持飞书契约：

```text
card_id
category
card_type: bestseller | attribute | price_range
summary
raw_evidence: list[str]
last_updated
confidence
```

Pydantic 模型使用 `extra="forbid"`，并执行以下门禁：

- `card_id` 由规范品类、卡片类型、来源分区和快照版本稳定生成；同一构建中不得重复。
- `category` 必须命中人工别名表，`last_updated` 必须读取源快照元数据（首期取
  `state/collection_state.json.updated_at`）且不能晚于构建时间；缺失时拒绝构建，不能用当前时间
  冒充采集时间。
- `raw_evidence` 中引用的商品必须能在源数据中反查，证据为空时拒绝入库。
- `confidence` 由构建器覆盖为“有效证据数 / 该卡片应覆盖的总样本数”，不接受 LLM 自评，也不
  叠加人工业务权重：价格卡使用有效 CNY 价格覆盖率，属性卡使用可识别值覆盖率，爆款卡使用
  具有销量或评分证据的本平台样本覆盖率。
- `confidence < 0.5` 的卡片不发布；缺失的卡片类型在在线查询时通过低置信度/空结果路径暴露，
  不能补一张模型生成卡片。

每次构建输出审计 manifest，至少记录源文件 SHA-256、总行数、规范化成功/失败数、各类型卡片
数量、拒绝原因、模型与 Prompt 版本、Embedding 元数据、开始/结束时间和构建 ID。manifest 不含
密钥和完整模型提示。

### 4.5 代码与离线产物布局

目标模块划分：

```text
app/category/schemas.py          # CategoryCard、CategoryDocument、工具输出 Schema
app/category/normalization.py    # 人工别名表和标准化
app/category/cards.py            # 分位点、平台内候选、属性分布
app/category/extractor.py        # 离线/在线严格 JSON 抽取适配器
app/category/opensearch.py       # mapping、pipeline、Hybrid DSL
app/category/reranker.py         # Cross-Encoder 客户端与降级
app/category/cache.py            # Redis 查询缓存
app/category/service.py          # 召回、精排、提炼、摘要编排
app/category/build_index.py      # 离线构建与别名发布 CLI
```

离线命令目标为：

```text
python -m app.category.build_index
```

生成的 `category_cards.jsonl` 和 audit manifest 放在可配置的构建输出目录。运行时服务只通过
OpenSearch 别名读取已经发布的卡片，不读取半成品 JSONL。

### 4.6 Category 专用 OpenSearch 索引

CategoryInsight 使用独立索引和生命周期，不与 ItemSearch 商品索引、长期记忆索引混合：

- 稳定别名：`globuy-category`。
- 物理索引：`globuy-category-v{schema}-{build_id}`。
- BGE-M3 生成 1024 维归一化向量；复用现有进程级缓存的模型实例，但 Category 适配层必须把
  `semantic_text_version` 单独记录为 `category-card-v1`，不能沿用商品索引的
  `product-title-stable-attrs-v1`。
- 向量文本固定为 `category + card_type + summary`，不嵌入 `raw_evidence`。
- 中文全文字段使用仓库现有 CJK analyzer；向量使用 Lucene HNSW + cosine。
- `_meta` 记录 embedding model/revision/dimensions/normalized、card schema、normalizer 版本、源
  数据哈希、构建 ID 和三条 Pipeline 名称。

索引文档使用内部 `CategoryDocument`，在 `CategoryCard` 之外增加 `category_key`、
`semantic_text` 和 `content_vector`。建议 mapping：

```text
card_id          keyword
category_key     keyword
category         text(cjk)
card_type        keyword
summary          text(cjk)
raw_evidence     text/index=false
last_updated     date
confidence       float
semantic_text    text/index=false
content_vector   knn_vector(1024, lucene, cosinesimil)
```

发布前必须校验：文档计数一致、`card_id` 唯一、耳机三类卡片均至少一张、向量维度与归一化元数据
一致、抽样查询可返回结果、Category Pipeline 可执行。全部通过后才原子切换别名；失败时保留旧
别名并删除或隔离未发布索引。

### 4.7 Category 专用 Hybrid Search Pipeline

飞书方案中的 `min_max + arithmetic_mean + weights` 只用于 CategoryCard 召回，不修改已经固定的
ItemSearch 无权重 RRF，也不是商品综合评分。为避免在请求参数中临时覆盖权重造成版本和顺序
错误，首期注册三个有版本号的 Category Pipeline：

| Pipeline | Query 形态 | 子路权重 `[KNN, BM25]` |
|---|---|---|
| `globuy-category-exact-v1` | 纯品类/型号名词 | `[0.5, 0.5]` |
| `globuy-category-balanced-v1` | 品类 + 属性约束，默认 | `[0.7, 0.3]` |
| `globuy-category-semantic-v1` | 气质、风格、场景等语义描述 | `[0.9, 0.1]` |

Pipeline 使用 `normalization-processor(min_max)` 与
`combination(arithmetic_mean)`。Hybrid Query 中 KNN 必须放在第一条、BM25 放在第二条，确保
数组权重与子路顺序一一对应。完全口语化且 BM25 明显只会产生品类词噪声的请求走纯 KNN，不把
BM25 权重伪装为 0。

Query 形态由受版本控制的确定性规则判断；无法确定时走 `balanced`。规则只选择召回配置，不
生成业务结论。OpenSearch 3.7.0 的 Pipeline DSL 和 `_score` 范围必须用真实容器做兼容性测试，
验证通过前不得发布 Category 别名。

### 4.8 配置契约

在现有 `Settings` 基础上明确增加或重命名以下配置；默认值不包含凭据：

```text
category_dataset_path
category_aliases_path
category_build_output_dir
opensearch_category_alias = "globuy-category"
opensearch_category_index_prefix = "globuy-category-v"
opensearch_category_pipeline_exact
opensearch_category_pipeline_balanced
opensearch_category_pipeline_semantic
category_coarse_k = 30
category_quick_k = 8
category_deep_k = 15
category_min_confidence = 0.5
reranker_endpoint
reranker_timeout_seconds = 3
category_reranker_required = false
category_rerank_bypass_score = null
redis_url
category_cache_ttl_seconds = 3600
```

当前 `opensearch_category_index` 含义不清，实施时迁移为显式 alias/prefix，避免把稳定别名误当成
可覆盖的物理索引。Reranker 服务当前不在 `compose.yaml`，实施时必须新增健康检查或明确使用外部
本机端点；Redis 已有容器，但 API 端仍需新增客户端依赖和超时配置。

## 五、CategoryInsight 在线召回、精排与抽取

### 5.1 工具与输出契约

公开工具接口保持：

```text
category_insight(
    category: str,
    depth: "quick" | "deep" = "quick"
)
```

`category` 接受用户原始品类短语；工具内部先得到 `canonical_category`，同时保留原短语作为
检索 query。输出至少包含：

```text
status: ok | partial | insufficient_data | not_configured | error
category
depth
components
bestsellers
attributes
price_tiers
confidence
needs_external_validation
data_as_of
retrieval_mode
cache_hit
message
```

原始卡片、`raw_evidence`、向量、Pipeline 原始分数、模型提示和内部推理不进入工具输出。

### 5.2 在线流水线

一次调用按以下顺序执行：

1. 规范化品类并检查别名覆盖；不支持的品类直接走冷启动空结果。
2. 使用规范品类、原始 query、`depth`、索引构建 ID、抽取 Prompt 版本组成缓存键并查询 Redis。
3. BGE-M3 编码原始 query。
4. 对 `category_key` 做精确过滤；`quick` 只召回 `bestseller/price_range`，`deep` 召回三种类型。
5. 依据 query 形态选择 Category Pipeline，执行 KNN + BM25 Hybrid Query；BM25 使用
   `category^2 + summary`，粗排固定取 30 张并排除 `content_vector/semantic_text`。
6. 对粗排卡片做按需 Cross-Encoder 精排，`quick` 取 8 张，`deep` 取 15 张。
7. 按 `card_type` 分组，把卡片的 summary、必要 raw_evidence、confidence 和时间戳交给现有
   DeepSeek 做一次严格 JSON 提炼。
8. Pydantic 校验输出，计算最终 confidence、写成功缓存并上报 monitor。

`quick` 返回 components、bestsellers 和 price_tiers，attributes 固定为空；`deep` 在此基础上
返回属性分布。最终 `confidence` 是实际采用卡片置信度的算术平均，结果使用最旧卡片时间作为
`data_as_of`。`confidence < 0.5` 或发生任何降级时，
`needs_external_validation=true`。

### 5.3 Cross-Encoder 精排

Hybrid 粗排的目标是高召回，精排负责排除“词面相关但品类语义错位”的卡片。目标模型采用飞书
方案中的冻结 `BAAI/bge-reranker-v2-m3`，不训练、不微调。实现 `RerankerClient` 抽象，首个后端
连接本机服务：

```text
POST /rerank
input:  {query: str, candidates: list[str]}
output: {scores: list[float]}  # 与 candidates 同序
timeout: 3s
```

精排输入为“原始 query × 每张卡片 summary”，按 Cross-Encoder 分数降序稳定排序；并列保持
Hybrid 粗排顺序。以下两种情况跳过精排：

- 粗排候选数 `<= final_top_k`。
- 离线召回评测已经校准了高置信首位阈值，且粗排第一名超过该阈值；阈值未校准时此快捷路径
  关闭，不能拍脑袋填写。

Reranker 未配置时返回 `not_configured` 还是降级由工具配置决定；首期在线默认允许降级为粗排
Top-K，并把 `status/retrieval_mode` 标为 `partial/hybrid_without_rerank`。超时同样降级，不把
Reranker 故障扩大为整次工具失败。

### 5.4 小模型结构化提炼

在线提炼继续使用现有 DeepSeek OpenAI 兼容配置，不增加第二套对话模型服务：

- 使用 `response_format={"type":"json_object"}`。
- Prompt 包含完整 JSON Schema、quick/deep 差异、输出示例及“只能依据给定卡片”的硬约束。
- 只允许模型做 `summary -> CategoryInsightOutput` 的结构转换；数量、价格档、百分比和置信度必须
  与卡片一致。
- `components` 只能来自明确套装组成；耳机首期为空，不能从“耳机配件”常识推断。
- 空响应或非法 JSON 只允许一次带校验错误的纠错重试；第二次仍失败返回 `error`，不以正则解析
  或内置常识伪装成功。
- 原始卡片可以作为模型输入，但工具对主 Loop 只返回压缩后的结构化结论。

### 5.5 Redis 查询级缓存

Redis 是可选性能层，不是事实源。TTL 默认 3600 秒，缓存键不能只使用 category/depth，完整键为：

```text
cinsight:{canonical_category}:{query_hash}:{depth}:{index_build_id}:{prompt_version}
```

只缓存通过 Pydantic 校验且 `status=ok` 的输出；`partial/not_configured/error/insufficient_data` 不
缓存。别名切换后 `index_build_id` 改变，旧缓存自然失效。Redis 不可用时记录 monitor 事件并继续
无缓存执行，不影响事实正确性。实施时在依赖和配置中明确增加 Redis URL、TTL、连接/读取超时，
不复用长期记忆接口。

### 5.6 冷启动与分层降级

| 场景 | 工具行为 |
|---|---|
| OpenSearch/Category 别名未配置 | `not_configured`，空 insight，confidence=0 |
| OpenSearch 已配置但运行异常 | `error`，空 insight，confidence=0 |
| BGE-M3 编码不可用 | 退化为仅 BM25，`partial` |
| Reranker 超时或不可用 | 跳过精排，返回 Hybrid 粗排 Top-K，`partial` |
| 规范品类无卡片或召回为空 | `insufficient_data`，confidence=0 |
| DeepSeek 抽取两次失败 | `error`，不返回猜测结果 |
| Redis 不可用 | 无缓存继续，状态不因缓存单独降级 |

对新品类，CategoryInsight 不在在线路径写索引。主 Loop 收到空 insight 后可调用 WebSearch；如果
WebSearch 有真实来源，则使用同一严格抽取器生成仅本轮可用的临时卡片，但不入库。WebSearch
未配置时，转 ChatFallback 说明知识库边界或向用户询问。所有降级结果都交给主 Loop 决定下一步，
不得吞掉异常或伪装高置信度成功。

### 5.7 Monitor 与可观测性

工具开始/结束事件除通用 thread/run/duration/status 外，记录以下脱敏字段：

```text
canonical_category, depth, cache_hit, retrieval_profile,
coarse_count, rerank_used, final_count, cards_by_type,
index_build_id, data_as_of, degraded_reason
```

事件不包含 `raw_evidence`、商品长文本、Embedding、完整模型请求或内部推理。离线构建则通过 audit
manifest 记录每个阶段的输入数、输出数和拒绝原因，在线 monitor 与离线 manifest 可以通过
`index_build_id` 对齐。

## 六、与 Planner、ItemPicker 和主 Agent 集成

调用策略：

- 类别需要拆成子品类、组件或套装时：先 CategoryInsight quick，再 ItemSearch。
- 已获得候选但需要价格档位或典型属性时：ItemSearch 后执行 CategoryInsight deep，再调用 ItemPicker。
- CategoryInsight 本身可以作为普通工具由主 Loop 直接调用；fork 不是必选项。
- 当内部链路达到“编码/召回 → 精排 → 小模型提炼”三个阶段，且 8～15 张卡片证据会污染主
  Loop 上下文时，优先通过 DispatchTool 隔离执行；`depth=deep` 通常满足这两个条件。
- 子 Agent 只回传经过 Pydantic 校验的 CategoryInsightOutput，不回传原始卡片。
- 三平台 ItemSearch 必须通过三个同轮 DispatchTool 调用并行执行。
- 单平台 ItemSearch 直接调用，不产生无意义派生。

ItemPicker 只做最小扩展：

- 可选接收 CategoryInsight 结构化上下文。
- 为商品标注价格档位、明确属性匹配和组件覆盖。
- 保留现有 `retrieval_rank → rating → price` 排序。
- 不根据 CategoryInsight 创建综合得分。
- 只有用户明确选择某个属性或价格档位时，才将其转化为确定性筛选或排序条件。

系统提示中明确：

- 禁止把运费缺失解释为包邮。
- ShippingCalc 负责单报价计算，PriceCompare 负责多报价比较。
- CategoryInsight 是有数据边界的 RAG 工具，不是模型常识问答工具。
- CategoryInsight 低置信度、`partial` 或 `insufficient_data` 时应尝试 WebSearch；WebSearch 的
  结果只能生成本轮临时卡片，不能在线写库。WebSearch 不可用则向用户说明限制。
- CategoryInsight 的 `data_as_of/retrieval_mode` 是结果可信度的一部分，不能省略离线快照声明。

## 七、相对原文档的调整

- 原文档认为 ShippingCalc 无需实现；根据用户明确选择，本项目将其改造成国内单报价费用工具。
- 原文档使用 Query Tower HTTP 服务；本项目复用冻结的本地 BGE-M3，并校验与索引元数据一致。
- 原文档使用 IK 和 Faiss engine；本项目改用现有 CJK analyzer 与 Lucene HNSW/COSINE。
- CategoryCard 仍按飞书方案使用 `min_max + arithmetic_mean`，默认 `[KNN 0.7, BM25 0.3]`，
  并把不同 query 形态实现成版本化 Pipeline；ItemSearch 继续使用无权重 RRF，二者不混用。
- 飞书示例中的临时请求级权重覆盖改为三个固定 Pipeline，避免权重顺序和运行版本不可审计。
- 飞书方案的 BGE-Reranker-v2-m3、Top-30 粗排、Top-8/15 精排、Redis 缓存、冷启动和分层降级
  已纳入本计划；Reranker 阈值必须经离线评测校准，不能凭经验硬编码。
- 原文档的数据来源可以包含内部销量、公开榜单和历史交易；首期只使用现有耳机快照。
- 原文档让小模型自评 confidence；本项目改为代码计算有效证据覆盖率，小模型不得改写。
- 原文档允许子 Agent 技术上继续派生；本项目限制派生深度为 1。
- 原文档 CategoryInsight 没有统一状态字段；本项目增加状态、错误信息和外部验证标志。
- 原文档没有规定缺失运费语义；本项目明确未知运费不得按零处理。
- CategoryInsight 对 ItemPicker 只提供证据和确定性标注，不引入新的商品评分机制。

## 八、测试与验收

### 8.1 单元测试

- ShippingCalc：包邮、收费运费、未知运费、数量、负数、非法币种和遗留关税参数。
- PriceCompare：完整比较、部分缺失运费、全部缺失、稳定并列排序和混合币种。
- DispatchTool：三任务并发、单任务失败隔离、线程隔离、最大深度和监控事件。
- 品类归一：别名命中、未知品类、数据包 manifest 优先、模型不能改写 canonical category。
- CategoryCard：`extra=forbid`、稳定 card_id、离线生成可重复性、证据反查、80 字限制、覆盖率
  confidence、低置信拒绝和无虚构字段。
- 卡片生成：挂牌价三等分位、重合分位拒绝、三平台独立爆款证据、属性 unknown 分母和分布和为 1。
- Category Pipeline：KNN/BM25 子路顺序、0.5/0.5、0.7/0.3、0.9/0.1 三套 Pipeline、纯 KNN
  分支、category_key 过滤和 `_source` 向量排除。
- Reranker：Top-30 到 Top-8/15、稳定并列、候选不足旁路、超时降级和经评测阈值旁路。
- CategoryInsight：quick/deep、缓存命中/失效、未知类别、空索引、BM25-only 降级、非法模型输出
  与一次纠错重试。
- 冷启动：临时 WebSearch 卡片不入库，WebSearch 未配置时返回边界说明。
- 安全性：工具输出不包含 raw_evidence、向量、提示词或内部推理。
- ItemPicker：加入类别上下文后不改变基础排序，不产生综合分数。

### 8.2 集成测试

- 使用现有 1000 条耳机数据完成“标准化 → 制卡 → 门禁 → 向量化 → 物理索引 → 别名切换”，
  核对 audit manifest 和 OpenSearch `_meta`。
- 在真实 OpenSearch 3.7.0 容器验证 normalization-processor、三条 Pipeline、Hybrid DSL、权重
  顺序、CJK 查询和别名原子切换；验证失败不得发布。
- 验证“耳机”quick/deep 均返回符合 Schema 的结果。
- 验证其他类别返回 `insufficient_data`。
- 验证本地 Reranker 正常、超时和不可用三条路径；真实模型验收只使用冻结模型，不调用付费 API。
- 验证 Redis 缓存键包含 query hash、depth、index build ID 和 Prompt 版本，别名切换后不会读旧值。
- 模拟三个平台 DispatchTool 并行搜索并汇总。
- 对带运费和缺失运费的候选执行 PriceCompare。
- 自动测试使用 Fake Encoder、Fake OpenSearch、Fake Reranker、Fake Redis 和 Fake Extractor，不调用
  付费接口。

### 8.3 最终检查

- 执行完整 `pytest`。
- 执行 `ruff` 和 `compileall`。
- 回归现有 ItemSearch、AgentLoop 和监控测试。
- 保存类别构建 audit manifest；记录真实索引文档数、各卡片类型数、Pipeline 和模型元数据。
- 对一组名词、属性约束、语义描述和口语 query 做离线 Recall@K/MRR/NDCG 评测，用于验证 Pipeline
  选择和校准 rerank 旁路阈值；没有标注集时只报告可复现的人工小样本，不声称质量提升。
- 修复本次涉及文件中的中文乱码并统一为 UTF-8。
- 实施完成后更新 `docs/project-status.md`，记录类别索引、数据限制、国内 ShippingCalc 和相对原文档的调整。

## 九、当前实施状态

本文档中的国内 ShippingCalc、PriceCompare、CategoryCard 三步 ETL、严格 Schema/门禁、审计
产物、独立 OpenSearch 版本化索引、三套 Category Pipeline、BGE-M3 Hybrid 召回、可选 HTTP
Reranker 客户端、Redis 查询缓存、quick/deep 提炼、冷启动与分层降级已经实现。Planner、Prompt、
ItemPicker、Dispatch 回流和 monitor 也已同步；自动测试使用 Fake 依赖，不调用付费模型。

本机已用确定性无付费路径生成 7 张耳机卡片并发布至
`globuy-category-v1-20260720111400-94c03755`，别名 `globuy-category`、三条 Pipeline、真实
BGE-M3 Hybrid Query 和 Redis 连通性均已验证。默认生产 CLI 仍会使用项目 DeepSeek 配置执行
严格 JSON 压缩，但本次实际调用涉及把聚合卡片草稿发送到外部兼容端点，安全策略要求知情后的
单独授权，因此尚未切换为 DeepSeek 生产制卡版本。本机冻结 `BAAI/bge-reranker-v2-m3` 服务也未
配置；当粗排候选多于目标 Top-K、确实需要精排时，在线检索会按契约降级为
`partial/hybrid_without_rerank`，候选不足时直接旁路精排，不会伪装成完整成功。
