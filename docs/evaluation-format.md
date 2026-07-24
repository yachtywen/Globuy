# Shopping evaluation record format

每行一个真实运行记录；不要填造 Provider 结果。脚本命令：

```powershell
python -m app.eval.shopping_benchmark output/eval/records.jsonl
```

```json
{"case_id":"headphones-budget-01","duration_ms":1280,"cache_hit":false,"expected_item_ids":["jingdong:123"],"keyword_top3":["jingdong:456"],"hybrid_top3":["jingdong:123"],"expected_memory_keys":["budget","no_in_ear"],"recalled_memory_keys":["budget","no_in_ear"],"tool_calls":[{"name":"item_search","status":"ok"}],"provider_attempts":[{"platform":"jingdong","status":"ok"}],"status":"succeeded"}
```

- `expected_item_ids`：人工核验后认为应出现在 Top-3 的商品 ID；无标注时可省略，不计入命中率。
- `keyword_top3` 和 `hybrid_top3`：同一候选池上两种检索方式的前 3 个 ID，用于公平比较。
- `expected_memory_keys`：该查询应该被召回的用户确认记忆；不该召回的记忆不能写进该数组，脚本会统计其误召回数量。
- `provider_attempts`：实时链路接通后填写；离线阶段可省略。
