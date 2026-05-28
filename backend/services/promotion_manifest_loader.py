"""Promotion manifest runtime loader + 灰度 decision.

Plan §P2.3 — Runtime Guard.

Reads `case-workbench-ai/promotion/manifest.json` (produced/validated by
`backend/scripts/compute_manifest_hashes.py`, see plan §P2.1) and decides at
runtime whether a given case_id should be **promoted** (treated as production-
eligible) or remain in **shadow** (candidate-only, non-publishable) per the
渐进灰度路径 in plan §P2.4.

Decision matrix (`promotion_state` → behavior):
    shadow       → False (no case promoted; candidate-only writes)
    p10          → True iff hash(case_id) mod 100 < 10  (~10% bucket)
    p25          → True iff hash(case_id) mod 100 < 25  (~25% bucket)
    p50          → True iff hash(case_id) mod 100 < 50  (~50% bucket)
    p100         → True (all cases promoted)
    rolled_back  → False (atomic rollback to shadow behavior)
    <missing>    → False (fail-closed)
    <unknown>    → False (fail-closed, unknown states cannot be trusted)

Hash uses sha256(str(case_id))[:8] interpreted as hex → mod 100.  This is a
deterministic stable hash — same `case_id` ⇒ same bucket across runs,
processes, Python versions, and machines.  (Python's builtin `hash()` is
randomized per process via PYTHONHASHSEED and therefore unusable here.)

Default behavior when manifest is missing / invalid is identical to the legacy
hardcoded `candidate_only=True` path, preserving backward compatibility for
deployments that have not yet provisioned a manifest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# Repo root resolution: this file lives at
#   <repo>/backend/services/promotion_manifest_loader.py
# so parents[2] = <repo>.
_REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_MANIFEST_PATH = _REPO_ROOT / "case-workbench-ai" / "promotion" / "manifest.json"

# Mirror of plan §P2.4 / compute_manifest_hashes.VALID_PROMOTION_STATES.
# Duplicated locally so this module has zero coupling to the scripts package.
VALID_STATES: frozenset[str] = frozenset(
    {"shadow", "p10", "p25", "p50", "p100", "rolled_back"}
)

# Fail-closed default state.
FAIL_CLOSED_STATE = "shadow"

# Wave 13 H-4 — schema_version drift detection. The manifest carries an
# explicit ``schema_version`` (currently 1, see ``case-workbench-ai/promotion/
# manifest.json``). A future bump (e.g. v2 renaming ``promotion_state`` or
# repurposing ``bindings``) MUST not be silently consumed by an older loader
# because the decision matrix (shadow/p10/.../rolled_back) might mean
# something different. Add explicit forward-compat fail-closed: any
# ``schema_version`` outside this allowlist causes ``load_manifest`` to
# return None (same fail-closed behavior as a missing / malformed file), and
# downstream ``should_promote`` resolves to False. When we *do* publish a
# new schema, bump this set in lockstep with the code that understands the
# new layout.
#
# Missing ``schema_version`` is tolerated and treated as v1 to preserve
# backward compatibility for hand-rolled manifests that predate the field.
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})


def load_manifest(path: Path | None = None) -> dict[str, Any] | None:
    """Read the promotion manifest from disk.

    Returns the parsed dict on success, or **None** if the file does not exist
    or cannot be parsed as JSON.  We never raise — callers downstream
    (`get_promotion_state`, `should_promote`) treat `None` as fail-closed
    shadow, which is the same as legacy hardcoded behavior.

    Wave 13 H-4: if the manifest carries an unsupported ``schema_version``
    we reject the load (returns None) rather than silently consume a
    future schema with potentially incompatible semantics.
    """
    target = path if path is not None else DEFAULT_MANIFEST_PATH
    try:
        if not target.exists() or not target.is_file():
            return None
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    # H-4: schema_version drift gate. Missing field is tolerated (BC); any
    # explicit value must be an int (not bool) AND in the supported set.
    schema_version = parsed.get("schema_version")
    if schema_version is not None and (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in SUPPORTED_SCHEMA_VERSIONS
    ):
        return None
    return parsed


def get_promotion_state(manifest: dict[str, Any] | None = None) -> str:
    """Resolve the active `promotion_state` from a manifest.

    When `manifest is None`, load from the default path.  Returns the state
    string if valid; otherwise `'shadow'` (fail-closed).
    """
    m = manifest if manifest is not None else load_manifest()
    if m is None:
        return FAIL_CLOSED_STATE
    state = m.get("promotion_state")
    if not isinstance(state, str):
        return FAIL_CLOSED_STATE
    if state not in VALID_STATES:
        return FAIL_CLOSED_STATE
    return state


def _stable_bucket(case_id: int) -> int:
    """Deterministic hash bucket in [0, 100).

    Uses sha256(str(case_id))[:8] as a hex int mod 100.  Stable across
    processes / machines / Python versions — required for grayscale rollouts
    where the same case must consistently fall in the same bucket.
    """
    digest = hashlib.sha256(str(case_id).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def should_promote(
    case_id: int, *, manifest: dict[str, Any] | None = None
) -> bool:
    """Decide whether `case_id` is promoted under the current manifest state.

    See module docstring for the full decision matrix.  Fail-closed on every
    unknown / invalid input (returns False).
    """
    state = get_promotion_state(manifest)
    if state == "p100":
        return True
    if state in {"shadow", "rolled_back"}:
        return False
    if state == "p10":
        return _stable_bucket(case_id) < 10
    if state == "p25":
        return _stable_bucket(case_id) < 25
    if state == "p50":
        return _stable_bucket(case_id) < 50
    # Defensive: get_promotion_state already maps unknown → shadow → False
    # above, but keep an explicit fail-closed branch in case the state set
    # is extended without updating this dispatcher.
    return False


__all__ = [
    "DEFAULT_MANIFEST_PATH",
    "VALID_STATES",
    "FAIL_CLOSED_STATE",
    "SUPPORTED_SCHEMA_VERSIONS",
    "load_manifest",
    "get_promotion_state",
    "should_promote",
]
