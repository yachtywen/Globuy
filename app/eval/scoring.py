"""Deterministic hard gates and weighted evaluation scoring."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.eval.schemas import (
    CaseEvidence,
    CaseResult,
    CriterionResult,
    EvalCriterion,
    EvaluationCase,
)

LEVEL_WEIGHTS = {"p0": 0.50, "p1": 0.35, "p2": 0.15}
PASS_THRESHOLD = 0.70
HIGH_VALUE_THRESHOLD = 0.90
TERMINAL_EVENTS = {"RUN_FINISHED", "RUN_ERROR", "TASK_CANCELLED"}
_URL_RE = re.compile(r"https?://[^\s<>\]\[()]+")
_AMOUNT_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d{1,2})?)\s*(?:元|CNY|人民币)", re.I)


def _picks(evidence: CaseEvidence) -> list[dict[str, Any]]:
    value = evidence.result.get("picks", [])
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _final_text(evidence: CaseEvidence) -> str:
    return str(evidence.result.get("final_text") or "")


def _tool_names(events: Iterable[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for item in events:
        if item.get("event") != "TOOL_CALL_START":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        name = data.get("tool_name")
        if isinstance(name, str):
            names.append(name)
    return names


def _as_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return []


def _fact_for_pick(pick: dict[str, Any], evidence: CaseEvidence) -> Any | None:
    for fact in evidence.catalog:
        if pick.get("offer_id") and pick.get("offer_id") == fact.offer_id:
            return fact
        if pick.get("product_id") and pick.get("product_id") == fact.product_id:
            return fact
        if pick.get("item_id") == fact.item_id and pick.get("platform") == fact.platform:
            return fact
    return None


def _facts_match(evidence: CaseEvidence) -> tuple[bool, str]:
    picks = _picks(evidence)
    if not picks:
        return True, "没有结构化商品需要核验"
    if not evidence.catalog:
        return False, "缺少商品事实快照，无法核验结构化结果"
    for pick in picks:
        fact = _fact_for_pick(pick, evidence)
        if fact is None:
            return False, f"商品 {pick.get('item_id', '<unknown>')} 无可追溯事实"
        checks = {
            "title": fact.title,
            "platform": fact.platform,
            "currency": fact.currency,
            "product_url": fact.product_url,
        }
        for field, expected in checks.items():
            if expected is not None and pick.get(field) != expected:
                return False, f"{fact.item_id} 的 {field} 与事实不一致"
        try:
            if abs(float(pick.get("price")) - fact.price) > 0.01:
                return False, f"{fact.item_id} 的价格与事实不一致"
        except (TypeError, ValueError):
            return False, f"{fact.item_id} 的价格无效"
    return True, f"{len(picks)} 个商品均可追溯且字段一致"


def _grounded_amounts(evidence: CaseEvidence) -> tuple[bool, str]:
    amounts = [float(item) for item in _AMOUNT_RE.findall(_final_text(evidence))]
    allowed = {round(fact.price, 2) for fact in evidence.catalog}
    allowed.update(round(float(pick["price"]), 2) for pick in _picks(evidence) if "price" in pick)
    unknown = [amount for amount in amounts if round(amount, 2) not in allowed]
    if unknown:
        return False, f"回答包含未由商品事实支持的金额：{unknown[:5]}"
    return True, f"核验了 {len(amounts)} 个人民币金额"


def _grounded_urls(evidence: CaseEvidence) -> tuple[bool, str]:
    urls = {url.rstrip(".,，。") for url in _URL_RE.findall(_final_text(evidence))}
    allowed = {fact.product_url for fact in evidence.catalog if fact.product_url}
    allowed.update(
        pick.get("product_url") for pick in _picks(evidence) if pick.get("product_url")
    )
    unknown = sorted(urls - allowed)
    if unknown:
        return False, f"回答包含未由商品事实支持的链接：{unknown[:3]}"
    return True, f"核验了 {len(urls)} 个来源链接"


def evaluate_assertion(criterion: EvalCriterion, evidence: CaseEvidence) -> tuple[bool, str]:
    assertion = criterion.assertion
    if assertion is None:
        return False, "缺少确定性断言"
    kind, value = assertion.type, assertion.value
    picks = _picks(evidence)
    text = _final_text(evidence)
    if kind == "terminal_status":
        expected = set(_as_strings(value))
        passed = evidence.terminal_status in expected
        return passed, f"终态={evidence.terminal_status}，期望={sorted(expected)}"
    if kind == "terminal_event_once":
        terminal = [item for item in evidence.events if item.get("event") in TERMINAL_EVENTS]
        by_run: dict[str, int] = {}
        for item in terminal:
            run_id = str(item.get("run_id") or "unknown")
            by_run[run_id] = by_run.get(run_id, 0) + 1
        passed = bool(by_run) and all(count == 1 for count in by_run.values())
        return passed, f"各 run 终态事件计数={by_run}"
    if kind == "required_tools":
        actual, expected = set(_tool_names(evidence.events)), set(_as_strings(value))
        missing = sorted(expected - actual)
        return not missing, "工具齐全" if not missing else f"缺少工具：{missing}"
    if kind == "forbidden_tools":
        actual, forbidden = set(_tool_names(evidence.events)), set(_as_strings(value))
        used = sorted(actual & forbidden)
        return not used, "未调用禁止工具" if not used else f"调用了禁止工具：{used}"
    if kind == "max_price":
        try:
            limit = float(value)
        except (TypeError, ValueError):
            return False, "max_price 配置无效"
        over = [pick.get("item_id") for pick in picks if float(pick.get("price", -1)) > limit]
        return not over, f"预算上限 {limit:g}，超限商品={over}"
    if kind == "platform_count":
        expected = int(value)
        actual = len({pick.get("platform") for pick in picks if pick.get("platform")})
        return actual >= expected, f"平台数={actual}，最低要求={expected}"
    if kind == "picks_empty":
        expected = bool(value if value is not None else True)
        passed = (len(picks) == 0) is expected
        return passed, f"结构化商品数量={len(picks)}"
    if kind == "facts_match_catalog":
        return _facts_match(evidence)
    if kind == "final_text_nonempty":
        return bool(text.strip()), f"最终回答字符数={len(text.strip())}"
    if kind == "contains_text":
        expected = _as_strings(value)
        missing = [item for item in expected if item not in text]
        return not missing, "包含期望文本" if not missing else f"缺少文本：{missing}"
    if kind == "forbids_text":
        forbidden = [item for item in _as_strings(value) if item in text]
        return not forbidden, "未出现禁止文本" if not forbidden else f"出现禁止文本：{forbidden}"
    if kind == "amounts_are_grounded":
        return _grounded_amounts(evidence)
    if kind == "urls_are_grounded":
        return _grounded_urls(evidence)
    return False, f"不支持的断言类型：{kind}"


def score_evidence(
    case: EvaluationCase,
    evidence: CaseEvidence,
    *,
    llm_results: dict[str, tuple[bool, str]] | None = None,
) -> CaseResult:
    criteria: list[CriterionResult] = []
    missing_llm = False
    for level in ("p0", "p1", "p2"):
        for criterion in getattr(case.rubric, level):
            if criterion.judge == "deterministic":
                passed, reason = evaluate_assertion(criterion, evidence)
            elif llm_results is None or criterion.id not in llm_results:
                passed, reason, missing_llm = False, "LLM Judge 未执行或缺少结果", True
            else:
                passed, reason = llm_results[criterion.id]
            criteria.append(
                CriterionResult(
                    criterion_id=criterion.id,
                    level=level,
                    description=criterion.description,
                    passed=passed,
                    reason=reason,
                    judge=criterion.judge,
                )
            )

    ratios: dict[str, float] = {}
    for level in ("p0", "p1", "p2"):
        items = [item for item in criteria if item.level == level]
        ratios[level] = sum(item.passed for item in items) / len(items)
    score = round(sum(LEVEL_WEIGHTS[level] * ratios[level] for level in LEVEL_WEIGHTS), 4)
    p0_pass = ratios["p0"] == 1.0
    if evidence.execution_status == "timeout":
        verdict = "TIMEOUT"
    elif evidence.execution_status in {"error", "failed"}:
        verdict = "ERROR"
    elif missing_llm:
        verdict = "PARTIAL"
    else:
        verdict = "PASS" if p0_pass and score >= PASS_THRESHOLD else "FAIL"
    return CaseResult(
        case_id=case.id,
        description=case.description,
        suite=case.suite,
        verdict=verdict,
        score=score,
        p0_pass=p0_pass,
        criteria=criteria,
        duration_ms=evidence.duration_ms,
        transcript=evidence.transcript,
        error=evidence.error,
    )


__all__ = [
    "HIGH_VALUE_THRESHOLD",
    "LEVEL_WEIGHTS",
    "PASS_THRESHOLD",
    "evaluate_assertion",
    "score_evidence",
]
