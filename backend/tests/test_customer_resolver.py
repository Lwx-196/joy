"""Unit tests for backend.customer_resolver.

Two layers tested here:
1. `normalize()` — pure regex strip of dates / parens / project tails.
2. `resolve()` + create/update/merge — DB-backed candidate search.

The KEY rule from the module docstring is "NEVER auto-merge". Several tests
pin that boundary: even an obvious 0.95 similarity must still surface as
`decision='candidates'`, not `'matched'`, unless the name matches exactly
after normalization.
"""
from __future__ import annotations

import pytest

from backend import customer_resolver as cr


# ----------------------------------------------------------------------
# normalize() — pure
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("张三", "张三"),
        ("  张三 ", "张三"),
        ("", ""),
        ("张三 2025.01.05", "张三"),
        ("张三 2025-01-05 复诊", "张三"),
        ("张三（项目）", "张三"),
        ("张三(case)", "张三"),
        ("张三 案例", "张三"),
        ("张三 项目", "张三"),
        ("张三 术后", "张三"),
        ("张三 术前", "张三"),
        ("张三-1.5万", "张三-1.5万"),  # numeric not date — unchanged
    ],
)
def test_normalize_strips_known_suffixes(raw, expected):
    assert cr.normalize(raw) == expected


def test_normalize_empty_string_returns_empty():
    assert cr.normalize("") == ""


# ----------------------------------------------------------------------
# resolve() — DB-backed
# ----------------------------------------------------------------------


def test_resolve_empty_raw_returns_new_decision(temp_db):
    from backend import db

    with db.connect() as conn:
        result = cr.resolve("", conn)
    assert result.decision == "new"
    assert result.customer_id is None
    assert result.candidates == []
    assert "为空" in result.suggestion


def test_resolve_direct_match_by_canonical_name(temp_db):
    from backend import db

    with db.connect() as conn:
        cust_id = cr.create_customer(conn, "张三")
        result = cr.resolve("张三", conn)
    assert result.decision == "matched"
    assert result.customer_id == cust_id
    assert len(result.candidates) == 1
    assert result.candidates[0]["canonical_name"] == "张三"


def test_resolve_direct_match_via_alias(temp_db):
    from backend import db

    with db.connect() as conn:
        cust_id = cr.create_customer(conn, "张三", aliases=["小张"])
        result = cr.resolve("小张", conn)
    assert result.decision == "matched"
    assert result.customer_id == cust_id


def test_resolve_match_after_normalization_strips_date_suffix(temp_db):
    """`张三 2025.01.05` should match `张三` after normalization."""
    from backend import db

    with db.connect() as conn:
        cust_id = cr.create_customer(conn, "张三")
        result = cr.resolve("张三 2025.01.05", conn)
    assert result.decision == "matched"
    assert result.customer_id == cust_id
    assert result.normalized == "张三"


def test_resolve_similarity_match_returns_candidates_not_matched(temp_db):
    """KEY rule: never auto-merge. A 0.8+ similarity must still ask for
    confirmation as 'candidates', not silently become 'matched'.

    Use a Latin name pair where Levenshtein.ratio is well-above 0.7 (1-char
    diff in 10-char strings → ratio 0.9). For short CJK strings the ratio
    drops below 0.7 quickly (3-char names with 1 char diff → 0.667).
    """
    from backend import db

    with db.connect() as conn:
        cr.create_customer(conn, "Alice Wang")
        result = cr.resolve("Alice Wong", conn)  # 1-char diff
    assert result.decision == "candidates"
    assert result.customer_id is None
    assert len(result.candidates) >= 1
    assert all("similarity" in c for c in result.candidates)
    # Sorted descending
    sims = [c["similarity"] for c in result.candidates]
    assert sims == sorted(sims, reverse=True)


def test_resolve_below_threshold_returns_new(temp_db):
    """Similarity < 0.7 → no candidate, return decision='new'."""
    from backend import db

    with db.connect() as conn:
        cr.create_customer(conn, "张三")
        result = cr.resolve("李四", conn)
    assert result.decision == "new"
    assert result.candidates == []
    assert "建议新建客户" in result.suggestion


def test_resolve_no_customers_in_db_returns_new(temp_db):
    from backend import db

    with db.connect() as conn:
        result = cr.resolve("陌生姓名", conn)
    assert result.decision == "new"


# ----------------------------------------------------------------------
# create_customer / update_customer / merge_cases_to_customer
# ----------------------------------------------------------------------


def test_create_customer_persists_and_returns_id(temp_db):
    from backend import db

    with db.connect() as conn:
        cust_id = cr.create_customer(conn, "张三", aliases=["小张"], notes="test")
        row = conn.execute(
            "SELECT * FROM customers WHERE id = ?", (cust_id,)
        ).fetchone()
    assert row["canonical_name"] == "张三"
    assert row["notes"] == "test"


def test_create_customer_aliases_stored_as_utf8_json(temp_db):
    """`ensure_ascii=False` keeps CJK readable in the DB rather than \\uXXXX."""
    from backend import db

    with db.connect() as conn:
        cust_id = cr.create_customer(conn, "张三", aliases=["小张", "三爷"])
        row = conn.execute(
            "SELECT aliases_json FROM customers WHERE id = ?", (cust_id,)
        ).fetchone()
    assert "小张" in row["aliases_json"]
    assert "\\u" not in row["aliases_json"]


def test_update_customer_partial_field_only_changes_specified(temp_db):
    from backend import db

    with db.connect() as conn:
        cust_id = cr.create_customer(conn, "原名", aliases=["a"], notes="原备注")
        cr.update_customer(conn, cust_id, canonical_name="新名")
        row = conn.execute(
            "SELECT * FROM customers WHERE id = ?", (cust_id,)
        ).fetchone()
    assert row["canonical_name"] == "新名"
    assert row["notes"] == "原备注"  # unchanged


def test_update_customer_no_fields_is_noop(temp_db):
    """Calling update_customer with no kwargs should not raise and not bump
    `updated_at` (we observe by checking the value didn't change).
    """
    from backend import db

    with db.connect() as conn:
        cust_id = cr.create_customer(conn, "张三")
        before = conn.execute(
            "SELECT updated_at FROM customers WHERE id = ?", (cust_id,)
        ).fetchone()["updated_at"]
        cr.update_customer(conn, cust_id)  # no kwargs
        after = conn.execute(
            "SELECT updated_at FROM customers WHERE id = ?", (cust_id,)
        ).fetchone()["updated_at"]
    assert before == after


def test_merge_cases_to_customer_updates_only_listed_ids(temp_db, seed_case):
    from backend import db

    a = seed_case(abs_path="/tmp/merge-a")
    b = seed_case(abs_path="/tmp/merge-b")
    c = seed_case(abs_path="/tmp/merge-c")

    with db.connect() as conn:
        cust_id = cr.create_customer(conn, "客户A")
        rowcount = cr.merge_cases_to_customer(conn, cust_id, [a, c])
    assert rowcount == 2

    with db.connect() as conn:
        rows = {
            r["id"]: r["customer_id"]
            for r in conn.execute(
                "SELECT id, customer_id FROM cases WHERE id IN (?, ?, ?)",
                (a, b, c),
            )
        }
    assert rows[a] == cust_id
    assert rows[c] == cust_id
    assert rows[b] is None  # untouched


def test_merge_cases_empty_list_returns_zero(temp_db):
    from backend import db

    with db.connect() as conn:
        cust_id = cr.create_customer(conn, "客户")
        rowcount = cr.merge_cases_to_customer(conn, cust_id, [])
    assert rowcount == 0
