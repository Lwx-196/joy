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


# ---------- 4K size/quality 接入（owner 2026-06-11 拍板：rsta 优先高分辨率） ----------


def test_seed_sizes_and_quality_from_env():
    env = dict(_env(),
               TUZI_IMAGE_PRIMARY_SIZES="2560x2560, 2048x2048",
               TUZI_IMAGE_PRIMARY_QUALITY="high")
    p = ip.build_registry(env)["tuzi"]
    assert p.sizes == ("2560x2560", "2048x2048")
    assert p.quality == "high"


def test_seed_sizes_default_empty():
    p = ip.build_registry(_env())["tuzi"]
    assert p.sizes == ()
    assert p.quality == ""


def test_size_rungs_ladder_ends_with_bare():
    p = ip.build_registry(dict(_env(),
                               TUZI_IMAGE_PRIMARY_SIZES="2560x2560",
                               TUZI_IMAGE_PRIMARY_QUALITY="high"))["tuzi"]
    assert ip._size_rungs(p) == [{"size": "2560x2560", "quality": "high"}, {}]


def test_size_rungs_no_sizes_is_single_bare_rung():
    # 无 sizes 配置 = 单档空 dict = 与旧行为字节一致
    p = ip.build_registry(_env())["tuzi"]
    assert ip._size_rungs(p) == [{}]


def test_size_rungs_sizes_without_quality():
    p = ip.build_registry(dict(_env(), TUZI_IMAGE_PRIMARY_SIZES="2048x2048"))["tuzi"]
    assert ip._size_rungs(p) == [{"size": "2048x2048"}, {}]


def test_is_size_rejection_matches_rsta_errors():
    # rsta 实测三种拒绝文案（2026-06-11 探针）
    assert ip._is_size_rejection(400, '{"code": "invalid_value", "message": "Invalid size \'4096x4096\'..."}')
    assert ip._is_size_rejection(400, '{"message": "Requested resolution exceeds the current pixel budget."}')
    assert ip._is_size_rejection(400, '{"type": "image_generation_user_error"}')


def test_is_size_rejection_ignores_other_errors():
    assert not ip._is_size_rejection(401, "invalid size")   # 非 400 = 鉴权类，走换 provider
    assert not ip._is_size_rejection(400, '{"message": "invalid prompt"}')
    assert not ip._is_size_rejection(500, "pixel budget")


# ---------- stream=true SSE（2026-06-12 探针：破 rsta 反代 60s read timeout） ----------


def test_seed_stream_from_env():
    p = ip.build_registry(dict(_env(), TUZI_IMAGE_PRIMARY_STREAM="1"))["tuzi"]
    assert p.stream is True


def test_seed_stream_truthy_variants():
    for v in ("true", "TRUE", "yes"):
        assert ip.build_registry(dict(_env(), TUZI_IMAGE_PRIMARY_STREAM=v))["tuzi"].stream is True
    for v in ("", "0", "false", "off"):
        assert ip.build_registry(dict(_env(), TUZI_IMAGE_PRIMARY_STREAM=v))["tuzi"].stream is False


def test_seed_stream_default_off():
    # 未配 STREAM = 行为与旧版字节一致（tuzi/flashapi 等不受影响）
    assert ip.build_registry(_env())["tuzi"].stream is False


def test_sse_collect_b64_completed_event_wins():
    lines = [
        b":",   # keepalive 注释行（rsta 实测每 10-30s 一发）
        b"",
        b'data: {"type": "image_edit.partial_image", "b64_json": "AAA"}',
        b'data: {"type": "image_edit.completed", "b64_json": "BBB"}',
        b'data: {"b64_json": "CCC"}',   # completed 后即收口，不该读到
    ]
    assert ip._sse_collect_b64(lines) == "BBB"


def test_sse_collect_b64_plain_b64_then_done():
    lines = [b'data: {"b64_json": "CCC"}', b"data: [DONE]"]
    assert ip._sse_collect_b64(lines) == "CCC"


def test_sse_collect_b64_completed_without_b64_keeps_last_partial():
    lines = [
        b'data: {"b64_json": "DDD"}',
        b'data: {"type": "image_edit.completed"}',
    ]
    assert ip._sse_collect_b64(lines) == "DDD"


def test_sse_collect_b64_skips_malformed_json():
    lines = [b"data: not-json{{", b'data: {"b64_json": "EEE"}']
    assert ip._sse_collect_b64(lines) == "EEE"


def test_sse_collect_b64_no_image_returns_none():
    assert ip._sse_collect_b64([b":", b"", b'data: {"type": "ping"}']) is None
