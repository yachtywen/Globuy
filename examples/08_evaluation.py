"""Chapter 8: build a rubric and score a saved answer."""

from pprint import pprint

from app.eval import build_rubric, judge_answer

answer = (
    "根据工具返回的来源与价格，候选 A 更符合 1000 元预算。"
    "目前运费未知，建议下单前重新核验库存、总价和售后政策。"
) * 5
rubric = build_rubric("选择一款通勤降噪耳机")
pprint(rubric.model_dump())
pprint(judge_answer(answer, rubric).model_dump())
