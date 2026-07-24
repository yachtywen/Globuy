# Three-platform realtime search check

Use this after starting OpenSearch, Redis, and the local backend dependencies.

```powershell
Set-Location C:\Users\Lenovo\Desktop\模板例子\code\Globuy
conda run -n globuy python scripts/check_realtime_product_search.py --query "降噪耳机"
```

Expected result: each of `taobao`, `jingdong`, and `douyin` prints `status=ok` and a non-zero candidate count, followed by `三平台实时商品搜索通过。`.

`source=hybrid_realtime_catalog` means the live candidates completed OpenSearch BM25 + BGE-M3 + RRF retrieval. `source=realtime_provider` means Just One returned real candidates, while the local hybrid layer was unavailable; the results remain usable and the message states the degradation.

If one platform fails, keep the script output and the FastAPI terminal log. Do not print or share the API token.
