"""Evaluate RelevantRetention@36 on a frozen, manually audited candidate set."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.products.grouping import group_candidates
from app.recall.transient_hybrid import bm25_order, select_hybrid_groups
from app.search.candidate_encoder import CandidateEmbeddingEncoder
from app.search.schemas import Candidate


def _load_candidates(path: Path) -> tuple[list[Candidate], str]:
    content = path.read_bytes()
    rows = [
        Candidate.model_validate_json(line)
        for line in content.decode("utf-8").splitlines()
        if line
    ]
    return rows, hashlib.sha256(content).hexdigest()


def _case_pool(
    case: dict[str, Any],
    by_id: dict[str, Candidate],
    candidates: list[Candidate],
    pool_size_override: int | None,
) -> list[Candidate]:
    relevant = list(dict.fromkeys(case["relevant_item_ids"]))
    missing = [item_id for item_id in relevant if item_id not in by_id]
    if missing:
        raise ValueError(f"{case['id']} references missing items: {missing}")
    pool_size = pool_size_override or int(case["pool_size"])
    if len(relevant) > pool_size:
        raise ValueError(f"{case['id']} has more positives than its pool")
    relevant_set = set(relevant)
    negatives = sorted(
        (item for item in candidates if item.item_id not in relevant_set),
        key=lambda item: hashlib.sha256(
            f"{case['id']}:{item.item_id}".encode()
        ).digest(),
    )[: pool_size - len(relevant)]
    pool = [*(by_id[item_id] for item_id in relevant), *negatives]
    return sorted(
        pool,
        key=lambda item: hashlib.sha256(
            f"pool:{case['id']}:{item.item_id}".encode()
        ).digest(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--pool-size", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    candidates, dataset_sha256 = _load_candidates(args.dataset)
    by_id = {candidate.item_id: candidate for candidate in candidates}
    suite = json.loads(args.cases.read_text(encoding="utf-8"))
    encoder = CandidateEmbeddingEncoder(get_settings())
    encoder.warmup()
    results: list[dict[str, Any]] = []
    total_relevant = 0
    total_retained = 0
    exact_relevant = 0
    exact_retained = 0
    for case in suite["cases"]:
        pool = _case_pool(case, by_id, candidates, args.pool_size)
        groups, grouping = group_candidates(pool)
        selection = select_hybrid_groups(
            groups,
            lexical_query=case["lexical_query"],
            semantic_query=case["semantic_query"],
            encoder=encoder,
            limit=36,
            rank_constant=60,
        )
        selected_ids = {
            offer.item_id for group in selection.groups for offer in group.offers
        }
        lexical_order = bm25_order(groups, case["lexical_query"])
        lexical_ranks = {
            offer.item_id: rank
            for rank, group in enumerate(lexical_order, start=1)
            for offer in group.offers
        }
        relevant_ids = set(case["relevant_item_ids"])
        retained_ids = sorted(relevant_ids & selected_ids)
        total_relevant += len(relevant_ids)
        total_retained += len(retained_ids)
        if case["kind"] == "exact_identity":
            exact_relevant += len(relevant_ids)
            exact_retained += len(retained_ids)
        results.append(
            {
                "id": case["id"],
                "kind": case["kind"],
                "pool_offers": len(pool),
                "pool_groups": len(groups),
                "selected_groups": len(selection.groups),
                "relevant": len(relevant_ids),
                "retained": len(retained_ids),
                "retention": len(retained_ids) / len(relevant_ids),
                "missed_item_ids": sorted(relevant_ids - selected_ids),
                "relevant_bm25_ranks": {
                    item_id: lexical_ranks[item_id] for item_id in sorted(relevant_ids)
                },
                "collapsed_offers": grouping.collapsed_offers,
            }
        )
    retention = total_retained / total_relevant
    exact_retention = exact_retained / exact_relevant
    metadata = encoder.metadata
    report = {
        "schema_version": "candidate-hybrid-retention-report-v1",
        "dataset_sha256": dataset_sha256,
        "case_suite": args.cases.as_posix(),
        "pool_size_override": args.pool_size,
        "machine": {"system": platform.platform(), "processor": platform.processor()},
        "embedding": {
            "model": metadata.model_id,
            "revision": metadata.revision,
            "backend": metadata.backend,
            "dimensions": metadata.dimensions,
        },
        "summary": {
            "relevant_retention_at_36": retention,
            "relevant_retained": total_retained,
            "relevant_total": total_relevant,
            "exact_identity_retention_at_36": exact_retention,
            "exact_identity_retained": exact_retained,
            "exact_identity_total": exact_relevant,
            "relevant_retention_gate": retention >= 0.98,
            "exact_identity_gate": exact_retention == 1.0,
        },
        "cases": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if retention >= 0.98 and exact_retention == 1.0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
