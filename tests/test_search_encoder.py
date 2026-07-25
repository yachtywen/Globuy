import sys
from types import SimpleNamespace

import pytest

from app.search import encoder as encoder_module


def test_bge_m3_uses_legacy_pooling_compatibility_fallback(monkeypatch) -> None:
    class FakeModel:
        max_seq_length = 0

        def _first_module(self):
            return SimpleNamespace(auto_model=SimpleNamespace(config=SimpleNamespace()))

    fake_model = FakeModel()
    fake_transformers = SimpleNamespace(SentenceTransformer=object())
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_transformers)
    monkeypatch.setattr(
        encoder_module,
        "_load_local_first",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            TypeError("Pooling.__init__() missing required positional arguments")
        ),
    )
    monkeypatch.setattr(
        encoder_module,
        "_load_bge_m3_compat",
        lambda *args, **kwargs: fake_model,
    )
    settings = SimpleNamespace(
        embedding_device="cpu",
        embedding_model_name="BAAI/bge-m3",
        embedding_model_revision="revision",
        embedding_max_length=512,
    )

    model = encoder_module.BgeM3Encoder(settings)._load()

    assert model is fake_model
    assert model.max_seq_length == 512


def test_non_bge_pooling_type_error_is_not_hidden(monkeypatch) -> None:
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=object())
    )
    monkeypatch.setattr(
        encoder_module,
        "_load_local_first",
        lambda *args, **kwargs: (_ for _ in ()).throw(TypeError("Pooling.__init__() missing")),
    )
    settings = SimpleNamespace(
        embedding_device="cpu",
        embedding_model_name="other/model",
        embedding_model_revision="revision",
        embedding_max_length=512,
    )

    with pytest.raises(TypeError, match="Pooling"):
        encoder_module.BgeM3Encoder(settings)._load()
