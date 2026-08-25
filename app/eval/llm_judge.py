"""Optional independent LLM judge for semantic P1/P2 criteria."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings
from app.eval.schemas import CaseEvidence, EvaluationCase
from app.observability.manager import ObservabilityManager
from app.observability.metrics import context_metrics, normalize_generation_usage
from app.observability.redaction import summarize

JUDGE_SYSTEM_PROMPT = """你是严格的购物 Agent 质量评测员。
你只评估给定的 P1 行为命中和 P2 表达标准；P0 事实与安全由程序判定，不属于你的职责。
只能依据对话、结构化结果、脱敏工具轨迹和前置事实判断。证据不足时判定不通过。
不要补充商品事实，不要输出思维链。只输出以下结构的 JSON：
{"results":[{"criterion_id":"...","reason":"简短证据结论","passed":true}]}。"""


class JudgeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    reason: str = Field(min_length=1, max_length=1000)
    passed: bool


class JudgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[JudgeItem]


@dataclass(frozen=True)
class JudgeConfig:
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float = 120.0
    max_retries: int = 3
    retry_base_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> JudgeConfig | None:
        # Settings loads the repository-local, gitignored .env and still gives
        # real process environment variables the normal higher precedence.
        settings = Settings()
        model = (settings.eval_judge_model or "").strip()
        base_url = (settings.eval_judge_base_url or "").strip()
        api_key = (
            settings.eval_judge_api_key.get_secret_value().strip()
            if settings.eval_judge_api_key is not None
            else ""
        )
        if not model or not base_url or not api_key:
            return None
        return cls(
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=settings.eval_judge_timeout_seconds,
        )


class JudgeProtocolError(RuntimeError):
    """Raised when the judge returns an invalid or incomplete rubric result."""


def _criteria(case: EvaluationCase) -> list[dict[str, str]]:
    return [
        {"criterion_id": criterion.id, "level": level, "description": criterion.description}
        for level in ("p1", "p2")
        for criterion in getattr(case.rubric, level)
        if criterion.judge == "llm"
    ]


def _evidence_payload(case: EvaluationCase, evidence: CaseEvidence) -> dict[str, Any]:
    tools = []
    for item in evidence.events:
        if item.get("event") not in {"TOOL_CALL_START", "TOOL_CALL_END"}:
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        tools.append(
            {
                "event": item.get("event"),
                "sequence": item.get("sequence"),
                "tool_name": data.get("tool_name"),
                "status": data.get("status"),
            }
        )
    return {
        "prior_context": case.prior_context,
        "transcript": evidence.transcript,
        "structured_result": evidence.result,
        "tool_trace": tools,
        "criteria": _criteria(case),
    }


def _parse_response(payload: dict[str, Any], expected_ids: set[str]) -> dict[str, tuple[bool, str]]:
    try:
        content = payload["choices"][0]["message"]["content"]
        parsed = JudgeResponse.model_validate_json(content)
    except (KeyError, IndexError, TypeError, ValidationError) as exc:
        raise JudgeProtocolError("LLM Judge 响应不符合严格 JSON 协议") from exc
    actual_ids = [item.criterion_id for item in parsed.results]
    if len(actual_ids) != len(set(actual_ids)):
        raise JudgeProtocolError("LLM Judge 返回重复 criterion_id")
    if set(actual_ids) != expected_ids:
        raise JudgeProtocolError(
            f"LLM Judge criterion_id 不匹配：expected={sorted(expected_ids)}, "
            f"actual={sorted(actual_ids)}"
        )
    return {item.criterion_id: (item.passed, item.reason) for item in parsed.results}


async def call_llm_judge(
    case: EvaluationCase,
    evidence: CaseEvidence,
    config: JudgeConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    observability: ObservabilityManager | None = None,
) -> dict[str, tuple[bool, str]]:
    expected_ids = {item["criterion_id"] for item in _criteria(case)}
    if not expected_ids:
        return {}
    body = {
        "model": config.model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(_evidence_payload(case, evidence), ensure_ascii=False),
            },
        ],
    }
    endpoint = f"{config.base_url.rstrip('/')}/chat/completions"
    last_error: Exception | None = None
    trace_id = evidence.trace_ids[-1] if evidence.trace_ids else None
    messages = [
        SystemMessage(content=JUDGE_SYSTEM_PROMPT),
        HumanMessage(content=body["messages"][1]["content"]),
    ]
    scope = (
        observability.observe_generation(
            trace_id=trace_id,
            name="eval.judge",
            model=config.model,
            input=summarize(body["messages"]),
            metadata={"model_role": "judge", **context_metrics(messages).metadata()},
        )
        if observability is not None and trace_id is not None
        else nullcontext(None)
    )
    with scope as generation:
        async with httpx.AsyncClient(
            transport=transport, timeout=config.timeout_seconds
        ) as client:
            for attempt in range(config.max_retries):
                try:
                    response = await client.post(
                        endpoint,
                        headers={"Authorization": f"Bearer {config.api_key}"},
                        json=body,
                    )
                    if response.status_code == 429 or response.status_code >= 500:
                        response.raise_for_status()
                    if response.status_code >= 400:
                        raise JudgeProtocolError(
                            f"LLM Judge 请求失败：HTTP {response.status_code}"
                        )
                    payload = response.json()
                    result = _parse_response(payload, expected_ids)
                    if generation is not None:
                        usage = normalize_generation_usage(payload.get("usage"))
                        generation.update(
                            output=summarize(payload.get("choices", [])),
                            usage_details=usage.langfuse_usage_details(),
                            metadata={
                                "model_role": "judge",
                                **context_metrics(messages).metadata(),
                                **usage.metadata(),
                            },
                        )
                    return result
                except JudgeProtocolError as exc:
                    if generation is not None:
                        generation.update(
                            level="ERROR",
                            status_message=type(exc).__name__,
                            metadata={"model_role": "judge", "status": "validation_error"},
                        )
                    raise
                except (
                    httpx.TimeoutException,
                    httpx.NetworkError,
                    httpx.HTTPStatusError,
                ) as exc:
                    last_error = exc
                    retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                        exc.response.status_code == 429
                        or exc.response.status_code >= 500
                    )
                    if not retryable or attempt == config.max_retries - 1:
                        if generation is not None:
                            generation.update(
                                level="ERROR",
                                status_message=type(exc).__name__,
                                metadata={
                                    "model_role": "judge",
                                    "status": (
                                        "timeout"
                                        if isinstance(exc, httpx.TimeoutException)
                                        else "provider_error"
                                    ),
                                },
                            )
                        raise
                    await asyncio.sleep(config.retry_base_seconds * (2**attempt))
    raise last_error or RuntimeError("LLM Judge 重试耗尽")


__all__ = ["JudgeConfig", "JudgeProtocolError", "call_llm_judge"]
