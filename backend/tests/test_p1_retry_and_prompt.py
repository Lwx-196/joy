"""Step 3 of 4-mode plan: P1 retry + identity-preservation prompt prefix.

Verifies:
  * P1 retries once on subprocess failure (returncode != 0, timeout, parse error).
  * Prompt now leads with identity-preservation clause.
  * After all retries exhausted, silent-fail contract preserves input path.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from backend import ai_generation_adapter


def _make_test_jpg(tmp_path: Path, name: str = "input.jpg", size=(640, 480)) -> Path:
    img = Image.new("RGB", size, color=(180, 180, 180))
    p = tmp_path / name
    img.save(p, format="JPEG", quality=90)
    return p


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------

class TestP1Retry:

    def test_subprocess_nonzero_exit_triggers_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        src = _make_test_jpg(tmp_path)
        call_log = []

        def _fake_run(cmd, **kwargs):
            call_log.append(cmd)
            return subprocess.CompletedProcess(
                cmd, returncode=1, stdout="", stderr="simulated error"
            )

        monkeypatch.setattr(ai_generation_adapter.subprocess, "run", _fake_run)
        monkeypatch.setattr(ai_generation_adapter, "PS_ENHANCE_SCRIPT", tmp_path)
        # PS_ENHANCE_SCRIPT.is_file() must be True for the helper to proceed.
        # Use a real file as the script path.
        fake_script = tmp_path / "fake_ps_script.js"
        fake_script.write_text("// fake")
        monkeypatch.setattr(ai_generation_adapter, "PS_ENHANCE_SCRIPT", fake_script)

        result = ai_generation_adapter.run_direct_clinical_enhancement(
            src, brand="md_ai", focus_targets=["面颊"],
        )
        # All attempts failed → silent-fail returns input
        assert result == src
        # 1 + retry_count=1 → 2 attempts total
        assert len(call_log) == 2

    def test_first_attempt_succeeds_no_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        src = _make_test_jpg(tmp_path)
        generated = tmp_path / "out.jpg"
        Image.new("RGB", (320, 240), color=(200, 200, 200)).save(generated, format="JPEG")

        call_log = []
        def _fake_run(cmd, **kwargs):
            call_log.append(cmd)
            return subprocess.CompletedProcess(
                cmd, returncode=0,
                stdout=json.dumps({"success": True, "imagePath": str(generated)}),
                stderr="",
            )

        monkeypatch.setattr(ai_generation_adapter.subprocess, "run", _fake_run)
        fake_script = tmp_path / "fake_ps_script.js"
        fake_script.write_text("// fake")
        monkeypatch.setattr(ai_generation_adapter, "PS_ENHANCE_SCRIPT", fake_script)

        result = ai_generation_adapter.run_direct_clinical_enhancement(
            src, brand="md_ai", focus_targets=["面颊"],
        )
        assert result == generated
        assert len(call_log) == 1  # no retry needed

    def test_retry_eventually_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        src = _make_test_jpg(tmp_path)
        generated = tmp_path / "out.jpg"
        Image.new("RGB", (320, 240), color=(200, 200, 200)).save(generated, format="JPEG")

        call_log = []
        def _fake_run(cmd, **kwargs):
            call_log.append(cmd)
            if len(call_log) == 1:
                # First attempt: simulate transient failure
                return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="EAGAIN")
            # Second attempt: success
            return subprocess.CompletedProcess(
                cmd, returncode=0,
                stdout=json.dumps({"success": True, "imagePath": str(generated)}),
                stderr="",
            )

        monkeypatch.setattr(ai_generation_adapter.subprocess, "run", _fake_run)
        fake_script = tmp_path / "fake_ps_script.js"
        fake_script.write_text("// fake")
        monkeypatch.setattr(ai_generation_adapter, "PS_ENHANCE_SCRIPT", fake_script)

        result = ai_generation_adapter.run_direct_clinical_enhancement(
            src, brand="md_ai", focus_targets=["面颊"],
        )
        # Recovery path returns generated
        assert result == generated
        assert len(call_log) == 2

    def test_timeout_triggers_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        src = _make_test_jpg(tmp_path)
        call_log = []

        def _fake_run(cmd, **kwargs):
            call_log.append(cmd)
            raise subprocess.TimeoutExpired(cmd, timeout=240)

        monkeypatch.setattr(ai_generation_adapter.subprocess, "run", _fake_run)
        fake_script = tmp_path / "fake_ps_script.js"
        fake_script.write_text("// fake")
        monkeypatch.setattr(ai_generation_adapter, "PS_ENHANCE_SCRIPT", fake_script)

        result = ai_generation_adapter.run_direct_clinical_enhancement(
            src, brand="md_ai", focus_targets=["面颊"],
        )
        assert result == src
        # Timeout each attempt → 2 total attempts (1 + 1 retry)
        assert len(call_log) == 2

    def test_retry_count_zero_disables_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Explicit retry_count=0 → single attempt only, no retries."""
        src = _make_test_jpg(tmp_path)
        call_log = []

        def _fake_run(cmd, **kwargs):
            call_log.append(cmd)
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="x")

        monkeypatch.setattr(ai_generation_adapter.subprocess, "run", _fake_run)
        fake_script = tmp_path / "fake_ps_script.js"
        fake_script.write_text("// fake")
        monkeypatch.setattr(ai_generation_adapter, "PS_ENHANCE_SCRIPT", fake_script)

        result = ai_generation_adapter.run_direct_clinical_enhancement(
            src, brand="md_ai", focus_targets=["面颊"], retry_count=0,
        )
        assert result == src
        assert len(call_log) == 1


# ---------------------------------------------------------------------------
# Identity-preservation prompt prefix
# ---------------------------------------------------------------------------

class TestPromptPrefix:

    def test_prompt_leads_with_identity_clause(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        src = _make_test_jpg(tmp_path)
        captured: dict = {}

        def _capture_cmd(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")

        monkeypatch.setattr(ai_generation_adapter.subprocess, "run", _capture_cmd)
        fake_script = tmp_path / "fake_ps_script.js"
        fake_script.write_text("// fake")
        monkeypatch.setattr(ai_generation_adapter, "PS_ENHANCE_SCRIPT", fake_script)

        ai_generation_adapter.run_direct_clinical_enhancement(
            src, brand="md_ai", focus_targets=["面颊"], retry_count=0,
        )

        # Extract --prompt arg
        cmd = captured["cmd"]
        idx = cmd.index("--prompt")
        prompt = cmd[idx + 1]
        # Identity-preservation reinforcement must lead the prompt
        assert prompt.startswith("CRITICAL:"), f"prompt didn't start with CRITICAL clause: {prompt[:80]}"
        assert "preserve patient identity" in prompt.lower()
        assert "no over-smoothing" in prompt.lower()
