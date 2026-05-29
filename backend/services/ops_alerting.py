"""C3.0.3 — Alerting source-of-truth wiring.

Plan §C3.0.3 mandates that **the SLOReport + rollback_applier_result are
the only allowed alert event sources**. Anything else routed into the
alerting layer (random log scraping, anecdotal Slack pings, custom scripts)
is out of scope and explicitly rejected here.

This module compiles those two well-typed inputs into ``AlertEvent`` rows,
writes the lifecycle stages (``alert_fired`` → ``alert_acked`` →
``alert_resolved``) into ``ops_audit_log`` sharing one ``request_id`` as
correlation id (same pattern the applier already uses for
``rollback_started`` / ``rollback_completed``), and dispatches the event
through one or more channel adapters.

What this module **does not** do:

- Mutate the promotion manifest. (Stream A authority.)
- Mutate any applier / SLO monitor state. (Stream A authority.)
- Introduce a new alert table or schema migration. The audit log itself is
  the lifecycle store, which keeps the contract auditable without adding
  another moving part.

Audit row mapping per stage:

  Stage             outcome        endpoint
  -----             -------        --------
  alert_fired       ok | error     ops_alerting.fire
  alert_acked       ok             ops_alerting.ack
  alert_resolved    ok             ops_alerting.resolve

The ``http_status`` mirrors the channel response (200 = sent, 0 = stub no-op,
otherwise the upstream channel status).
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol


REVIEWER_SYSTEM = "system/ops_alerting"

ENDPOINT_FIRE = "ops_alerting.fire"
ENDPOINT_ACK = "ops_alerting.ack"
ENDPOINT_RESOLVE = "ops_alerting.resolve"

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"

# Source tokens — closed-world set, callers must pick one. Any other value
# is rejected at compile time so we don't accidentally accept ad-hoc sources.
SOURCE_SLO = "slo_report"
SOURCE_APPLIER = "rollback_applier"
ALLOWED_SOURCES: frozenset[str] = frozenset({SOURCE_SLO, SOURCE_APPLIER})


@dataclasses.dataclass(frozen=True)
class AlertEvent:
    """Compiled alert payload — immutable, JSON-serializable."""

    correlation_id: str
    source: str  # ALLOWED_SOURCES
    severity: str
    title: str
    detail: dict[str, Any]
    fired_at: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Channel adapters
# ---------------------------------------------------------------------------


class AlertChannel(Protocol):
    """Channel adapters dispatch a compiled AlertEvent and return a dict.

    The dict shape is intentionally simple: ``{"ok": bool, "http_status": int,
    "error": str | None, "channel": str}``. ``http_status`` is 0 for channels
    that don't talk HTTP (e.g. StubChannel writing to a file).
    """

    name: str

    def send(self, event: AlertEvent) -> dict[str, Any]: ...


class StubChannel:
    """Always-available channel that appends serialized alerts to a JSONL
    file. Used in tests (deterministic, no external dependency) and as a
    safe default in environments where the operator has not yet provisioned
    a webhook URL.
    """

    name = "stub_file"

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            env = os.environ.get("OPS_ALERT_STUB_FILE")
            if env:
                path = Path(env)
            else:
                path = Path(os.environ.get("TMPDIR", "/tmp")) / "ops_alerts.jsonl"
        self.path = path

    def send(self, event: AlertEvent) -> dict[str, Any]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            return {"ok": True, "http_status": 0, "error": None, "channel": self.name}
        except OSError as exc:
            return {
                "ok": False,
                "http_status": 0,
                "error": f"{exc.__class__.__name__}: {exc}",
                "channel": self.name,
            }


class WebhookChannel:
    """POST a JSON payload to a Slack-compatible webhook URL.

    Configured via the ``OPS_ALERT_WEBHOOK_URL`` env var. Disabled when the
    var is unset / empty — the factory below filters disabled channels out
    rather than failing at startup.
    """

    name = "webhook"

    def __init__(self, url: str, *, timeout: float = 3.0) -> None:
        self.url = url
        self.timeout = timeout

    def send(self, event: AlertEvent) -> dict[str, Any]:
        # Local import keeps the module importable in test environments
        # where urllib is patched and we don't want to attempt a network call.
        from urllib import error as _urllib_error
        from urllib import request as _urllib_request

        body = json.dumps(
            {
                "text": f"[{event.severity.upper()}] {event.title}",
                "correlation_id": event.correlation_id,
                "source": event.source,
                "detail": event.detail,
                "fired_at": event.fired_at,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        req = _urllib_request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _urllib_request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return {
                    "ok": 200 <= resp.status < 300,
                    "http_status": resp.status,
                    "error": None,
                    "channel": self.name,
                }
        except (_urllib_error.URLError, TimeoutError, OSError) as exc:
            return {
                "ok": False,
                "http_status": 0,
                "error": f"{exc.__class__.__name__}: {exc}",
                "channel": self.name,
            }


def default_channels() -> list[AlertChannel]:
    """Resolve the channel set from environment.

    Always includes ``StubChannel`` (gives us a deterministic file we can
    point operators at when the webhook is misconfigured); appends
    ``WebhookChannel`` when ``OPS_ALERT_WEBHOOK_URL`` is set.
    """
    chans: list[AlertChannel] = [StubChannel()]
    url = (os.environ.get("OPS_ALERT_WEBHOOK_URL") or "").strip()
    if url:
        chans.append(WebhookChannel(url))
    return chans


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_correlation_id() -> str:
    return f"alert-{uuid.uuid4().hex[:16]}"


def compile_alert_from_slo_report(
    report: Any, *, correlation_id: str | None = None
) -> AlertEvent | None:
    """Map an ``SLOReport`` (or its ``.to_dict()`` shape) onto an
    ``AlertEvent``. Returns ``None`` when the report does not warrant an
    alert (recommendation ∈ {continue}).

    Severity tiers:
      - ``rollback`` / ``stop_loss_halt`` → critical
      - ``insufficient_data`` / ``monitoring_paused`` → warn
      - anything else (other than ``continue``) → info

    We accept both the dataclass and a dict shape because the SLO monitor's
    output is sometimes consumed via ``to_dict()`` (REST surface) and
    sometimes as the dataclass (in-process callers).
    """
    data = report.to_dict() if hasattr(report, "to_dict") else report
    if not isinstance(data, dict):
        return None
    rec = str(data.get("recommendation") or "")
    if rec == "continue":
        return None
    severity_map = {
        "rollback": SEVERITY_CRITICAL,
        "stop_loss_halt": SEVERITY_CRITICAL,
        "insufficient_data": SEVERITY_WARN,
        "monitoring_paused": SEVERITY_WARN,
    }
    severity = severity_map.get(rec, SEVERITY_INFO)
    return AlertEvent(
        correlation_id=correlation_id or _new_correlation_id(),
        source=SOURCE_SLO,
        severity=severity,
        title=f"SLO recommendation: {rec}",
        detail={
            "recommendation": rec,
            "within_slo": data.get("within_slo"),
            "sample_size": data.get("sample_size"),
            "window_hours": data.get("window_hours"),
            "violations": data.get("violations") or [],
            "notes": data.get("notes") or "",
            "promotion_state": (data.get("evidence") or {}).get("promotion_state"),
        },
        fired_at=_utc_now_iso(),
    )


def compile_alert_from_applier_result(
    result: dict[str, Any], *, correlation_id: str | None = None
) -> AlertEvent | None:
    """Map a ``promotion_rollback_applier`` result dict onto an
    ``AlertEvent``. Returns ``None`` for benign outcomes that do not
    warrant ops attention.
    """
    if not isinstance(result, dict):
        return None
    reason = str(result.get("reason") or "")
    outcome = str(result.get("outcome") or "")
    # Benign tokens — no alert.
    if reason in {
        "no_rollback_needed",
        "insufficient_data",
        "monitoring_paused",
        "already_rolled_back",
        "dry_run",
        "baseline_unmeasured_only_no_real_breach",
    }:
        return None
    # Critical: an actual rollback fired, OR stop-loss alert, OR aborted halfway.
    critical_reasons = {
        "rollback_applied",
        "stop_loss_halt_alert",
        "concurrent_apply_in_progress",
        "missing_baseline_bindings",
        "invalid_manifest_state",
        "no_manifest",
    }
    if reason in critical_reasons or outcome in {
        "rollback_started",
        "rollback_completed",
        "rollback_aborted",
        "stop_loss_halt_alert",
    }:
        severity = SEVERITY_CRITICAL
    else:
        severity = SEVERITY_WARN
    return AlertEvent(
        correlation_id=correlation_id
        or str(result.get("request_id") or _new_correlation_id()),
        source=SOURCE_APPLIER,
        severity=severity,
        title=f"Rollback applier: {reason or outcome or 'unknown'}",
        detail={
            "outcome": outcome,
            "reason": reason,
            "from_state": result.get("from_state"),
            "to_state": result.get("to_state"),
            "recommendation": result.get("recommendation"),
        },
        fired_at=_utc_now_iso(),
    )


# ---------------------------------------------------------------------------
# Lifecycle audit
# ---------------------------------------------------------------------------


class AlertNotFoundError(Exception):
    """A lifecycle op (ack/resolve) targeted a correlation_id with no
    ``alert_fired`` row. The REST layer maps this to HTTP 404."""


class AlertStateError(Exception):
    """An illegal lifecycle transition (e.g. ack/resolve after the alert was
    already resolved). The REST layer maps this to HTTP 409."""


def _lifecycle_stages(
    conn: sqlite3.Connection, correlation_id: str
) -> set[str]:
    """Return the set of lifecycle endpoints already recorded for this
    correlation_id (subset of {fire, ack, resolve}).

    The audit log is the lifecycle store (no separate alert table), so the
    current state of an alert is derived from which stage rows exist.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT endpoint FROM ops_audit_log
        WHERE request_id = ? AND endpoint IN (?, ?, ?)
        """,
        (correlation_id, ENDPOINT_FIRE, ENDPOINT_ACK, ENDPOINT_RESOLVE),
    ).fetchall()
    return {row[0] for row in rows}


def _insert_audit_row(
    conn: sqlite3.Connection,
    *,
    correlation_id: str,
    endpoint: str,
    payload: dict[str, Any],
    response: dict[str, Any],
    outcome: str,
    http_status: int,
    reason: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO ops_audit_log
          (request_id, endpoint, reviewer, reason,
           payload_json, response_json, outcome, http_status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            correlation_id,
            endpoint,
            REVIEWER_SYSTEM,
            reason,
            json.dumps(payload, ensure_ascii=False, default=str),
            json.dumps(response, ensure_ascii=False, default=str),
            outcome,
            int(http_status),
            _utc_now_iso(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def fire_alert(
    event: AlertEvent,
    *,
    conn: sqlite3.Connection,
    channels: Iterable[AlertChannel] | None = None,
    dispatch: bool = True,
) -> dict[str, Any]:
    """Send ``event`` through every channel + write the ``alert_fired`` row.

    ``dispatch=False`` (used by dry-run / synthetic tests) skips the channel
    send but still writes the audit row so the lifecycle is fully traceable.
    """
    if event.source not in ALLOWED_SOURCES:
        raise ValueError(f"event.source must be one of {sorted(ALLOWED_SOURCES)}")
    # Idempotent by correlation_id: a second fire for an already-fired alert is
    # a no-op (don't duplicate the audit row or re-dispatch to channels). The
    # applier path reuses the applier request_id as the correlation_id, so a
    # replayed result must not page on-call twice.
    if ENDPOINT_FIRE in _lifecycle_stages(conn, event.correlation_id):
        return {
            "event": event.to_dict(),
            "dispatched": False,
            "channels": [],
            "idempotent": True,
            "note": "alert already fired for this correlation_id",
        }
    chans = list(channels) if channels is not None else default_channels()
    results: list[dict[str, Any]] = []
    aggregate_http = 0
    aggregate_ok = True
    if dispatch:
        for ch in chans:
            r = ch.send(event)
            results.append(r)
            if not r.get("ok"):
                aggregate_ok = False
            if r.get("http_status"):
                aggregate_http = int(r["http_status"])
    outcome = "ok" if aggregate_ok else "error"
    response = {
        "event": event.to_dict(),
        "dispatched": dispatch,
        "channels": results,
    }
    _insert_audit_row(
        conn,
        correlation_id=event.correlation_id,
        endpoint=ENDPOINT_FIRE,
        payload={"source": event.source, "severity": event.severity, "title": event.title},
        response=response,
        outcome=outcome,
        http_status=aggregate_http if aggregate_http else (200 if aggregate_ok else 500),
        reason=event.detail.get("reason") if isinstance(event.detail, dict) else None,
    )
    return response


def ack_alert(
    *,
    conn: sqlite3.Connection,
    correlation_id: str,
    operator: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Mark an alert correlation_id as acknowledged. Operator must supply
    their identity (from ``on-call-rotation.md``).

    Raises :class:`AlertNotFoundError` when no ``alert_fired`` row exists for
    the correlation_id, and :class:`AlertStateError` when the alert was already
    resolved (acking a closed alert is a no-op the operator should not be able
    to record silently)."""
    stages = _lifecycle_stages(conn, correlation_id)
    if ENDPOINT_FIRE not in stages:
        raise AlertNotFoundError(
            f"no fired alert for correlation_id {correlation_id!r}"
        )
    if ENDPOINT_RESOLVE in stages:
        raise AlertStateError(
            f"alert {correlation_id!r} already resolved; cannot ack"
        )
    payload = {"operator": operator, "note": note}
    response = {"correlation_id": correlation_id, "stage": "acked"}
    _insert_audit_row(
        conn,
        correlation_id=correlation_id,
        endpoint=ENDPOINT_ACK,
        payload=payload,
        response=response,
        outcome="ok",
        http_status=200,
        reason=note,
    )
    return response


