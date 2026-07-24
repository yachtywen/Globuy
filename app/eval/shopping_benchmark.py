"""Aggregate reproducible shopping-Agent evaluation records into a JSON and Markdown report.

The evaluator deliberately consumes recorded run facts instead of calling a paid model or provider.
One JSON object per line is expected; see ``docs/evaluation-format.md`` for the wire format.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]


def load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number} is not valid JSON") from exc
        if not isinstance(item, dict) or not isinstance(item.get("case_id"), str):
            raise ValueError(f"{path}:{line_number} must contain string case_id")
        records.append(item)
    return records


def _top3_hit(record: dict[str, Any], field: str) -> bool | None:
    expected = set(map(str, record.get("expected_item_ids") or []))
    returned = set(map(str, (record.get(field) or [])[:3]))
    return bool(expected & returned) if expected else None


def evaluate(records: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [float(item["duration_ms"]) for item in records if isinstance(item.get("duration_ms"), (int, float))]
    cached = [item for item in records if isinstance(item.get("cache_hit"), bool)]
    keyword_hits = [_top3_hit(item, "keyword_top3") for item in records]
    hybrid_hits = [_top3_hit(item, "hybrid_top3") for item in records]
    keyword_hits = [item for item in keyword_hits if item is not None]
    hybrid_hits = [item for item in hybrid_hits if item is not None]
    memory_expected = memory_recalled = memory_false_positive = 0
    tool_calls = tool_failures = cancelled_expected = cancelled_ok = 0
    platform = defaultdict(lambda: Counter(attempted=0, succeeded=0, failed=0))
    for record in records:
        expected_keys = set(map(str, record.get("expected_memory_keys") or []))
        recalled_keys = set(map(str, record.get("recalled_memory_keys") or []))
        memory_expected += len(expected_keys)
        memory_recalled += len(expected_keys & recalled_keys)
        memory_false_positive += len(recalled_keys - expected_keys)
        calls = record.get("tool_calls") or []
        if isinstance(calls, list):
            tool_calls += len(calls)
            tool_failures += sum(1 for call in calls if isinstance(call, dict) and call.get("status") in {"error", "failed"})
        if record.get("expect_cancelled") is True:
            cancelled_expected += 1
            cancelled_ok += record.get("status") == "cancelled"
        for item in record.get("provider_attempts") or []:
            if not isinstance(item, dict) or not isinstance(item.get("platform"), str):
                continue
            bucket = platform[item["platform"]]
            bucket["attempted"] += 1
            if item.get("status") == "ok": bucket["succeeded"] += 1
            else: bucket["failed"] += 1
    return {
        "case_count": len(records),
        "latency_ms": {"average": round(mean(durations), 2) if durations else None, "p95": _p95(durations)},
        "cache": {"observed_runs": len(cached), "hit_rate": _ratio(sum(item["cache_hit"] for item in cached), len(cached))},
        "top3": {"keyword_hit_rate": _ratio(sum(keyword_hits), len(keyword_hits)), "hybrid_hit_rate": _ratio(sum(hybrid_hits), len(hybrid_hits))},
        "memory": {"recall": _ratio(memory_recalled, memory_expected), "false_positive_count": memory_false_positive},
        "agent": {"tool_failure_rate": _ratio(tool_failures, tool_calls), "cancel_success_rate": _ratio(cancelled_ok, cancelled_expected)},
        "platforms": {name: {**counts, "success_rate": _ratio(counts["succeeded"], counts["attempted"])} for name, counts in sorted(platform.items())},
    }


def markdown(report: dict[str, Any]) -> str:
    def value(item: Any) -> str: return "—" if item is None else str(item)
    rows = ["# Globuy Shopping Evaluation", "", f"- Cases: {report['case_count']}", "", "| Metric | Value |", "|---|---|"]
    rows.extend([f"| Average latency | {value(report['latency_ms']['average'])} ms |", f"| P95 latency | {value(report['latency_ms']['p95'])} ms |", f"| Cache hit rate | {value(report['cache']['hit_rate'])} |", f"| Keyword Top-3 hit rate | {value(report['top3']['keyword_hit_rate'])} |", f"| Hybrid Top-3 hit rate | {value(report['top3']['hybrid_hit_rate'])} |", f"| Memory recall | {value(report['memory']['recall'])} |", f"| Memory false positives | {report['memory']['false_positive_count']} |", f"| Tool failure rate | {value(report['agent']['tool_failure_rate'])} |", f"| Cancellation success rate | {value(report['agent']['cancel_success_rate'])} |"])
    if report["platforms"]:
        rows.extend(["", "## Provider status", "", "| Platform | Attempts | Success rate |", "|---|---:|---:|"])
        rows.extend(f"| {name} | {item['attempted']} | {value(item['success_rate'])} |" for name, item in report["platforms"].items())
    return "\n".join(rows) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSONL evaluation records")
    parser.add_argument("--json-output", type=Path, default=Path("output/eval/shopping-report.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("output/eval/shopping-report.md"))
    args = parser.parse_args()
    report = evaluate(load_records(args.input))
    for path, content in ((args.json_output, json.dumps(report, ensure_ascii=False, indent=2) + "\n"), (args.markdown_output, markdown(report))):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__": main()
