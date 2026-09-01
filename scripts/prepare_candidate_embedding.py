"""Prepare the local BGE-small model used by transient candidate selection.

This is an explicit deployment/development command. Runtime requests never export or
download a model implicitly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from sentence_transformers import (
    SentenceTransformer,
    export_dynamic_quantized_onnx_model,
)

from app.config import get_settings


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=settings.candidate_embedding_model_name)
    parser.add_argument("--revision", default=settings.candidate_embedding_model_revision)
    parser.add_argument(
        "--output", type=Path, default=settings.candidate_embedding_onnx_path
    )
    parser.add_argument(
        "--quantization-config",
        default="avx512_vnni",
        choices=("avx2", "avx512", "avx512_vnni", "arm64"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    model = SentenceTransformer(args.model, revision=args.revision, backend="onnx")
    model.max_seq_length = get_settings().candidate_embedding_max_length
    model.save_pretrained(str(args.output))
    export_dynamic_quantized_onnx_model(
        model,
        quantization_config=args.quantization_config,
        model_name_or_path=str(args.output),
    )
    vectors = np.asarray(
        model.encode(
            ["通勤降噪耳机", "头戴式蓝牙主动降噪耳机"],
            normalize_embeddings=True,
            convert_to_numpy=True,
        ),
        dtype="float32",
    )
    expected = (2, get_settings().candidate_embedding_dimensions)
    if vectors.shape != expected or not np.isfinite(vectors).all():
        raise RuntimeError(f"candidate embedding validation failed: {vectors.shape} != {expected}")
    config = getattr(getattr(model, "_first_module", lambda: None)(), "auto_model", None)
    config = getattr(config, "config", None)
    metadata = {
        "model_id": args.model,
        "requested_revision": args.revision,
        "resolved_revision": getattr(config, "_commit_hash", None) or args.revision,
        "dimensions": vectors.shape[1],
        "normalized": bool(np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-4)),
        "quantization_config": args.quantization_config,
    }
    (args.output / "candidate_embedding_manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
