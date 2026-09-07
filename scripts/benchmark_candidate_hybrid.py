"""Benchmark cold, warm-uncached and cache-hit transient Hybrid selection.

The input may be the repository Candidate JSONL snapshot or a JSON array of
CandidateGroup objects. No Provider or LLM is called.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.products.grouping import CandidateGroup, group_candidates
from app.recall.transient_hybrid import FaissSelection, select_faiss_groups
from app.search.candidate_encoder import CandidateEmbeddingEncoder
from app.search.schemas import Candidate


def _load_groups(path: Path) -> list[CandidateGroup]:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.casefold() == ".jsonl":
        candidates = [Candidate.model_validate_json(line) for line in raw.splitlines() if line]
        groups, _ = group_candidates(candidates)
    else:
        payload = json.loads(raw)
        groups = [CandidateGroup.model_validate(item) for item in payload]
    buckets: dict[str, list[CandidateGroup]] = defaultdict(list)
    for group in groups:
        buckets[group.representative.platform].append(group)
    balanced: list[CandidateGroup] = []
    while any(buckets.values()):
        for source in ("taobao", "jingdong", "douyin"):
            if buckets[source]:
                balanced.append(buckets[source].pop(0))
    return balanced


def _select(
    groups: list[CandidateGroup], *, query: str, encoder: CandidateEmbeddingEncoder
) -> tuple[float, FaissSelection]:
    started = time.perf_counter()
    selection = select_faiss_groups(
        groups,
        lexical_query=query,
        semantic_query=query,
        encoder=encoder,
        limit=36,
        rank_constant=60,
    )
    return (time.perf_counter() - started) * 1000, selection


def _percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[max(0, int(len(values) * fraction) - 1)]


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("groups_or_candidates")
    parser.add_argument("--query", required=True)
    parser.add_argument("--sizes", default="45,60,120")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.runs < 2:
        parser.error("--runs must be at least 2")

    all_groups = _load_groups(Path(args.groups_or_candidates))
    results: list[dict[str, Any]] = []
    for size in (int(value) for value in args.sizes.split(",")):
        groups = all_groups[:size]
        if len(groups) != size:
            parser.error(f"input only contains {len(all_groups)} groups; requested {size}")

        cold_encoder = CandidateEmbeddingEncoder(get_settings())
        cold_ms, cold_selection = _select(groups, query=args.query, encoder=cold_encoder)

        warm_encoder = CandidateEmbeddingEncoder(get_settings())
        metadata = warm_encoder.warmup()
        warm_uncached: list[float] = []
        for _ in range(args.runs):
            warm_encoder.clear_cache()
            duration, _ = _select(groups, query=args.query, encoder=warm_encoder)
            warm_uncached.append(duration)

        warm_encoder.clear_cache()
        _select(groups, query=args.query, encoder=warm_encoder)
        cache_hit = [
            _select(groups, query=args.query, encoder=warm_encoder)[0]
            for _ in range(args.runs)
        ]
        results.append(
            {
                "groups": size,
                "cold_total_ms": round(cold_ms, 3),
                "cold_embedding_ms": cold_selection.embedding_duration_ms,
                "warm_uncached_total": _summary(warm_uncached),
                "cache_hit_total": _summary(cache_hit),
                "runs": args.runs,
                "model": metadata.model_id,
                "revision": metadata.revision,
                "backend": metadata.backend,
            }
        )
    report = {
        "schema_version": "candidate-hybrid-benchmark-v1",
        "machine": {
            "system": platform.platform(),
            "processor": platform.processor(),
        },
        "query": args.query,
        "results": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
