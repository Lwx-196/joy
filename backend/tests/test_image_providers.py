"""Tests for image_providers — registry build / chain resolution (pure, no network)."""
from __future__ import annotations

from backend.services import image_providers as ip


def _env():
    return {
        "TUZI_IMAGE_PRIMARY_BASE_URL": "https://ai.flashapi.top/v1",
        "TUZI_IMAGE_PRIMARY_API_KEY": "sk-primary",
        "TUZI_IMAGE_PRIMARY_MODELS": "gpt-image-2",
        "TUZI_IMAGE_PRIMARY_API_FORMAT": "images_edit",
        "TUZI_IMAGE_LEGACY_BASE_URL": "https://api.tu-zi.com/v1",
        "TUZI_IMAGE_LEGACY_API_KEY": "sk-legacy",
        "TUZI_IMAGE_LEGACY_MODEL": "gemini-3-pro-image-preview-2k-vip",
        # user-added provider
        "PANEL_IMG_CODE77_BASE_URL": "https://api.77code.example/v1",
        "PANEL_IMG_CODE77_API_KEY": "sk-77",
        "PANEL_IMG_CODE77_MODEL": "gpt-image-1",
        "PANEL_IMAGE_PROVIDERS": "code77,tuzi",
    }


def test_registry_seeds_tuzi_and_custom():
    reg = ip.build_registry(_env())
    assert reg["tuzi"].base_url == "https://ai.flashapi.top/v1"
    assert reg["tuzi"].model == "gpt-image-2"
    assert reg["tuzi"].ready
    assert reg["tuzi_legacy"].model == "gemini-3-pro-image-preview-2k-vip"
    assert reg["code77"].base_url == "https://api.77code.example/v1"
    assert reg["code77"].ready


def test_base_url_strips_chat_completions_suffix():
    env = dict(_env(), TUZI_IMAGE_PRIMARY_BASE_URL="https://x.example/v1/chat/completions")
    assert ip.build_registry(env)["tuzi"].base_url == "https://x.example/v1"


def test_models_csv_takes_first():
    env = dict(_env(), TUZI_IMAGE_PRIMARY_MODELS="gpt-image-2, gpt-image-2-vip")
    assert ip.build_registry(env)["tuzi"].model == "gpt-image-2"


def test_resolve_chain_respects_priority_csv():
    chain = ip.resolve_chain(_env())
    assert [p.name for p in chain] == ["code77", "tuzi"]


def test_resolve_chain_explicit_overrides():
    chain = ip.resolve_chain(_env(), explicit=["tuzi"])
    assert [p.name for p in chain] == ["tuzi"]


def test_resolve_chain_drops_unready():
    env = {k: v for k, v in _env().items() if k != "PANEL_IMG_CODE77_API_KEY"}
    chain = ip.resolve_chain(env, explicit=["code77", "tuzi"])
    assert [p.name for p in chain] == ["tuzi"]  # code77 missing key → dropped


def test_default_chain_is_tuzi():
    env = {k: v for k, v in _env().items() if k != "PANEL_IMAGE_PROVIDERS"}
    assert [p.name for p in ip.resolve_chain(env)] == ["tuzi"]


def test_missing_env_file_returns_empty():
    assert ip.load_env_file("/no/such/file.env") == {}


def test_generate_with_fallback_no_providers_raises():
    import pytest
    with pytest.raises(RuntimeError, match="no ready image provider"):
        ip.generate_with_fallback([], b"x", "prompt")
