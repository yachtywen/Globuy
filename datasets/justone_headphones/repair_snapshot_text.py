"""Repair normalized snapshot text from cached successful provider responses.

This module never calls a provider. It rebuilds only human-readable text fields
from the redacted raw JSON already stored in the dataset bundle.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from datasets.justone_headphones.collector import (
    PLATFORMS,
    _business_code,
    normalize_search_item,
    search_items,
)


@dataclass(frozen=True, slots=True)
class RepairReport:
    source_records: int
    repaired_records: int
    missing_item_ids: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_item_ids and self.source_records == self.repaired_records


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _raw_candidate_map(raw_dir: Path) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for platform in PLATFORMS:
        for folder in ("search", "imported"):
            for path in sorted((raw_dir / platform / folder).glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(payload, dict) or _business_code(payload) != "0":
                    continue
                raw_items, _ = search_items(platform, payload)
                for raw_item in raw_items:
                    candidate = normalize_search_item(
                        platform,
                        raw_item,
                        keyword="snapshot-repair",
                        page=0,
                        captured_at="snapshot-repair",
                    )
                    if candidate is not None:
                        candidates.setdefault(candidate["item_id"], candidate)
    return candidates


def repair_rows(
    rows: list[dict[str, Any]], raw_dir: Path
) -> tuple[list[dict[str, Any]], RepairReport]:
    raw_candidates = _raw_candidate_map(raw_dir)
    missing = tuple(sorted(row["item_id"] for row in rows if row["item_id"] not in raw_candidates))
    if missing:
        return rows, RepairReport(len(rows), 0, missing)

    repaired: list[dict[str, Any]] = []
    for row in rows:
        clean = raw_candidates[row["item_id"]]
        updated = dict(row)
        updated["title"] = clean["title"]
        updated["attributes"] = clean["attributes"]
        repaired.append(updated)
    return repaired, RepairReport(len(rows), len(repaired), ())


def repair_snapshot(
    normalized_path: Path,
    raw_dir: Path,
    *,
    state_candidates_path: Path | None = None,
    write: bool = False,
) -> RepairReport:
    rows = _load_jsonl(normalized_path)
    repaired, report = repair_rows(rows, raw_dir)
    if not report.complete:
        preview = ", ".join(report.missing_item_ids[:10])
        raise ValueError(f"无法从 raw 响应恢复 {len(report.missing_item_ids)} 个商品: {preview}")

    if write:
        normalized_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in repaired),
            encoding="utf-8",
        )
        if state_candidates_path is not None and state_candidates_path.exists():
            state_rows = json.loads(state_candidates_path.read_text(encoding="utf-8"))
            state_by_id = {row["item_id"]: row for row in repaired}
            for state_row in state_rows:
                repaired_row = state_by_id.get(state_row.get("item_id"))
                if repaired_row is not None:
                    state_row["title"] = repaired_row["title"]
                    state_row["attributes"] = repaired_row["attributes"]
            state_candidates_path.write_text(
                json.dumps(state_rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair cached snapshot text without API calls")
    parser.add_argument("--root", type=Path, default=Path("datasets/headphones_1000"))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    report = repair_snapshot(
        args.root / "normalized" / "headphones.jsonl",
        args.root / "raw",
        state_candidates_path=args.root / "state" / "candidates.json",
        write=args.write,
    )
    print(json.dumps({
        "source_records": report.source_records,
        "repaired_records": report.repaired_records,
        "missing_item_ids": list(report.missing_item_ids),
        "written": args.write,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
