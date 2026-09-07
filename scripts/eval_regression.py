#!/usr/bin/env python
"""Run Globuy offline contracts or opt-in live quality regression."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402
from app.eval.llm_judge import JudgeConfig, call_llm_judge  # noqa: E402
from app.eval.memory_runner import run_memory_suite  # noqa: E402
from app.eval.reporting import (  # noqa: E402
    append_high_value_trace,
    render_report_with_evidence,
    write_events,
    write_json,
)
from app.eval.runner import LiveEvaluationClient, fixture_evidence, load_case_file  # noqa: E402
from app.eval.schemas import CaseEvidence, CaseResult, EvaluationCase  # noqa: E402
from app.eval.scoring import score_evidence  # noqa: E402
from app.observability import ObservabilityManager  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _manifest(
    evaluation_id: str,
    cases_path: Path,
    suite: str,
    capabilities: dict[str, Any],
    judge: JudgeConfig | None,
) -> dict[str, Any]:
    settings = get_settings()
    prompt_path = PROJECT_ROOT / settings.prompt_file
    return {
        "schema_version": "1.0",
        "evaluation_id": evaluation_id,
        "created_at": datetime.now(UTC).isoformat(),
        "suite": suite,
        "git_sha": _git_sha(),
        "cases_sha256": _sha256(cases_path),
        "prompt_sha256": _sha256(prompt_path) if prompt_path.exists() else None,
        "main_model": settings.llm_model,
        "judge_model": judge.model if judge else None,
        "product_search_backend": settings.product_search_backend,
        "candidate_embedding_model": settings.candidate_embedding_model_name,
        "candidate_embedding_revision": settings.candidate_embedding_model_revision,
        "candidate_embedding_dimensions": settings.candidate_embedding_dimensions,
        "memory_store_backend": settings.memory_store_backend,
        "capabilities": capabilities,
        "external_calls_authorized": False,
        "training_use": False,
    }


async def _judge_case(
    case: EvaluationCase,
    evidence: CaseEvidence,
    judge_config: JudgeConfig | None,
    observability: ObservabilityManager | None = None,
) -> tuple[dict[str, tuple[bool, str]] | None, str | None]:
    has_llm = any(
        criterion.judge == "llm"
        for level in ("p1", "p2")
        for criterion in getattr(case.rubric, level)
    )
    if not has_llm or judge_config is None:
        return None, None
    try:
        return await call_llm_judge(
            case,
            evidence,
            judge_config,
            observability=observability,
        ), None
    except Exception as exc:  # noqa: BLE001 - report the case and continue the suite
        return None, f"LLM Judge 失败：{type(exc).__name__}: {exc}"


async def _score(
    case: EvaluationCase,
    evidence: CaseEvidence,
    judge_config: JudgeConfig | None,
    observability: ObservabilityManager | None = None,
) -> CaseResult:
    judged, judge_error = await _judge_case(
        case, evidence, judge_config, observability
    )
    result = score_evidence(case, evidence, llm_results=judged)
    if judge_error:
        return result.model_copy(update={"verdict": "ERROR", "error": judge_error})
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("offline", "live"), default="offline")
    parser.add_argument(
        "--domain", choices=("shopping", "memory", "all"), default="shopping"
    )
    parser.add_argument("--cases", type=Path, default=PROJECT_ROOT / "eval" / "cases.yaml")
    parser.add_argument(
        "--memory-cases",
        type=Path,
        default=PROJECT_ROOT / "eval" / "memory-cases.yaml",
    )
    parser.add_argument("--only", help="只运行指定 case id")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--allow-external-tools", action="store_true")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument(
        "--publish-langfuse-scores",
        action="store_true",
        help="显式把 live case 分数关联到对应 LangFuse Trace",
    )
    args = parser.parse_args()

    evaluation_id = f"{args.suite}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    output_dir = args.output or PROJECT_ROOT / "output" / "eval" / evaluation_id
    memory_pass = True
    if args.domain == "all" or (args.domain == "memory" and args.suite == "offline"):
        memory_pass = await run_memory_suite(
            args.memory_cases,
            output_dir / "memory",
            database_url=os.getenv("GLOBUY_TEST_POSTGRES_URL"),
        )
        if args.domain == "memory":
            print(f"记忆评测报告已写入：{output_dir / 'memory' / 'report.md'}")
            return 0 if memory_pass else 1

    case_file = load_case_file(args.cases)
    cases = [case for case in case_file.cases if case.suite == args.suite]
    if args.domain == "memory":
        cases = [case for case in cases if case.id.startswith("live-memory-")]
    elif args.domain == "shopping":
        cases = [case for case in cases if not case.id.startswith("live-memory-")]
    if args.only:
        cases = [case for case in cases if case.id == args.only]
    if not cases:
        raise SystemExit("没有匹配的评测 case")
    if args.suite == "live" and not args.allow_model_calls:
        raise SystemExit("拒绝运行 live 评测：请显式添加 --allow-model-calls")
    if args.publish_langfuse_scores and args.suite != "live":
        raise SystemExit("--publish-langfuse-scores 仅允许用于 live 评测")

    # Evaluation directories are intentionally reusable so a failed preflight
    # can be retried without deleting the earlier diagnostic artifacts.
    output_dir.mkdir(parents=True, exist_ok=True)
    judge_config = JudgeConfig.from_env() if args.judge else None
    if args.judge and judge_config is None:
        print("警告：Judge 未配置，含 LLM criterion 的 case 将标为 PARTIAL", flush=True)
    observability = (
        ObservabilityManager(get_settings())
        if args.judge or args.publish_langfuse_scores
        else None
    )

    capabilities: dict[str, Any] = {"mode": "fixture", "external_tools": False}
    evidence_by_case: dict[str, CaseEvidence] = {}
    results: list[CaseResult] = []
    if args.suite == "offline":
        for case in cases:
            evidence = fixture_evidence(case)
            evidence_by_case[case.id] = evidence
            results.append(await _score(case, evidence, None))
    else:
        async with LiveEvaluationClient(args.base_url) as live:
            capabilities = await live.health()
            if any(case.requirements.model for case in cases) and capabilities.get(
                "model_provider"
            ) != "openai-compatible":
                raise SystemExit(
                    "拒绝运行 live 质量评测：case 需要真实模型，但服务端不是 "
                    "openai-compatible 模式"
                )
            external_enabled = bool(
                capabilities.get("product_provider_configured")
                or capabilities.get("web_search_configured")
            )
            if external_enabled and not args.allow_external_tools:
                raise SystemExit(
                    "拒绝运行 live 评测：服务端外部工具已配置；"
                    "请关闭它们或显式添加 --allow-external-tools"
                )
            await live.login()
            for case in cases:
                print(f"== 评测 {case.id} ...", flush=True)
                evidence = await live.run_case(case)
                evidence_by_case[case.id] = evidence
                result = await _score(case, evidence, judge_config, observability)
                results.append(result)
                print(f"   -> {result.verdict} ({result.score:.3f})", flush=True)

    published_scores = 0
    if args.publish_langfuse_scores:
        if observability is None or not observability.enabled:
            raise SystemExit("LangFuse 未正确配置，拒绝发布评测分数")
        for result in results:
            for trace_id in evidence_by_case[result.case_id].trace_ids:
                published_scores += int(
                    observability.publish_score(
                        trace_id=trace_id,
                        name="globuy.eval.score",
                        value=result.score,
                        comment=f"{evaluation_id}/{result.case_id}",
                        metadata={"case_id": result.case_id, "verdict": result.verdict},
                    )
                )
                published_scores += int(
                    observability.publish_score(
                        trace_id=trace_id,
                        name="globuy.eval.p0_pass",
                        value=1.0 if result.p0_pass else 0.0,
                        data_type="BOOLEAN",
                        comment=f"{evaluation_id}/{result.case_id}",
                    )
                )
    if observability is not None:
        await observability.shutdown()

    manifest = _manifest(evaluation_id, args.cases, args.suite, capabilities, judge_config)
    manifest["domain"] = args.domain
    if args.domain == "all":
        manifest["memory_cases_sha256"] = _sha256(args.memory_cases)
        manifest["memory_p0_pass"] = memory_pass
    manifest["external_calls_authorized"] = bool(
        args.allow_model_calls
        or args.allow_external_tools
        or args.judge
        or args.publish_langfuse_scores
    )
    manifest["langfuse_scores_published"] = published_scores
    write_json(output_dir / "manifest.json", manifest)
    write_events(output_dir / "events.jsonl", evidence_by_case)
    write_json(
        output_dir / "case-results.json",
        [result.model_dump(mode="json") for result in results],
    )
    report = render_report_with_evidence(results, evidence_by_case, evaluation_id)
    if args.domain == "all":
        memory_report = (output_dir / "memory" / "report.md").read_text(encoding="utf-8")
        report = f"{report.rstrip()}\n\n---\n\n{memory_report}"
    (output_dir / "report.md").write_text(report, encoding="utf-8")
    trace_path = PROJECT_ROOT / "output" / "eval" / "accepted-traces.jsonl"
    for result in results:
        append_high_value_trace(
            trace_path,
            result,
            evidence_by_case[result.case_id],
            evaluation_id=evaluation_id,
            manifest_fingerprint={
                key: manifest[key]
                for key in ("git_sha", "cases_sha256", "prompt_sha256", "main_model")
            },
        )
    print(f"报告已写入：{output_dir / 'report.md'}")
    return 0 if memory_pass and all(result.verdict == "PASS" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
