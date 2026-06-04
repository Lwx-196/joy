"""External identity embedding worker.

This script is intended to run inside an isolated Python environment with real
InsightFace/ArcFace-compatible dependencies installed. It emits JSON only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROVIDER_NAME = "insightface_arcface_v1"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _dependency_status() -> dict[str, bool]:
    import importlib.util

    return {
        "numpy": importlib.util.find_spec("numpy") is not None,
        "onnxruntime": importlib.util.find_spec("onnxruntime") is not None,
        "insightface": importlib.util.find_spec("insightface") is not None,
        "cv2": importlib.util.find_spec("cv2") is not None,
    }


def _probe() -> dict[str, Any]:
    deps = _dependency_status()
    ready = all(deps.values())
    payload: dict[str, Any] = {
        "status": "available" if ready else "unavailable",
        "provider": PROVIDER_NAME if ready else None,
        "dependencies": deps,
        "embedding_dim": 512 if ready else None,
    }
    if not ready:
        payload["reason"] = "Missing real InsightFace/ArcFace dependencies."
    return payload


def _load_app() -> Any:
    import os

    from insightface.app import FaceAnalysis

    model_name = os.environ.get("CASE_WORKBENCH_INSIGHTFACE_MODEL", "buffalo_l")
    det_size_raw = os.environ.get("CASE_WORKBENCH_INSIGHTFACE_DET_SIZE", "640,640")
    try:
        width, height = [int(part.strip()) for part in det_size_raw.split(",", 1)]
    except Exception:
        width, height = 640, 640
    app = FaceAnalysis(name=model_name, providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_size=(width, height))
    return app


def _largest_face(faces: list[Any]) -> Any | None:
    if not faces:
        return None

    def area(face: Any) -> float:
        bbox = getattr(face, "bbox", None)
        if bbox is None or len(bbox) < 4:
            return 0.0
        return max(0.0, float(bbox[2]) - float(bbox[0])) * max(0.0, float(bbox[3]) - float(bbox[1]))

    return sorted(faces, key=area, reverse=True)[0]


def _embed_paths(paths: list[str]) -> dict[str, Any]:
    import cv2

    app = _load_app()
    items: list[dict[str, Any]] = []
    for raw_path in paths:
        image_path = Path(raw_path)
        item: dict[str, Any] = {"path": str(image_path)}
        if not image_path.is_file():
            item.update({"status": "missing_file"})
            items.append(item)
            continue
        image = cv2.imread(str(image_path))
        if image is None:
            item.update({"status": "image_decode_failed"})
            items.append(item)
            continue
        faces = app.get(image)
        face = _largest_face(list(faces or []))
        if face is None:
            item.update({"status": "no_face", "face_count": 0})
            items.append(item)
            continue
        embedding = getattr(face, "normed_embedding", None)
        if embedding is None:
            item.update({"status": "embedding_missing", "face_count": len(faces or [])})
            items.append(item)
            continue
        bbox = getattr(face, "bbox", None)
        item.update(
            {
                "status": "embedded",
                "embedding": [round(float(value), 6) for value in embedding.tolist()],
                "face_count": len(faces or []),
                "det_score": round(float(getattr(face, "det_score", 0.0) or 0.0), 6),
                "bbox": [round(float(value), 2) for value in bbox.tolist()] if bbox is not None else None,
            }
        )
        items.append(item)
    return {
        "status": "ok",
        "provider": PROVIDER_NAME,
        "embedding_dim": 512,
        "items": items,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="InsightFace identity embedding worker.")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--images-json", default="[]")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        payload = _probe() if args.probe else _embed_paths([str(item) for item in json.loads(args.images_json)])
    except Exception as exc:  # noqa: BLE001
        payload = {"status": "error", "provider": PROVIDER_NAME, "error_type": type(exc).__name__, "error": str(exc)[:800]}
    print(_json(payload))
    return 0 if payload.get("status") not in {"error"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
