"""Dynamic evaluation rubric generation."""

from pydantic import BaseModel, Field


class Criterion(BaseModel):
    name: str
    description: str
    weight: float = Field(gt=0, le=1)


class Rubric(BaseModel):
    task: str
    criteria: list[Criterion]


def build_rubric(task: str) -> Rubric:
    criteria = [
        Criterion(name="faithfulness", description="结论是否能由工具事实支持", weight=0.35),
        Criterion(name="constraint_fit", description="是否满足预算和偏好", weight=0.25),
        Criterion(name="completeness", description="是否覆盖关键比较维度", weight=0.20),
        Criterion(name="actionability", description="是否给出可执行下一步", weight=0.20),
    ]
    return Rubric(task=task, criteria=criteria)
