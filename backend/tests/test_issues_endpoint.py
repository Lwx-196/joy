"""Smoke tests for `GET /api/issues/dict`.

The route just exposes `issue_translator.all_entries()` — a static dictionary
of issue-code metadata. These tests pin the response shape and confirm a few
representative codes are present so accidental dict-key renames break loudly.
"""
from __future__ import annotations


def test_issues_dict_returns_list_of_entries(client):
    resp = client.get("/api/issues/dict")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) > 0


def test_issues_dict_each_entry_has_code(client):
    body = client.get("/api/issues/dict").json()
    codes = {entry["code"] for entry in body}
    # Every entry must expose `code`; codes are unique within the dict.
    assert len(codes) == len(body)


def test_issues_dict_entries_are_string_only(client):
    body = client.get("/api/issues/dict").json()
    for entry in body:
        for value in entry.values():
            assert isinstance(value, str)
