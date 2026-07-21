# OneBound 耳机数据采集

本目录用于在严格预算上限内采集淘宝、京东耳机候选。生成数据保存在本目录的
`raw/`、`normalized/`、`reports/`、`state/` 中，这些目录不会提交到 Git。

凭据只放在仓库根目录被忽略的 `.env`：

```dotenv
GLOBUY_ONEBOUND_KEY=
GLOBUY_ONEBOUND_SECRET=
GLOBUY_ONEBOUND_FALLBACK_KEY=
GLOBUY_ONEBOUND_FALLBACK_SECRET=
```

执行顺序：

```powershell
conda run -n globuy python -m datasets.onebound_headphones.collect dry-run
conda run -n globuy python -m datasets.onebound_headphones.collect collect --smoke-only
conda run -n globuy python -m datasets.onebound_headphones.collect resume
```

若某个平台尚未开通，可以只续传已授权的平台，例如：

```powershell
conda run -n globuy python -m datasets.onebound_headphones.collect resume --platform taobao
```

若万邦控制台已经补充额度或开通接口，才可以对之前缓存的配额/权限错误显式重试一次：

```powershell
conda run -n globuy python -m datasets.onebound_headphones.collect resume --retry-cached-provider-errors
```

需要使用备用账号时显式指定，采集器不会在后台静默切换账号：

```powershell
conda run -n globuy python -m datasets.onebound_headphones.collect resume --account fallback --retry-cached-provider-errors
```

该开关不会重试普通 `data error`，同一组参数也最多新增一次显式重试记录。若再次失败，必须先
检查控制台状态，不能删除 `reports/request_ledger.jsonl` 或 `raw/` 来绕过保护。

采集器不并发、不自动重试，默认最多调用 70 次搜索和 20 次详情，估算费用硬上限为 2 元。
若中断，必须使用 `resume`；程序会复用已落盘响应，已经登记但没有响应文件的请求也不会自动
重发。搜索页只生成基础候选，淘宝、京东各 10 条详情样本才会填充 `attributes`。

2026-07-17 使用主、备用两个账号完成真实验证：本地已保留淘宝 500 条唯一候选和 1 条详情
增强样本；两个账号的京东搜索接口均未开通。按京东官方完整参数进行的额外复测又被备用账号
额度拦截。当前共登记 33 次搜索和 2 次详情请求，按调研单价估算 0.772 元；第 2 次淘宝详情
请求也被供应商额度拦截。真实进度以本机
`reports/manifest.json` 为准；补充额度并开通 `jd.item_search`、`jd.item_get` 后可续传。
