"""Issue dictionary endpoint."""
from __future__ import annotations

from fastapi import APIRouter

from .. import issue_translator

router = APIRouter(prefix="/api/issues", tags=["issues"])


@router.get("/dict")
def issue_dict() -> list[dict]:
    return issue_translator.all_entries()
