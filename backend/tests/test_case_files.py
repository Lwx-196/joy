"""Tests for backend.services.case_files — path traversal safety guard."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.services.case_files import TRASH_DIR_NAME, resolve_existing_source


class TestResolveExistingSource:
    def test_valid_jpg(self, tmp_path: Path) -> None:
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff")
        result = resolve_existing_source(tmp_path, "photo.jpg")
        assert result == img.resolve()

    def test_valid_png_in_subdir(self, tmp_path: Path) -> None:
        sub = tmp_path / "front"
        sub.mkdir()
        img = sub / "before.png"
        img.write_bytes(b"\x89PNG")
        result = resolve_existing_source(tmp_path, "front/before.png")
        assert result == img.resolve()

    def test_none_filename_raises_400(self, tmp_path: Path) -> None:
        with pytest.raises(HTTPException) as exc_info:
            resolve_existing_source(tmp_path, None)
        assert exc_info.value.status_code == 400

    def test_empty_filename_raises_400(self, tmp_path: Path) -> None:
        with pytest.raises(HTTPException) as exc_info:
            resolve_existing_source(tmp_path, "")
        assert exc_info.value.status_code == 400

    def test_dot_filename_raises_400(self, tmp_path: Path) -> None:
        with pytest.raises(HTTPException) as exc_info:
            resolve_existing_source(tmp_path, ".")
        assert exc_info.value.status_code == 400

    def test_dotdot_filename_raises_400(self, tmp_path: Path) -> None:
        with pytest.raises(HTTPException) as exc_info:
            resolve_existing_source(tmp_path, "..")
        assert exc_info.value.status_code == 400

    def test_absolute_path_raises_400(self, tmp_path: Path) -> None:
        with pytest.raises(HTTPException) as exc_info:
            resolve_existing_source(tmp_path, "/etc/passwd")
        assert exc_info.value.status_code == 400

    def test_dotdot_traversal_raises_400(self, tmp_path: Path) -> None:
        with pytest.raises(HTTPException) as exc_info:
            resolve_existing_source(tmp_path, "../secret.jpg")
        assert exc_info.value.status_code == 400

    def test_trash_dir_raises_400(self, tmp_path: Path) -> None:
        trash = tmp_path / TRASH_DIR_NAME
        trash.mkdir()
        img = trash / "deleted.jpg"
        img.write_bytes(b"\xff\xd8\xff")
        with pytest.raises(HTTPException) as exc_info:
            resolve_existing_source(tmp_path, f"{TRASH_DIR_NAME}/deleted.jpg")
        assert exc_info.value.status_code == 400
        assert "trashed" in exc_info.value.detail

    def test_unsupported_extension_raises_400(self, tmp_path: Path) -> None:
        txt = tmp_path / "notes.txt"
        txt.write_bytes(b"hello")
        with pytest.raises(HTTPException) as exc_info:
            resolve_existing_source(tmp_path, "notes.txt")
        assert exc_info.value.status_code == 400
        assert "unsupported" in exc_info.value.detail

    def test_nonexistent_file_raises_404(self, tmp_path: Path) -> None:
        with pytest.raises(HTTPException) as exc_info:
            resolve_existing_source(tmp_path, "missing.jpg")
        assert exc_info.value.status_code == 404

    def test_symlink_escape_raises_400(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside.jpg"
        outside.write_bytes(b"\xff\xd8\xff")
        link = tmp_path / "escape.jpg"
        link.symlink_to(outside)
        with pytest.raises(HTTPException) as exc_info:
            resolve_existing_source(tmp_path, "escape.jpg")
        assert exc_info.value.status_code == 400

    def test_heic_extension_accepted(self, tmp_path: Path) -> None:
        img = tmp_path / "photo.heic"
        img.write_bytes(b"heic-data")
        result = resolve_existing_source(tmp_path, "photo.heic")
        assert result == img.resolve()

    def test_webp_extension_accepted(self, tmp_path: Path) -> None:
        img = tmp_path / "photo.webp"
        img.write_bytes(b"webp-data")
        result = resolve_existing_source(tmp_path, "photo.webp")
        assert result == img.resolve()

    def test_case_dir_as_string(self, tmp_path: Path) -> None:
        img = tmp_path / "test.jpeg"
        img.write_bytes(b"\xff\xd8\xff")
        result = resolve_existing_source(str(tmp_path), "test.jpeg")
        assert result == img.resolve()
