import asyncio

from app.eval.collect_cancellation_evidence import collect


def test_collect_records_real_run_registry_cancellation(tmp_path) -> None:
    output = tmp_path / "records.jsonl"
    output.write_text("", encoding="utf-8")

    record = asyncio.run(collect(output))

    assert record["status"] == "cancelled"
    assert record["expect_cancelled"] is True
