from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.server import create_app
from app.config import Settings
from app.eval.llm_judge import JudgeConfig, JudgeProtocolError, call_llm_judge
from app.eval.reporting import append_high_value_trace, render_report, sanitize
from app.eval.runner import fixture_evidence, load_case_file
from app.eval.schemas import CaseEvidence, EvaluationCase
from app.eval.scoring import score_evidence


def case_payload(*, llm: bool = False) -> dict:
    p1_judge = "llm" if llm else "deterministic"
    p2_judge = "llm" if llm else "deterministic"
    return {
        "id": "eval-case",
        "description": "评测用例",
        "suite": "live" if llm else "offline",
        "turns": [{"query": "帮我选一个商品"}],
        "rubric": {
            "p0": [
                {
                    "id": "facts_ok",
                    "description": "商品事实一致",
                    "judge": "deterministic",
                    "assertion": {"type": "facts_match_catalog"},
                }
            ],
            "p1": [
                {
                    "id": "behavior_ok",
                    "description": "行为符合需求",
                    "judge": p1_judge,
                    **(
                        {}
                        if llm
                        else {"assertion": {"type": "terminal_status", "value": ["complete"]}}
                    ),
                }
            ],
            "p2": [
                {
                    "id": "clear_text",
                    "description": "表达清楚",
                    "judge": p2_judge,
                    **(
                        {}
                        if llm
                        else {"assertion": {"type": "final_text_nonempty"}}
                    ),
                }
            ],
        },
        **(
            {}
            if llm
            else {
                "fixture": {
                    "execution_status": "succeeded",
                    "terminal_status": "complete",
                    "result": {
                        "status": "complete",
                        "final_text": "推荐测试商品，价格 99 元。",
                        "picks": [
                            {
                                "item_id": "item-1",
                                "offer_id": "offer-1",
                                "platform": "taobao",
                                "title": "测试商品",
                                "price": 99,
                                "currency": "CNY",
                                "product_url": "https://example.invalid/item-1",
                            }
                        ],
                    },
                    "catalog": [
                        {
                            "item_id": "item-1",
                            "offer_id": "offer-1",
                            "platform": "taobao",
                            "title": "测试商品",
                            "price": 99,
                            "currency": "CNY",
                            "product_url": "https://example.invalid/item-1",
                        }
                    ],
                }
            }
        ),
    }


def test_case_schema_rejects_duplicate_ids_and_llm_p0() -> None:
    payload = case_payload()
    payload["rubric"]["p1"][0]["id"] = "facts_ok"
    with pytest.raises(ValidationError, match="duplicate criterion"):
        EvaluationCase.model_validate(payload)

    payload = case_payload()
    payload["rubric"]["p0"][0] = {
        "id": "unsafe_p0",
        "description": "不允许",
        "judge": "llm",
    }
    with pytest.raises(ValidationError, match="P0 criteria"):
        EvaluationCase.model_validate(payload)


def test_case_file_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    payload = {"schema_version": "1.0", "cases": [case_payload(), case_payload()]}
    path = tmp_path / "cases.yaml"
    import yaml

    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="case ids must be unique"):
        load_case_file(path)


def test_deterministic_fact_tampering_is_a_hard_failure() -> None:
    case = EvaluationCase.model_validate(case_payload())
    evidence = fixture_evidence(case)
    clean = score_evidence(case, evidence)
    assert clean.verdict == "PASS"

    tampered = evidence.model_copy(deep=True)
    tampered.result["picks"][0]["price"] = 199
    result = score_evidence(case, tampered)
    assert result.verdict == "FAIL"
    assert result.p0_pass is False
    assert "价格与事实不一致" in result.criteria[0].reason


def test_missing_llm_judge_is_partial_not_pass() -> None:
    case = EvaluationCase.model_validate(case_payload(llm=True))
    evidence = CaseEvidence(
        execution_status="succeeded",
        terminal_status="complete",
        result={"final_text": "没有推荐商品。", "picks": []},
    )
    result = score_evidence(case, evidence)
    assert result.verdict == "PARTIAL"
    assert result.p0_pass is True


@pytest.mark.asyncio
async def test_llm_judge_validates_exact_criterion_ids_and_retries() -> None:
    case = EvaluationCase.model_validate(case_payload(llm=True))
    evidence = CaseEvidence(
        execution_status="succeeded",
        terminal_status="complete",
        result={"final_text": "回答", "picks": []},
        transcript="[用户] 问题\n[Globuy] 回答",
    )
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": "limited"})
        content = json.dumps(
            {
                "results": [
                    {"criterion_id": "behavior_ok", "reason": "命中", "passed": True},
                    {"criterion_id": "clear_text", "reason": "清楚", "passed": True},
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    result = await call_llm_judge(
        case,
        evidence,
        JudgeConfig(
            model="judge-test",
            base_url="https://judge.invalid/v1",
            api_key="secret",
            retry_base_seconds=0,
        ),
        transport=httpx.MockTransport(handler),
    )
    assert calls == 2
    assert result["behavior_ok"][0] is True


@pytest.mark.asyncio
async def test_llm_judge_rejects_missing_or_extra_criteria() -> None:
    case = EvaluationCase.model_validate(case_payload(llm=True))
    evidence = CaseEvidence(execution_status="succeeded", result={})
    content = json.dumps(
        {"results": [{"criterion_id": "unexpected", "reason": "x", "passed": True}]}
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    with pytest.raises(JudgeProtocolError, match="criterion_id 不匹配"):
        await call_llm_judge(
            case,
            evidence,
            JudgeConfig(model="j", base_url="https://judge.invalid", api_key="secret"),
            transport=httpx.MockTransport(handler),
        )


def test_reporting_redacts_secrets_and_only_collects_high_value(tmp_path: Path) -> None:
    case = EvaluationCase.model_validate(case_payload())
    evidence = fixture_evidence(case)
    result = score_evidence(case, evidence)
    assert "secret" not in json.dumps(sanitize({"api_key": "secret"}))
    report = render_report([result], "eval-1")
    assert "1/1 PASS" in report
    trace = tmp_path / "traces.jsonl"
    assert append_high_value_trace(
        trace,
        result,
        evidence,
        evaluation_id="eval-1",
        manifest_fingerprint={"api_key": "secret"},
    )
    assert "secret" not in trace.read_text(encoding="utf-8")
    assert json.loads(trace.read_text(encoding="utf-8"))["training_use"] is False


def test_health_exposes_safe_evaluation_preflight_flags(tmp_path: Path) -> None:
    async def fake_runner(query: str, _thread_id: str) -> tuple[str, dict]:
        return query, {}

    settings = Settings(
        model_provider="mock",
        product_provider="none",
        web_search_provider="none",
        redis_url=None,
        legacy_sqlite_enabled=True,
        session_db_path=tmp_path / "sessions.sqlite3",
        output_dir=tmp_path / "output",
    )
    with TestClient(create_app(settings=settings, agent_runner=fake_runner)) as client:
        payload = client.get("/healthz").json()
    assert payload["product_provider_configured"] is False
    assert payload["web_search_configured"] is False
    assert payload["category_cache_enabled"] is False
    assert "api_key" not in payload
