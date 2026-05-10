"""Configurable QA policy for AI after-image simulation jobs."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SIMULATION_DECISION_THRESHOLDS: dict[str, float] = {
    "approve_full_max": 4.5,
    "approve_non_target_max": 3.0,
    "approve_p95_max": 12.0,
    "approve_changed_ratio_max": 0.08,
    "approve_target_min": 1.5,
    "reject_full_min": 10.0,
    "reject_non_target_min": 8.0,
    "reject_p95_min": 35.0,
    "reject_changed_ratio_min": 0.25,
}

POLICY_PATH = Path(
    os.environ.get(
        "CASE_WORKBENCH_AI_REVIEW_POLICY",
        str(Path(__file__).resolve().parent.parent / "case-workbench-ai" / "ai-review-policy.json"),
    )
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_ai_review_policy() -> dict[str, Any]:
    return {
        "version": 1,
        "name": "controlled_region_diff_v1",
        "description": "AI 增强受控审核策略：限制框外变化，要求目标区域有可见局部变化。",
        "thresholds": dict(DEFAULT_SIMULATION_DECISION_THRESHOLDS),
        "updated_at": None,
    }


def _coerce_thresholds(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise ValueError("thresholds must be an object")
    thresholds = dict(DEFAULT_SIMULATION_DECISION_THRESHOLDS)
    for key, value in raw.items():
        if key not in DEFAULT_SIMULATION_DECISION_THRESHOLDS:
            raise ValueError(f"unknown threshold: {key}")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"threshold {key} must be numeric") from exc
        if number < 0:
            raise ValueError(f"threshold {key} cannot be negative")
        thresholds[key] = number
    return thresholds


def normalize_ai_review_policy(raw: Any) -> dict[str, Any]:
    base = default_ai_review_policy()
    if not isinstance(raw, dict):
        raise ValueError("policy must be an object")
    thresholds = _coerce_thresholds(raw.get("thresholds", {}))
    name = str(raw.get("name") or base["name"]).strip() or base["name"]
    description = str(raw.get("description") or base["description"]).strip()
    try:
        version = int(raw.get("version") or base["version"])
    except (TypeError, ValueError):
        version = int(base["version"])
    return {
        "version": max(1, version),
        "name": name,
        "description": description,
        "thresholds": thresholds,
        "updated_at": raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None,
    }


def load_ai_review_policy() -> dict[str, Any]:
    if not POLICY_PATH.is_file():
        return default_ai_review_policy()
    try:
        raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        return normalize_ai_review_policy(raw)
    except (OSError, ValueError, TypeError):
        return default_ai_review_policy()


def preview_ai_review_policy(raw: dict[str, Any]) -> dict[str, Any]:
    current = load_ai_review_policy()
    merged = {
        **current,
        **raw,
        "thresholds": {
            **current.get("thresholds", {}),
            **(raw.get("thresholds") or {}),
        },
        "version": current.get("version") or 1,
        "updated_at": current.get("updated_at"),
    }
    return normalize_ai_review_policy(merged)


def save_ai_review_policy(raw: dict[str, Any]) -> dict[str, Any]:
    current = load_ai_review_policy()
    merged = {
        **current,
        **raw,
        "thresholds": {
            **current.get("thresholds", {}),
            **(raw.get("thresholds") or {}),
        },
        "version": int(current.get("version") or 1) + 1,
        "updated_at": _now_iso(),
    }
    policy = normalize_ai_review_policy(merged)
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    POLICY_PATH.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
    return policy
