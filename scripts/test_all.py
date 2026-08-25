"""Run Globuy's bounded, no-paid-call commit verification pipeline."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/test-runs/latest"))
    parser.add_argument("--skip-frontend", action="store_true")
    parser.add_argument("--postgres-url", default=os.getenv("GLOBUY_TEST_POSTGRES_URL"))
    parser.add_argument("--command-timeout", type=int, default=300)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    output = (root / args.output).resolve() if not args.output.is_absolute() else args.output
    temp_dir = output / f"tmp-{uuid4().hex}"
    output.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "TMP": str(temp_dir),
            "TEMP": str(temp_dir),
            "GLOBUY_TEST_POSTGRES_URL": args.postgres_url or "",
        }
    )
    python = sys.executable
    commands: list[tuple[str, list[str], Path]] = [
        ("ruff", [python, "-m", "ruff", "check", "app", "tests", "scripts", "alembic"], root),
        ("compileall", [python, "-m", "compileall", "-q", "app", "scripts"], root),
        (
            "pytest",
            [
                python,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                f"--basetemp={temp_dir / 'pytest'}",
            ],
            root,
        ),
        (
            "offline-eval",
            [
                python,
                "scripts/eval_regression.py",
                "--suite",
                "offline",
                "--domain",
                "all",
                "--output",
                str(output / "eval"),
            ],
            root,
        ),
    ]
    if not args.skip_frontend:
        commands.extend(
            [
                ("frontend-test", ["npm.cmd", "test"], root / "frontend"),
                ("frontend-build", ["npm.cmd", "run", "build"], root / "frontend"),
            ]
        )

    results: list[dict[str, object]] = []
    failed = False
    for name, command, cwd in commands:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command, cwd=cwd, env=env, check=False, timeout=args.command_timeout
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
        result = {
            "name": name,
            "exit_code": exit_code,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
        results.append(result)
        failed = failed or exit_code != 0

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "paid_calls_allowed": False,
        "postgres_enabled": bool(args.postgres_url),
        "results": results,
        "status": "failed" if failed else "passed",
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    shutil.rmtree(temp_dir, ignore_errors=True)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
