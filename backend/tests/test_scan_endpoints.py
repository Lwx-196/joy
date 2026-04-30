"""Smoke tests for `/api/scan` and `/api/scan/latest`.

DEFAULT_ROOTS point to dev-machine paths; tests monkeypatch them to either
empty list (no candidates) or a synthesized tmp directory tree so the scan
runs hermetically.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_scan_latest_returns_none_when_empty(client):
    resp = client.get("/api/scan/latest")
    assert resp.status_code == 200
    assert resp.json() == {"scan": None}


def test_scan_post_no_roots_records_zero_cases(
    client, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("backend.scanner.DEFAULT_ROOTS", [])
    resp = client.post("/api/scan", params={"mode": "full"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["new_count"] == 0
    assert body["updated_count"] == 0
    assert body["skipped_count"] == 0


def test_scan_invalid_mode_falls_back_to_incremental(
    client, monkeypatch: pytest.MonkeyPatch
):
    """Unknown modes silently coerce to 'incremental' (route shim)."""
    monkeypatch.setattr("backend.scanner.DEFAULT_ROOTS", [])
    resp = client.post("/api/scan", params={"mode": "garbage"})
    assert resp.status_code == 200

    latest = client.get("/api/scan/latest").json()
    assert latest["scan"]["mode"] == "incremental"


def test_scan_latest_returns_most_recent(
    client, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("backend.scanner.DEFAULT_ROOTS", [])
    client.post("/api/scan", params={"mode": "incremental"})
    client.post("/api/scan", params={"mode": "full"})
    latest = client.get("/api/scan/latest").json()["scan"]
    assert latest["mode"] == "full"
    assert latest["case_count"] == 0


def test_scan_full_picks_up_synthetic_case_dir(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end: synthesize a labeled-image dir and verify scan inserts it."""
    case_dir = tmp_path / "test-customer" / "2026.04.30 frontal"
    case_dir.mkdir(parents=True)
    (case_dir / "术前1.jpg").write_bytes(b"fakejpg")
    (case_dir / "术后1.jpg").write_bytes(b"fakejpg")

    monkeypatch.setattr("backend.scanner.DEFAULT_ROOTS", [tmp_path])
    resp = client.post("/api/scan", params={"mode": "full"})
    assert resp.status_code == 200
    assert resp.json()["new_count"] == 1

    cases = client.get("/api/cases").json()
    assert len(cases) == 1
    assert cases[0]["abs_path"] == str(case_dir)
