from app.eval.collect_personalization_evidence import _wearing_style


def test_wearing_style_uses_explicit_title_evidence() -> None:
    assert _wearing_style("挂耳式不入耳无线耳机") == "open-ear"
    assert _wearing_style("主动降噪入耳式耳机") == "in-ear"
    assert _wearing_style("头戴式蓝牙耳机") == "over-ear"
    assert _wearing_style("无线蓝牙耳机") == "unspecified"
