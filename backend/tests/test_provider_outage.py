"""F3 — provider outage 短路守卫单测（全 mock，零真调 API）。

验证 generate_with_fallback 在某 provider 连续 outage-class 失败后短路跳过它、
成功即清零、全 outage 抛 ProviderOutageError、瞬时错误不误触发短路。
"""
import pytest

from backend.services import image_providers as ip
from backend.services.image_providers import (
    ImageProvider, ProviderOutageError, generate_with_fallback,
)


def _prov(name: str) -> ImageProvider:
    return ImageProvider(name=name, base_url=f"https://{name}.test", api_key="k", model="gpt-image-2")


@pytest.fixture(autouse=True)
def _reset_outage():
    ip._PROVIDER_OUTAGE.clear()
    yield
    ip._PROVIDER_OUTAGE.clear()


def _server_error():
    return RuntimeError('SSE error event: {"type": "server_error", "message": "boom"}')


def test_outage_provider_skipped_after_threshold(monkeypatch):
    """rsta 连续 server_error → 第 3 槽起直接跳过 rsta，不再空跑（call 计数实锤）。"""
    rsta, tuzi = _prov("rsta"), _prov("tuzi")
    calls = {"rsta": 0, "tuzi": 0}

    def fake_images_edit(provider, image_bytes, prompt, *, mime="image/png", size_override=None):
        calls[provider.name] += 1
        if provider.name == "rsta":
            raise _server_error()
        return b"TUZI_LOWRES"

    monkeypatch.setattr(ip, "images_edit", fake_images_edit)

    for _ in range(3):
        raw, name = generate_with_fallback([rsta, tuzi], b"png", "p", mime="image/png")
        assert (raw, name) == (b"TUZI_LOWRES", "tuzi")

    # 旧行为 = rsta 2 attempt × 3 槽 = 6 次空跑；短路后：槽1×1 + 槽2×1 + 槽3 跳过 = 2 次。
    assert calls["rsta"] == 2, f"rsta 应只被试 2 次（之后短路跳过），实际 {calls['rsta']}"
    assert calls["tuzi"] == 3


def test_outage_class_skips_second_attempt(monkeypatch):
    """outage-class 错误不重试 2nd attempt（每槽省 ~150s）：单槽 rsta 只调 1 次。"""
    rsta, tuzi = _prov("rsta"), _prov("tuzi")
    calls = {"rsta": 0, "tuzi": 0}

    def fake(provider, image_bytes, prompt, *, mime="image/png", size_override=None):
        calls[provider.name] += 1
        if provider.name == "rsta":
            raise _server_error()
        return b"OK"

    monkeypatch.setattr(ip, "images_edit", fake)
    generate_with_fallback([rsta, tuzi], b"png", "p")
    assert calls["rsta"] == 1, "outage 类应跳过 2nd attempt"


def test_success_clears_outage(monkeypatch):
    """provider 恢复（成功）即清零毒化计数，不影响后续。"""
    rsta = _prov("rsta")
    state = {"fail": True}

    def fake(provider, image_bytes, prompt, *, mime="image/png", size_override=None):
        if state["fail"]:
            raise _server_error()
        return b"RECOVERED"

    monkeypatch.setattr(ip, "images_edit", fake)
    # 先失败一次 → 记一次 outage
    with pytest.raises(ProviderOutageError):
        generate_with_fallback([rsta], b"png", "p")
    assert "rsta" in ip._PROVIDER_OUTAGE
    # 恢复成功 → 清零
    state["fail"] = False
    raw, name = generate_with_fallback([rsta], b"png", "p")
    assert (raw, name) == (b"RECOVERED", "rsta")
    assert "rsta" not in ip._PROVIDER_OUTAGE


def test_all_providers_outage_raises_outage_error(monkeypatch):
    """全 provider outage → ProviderOutageError（前端可据此给「服务商暂不可用」清晰提示）。"""
    rsta, tuzi = _prov("rsta"), _prov("tuzi")

    def fake(provider, image_bytes, prompt, *, mime="image/png", size_override=None):
        raise RuntimeError(f"{provider.name} images/edits HTTP 503: upstream down")

    monkeypatch.setattr(ip, "images_edit", fake)
    with pytest.raises(ProviderOutageError):
        generate_with_fallback([rsta, tuzi], b"png", "p")


def test_transient_error_does_not_trip_breaker(monkeypatch):
    """瞬时（非 outage）错误：仍重试 2 attempt、不标记 outage、抛普通 RuntimeError（非 Outage）。"""
    rsta = _prov("rsta")
    calls = {"n": 0}

    def fake(provider, image_bytes, prompt, *, mime="image/png", size_override=None):
        calls["n"] += 1
        raise ValueError("connection reset by peer")  # 非 outage-class

    monkeypatch.setattr(ip, "images_edit", fake)
    with pytest.raises(RuntimeError) as ei:
        generate_with_fallback([rsta], b"png", "p")
    assert not isinstance(ei.value, ProviderOutageError)
    assert calls["n"] == 2, "瞬时错误应重试 2nd attempt"
    assert "rsta" not in ip._PROVIDER_OUTAGE


def test_outage_window_expiry():
    """窗口外不再视为 outage（helper 直测，显式时间戳）。"""
    ip._mark_outage("rsta", 1000.0)
    ip._mark_outage("rsta", 1000.5)   # count=2，窗口内累积
    assert ip._is_outaged("rsta", 1050.0) is True       # 窗口内 → 短路
    assert ip._is_outaged("rsta", 1000.0 + ip._OUTAGE_WINDOW_S + 1) is False  # 超窗 → 失效


def test_timeout_is_outage_class():
    """SSE 墙钟 cap TimeoutError 归 outage-class。"""
    assert ip._is_outage_error(TimeoutError("SSE stream wall-clock cap exceeded (300s)")) is True
    assert ip._is_outage_error(ValueError("random")) is False