def resolve_alert(
    *,
    conn: sqlite3.Connection,
    correlation_id: str,
    operator: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Mark an alert correlation_id as resolved.

    Raises :class:`AlertNotFoundError` when no ``alert_fired`` row exists for
    the correlation_id, and :class:`AlertStateError` on a double-resolve (the
    alert was already resolved)."""
    stages = _lifecycle_stages(conn, correlation_id)
    if ENDPOINT_FIRE not in stages:
        raise AlertNotFoundError(
            f"no fired alert for correlation_id {correlation_id!r}"
        )
    if ENDPOINT_RESOLVE in stages:
        raise AlertStateError(
            f"alert {correlation_id!r} already resolved"
        )
    payload = {"operator": operator, "note": note}
    response = {"correlation_id": correlation_id, "stage": "resolved"}
    _insert_audit_row(
        conn,
        correlation_id=correlation_id,
        endpoint=ENDPOINT_RESOLVE,
        payload=payload,
        response=response,
        outcome="ok",
        http_status=200,
        reason=note,
    )
    return response


__all__ = [
    "ALLOWED_SOURCES",
    "AlertChannel",
    "AlertEvent",
    "AlertNotFoundError",
    "AlertStateError",
    "ENDPOINT_ACK",
    "ENDPOINT_FIRE",
    "ENDPOINT_RESOLVE",
    "REVIEWER_SYSTEM",
    "SEVERITY_CRITICAL",
    "SEVERITY_INFO",
    "SEVERITY_WARN",
    "SOURCE_APPLIER",
    "SOURCE_SLO",
    "StubChannel",
    "WebhookChannel",
    "ack_alert",
    "compile_alert_from_applier_result",
    "compile_alert_from_slo_report",
    "default_channels",
    "fire_alert",
    "resolve_alert",
]
