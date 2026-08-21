"""Evaluation artifact rendering and secret-safe trace persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.eval.schemas import CaseEvidence, CaseResult
from app.eval.scoring import HIGH_VALUE_THRESHOLD

SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "csrf_token",
    "database_url",
    "password",
    "reasoning_content",
    "token",
}


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize(item) for item in value]
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sanitize(value), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def write_events(path: Path, evidence_by_case: dict[str, CaseEvidence]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as destination:
        for case_id, evidence in evidence_by_case.items():
            for event in evidence.events:
                record = sanitize({"case_id": case_id, **event})
                destination.write(json.dumps(record, ensure_ascii=False) + "\n")


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_report(results: list[CaseResult], evaluation_id: str) -> str:
    lines = [f"# Globuy 评测报告：{evaluation_id}", ""]
    for suite in ("offline", "live"):
        selected = [result for result in results if result.suite == suite]
        if not selected:
            continue
        completed = [result for result in selected if result.verdict in {"PASS", "FAIL"}]
        average = sum(result.score for result in completed) / len(completed) if completed else 0
        passed = sum(result.verdict == "PASS" for result in selected)
        lines.extend(
            [
                f"## {suite} 总览",
                "",
                f"{passed}/{len(selected)} PASS；已完成 case 平均分 {average:.3f}。",
                "",
                "| case | 描述 | 得分 | P0 | 结果 | 耗时 |",
                "|---|---|---:|---|---|---:|",
            ]
        )
        for result in selected:
            lines.append(
                f"| {_cell(result.case_id)} | {_cell(result.description)} | "
                f"{result.score:.3f} | {'通过' if result.p0_pass else '不通过'} | "
                f"{result.verdict} | {result.duration_ms} ms |"
            )
        lines.append("")

    for result in results:
        lines.extend([f"## {result.case_id}（{result.verdict}，{result.score:.3f}）", ""])
        if result.error:
            lines.append(f"执行错误：{result.error}")
            lines.append("")
        for item in result.criteria:
            mark = "PASS" if item.passed else "FAIL"
            lines.append(
                f"- [{item.level.upper()}][{mark}][{item.judge}] "
                f"{item.description}：{item.reason}"
            )
        lines.extend(
            [
                "",
                "<details><summary>对话记录</summary>",
                "",
                result.transcript,
                "",
                "</details>",
                "",
            ]
        )
    return "\n".join(lines)


def render_report_with_evidence(
    results: list[CaseResult],
    evidence_by_case: dict[str, CaseEvidence],
    evaluation_id: str,
) -> str:
    """Add trace correlation and tool latency to the human-readable report."""

    lines = [render_report(results, evaluation_id), "", "## 可观测性索引", ""]
    lines.extend(["| case | Trace IDs | 工具调用数 | 工具总耗时 |", "|---|---|---:|---:|"])
    for result in results:
        evidence = evidence_by_case[result.case_id]
        tool_ends = [event for event in evidence.events if event.get("event") == "TOOL_CALL_END"]
        total_ms = sum(
            int(event.get("data", {}).get("duration_ms") or 0) for event in tool_ends
        )
        trace_ids = "<br>".join(evidence.trace_ids) if evidence.trace_ids else "-"
        lines.append(
            f"| {_cell(result.case_id)} | {_cell(trace_ids)} | {len(tool_ends)} | {total_ms} ms |"
        )
    lines.append("")
    return "\n".join(lines)


def append_high_value_trace(
    path: Path,
    result: CaseResult,
    evidence: CaseEvidence,
    *,
    evaluation_id: str,
    manifest_fingerprint: dict[str, Any],
) -> bool:
    if result.verdict != "PASS" or not result.p0_pass or result.score < HIGH_VALUE_THRESHOLD:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    record = sanitize(
        {
            "evaluation_id": evaluation_id,
            "case_id": result.case_id,
            "score": result.score,
            "transcript": evidence.transcript,
            "tool_sequence": [
                item.get("data", {}).get("tool_name")
                for item in evidence.events
                if item.get("event") == "TOOL_CALL_START"
            ],
            "result": evidence.result,
            "trace_ids": evidence.trace_ids,
            "manifest": manifest_fingerprint,
            "training_use": False,
        }
    )
    with path.open("a", encoding="utf-8") as destination:
        destination.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True


__all__ = [
    "append_high_value_trace",
    "render_report",
    "render_report_with_evidence",
    "sanitize",
    "write_events",
    "write_json",
]
