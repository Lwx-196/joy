"""_ai_cache_key + _chain_size_sig 单测（4K 接入：分辨率配置掺 key，零配置零重烧）。"""
from __future__ import annotations

from backend.scripts.render_ai_enhanced_boards import _ai_cache_key, _chain_size_sig
from backend.services.image_providers import ImageProvider


def _provider(**kw) -> ImageProvider:
    base = dict(name="p", base_url="https://x/v1", api_key="sk-x", model="gpt-image-2")
    base.update(kw)
    return ImageProvider(**base)


def test_cache_key_default_sig_unchanged():
    # 空 sig 与旧二参调用字节一致 → 现存全部缓存保持热（零重烧保证）
    assert _ai_cache_key(b"png", "prompt") == _ai_cache_key(b"png", "prompt", "")


def test_cache_key_size_sig_changes_key():
    assert _ai_cache_key(b"png", "prompt") != _ai_cache_key(b"png", "prompt", "2560x2560@high")


def test_cache_key_distinct_sigs_distinct_keys():
    a = _ai_cache_key(b"png", "prompt", "2560x2560@high")
    b = _ai_cache_key(b"png", "prompt", "2048x2048@high")
    assert a != b


def test_chain_size_sig_takes_first_configured():
    chain = [
        _provider(name="tuzi"),                                          # 无 sizes
        _provider(name="rsta", sizes=("2560x2560",), quality="high"),    # 首个有 sizes
        _provider(name="flash", sizes=("2048x2048",)),
    ]
    assert _chain_size_sig(chain) == "2560x2560@high"


def test_chain_size_sig_quality_default_label():
    assert _chain_size_sig([_provider(sizes=("2048x2048",))]) == "2048x2048@default"


def test_chain_size_sig_empty_when_no_sizes():
    assert _chain_size_sig([_provider(), _provider(name="b")]) == ""
    assert _chain_size_sig([]) == ""
