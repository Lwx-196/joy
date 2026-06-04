from __future__ import annotations

import json
from pathlib import Path


def test_deepseek_client_uses_official_chat_completions_endpoint() -> None:
    from backend.services.deepseek_client import DeepSeekClient

    config = DeepSeekClient(env={"DEEPSEEK_API_KEY": "unit-key"}).configure()

    assert config.ready is True
    assert config.model == "deepseek-chat"
    assert config.endpoint == "https://api.deepseek.com/chat/completions"


def test_deepseek_client_posts_json_chat_payload_and_parses_response() -> None:
    from backend.services.deepseek_client import DeepSeekClient

    captured: dict = {}

    def post_json(url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        captured.update({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        return {
            "id": "deepseek-test-response",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "production_decision": {
                                    "keep_baseline_default": True,
                                    "can_promote_comfyui": False,
                                    "can_enable_vlm_autopass": False,
                                    "rationale": "unit",
                                },
                                "blockers": [],
                                "efficiency_actions": [],
                                "validation_plan": [],
                                "unverified_items": [],
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }

    client = DeepSeekClient(env={"DEEPSEEK_API_KEY": "unit-key"}, post_json=post_json, sleep=lambda _seconds: None)
    response = client.complete_json(system_prompt="system", user_prompt="user", timeout=9)

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer unit-key"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["messages"][0]["content"] == "system"
    assert response.parsed["production_decision"]["keep_baseline_default"] is True
    assert response.input_tokens == 11
    assert response.output_tokens == 7


def test_coordination_dry_run_reads_real_reports_and_redacts_secrets(tmp_path: Path) -> None:
    from backend.scripts.deepseek_execution_coordinator import run_coordination

    report_path = tmp_path / "gate.json"
    report_path.write_text(
        json.dumps(
            {
                "production_ready": False,
                "api_key": "secret-value",
                "reason_code": "hard_defects_present",
            }
        ),
        encoding="utf-8",
    )

    report = run_coordination(report_paths=[report_path], dry_run=True, env={})

    assert report["run_status"] == "dry_run_evidence_bundle_only"
    assert report["evidence_bundle"]["read_report_count"] == 1
    assert report["evidence_bundle"]["reports"][0]["summary_fields"]["production_ready"] is False
    assert report["evidence_bundle"]["reports"][0]["summary_fields"]["reason_code"] == "hard_defects_present"
    content = report["evidence_bundle"]["reports"][0]["content_text"]
    assert "***REDACTED***" in content
    assert "secret-value" not in content
    assert report["deepseek_advisory"] is None


def test_evidence_bundle_computes_current_state_for_live_status_and_candidate30(tmp_path: Path) -> None:
    from backend.scripts.deepseek_execution_coordinator import build_evidence_bundle

    live_status = tmp_path / "live_service_status_report.json"
    live_status.write_text(
        json.dumps(
            {
                "generated_at": "2026-05-18T19:00:38+00:00",
                "summary": {"backend_5295_case45": "200"},
                "checks": [{"name": "backend_5295_case45", "ok": True}],
            }
        ),
        encoding="utf-8",
    )
    candidate30 = tmp_path / "comfyui_local_region_candidate30_stability_report.json"
    candidate30.write_text(
        json.dumps(
            {
                "real_record_count": 30,
                "ok_count": 30,
                "failed_count": 0,
                "mps_error_count": 0,
                "skipped_pair_candidate_count": 3,
            }
        ),
        encoding="utf-8",
    )

    bundle = build_evidence_bundle([live_status, candidate30])

    state = bundle["computed_current_state"]
    assert state["live_services"]["summary"]["backend_5295_case45"] == "200"
    assert state["live_services"]["all_ok"] is True
    stability = state["local_region_candidate30_stability"]
    assert stability["real_record_count"] == 30
    assert stability["failed_count"] == 0
    assert stability["stability_blocker"] is False
    assert stability["skipped_rows_block_current_30_run"] is False


def test_coordination_missing_key_fails_closed_without_advisory(tmp_path: Path) -> None:
    from backend.scripts.deepseek_execution_coordinator import run_coordination

    report_path = tmp_path / "gate.json"
    report_path.write_text(json.dumps({"production_ready": False}), encoding="utf-8")

    report = run_coordination(report_paths=[report_path], env={})

    assert report["run_status"] == "blocked_missing_deepseek_api_key"
    assert report["deepseek_api"]["ready"] is False
    assert report["deepseek_advisory"] is None
    assert "未验证/无法获取" in report["unverified_items"][0]


def test_coordination_clamps_deepseek_advisory_to_fail_closed_when_evidence_blocks(tmp_path: Path) -> None:
    from backend.services.deepseek_client import DeepSeekClient
    from backend.scripts.deepseek_execution_coordinator import run_coordination

    report_path = tmp_path / "gate.json"
    report_path.write_text(json.dumps({"production_ready": False, "promote_to_default": False}), encoding="utf-8")

    def post_json(_url: str, _headers: dict[str, str], _payload: dict, _timeout: float) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "production_decision": {
                                    "keep_baseline_default": False,
                                    "can_promote_comfyui": True,
                                    "can_enable_vlm_autopass": True,
                                    "rationale": "model suggested promotion",
                                },
                                "blockers": [],
                                "efficiency_actions": [{"id": "parallel-report-triage"}],
                                "validation_plan": [],
                                "unverified_items": [],
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3},
        }

    client = DeepSeekClient(env={"DEEPSEEK_API_KEY": "unit-key"}, post_json=post_json, sleep=lambda _seconds: None)
    report = run_coordination(report_paths=[report_path], client=client)

    decision = report["deepseek_advisory"]["production_decision"]
    assert report["run_status"] == "completed"
    assert decision["keep_baseline_default"] is True
    assert decision["can_promote_comfyui"] is False
    assert decision["can_enable_vlm_autopass"] is False
    assert "clamped fail-closed" in decision["rationale"]
