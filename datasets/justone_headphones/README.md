# Just One API 耳机数据采集

本目录用于以搜索接口收集淘宝、京东、抖音商城约 1000 条耳机候选，默认目标分别为
334、333、333 条。采集器顺序请求、不并发、默认不重试，支持断点续传和本地响应复用。

真实 Token 只放在本目录被 Git 忽略的 `.env`：

```dotenv
GLOBUY_JUSTONE_TOKEN=
```

执行顺序：

```powershell
conda run -n globuy python -m datasets.justone_headphones.collect dry-run
conda run -n globuy python -m datasets.justone_headphones.collect collect --smoke-only
conda run -n globuy python -m datasets.justone_headphones.collect resume
```

若出现官方文档声明不计费的业务错误，可检查状态后显式重试一次：

```powershell
conda run -n globuy python -m datasets.justone_headphones.collect resume --retry-nonbillable-errors
```

生成内容：

- `normalized/headphones.jsonl`：标准化主数据；
- `normalized/headphones.csv`：UTF-8 BOM 表格，`attributes` 为 JSON 字符串；
- `raw/`：不含 Token 的原始业务响应；
- `reports/manifest.json`：数量、调用次数和保守费用估算；
- `reports/quality_report.json`：字段覆盖、重复和原始页统计；
- `reports/request_ledger.jsonl`：脱敏请求账本；
- `state/`：候选与分页断点。

安全限制：只调用商品搜索，不调用详情、评论或 SKU；最多 80 次成功搜索和 140 次总尝试。
官方公开文档没有展示端点单价，因此暂按 0.10 元/成功调用估算并设置 8 元估算上限，真实扣费
必须以控制台为准。

## 2026-07-17 实际采集进度

- 淘宝：36 次成功搜索，形成 350 条去重候选；
- 京东：20 次成功搜索，形成 333 条去重候选，已达到目标；
- 抖音商城：导入 1 份用户成功响应并完成 21 次成功搜索，形成 317 条去重候选；
- 合计：1000 条，重复商品 ID 为 0；标题、价格、主图和来源链接覆盖率均为 100%；
- 调用：125 次尝试、77 次成功搜索；48 次业务码 301 未计入成功调用。按未验证的 0.10 元/成功
  调用假设估算 7.70 元，实际扣费以控制台为准；
- 京东相对主图路径已在本地映射为可访问的京东 CDN URL；抖音真实响应的嵌套价格已按“分→元”
  转换，销量、好评率、主图、来源链接和类目按原始字段保留，没有新增详情调用。

已完成约 1000 条目标。最终为淘宝 350、京东 333、抖音 317 条；为了避开抖音的间歇性 301，
最后 16 条补自淘宝，三平台仍均有充足覆盖。不要删除 `raw/`、`reports/request_ledger.jsonl`
或 `state/`，否则可能造成重复付费调用。
