"""A deterministic baseline judge; replaceable with an LLM judge later."""

from pydantic import BaseModel, Field

from app.eval.rubric import Rubric


class JudgeResult(BaseModel):
    total_score: float = Field(ge=0, le=1)
    criterion_scores: dict[str, float]
    feedback: list[str]


def judge_answer(answer: str, rubric: Rubric) -> JudgeResult:
    text = answer.strip()
    signals = {
        "faithfulness": any(word in text for word in ("来源", "工具", "未核验", "未知")),
        "constraint_fit": any(word in text for word in ("预算", "偏好", "需求")),
        "completeness": len(text) >= 120,
        "actionability": any(word in text for word in ("建议", "下一步", "下单前")),
    }
    scores = {
        criterion.name: 1.0 if signals.get(criterion.name) else 0.0 for criterion in rubric.criteria
    }
    total = sum(criterion.weight * scores[criterion.name] for criterion in rubric.criteria)
    feedback = [
        f"需要增强：{criterion.description}"
        for criterion in rubric.criteria
        if scores[criterion.name] == 0
    ]
    return JudgeResult(
        total_score=round(total, 4),
        criterion_scores=scores,
        feedback=feedback,
    )
