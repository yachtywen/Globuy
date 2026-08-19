"""Strict contracts shared by evaluation cases, evidence and reports."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EvaluationSuite = Literal["offline", "live"]
JudgeKind = Literal["deterministic", "llm"]
CriterionLevel = Literal["p0", "p1", "p2"]
Verdict = Literal["PASS", "FAIL", "ERROR", "TIMEOUT", "PARTIAL", "SKIP"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssertionSpec(StrictModel):
    type: Literal[
        "terminal_status",
        "terminal_event_once",
        "required_tools",
        "forbidden_tools",
        "max_price",
        "platform_count",
        "picks_empty",
        "facts_match_catalog",
        "final_text_nonempty",
        "contains_text",
        "forbids_text",
        "amounts_are_grounded",
        "urls_are_grounded",
    ]
    value: Any = None


class EvalCriterion(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    description: str = Field(min_length=1, max_length=500)
    judge: JudgeKind
    assertion: AssertionSpec | None = None

    @model_validator(mode="after")
    def deterministic_requires_assertion(self) -> EvalCriterion:
        if self.judge == "deterministic" and self.assertion is None:
            raise ValueError("deterministic criterion requires an assertion")
        if self.judge == "llm" and self.assertion is not None:
            raise ValueError("llm criterion must not define an assertion")
        return self


class EvalRubric(StrictModel):
    p0: list[EvalCriterion] = Field(min_length=1)
    p1: list[EvalCriterion] = Field(min_length=1)
    p2: list[EvalCriterion] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_levels_and_ids(self) -> EvalRubric:
        seen: set[str] = set()
        for level in ("p0", "p1", "p2"):
            for criterion in getattr(self, level):
                if criterion.id in seen:
                    raise ValueError(f"duplicate criterion id: {criterion.id}")
                seen.add(criterion.id)
                if level == "p0" and criterion.judge != "deterministic":
                    raise ValueError("P0 criteria must use deterministic judging")
        return self


class EvalTurn(StrictModel):
    query: str = Field(min_length=1, max_length=20_000)


class MemorySetup(StrictModel):
    category: Literal["blacklist", "preference", "history"]
    key: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(default=1.0, ge=0, le=1)


class EvalRequirements(StrictModel):
    model: bool = False
    item_index: bool = False
    category_index: bool = False
    external_tools: bool = False


class EvaluationCase(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    description: str = Field(min_length=1, max_length=300)
    suite: EvaluationSuite
    requirements: EvalRequirements = Field(default_factory=EvalRequirements)
    timeout_seconds: float = Field(default=300, gt=0, le=1800)
    prior_context: str = Field(default="", max_length=8000)
    setup_memories: list[MemorySetup] = Field(default_factory=list)
    turns: list[EvalTurn] = Field(min_length=1)
    rubric: EvalRubric
    fixture: dict[str, Any] | None = None

    @model_validator(mode="after")
    def offline_requires_fixture(self) -> EvaluationCase:
        if self.suite == "offline" and self.fixture is None:
            raise ValueError("offline cases require fixture evidence")
        return self


class EvaluationCaseFile(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    cases: list[EvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_case_ids(self) -> EvaluationCaseFile:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
        return self


class CatalogFact(StrictModel):
    item_id: str
    product_id: str | None = None
    offer_id: str | None = None
    platform: str
    title: str
    price: float
    currency: str = "CNY"
    product_url: str | None = None


class CaseEvidence(StrictModel):
    execution_status: Literal["succeeded", "failed", "cancelled", "timeout", "error"]
    terminal_status: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    catalog: list[CatalogFact] = Field(default_factory=list)
    transcript: str = ""
    duration_ms: int = Field(default=0, ge=0)
    error: str | None = None


class CriterionResult(StrictModel):
    criterion_id: str
    level: CriterionLevel
    description: str
    passed: bool
    reason: str
    judge: JudgeKind


class CaseResult(StrictModel):
    case_id: str
    description: str
    suite: EvaluationSuite
    verdict: Verdict
    score: float = Field(ge=0, le=1)
    p0_pass: bool
    criteria: list[CriterionResult]
    duration_ms: int = Field(ge=0)
    transcript: str = ""
    error: str | None = None


__all__ = [
    "AssertionSpec",
    "CaseEvidence",
    "CaseResult",
    "CatalogFact",
    "CriterionResult",
    "EvalCriterion",
    "EvalRubric",
    "EvaluationCase",
    "EvaluationCaseFile",
    "EvaluationSuite",
]
