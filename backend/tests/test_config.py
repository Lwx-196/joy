from __future__ import annotations

from backend import stress
from backend.config import REPO_ROOT, clear_settings_cache, get_settings


def test_settings_default_paths(monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.delenv("CASE_WORKBENCH_DB_PATH", raising=False)
    monkeypatch.delenv("CASE_WORKBENCH_COMFYUI_WORKFLOW_DIR", raising=False)
    monkeypatch.delenv("CASE_WORKBENCH_COMFYUI_BASE_URL", raising=False)
    monkeypatch.delenv("COMFYUI_BASE_URL", raising=False)

    settings = get_settings()

    assert settings.db_path() == (REPO_ROOT / "case-workbench.db").resolve()
    assert settings.comfyui_workflow_dir_path() == (REPO_ROOT / "comfyui-workflows").resolve()
    assert settings.comfyui_base_url_value() == "http://127.0.0.1:8188"


def test_settings_env_overrides(monkeypatch, tmp_path) -> None:
    clear_settings_cache()
    monkeypatch.setenv("CASE_WORKBENCH_DB_PATH", str(tmp_path / "case.db"))
    monkeypatch.setenv("CASE_WORKBENCH_OUTPUT_ROOT", str(tmp_path / "out"))
    monkeypatch.setenv("CASE_WORKBENCH_SIMULATION_ROOT", str(tmp_path / "sim"))
    monkeypatch.setenv("CASE_WORKBENCH_COMFYUI_BASE_URL", "http://127.0.0.1:8199/")
    monkeypatch.setenv("CASE_WORKBENCH_COMFYUI_MAX_CONCURRENCY", "3")

    settings = get_settings()

    assert settings.db_path() == (tmp_path / "case.db").resolve()
    assert settings.output_root() == (tmp_path / "out").resolve()
    assert settings.simulation_root(REPO_ROOT / "default-sim") == (tmp_path / "sim").resolve()
    assert settings.comfyui_base_url_value() == "http://127.0.0.1:8199"
    assert settings.comfyui_max_concurrency == 3


def test_stress_helpers_read_current_environment(monkeypatch, tmp_path) -> None:
    clear_settings_cache()
    monkeypatch.setenv("CASE_WORKBENCH_DB_PATH", str(tmp_path / "stress.db"))
    monkeypatch.setenv("CASE_WORKBENCH_STRESS_MODE", "1")
    monkeypatch.setenv("CASE_WORKBENCH_AI_ALLOW_EXTERNAL", "1")
    monkeypatch.setenv("CASE_WORKBENCH_STRESS_RUN_ID", "run-1")

    assert stress.configured_db_path() == (tmp_path / "stress.db").resolve()
    assert stress.is_stress_mode() is True
    assert stress.allow_external_ai() is True
    assert stress.stress_run_id() == "run-1"

    monkeypatch.setenv("CASE_WORKBENCH_STRESS_MODE", "0")
    clear_settings_cache()
    assert stress.is_stress_mode() is False


def test_settings_cache_can_be_cleared(monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.setenv("CASE_WORKBENCH_AI_QUALITY", "4k")
    first = get_settings()

    monkeypatch.setenv("CASE_WORKBENCH_AI_QUALITY", "2k")
    assert get_settings().ai_quality == "4k"

    clear_settings_cache()
    second = get_settings()
    assert second.ai_quality == "2k"
    assert second is not first


def test_secret_values_are_masked(monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-secret")

    settings = get_settings()

    assert settings.deepseek_api_key.get_secret_value() == "unit-secret"
    assert "unit-secret" not in repr(settings)
