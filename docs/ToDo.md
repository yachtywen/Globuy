# Globuy ToDo and Handoff

Last updated: 2026-07-24

## Current verified baseline

- Registration, login, MySQL session persistence, WebSocket task events,
  cancellation, history, and preference-domain management are available.
- New users receive six editable preference domains. Memory candidates are not
  saved automatically; users can edit, assign a domain, and confirm them.
- Budget and explicit-preference trigger words now create a candidate memory
  and automatically open the confirmation dialog after a successful result.
- Just One realtime search has been smoke-tested successfully for Taobao, JD,
  and Douyin. Candidates use a ten-minute `query + platform` cache and the
  OpenSearch BM25 + BGE-M3 + RRF path.
- Realtime results remain usable if the local hybrid layer is temporarily
  unavailable; the response identifies this as `realtime_provider` rather than
  pretending that it completed hybrid retrieval.
- OpenSearch must use `opensearchproject/opensearch:2.19.1`. The host backend
  must use port `9200`.
- Marketplace images are normalized for JD relative paths and requested through
  an allowlisted backend image proxy. Source CDNs can still legitimately fail,
  in which case the UI uses its placeholder image.
- Agent runs now end when `item_picker` has valid picks, preventing a repeated
  Think/Reflect loop from exhausting LangGraph recursion.

## P0: finish the realtime shopping loop

### 1. Persist realtime candidates before a wishlist add

**Status: not complete. This is the current functional blocker.**

Realtime candidates are indexed into OpenSearch but are not yet inserted into
MySQL `Product`, `Offer`, and `OfferObservation` records. Therefore a live
result can have a deterministic `offer_id` in the card but the wishlist service
returns `OFFER_NOT_FOUND` / no matching offer when the user clicks add.

Implement this before enabling wishlist adds for live results:

1. In the realtime candidate path, upsert `Product` and `Offer` by platform and
   source item/SKU identity.
2. Create a `SourceSnapshot` and an `OfferObservation` for each successful
   provider response. Preserve provider, platform, capture time, request key,
   price, rating, sales, image URL, and product URL.
3. Publish the same MySQL-derived document to OpenSearch through the existing
   product outbox, or make the direct realtime index update idempotent with the
   database write.
4. Only enable the wishlist button after the offer is persisted. Keep existing
   offline offers working.

Acceptance test: run a realtime search, add one result from each platform to a
wishlist, reload the page, and verify title, image, price, source link, and
price history all remain available.

### 2. Implement provider detail refresh for wishlist prices

**Status: partial.** The manual refresh button and worker scheduling exist, but
the Just One product-detail adapter is not implemented for all platforms.

1. Add `platform + source_item_id` detail requests for Taobao, JD, and Douyin.
2. Update `Offer.current_price` and append `OfferObservation` on success.
3. Preserve the prior price and record a retryable error on provider failure.
4. Verify manual refresh and the daily worker without duplicate observations.

### 3. Make three-platform recommendations observable in the UI

**Status: partial.** The provider smoke script proves three-platform retrieval,
but a normal Agent recommendation does not yet guarantee that all three
platforms are represented in the final picks.

1. Record per-platform search outcomes in the task result.
2. When multiple platforms succeed, retain platform provenance through dispatch,
   item picker, and final cards.
3. Add a deterministic test proving that a successful Douyin candidate can
   become a displayed recommendation when it satisfies the constraints.

## P1: stabilize and prove personalization

### 4. Add focused tests for memory-candidate triggering

Test budget, positive preference, blacklist, personal constraints, duplicate
candidate merging, dialog auto-open, cancel, edit, domain selection, and
confirmed persistence. Test that no candidate is written without confirmation.

### 5. Improve Agent convergence coverage

The item-picker terminal fallback prevents the observed recursion failure. Add
tests for repeated tool calls, missing picker results, provider failures,
multi-platform forks, cancellation, and a maximum graceful fallback response.

### 6. Realtime image quality checks

Add tests for JD `jfs/` URL normalization, allowlisted image proxy validation,
unsupported hosts, non-image responses, and fallback rendering. Do not proxy
arbitrary URLs.

## P2: evaluation and presentation

1. Collect at least 30 real query records in `output/eval/records.jsonl` using
   [evaluation-format.md](evaluation-format.md).
2. Run:

```powershell
python -m app.eval.shopping_benchmark output/eval/records.jsonl
```

3. Use the generated JSON and Markdown report to show platform success rate,
   mean/P95 latency, cache hit rate, keyword versus hybrid Top-3 results,
   preference recall correctness, tool failure rate, and cancellation success.
4. Add a small demonstration set with explicit preferences, for example budget,
   brand preference, and a rejected wearing style. Show that confirmation in one
   run affects retrieval in the next run.

## Working rules

- Do not commit `.env`, provider tokens, raw provider responses, database
  volumes, model caches, or generated output.
- Do not represent offline snapshots as realtime prices or stock.
- After every material change, update `docs/project-status.md`, run targeted
  tests, and record any remaining failure honestly.
