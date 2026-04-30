"""Customer name normalization and resolution (P0-3 candidate generator).

Key rule: NEVER auto-merge. Always return candidates for human confirmation.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import Levenshtein


_DATE_RE = re.compile(r"\d{4}[\.\-/]\d{1,2}[\.\-/]\d{1,2}.*$")
_PAREN_RE = re.compile(r"[\(（][^)）]*[\)）]")
_PROJECT_TAIL_RE = re.compile(r"[\s_\-·•]+(?:案例|项目|术后|术前)$")


def normalize(raw: str) -> str:
    """Strip parens content, date suffixes, project tails. Return main customer name."""
    if not raw:
        return ""
    s = raw.strip()
    s = _PAREN_RE.sub("", s)
    s = _DATE_RE.sub("", s)
    s = _PROJECT_TAIL_RE.sub("", s)
    s = re.sub(r"[\s_\-·•]+$", "", s)
    return s.strip()


@dataclass
class ResolveResult:
    raw: str
    normalized: str
    customer_id: int | None
    candidates: list[dict[str, Any]]
    suggestion: str
    decision: str  # "matched" | "candidates" | "new"


def _row_to_customer(row: sqlite3.Row) -> dict[str, Any]:
    aliases = json.loads(row["aliases_json"] or "[]")
    return {
        "id": row["id"],
        "canonical_name": row["canonical_name"],
        "aliases": aliases,
        "notes": row["notes"],
    }


def _try_direct_match(conn: sqlite3.Connection, normalized: str) -> dict[str, Any] | None:
    if not normalized:
        return None
    rows = conn.execute("SELECT * FROM customers").fetchall()
    for row in rows:
        if row["canonical_name"] == normalized or normalize(row["canonical_name"]) == normalized:
            return _row_to_customer(row)
        aliases = json.loads(row["aliases_json"] or "[]")
        for alias in aliases:
            if alias == normalized or normalize(alias) == normalized:
                return _row_to_customer(row)
    return None


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return Levenshtein.ratio(a, b)


def resolve(raw: str, conn: sqlite3.Connection) -> ResolveResult:
    normalized = normalize(raw)
    if not normalized:
        return ResolveResult(raw, normalized, None, [], "原始客户名为空", "new")

    direct = _try_direct_match(conn, normalized)
    if direct:
        return ResolveResult(raw, normalized, direct["id"], [direct], f"已绑定到 {direct['canonical_name']}", "matched")

    rows = conn.execute("SELECT * FROM customers").fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        sim = _similarity(normalized, row["canonical_name"])
        for alias in json.loads(row["aliases_json"] or "[]"):
            sim = max(sim, _similarity(normalized, alias))
        if sim >= 0.7:
            cust = _row_to_customer(row)
            cust["similarity"] = round(sim, 3)
            candidates.append(cust)

    candidates.sort(key=lambda c: c.get("similarity", 0.0), reverse=True)

    if candidates:
        return ResolveResult(
            raw, normalized, None, candidates,
            f"找到 {len(candidates)} 个相似客户，需人工确认",
            "candidates",
        )

    return ResolveResult(
        raw, normalized, None, [],
        f"建议新建客户：{normalized}",
        "new",
    )


def create_customer(conn: sqlite3.Connection, canonical_name: str, aliases: list[str] | None = None, notes: str | None = None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO customers (canonical_name, aliases_json, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (canonical_name, json.dumps(aliases or [], ensure_ascii=False), notes, now, now),
    )
    return cur.lastrowid


def update_customer(conn: sqlite3.Connection, customer_id: int, *, canonical_name: str | None = None, aliases: list[str] | None = None, notes: str | None = None) -> None:
    fields = []
    values: list[Any] = []
    if canonical_name is not None:
        fields.append("canonical_name = ?")
        values.append(canonical_name)
    if aliases is not None:
        fields.append("aliases_json = ?")
        values.append(json.dumps(aliases, ensure_ascii=False))
    if notes is not None:
        fields.append("notes = ?")
        values.append(notes)
    if not fields:
        return
    fields.append("updated_at = ?")
    values.append(datetime.now(timezone.utc).isoformat())
    values.append(customer_id)
    conn.execute(f"UPDATE customers SET {', '.join(fields)} WHERE id = ?", values)


def merge_cases_to_customer(conn: sqlite3.Connection, customer_id: int, case_ids: list[int]) -> int:
    if not case_ids:
        return 0
    placeholders = ",".join("?" * len(case_ids))
    cur = conn.execute(
        f"UPDATE cases SET customer_id = ? WHERE id IN ({placeholders})",
        [customer_id, *case_ids],
    )
    return cur.rowcount
