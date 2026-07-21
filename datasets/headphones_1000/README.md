# 1000 条耳机数据快照

本目录是为后续结构化处理准备的独立数据交付包，生成于 2026-07-18。

当前共 1000 条去重候选：淘宝 350 条、京东 333 条、抖音商城 317 条。主数据字段为
`item_id`、`platform`、`title`、`price`、`currency`、`rating`、`sales`、`image_url`、
`source_url`、`captured_at`、`attributes`、`detail_enriched`、`source_keyword`、`source_page`。

- `normalized/headphones.jsonl`：后续程序处理的主入口，一行一条标准化商品；
- `normalized/headphones.csv`：同一数据的便于人工查看版本，`attributes` 为 JSON 字符串；
- `raw/`：已脱敏的搜索接口原始响应，用于重新映射字段；
- `reports/`：数据量、字段覆盖、请求账本和导入记录；
- `state/`：采集完成时的候选与分页状态快照。

## 结构化候选集

`structured/itemsearch_candidates.jsonl` 和 `structured/itemsearch_candidates.csv` 是按 ItemSearch
候选签名清理后的主入口。它们只保留 `item_id`、`platform`、`title`、`price`、`currency`、
`rating`、`sales`、`image_url`、`attributes`；京东平台统一标记为 `jingdong`，图片只保留 URL。
字段约束、覆盖率和删减说明见 `structured/schema.json`、`structured/quality_report.json`。

## 图片与品类审计

`structured/image_url_audit.jsonl` 保存逐条图片 URL 的只读连通性检测结果，
`structured/dataset_audit_report.json` 汇总图片可访问性、内容类型、价格带和标题关键词品类覆盖。
2026-07-18 的全量检测中，1000 条 URL 都收到 2xx 响应，996 条返回标准 `image/*` 内容类型；
4 条抖音 URL 返回 `application/octet-stream`，仍可访问但不能仅凭响应头确认图片 MIME 类型。淘宝
350 条可访问，但在当前运行环境中需跳过严格 TLS 域名校验（CDN 证书域名不匹配），该事实已逐条
标记，不能等同于“严格 HTTPS 校验通过”。检测只读取响应头或 1 字节 Range，不下载或人工查看完整图片。

注意：本批数据只调用搜索接口，`detail_enriched` 均为 `false`；`attributes` 是搜索结果可见的
结构化元数据，不应当作完整详情、SKU 或规格参数。目录中的真实数据、原始响应和报告均为本地
生成文件，已由 Git 忽略；不包含 API Token。
