"""Centralized environment settings for case-workbench runtime paths and gates."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


def _path_or_default(raw: str | Path | None, default: Path) -> Path:
    value = str(raw or "").strip()
    if not value:
        return default.resolve()
    return Path(value).expanduser().resolve()


def _optional_path(raw: str | Path | None) -> Path | None:
    value = str(raw or "").strip()
    if not value:
        return None
    return Path(value).expanduser().resolve()


class AppSettings(BaseSettings):
    """Pydantic-backed settings with the existing env var names preserved."""

    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    case_workbench_db_path: str = Field(default="", validation_alias="CASE_WORKBENCH_DB_PATH")
    sqlite_busy_timeout_ms: int = Field(default=5000, validation_alias="CASE_WORKBENCH_SQLITE_BUSY_TIMEOUT_MS")
    schema_lock_timeout_sec: int = Field(default=30, validation_alias="CASE_WORKBENCH_SCHEMA_LOCK_TIMEOUT_SEC")
    case_workbench_output_root: str = Field(default="", validation_alias="CASE_WORKBENCH_OUTPUT_ROOT")
    case_workbench_simulation_root: str = Field(default="", validation_alias="CASE_WORKBENCH_SIMULATION_ROOT")
    case_workbench_stress_mode: bool = Field(default=False, validation_alias="CASE_WORKBENCH_STRESS_MODE")
    case_workbench_stress_run_id: str = Field(default="", validation_alias="CASE_WORKBENCH_STRESS_RUN_ID")
    case_workbench_stress_allow_destructive: bool = Field(
        default=False,
        validation_alias="CASE_WORKBENCH_STRESS_ALLOW_DESTRUCTIVE",
    )
    case_workbench_ai_allow_external: bool = Field(default=False, validation_alias="CASE_WORKBENCH_AI_ALLOW_EXTERNAL")

    deepseek_api_key: SecretStr = Field(default="", validation_alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="https://api.deepseek.com", validation_alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field(default="deepseek-chat", validation_alias="DEEPSEEK_MODEL")
    deepseek_retry_max_attempts: int = Field(default=3, validation_alias="DEEPSEEK_RETRY_MAX_ATTEMPTS")
    deepseek_retry_base_seconds: float = Field(default=1.0, validation_alias="DEEPSEEK_RETRY_BASE_SECONDS")
    deepseek_retry_max_seconds: float = Field(default=30.0, validation_alias="DEEPSEEK_RETRY_MAX_SECONDS")

    ai_quality: str = Field(default="4k", validation_alias="CASE_WORKBENCH_AI_QUALITY")
    ai_timeout_sec: int = Field(default=240, validation_alias="CASE_WORKBENCH_AI_TIMEOUT_SEC")
    ps_env_file: str = Field(default="", validation_alias="CASE_WORKBENCH_PS_ENV_FILE")
    ps_enhance_script: str = Field(default="", validation_alias="CASE_WORKBENCH_PS_ENHANCE_SCRIPT")

    comfyui_base_url: str = Field(
        default="http://127.0.0.1:8188",
        validation_alias=AliasChoices("CASE_WORKBENCH_COMFYUI_BASE_URL", "COMFYUI_BASE_URL"),
    )
    comfyui_workflow_dir: str = Field(default="", validation_alias="CASE_WORKBENCH_COMFYUI_WORKFLOW_DIR")
    comfyui_max_concurrency: int = Field(default=1, validation_alias="CASE_WORKBENCH_COMFYUI_MAX_CONCURRENCY")
    comfyui_min_free_memory_mb: int = Field(default=1024, validation_alias="CASE_WORKBENCH_COMFYUI_MIN_FREE_MEMORY_MB")
    comfyui_timeout_sec: int = Field(default=300, validation_alias="CASE_WORKBENCH_COMFYUI_TIMEOUT_SEC")
    comfyui_max_retries: int = Field(default=2, validation_alias="CASE_WORKBENCH_COMFYUI_MAX_RETRIES")
    comfyui_model_root: str = Field(default="", validation_alias="CASE_WORKBENCH_COMFYUI_MODEL_ROOT")

    def db_path(self) -> Path:
        return _path_or_default(self.case_workbench_db_path, REPO_ROOT / "case-workbench.db")

    def output_root(self) -> Path | None:
        return _optional_path(self.case_workbench_output_root)

    def simulation_root(self, default_root: Path) -> Path:
        explicit = _optional_path(self.case_workbench_simulation_root)
        if explicit is not None:
            return explicit
        output_root = self.output_root()
        if output_root is not None:
            return output_root / "simulation_jobs"
        return default_root

    def stress_run_id(self) -> str | None:
        value = self.case_workbench_stress_run_id.strip()
        return value or None

    def ps_enhance_script_path(self) -> Path:
        return _path_or_default(
            self.ps_enhance_script,
            Path("/Users/a1234/Desktop/飞书Claude/skills/case-layout-board/scripts/case_layout_enhance.js"),
        )

    def ps_env_file_path(self) -> Path:
        return _path_or_default(
            self.ps_env_file,
            Path("/Users/a1234/Desktop/飞书Claude/claude-feishu-bridge/.env"),
        )

    def comfyui_base_url_value(self) -> str:
        return (self.comfyui_base_url or "http://127.0.0.1:8188").strip().rstrip("/")

    def comfyui_workflow_dir_path(self) -> Path:
        return _path_or_default(self.comfyui_workflow_dir, REPO_ROOT / "comfyui-workflows")

    def comfyui_model_root_path(self) -> Path:
        return _path_or_default(
            self.comfyui_model_root,
            Path("/Users/a1234/Desktop/飞书Claude/ComfyUI/models"),
        )


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
