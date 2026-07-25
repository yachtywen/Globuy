# Globuy ToDo and Handoff

Last updated: 2026-07-25

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

**Status: implemented; live three-platform acceptance remains.**

Realtime provider responses now upsert MySQL `Product` and `Offer` rows and
create idempotent `SourceSnapshot`, `OfferObservation`, and product Outbox
records before exposing wishlist actions. Returned candidates carry
`wishlist_eligible`; the UI keeps the wishlist button disabled when persistence
is unavailable or fails. Existing offline offers remain eligible.

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

Automated acceptance covers idempotent persistence, stable identities, price,
image/source retention, Outbox creation, and the UI failure gate. The remaining
manual acceptance is to run a paid realtime search, add one result from each
platform to a wishlist, reload the page, and verify title, image, price, source
link, and price history with MySQL/OpenSearch running. The latest authorized
attempt did not reach the Provider because the payment layer rejected the
required preauthorization (available balance was below the preauthorization
amount); do not replace this acceptance with cached or synthetic results.

### 2. Implement provider detail refresh for wishlist prices

**Status: partial.** The manual refresh button and worker scheduling exist, but
the Just One adapter is not implemented for all platforms. Bounded live probes
verified Taobao detail pricing and JD's dedicated price response, and those two
paths are registered for API and CLI refresh workers. Douyin item detail
returned no price field; its official SKU endpoint is the remaining candidate,
but its OpenAPI success schema is empty. Two approved SKU probes for the same
real item and one probe for a second real item returned the documented business
code `301` with `data=null`. Official guidance defines `301` as upstream
collection failure and separates it from invalid parameters, authentication,
balance, quota, and rate limits. Douyin refresh remains disabled rather than
issuing unusable daily requests; obtain one successful SKU response before
adding its mapping.

1. Add `platform + source_item_id` detail requests for Taobao, JD, and Douyin.
2. Update `Offer.current_price` and append `OfferObservation` on success.
3. Preserve the prior price and record a retryable error on provider failure.
4. Verify manual refresh and the daily worker without duplicate observations.

The worker behavior in steps 2-4 and the Taobao/JD adapters are covered with
offline SQLite/fake-provider tests. Remaining work is the verified Douyin SKU
price mapping and a bounded live acceptance run. Realtime Douyin candidates now
retain `promotion_id` for that later verification.

### 3. Make three-platform recommendations observable in the UI

**Status: implemented; live three-platform acceptance remains.** Agent results
carry the latest trusted per-platform outcomes, and the UI displays status and
candidate counts. Platform provenance survives dispatch, deterministic picking,
and final-card hydration; a Douyin candidate display path is covered by tests.

1. Record per-platform search outcomes in the task result.
2. When multiple platforms succeed, retain platform provenance through dispatch,
   item picker, and final cards.
3. Add a deterministic test proving that a successful Douyin candidate can
   become a displayed recommendation when it satisfies the constraints.

## P1: stabilize and prove personalization

### 4. Add focused tests for memory-candidate triggering

**Status: completed.** Automated tests cover all cases below without persisting
an unconfirmed candidate.

Test budget, positive preference, blacklist, personal constraints, duplicate
candidate merging, dialog auto-open, cancel, edit, domain selection, and
confirmed persistence. Test that no candidate is written without confirmation.

### 5. Improve Agent convergence coverage

**Status: completed.** Provider failure, empty picker output, repeated routing,
multi-platform dispatch, cancellation, blocking recursion exhaustion, and
streaming recursion exhaustion have graceful terminal coverage.

The item-picker terminal fallback prevents the observed recursion failure. Add
tests for repeated tool calls, missing picker results, provider failures,
multi-platform forks, cancellation, and a maximum graceful fallback response.

### 6. Realtime image quality checks

**Status: completed.** Backend allowlist/content-type checks and frontend image
fallback rendering are covered by automated tests.

Add tests for JD `jfs/` URL normalization, allowlisted image proxy validation,
unsupported hosts, non-image responses, and fallback rendering. Do not proxy
arbitrary URLs.

## P2: evaluation and presentation

**Status: completed for local retrieval, personalization, and cancellation;
realtime Provider metrics remain explicitly unobserved.** The generated,
Git-ignored evaluation set contains 30 real OpenSearch retrieval cases (10 query
families × 3 platforms), two cross-run preference cases, and one real
`RunRegistry` cancellation case. It does not contain fabricated Provider
attempts.

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

Completed evidence:

- `app.eval.collect_retrieval_records` compares BM25 and the fixed BGE-M3 + RRF
  hybrid route against auditable all-title-anchor labels. All 30 cases had at
  least one labelled item.
- `app.eval.collect_personalization_evidence` uses a temporary MySQL user, the
  real `MemoryService`, memory Outbox and dedicated OpenSearch memory index.
  Confirmation changed recall from no keys to budget, Sony preference, and
  rejected in-ear style, then changed deterministic Picker Top-3 under the
  confirmed hard constraints. The QA user and indexed memories are removed and
  the removal is verified by the script.
- `app.eval.collect_cancellation_evidence` starts and cancels a real local
  `RunRegistry` task. The persisted terminal status is `cancelled`.
- The 33-case report records average/P95 latency, zero observed cache hits,
  keyword/hybrid Top-3 hit rates, memory recall and false positives, tool
  failures, cancellation, and per-platform OpenSearch retrieval success. The
  realtime Provider table is intentionally empty until the paid P0 acceptance
  can run.

## Working rules

- Do not commit `.env`, provider tokens, raw provider responses, database
  volumes, model caches, or generated output.
- Do not represent offline snapshots as realtime prices or stock.
- After every material change, update `docs/project-status.md`, run targeted
  tests, and record any remaining failure honestly.
