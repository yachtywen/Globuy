# ItemSearch 无训练混合检索完整实现方案

> 状态：已实施并通过本地验收  
> 确认日期：2026-07-19  
> 实施日期：2026-07-20  
> 实施边界：不训练、不微调、不建设人工评分或学习排序闭环

## 1. 方案摘要

- 将 ItemSearch 主链路确定为：`OpenSearch BM25 + 冻结 BGE-M3 Dense Vector + RRF`。
- 不训练、不微调、不实现 User/Query/Item 三塔、不采集负样本、不建设人工评分或学习排序机制。
- BGE-M3 只在本机 Conda `globuy` 进程中推理，优先使用 RTX 5060，显存不可用时自动回退
  CPU。模型使用 1024 维 Dense Embedding；官方模型支持中文及多语言并采用 MIT License。
  参考：[BGE-M3 模型卡](https://huggingface.co/BAAI/bge-m3)。
- ItemSearch 使用单一 OpenSearch 商品索引，通过 `platform` 硬过滤区分淘宝、京东、抖音，
  不再按平台维护三个 Faiss 文件。
- OpenSearch 采用内置 `cjk` 分词器、Lucene HNSW/COSINE 向量检索和无权重 RRF。RRF 使用
  默认 `rank_constant=60`，两路检索等权，不配置人为 0.7/0.3 分数。参考：
  [CJK Analyzer](https://docs.opensearch.org/latest/analyzers/language-analyzers/cjk/)、
  [RRF Processor](https://docs.opensearch.org/latest/search-plugins/search-pipelines/score-ranker-processor/)、
  [Hybrid Post-filtering](https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/post-filtering/)。
- 首版只使用现有 1000 条真实离线耳机快照，不调用 OneBound/JustOne 等付费实时 API。
- 保留现有 Faiss 实现作为实验性 ANN 基础设施，但不再接入 ItemSearch 主链路。

## 2. 对外接口与数据契约

### 2.1 ItemSearch

实现异步单平台工具：

```text
item_search(
    query: str,
    platform: "taobao" | "jingdong" | "douyin",
    top_k: int = 20,
    filters: SearchFilters | None = None
) -> ItemSearchOutput
```

约束：

- `query` 去除首尾空白后不能为空。
- `top_k` 范围为 `1..50`。
- 一次调用只搜索一个平台；跨平台由三个同质 fork 分别调用。
- 不保留无实际作用的 `user_id` 参数；长期个性化留到 BaseStore 阶段。
- 所有结构化过滤采用 AND 语义；`platform` 在召回前过滤，其余过滤条件在 RRF 后执行。

`SearchFilters`：

```text
min_price: float | null
max_price: float | null
currency: str | null
min_rating: float | null
min_sales: int | null
attribute_equals: dict[str, scalar] | null
```

- 校验价格非负且 `min_price <= max_price`。
- `attribute_equals` 支持字符串、数字、布尔值的精确匹配。
- 索引阶段把任意属性规范化为 `key=canonical_value` 的 keyword 数组，避免动态属性造成
  mapping 爆炸。

`Candidate` 保留原九字段，并增加两个已确认字段：

```text
item_id
platform
title
price
currency
rating
sales
image_url
attributes
product_url        # 平台商品详情页，不是数据供应商地址
retrieval_rank     # 从 1 开始的 RRF 最终名次
```

不输出采集时间、OneBound/JustOne 等供应商信息或内部向量分数。

`ItemSearchOutput`：

```text
status: "ok" | "not_configured" | "error"
platform
candidates
total_recall
truncated
message: str | null
```

- `total_recall` 定义为本次混合检索实际取得、尚未截断到 `top_k` 的候选池大小，不伪装成
  平台商品总量。
- 候选池大小为 `min(150, max(60, top_k * 3))`。
- `truncated = total_recall > len(candidates)`。
- OpenSearch、模型或索引未准备好时返回 `not_configured`；运行时检索失败返回 `error` 和
  空候选，绝不回退到编造商品。

### 2.2 同质 fork

新增主 Loop 元工具：

```text
dispatch_tool(demand: str) -> ForkResult
```

- 不计入九个业务工具。
- 多平台请求时，主 Agent 在同一轮产生淘宝、京东、抖音三个 `dispatch_tool` 调用，由
  LangGraph 并发执行。
- 单平台请求由主 Agent 直接调用 ItemSearch。
- 每个子 Agent 使用独立 `thread_id/checkpoint`，但共享父 Agent 的模型实例、完整业务工具集和
  完全相同的 System Prompt。
- fork 深度首版限制为 1，子 Agent 再次派生 fork 时返回明确错误，防止递归失控。
- `ForkResult` 返回子线程 ID、执行状态、紧凑回答及最多 10 个候选；完整工具轨迹留在子线程
  checkpoint 中。

## 3. 实现内容

### 3.1 数据修复与建库门禁

- 现有 normalized/structured 数据的中文标题和部分属性存在乱码；原始成功响应中的中文正常。
- 扩展离线重建逻辑：扫描缓存的成功 raw JSON，以平台商品 ID 匹配现有 1000 个候选，只替换
  标题和文本属性，保留既有商品集合、价格、销量、评分、图片和平台商品链接，不产生任何
  付费请求。
- 已验证当前 1000 个 ID 均可从 raw 响应恢复；若重建时有任一 ID 无法匹配，立即失败，不允许
  带缺口建索引。
- 重新生成 normalized 与 structured 文件；structured schema 升级，增加 `product_url`，
  运行时再补 `retrieval_rank`。
- Embedding 文本仅由正常中文标题和稳定语义属性构造。白名单包括品牌、型号、品类、佩戴方式、
  连接方式、降噪能力、使用场景；价格、销量、评分、店铺、促销、库存不进入向量。

### 3.2 冻结编码器

- 新增统一 `EmbeddingEncoder` 接口和 `BgeM3Encoder` 实现，测试可注入 Fake Encoder。
- 使用 `sentence-transformers` 加载 `BAAI/bge-m3`，最大输入长度 256，批量大小默认 16。
- CUDA 使用 FP16，CPU 使用 FP32；输出统一 L2 归一化。
- 首次建库时把模型 `main` 解析为不可变 revision SHA，并写入索引 `_meta`。
- 查询端读取索引中的模型 ID、revision、维度和归一化方式；任何不一致都返回
  `not_configured`，禁止混用向量空间。
- 模型以进程级懒加载单例存在，三路 fork 共用同一实例，不重复占用显存。

### 3.3 OpenSearch 商品索引

创建：

- 物理索引：`globuy-products-v1`
- 稳定别名：`globuy-products`
- Search Pipeline：`globuy-products-rrf`

核心 mapping：

- `item_id/platform/currency/attribute_terms`：`keyword`
- `title`：`text`，使用内置 `cjk` analyzer
- `price`：`double`
- `rating`：`float`
- `sales`：`long`
- `attributes`：保存原 JSON，但关闭内部动态索引
- `image_url/product_url`：保存但不索引
- `content_vector`：1024 维 `knn_vector`
- 向量引擎：Lucene HNSW
- 距离：`cosinesimil`

建库命令负责：

1. 校验数据修复和字段契约。
2. 幂等创建 Search Pipeline 与新物理索引。
3. 批量生成文档向量并 bulk 写入。
4. 校验总数为 1000，平台数为 `taobao=350`、`jingdong=333`、`douyin=317`。
5. 校验通过后原子切换别名；失败时保留旧别名，不发布半成品索引。

查询由两路组成：

- BM25：对 `title` 使用 CJK 全文匹配。
- KNN：使用同一个 BGE-M3 编码 query。
- 两路只共享 `platform` 召回前过滤；价格、币种、评分、销量和属性在无权重 RRF 完成后通过
  OpenSearch 顶层 `post_filter` 过滤。
- Search Pipeline 用无权重 RRF 融合最终排名。
- 检索先取 `min(150, max(60, top_k * 3))` 的扩大候选池，post-filter 后再截取 `top_k`；不足时
  如实返回较少结果。
- 不增加 Cross-Encoder reranker、人工分数或学习权重。

### 3.4 工具、Agent 与监控链路

- 将 ItemSearch 改成异步工具，内部只依赖检索服务，不直接操作模型和 OpenSearch 细节。
- 修正 `AgentLoop.fork()`：移除工具裁剪和追加 Prompt 参数，使其成为真正的同质克隆；原来的
  可配置行为迁移为独立 `expert()` 工厂。
- `dispatch_tool` 在运行时绑定父 Agent，创建独立 checkpointer 和 `fork_scope()`，并提取子线程
  中的 ItemSearch 结构化结果。
- 更新 System Prompt，固定以下行为：
  - 多平台才 fork；
  - 一个 fork 只负责一个平台；
  - 三个 dispatch 必须在同一 assistant turn 发出；
  - 子 Agent 不再 fork；
  - 子 Agent 先调用 ItemSearch，再按需要调用 ItemPicker。
- ItemPicker 排序改为：`retrieval_rank` 升序、评分降序、价格升序；缺少 rank 时保持输入顺序，
  不再依赖任意 `score` 字段。
- 将 Monitor 放入 ContextVar：
  - WebSocket 请求绑定根线程发布通道；
  - fork 事件记录父子线程关系；
  - ItemSearch 上报工具开始、参数、结束和结果摘要；
  - 子线程事件仍通过根 WebSocket 发送，但事件内保留真实子线程 ID；
  - 监控结果只发送状态、数量和截断信息，不重复传输整批候选。

### 3.5 配置、依赖与文档

- 增加商品索引、Pipeline、模型 ID、设备、批量大小、候选池上限和 fork 压缩数量配置。
- 移除活动配置中的三塔 HTTP endpoint；保留 Faiss 代码及测试，但明确标注为非 ItemSearch
  主链路。
- 推荐运行方式固定为：OpenSearch/Redis 使用 Compose，本机 Conda `globuy` 运行 API 与
  BGE-M3。
- 更新 canonical 文档、README 和 `.env.example`：
  - 正式记录本次用户授权的 ItemSearch 技术变更；
  - 将旧“三塔 + Faiss + min_max 0.7/0.3”描述标为已被当前无训练方案取代；
  - 明确 OpenSearch RRF、数据导入命令、模型首次下载、GPU/CPU 回退和故障状态；
  - 在同一实施回合更新 `docs/project-status.md` 的当前实现、已确认决策和变更记录。

## 4. 测试与验收

### 4.1 自动化测试

- 数据修复：
  - 1000 个候选全部能匹配 raw 响应；
  - 标题与选定 raw 源一致，不再包含已知乱码；
  - 平台数量、唯一 item_id、价格和图片字段不发生意外漂移。
- Schema：
  - 三个平台值严格为 `taobao/jingdong/douyin`；
  - top_k、价格范围和属性过滤参数正确校验；
  - product_url 是平台商品页，不包含供应商 API 地址。
- 编码器：
  - Fake Encoder 验证批处理、1024 维、归一化和模型元数据校验；
  - 单例模型被三个 fork 共用。
- OpenSearch：
  - 索引与 RRF Pipeline 可重复创建；
  - alias 只在完整导入后切换；
  - BM25、KNN 与平台召回前过滤分别生效；价格、评分、销量和属性只出现在 `post_filter`；
  - `total_recall/truncated/retrieval_rank` 语义正确。
- ItemSearch：
  - 服务未启动、索引不存在、模型不匹配和查询异常分别返回规定状态；
  - 不进行实时 API 请求，也不伪造候选。
- fork：
  - 三个 dispatch 并发执行；
  - 子线程 ID/checkpoint 互相隔离；
  - 模型、tool_set、system_prompt 与父 Loop 相同；
  - 深度超过 1 被拒绝；
  - fork 与 ItemSearch 监控事件均路由到根 WebSocket。
- ItemPicker：
  - 优先遵循 retrieval rank，再以评分和价格确定性破同名次。

### 4.2 集成验收

- 启动 OpenSearch 后完成真实 BGE-M3 建库，索引文档总数必须为 1000。
- 分别执行淘宝、京东、抖音单平台搜索，返回结果不得跨平台。
- 验证“主动降噪蓝牙耳机”、价格区间、京东自营属性等代表性查询。
- 发起一次三平台购物请求，确认产生三个并发同质 fork，主 Agent 能聚合三路紧凑结果。
- 运行完整 pytest 与 Ruff；自动测试不调用付费 LLM、OneBound 或其他外部商品接口。

## 5. 明确边界与默认值

- 本轮包含：乱码修复、离线索引构建、冻结模型推理、OpenSearch Hybrid、ItemSearch、
  ItemPicker、同质 fork、监控链路和文档同步。
- 本轮不包含：实时 Provider、详情补采、模型训练或微调、User Tower、长期记忆个性化、
  Cross-Encoder rerank、人工评分闭环、汇率、运费和关税。
- Candidate 返回平台商品链接和检索排名；不返回供应商、采集时间或内部向量分数。
- 价格等易变字段可以用于过滤和展示，但绝不进入 Embedding 文本。
- 当前工作区已有大量用户修改；实施时逐文件增量修改，不覆盖或清理无关变更。

## 6. 实施结果（2026-07-20）

- 从已缓存成功响应无付费调用地修复 1000/1000 条标题与属性，并生成 Candidate v2；平台计数为
  淘宝 350、京东 333、抖音 317，商品 ID 重复数为 0，`product_url` 覆盖率为 100%。
- 已建立 `globuy-products-v1`，写入 1000 条 BGE-M3 归一化向量并发布
  `globuy-products` 别名；Search Pipeline 为 `globuy-products-rrf`。
- 实测三平台“无线蓝牙降噪头戴式耳机 + 价格不高于 500 元”均只返回指定平台结果；真实
  `item_search` 工具调用返回 5 条京东候选，价格上限与连续检索排名均通过校验。
- 模型加载已改为本地缓存优先、缓存缺失才下载；当前环境 PyTorch 2.13.0 为 CPU 版，已验证
  自动 CPU 回退。后续安装可用 CUDA PyTorch 后无需修改业务代码。
- `fork()` 已成为同质完整克隆，专家裁剪独立为 `expert()`；动态 `dispatch_tool`、深度限制、
  独立 checkpoint/thread、根 WebSocket monitor 路由与候选压缩均有自动测试。
- 全量测试 62 项通过，Ruff 通过；测试未调用付费 LLM 或商品 Provider。三路 fork 的并发与聚合
  由 Fake Agent/ToolNode 验证，未为验收调用真实 DeepSeek。
- 用户随后确认过滤顺序：`platform` 保留在 `hybrid.filter` 作为单平台召回域；价格、币种、
  评分、销量和属性迁移到 RRF 后的顶层 `post_filter`。实现继续先取 60～150 条融合候选，再过滤
  并截取 `top_k`；真实京东查询同时验证了价格与销量 post-filter。
