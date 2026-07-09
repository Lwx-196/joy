"""T52 real independent VLM judge runner gates."""
from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path


def _write_tiny_png(path: Path) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _packet(tmp_path: Path) -> dict:
    baseline = tmp_path / "baseline.png"
    candidate = tmp_path / "candidate.png"
    _write_tiny_png(baseline)
    _write_tiny_png(candidate)
    return {
        "scope": "t51_independent_vlm_judge_packet_v1",
        "judge_item_count": 1,
        "human_label_count": 1,
        "judge_items": [
            {
                "ab_unit_id": "unit-1",
                "case_id": 101,
                "view": "front",
                "workflow": "local_region_enhance_v1@conservative",
                "baseline": {
                    "variant": "ps_model_router@default",
                    "packet_path": str(baseline),
                    "packet_relative_path": "baseline.png",
                },
                "candidate": {
                    "variant": "comfyui_local:local_region_enhance_v1@conservative",
                    "packet_path": str(candidate),
                    "packet_relative_path": "candidate.png",
                },
                "criteria": ["medical aesthetic delivery quality", "artifact-free local edit"],
            }
        ],
        "human_labels": [
            {
                "ab_unit_id": "unit-1",
                "winner_role": "baseline",
                "winner_variant": "ps_model_router@default",
                "reviewer": "human-reviewer",
            }
        ],
    }


def test_runner_blocks_without_real_provider_config(tmp_path: Path) -> None:
    from backend.scripts import comfyui_vlm_judge_runner

    results, report = comfyui_vlm_judge_runner.run_vlm_judge(
        _packet(tmp_path),
        packet_root=tmp_path,
        env={},
    )

    assert report["run_status"] == "blocked_missing_vlm_provider_config"
    assert "未验证/无法获取" in report["decision"]
    assert results["judgments"] == []
    assert results["real_vlm_judge"] is False


def test_openai_payload_is_blind_and_uses_real_packet_images(tmp_path: Path) -> None:
    from backend.scripts import comfyui_vlm_judge_runner

    packet = _packet(tmp_path)
    payload = comfyui_vlm_judge_runner.build_openai_responses_payload(
        packet["judge_items"][0],
        model="vision-quality-model",
        packet_root=tmp_path,
    )

    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload["model"] == "vision-quality-model"
    assert serialized.count('"type": "input_image"') == 2
    assert "data:image/png;base64," in serialized
    assert "human_labels" not in serialized
    assert "human_winner_role" not in serialized
    assert "human-reviewer" not in serialized
    assert payload["text"]["format"]["type"] == "json_object"


def test_judge_prompt_requires_structured_scores_and_manual_review_fallback(tmp_path: Path) -> None:
    from backend.scripts import comfyui_vlm_judge_runner

    prompt = comfyui_vlm_judge_runner._judge_prompt(_packet(tmp_path)["judge_items"][0])

    assert "criterion_scores" in prompt
    assert "1-5" in prompt
    assert "visual_evidence_summary" in prompt
    assert "tie" in prompt
    assert "manual_review" in prompt
    assert "Importable judgments must use winner_role baseline or candidate" in prompt


def test_openai_response_parses_to_importable_real_judgment(tmp_path: Path) -> None:
    from backend.scripts import comfyui_vlm_judge_runner

    item = _packet(tmp_path)["judge_items"][0]
    response = {
        "id": "resp_real_shape",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "ab_unit_id": "unit-1",
                                "winner_role": "candidate",
                                "confidence": 0.82,
                                "rationale": "Candidate has fewer visible artifacts.",
                                "risk_flags": ["artifact"],
                            }
                        ),
                    }
                ],
            }
        ],
    }

    judgment = comfyui_vlm_judge_runner.parse_openai_responses_judgment(
        response,
        item=item,
        provider="openai_responses",
        model="vision-quality-model",
    )

    assert judgment["ab_unit_id"] == "unit-1"
    assert judgment["winner_role"] == "candidate"
    assert judgment["judge_provider"] == "openai_responses"
    assert judgment["judge_model"] == "vision-quality-model"
    assert judgment["provider_response_id"] == "resp_real_shape"


def test_env_file_loads_gemini_provider_without_shell_export(tmp_path: Path) -> None:
    from backend.scripts import comfyui_vlm_judge_runner

    env_file = tmp_path / "judge.env"
    env_file.write_text(
        "\n".join(
            [
                "CASE_WORKBENCH_VLM_JUDGE_PROVIDER=gemini_generate_content",
                "CASE_WORKBENCH_VLM_JUDGE_MODEL=gemini-2.5-flash",
                "CASE_WORKBENCH_VLM_JUDGE_API_KEY=local-test-key",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    env = comfyui_vlm_judge_runner.load_env_files({}, [env_file])
    results, report = comfyui_vlm_judge_runner.run_vlm_judge(
        _packet(tmp_path),
        packet_root=tmp_path,
        env=env,
        post_json=lambda _url, _headers, _payload, _timeout: {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "ab_unit_id": "unit-1",
                                        "winner_role": "baseline",
                                        "confidence": 0.91,
                                        "rationale": "Baseline is cleaner.",
                                        "risk_flags": [],
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        },
    )

    assert report["run_status"] == "completed_real_vlm_judge"
    assert results["provider"] == "gemini_generate_content"
    assert results["model"] == "gemini-2.5-flash"
    assert results["judgments"][0]["judge_provider"] == "gemini_generate_content"


def test_manual_review_response_is_not_importable_or_promotable(tmp_path: Path) -> None:
    from backend.scripts import comfyui_vlm_judge_runner

    results, report = comfyui_vlm_judge_runner.run_vlm_judge(
        _packet(tmp_path),
        packet_root=tmp_path,
        env={
            "CASE_WORKBENCH_VLM_JUDGE_PROVIDER": "gemini_generate_content",
            "CASE_WORKBENCH_VLM_JUDGE_MODEL": "gemini-2.5-flash",
            "CASE_WORKBENCH_VLM_JUDGE_API_KEY": "local-test-key",
        },
        post_json=lambda _url, _headers, _payload, _timeout: {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "ab_unit_id": "unit-1",
                                        "winner_role": "manual_review",
                                        "confidence": 0.61,
                                        "visual_evidence_summary": "Both images have ambiguous artifacts.",
                                        "criterion_scores": {
                                            "medical aesthetic delivery quality": {"baseline": 3, "candidate": 3}
                                        },
                                        "rationale": "Ambiguous winner.",
                                        "risk_flags": ["low_confidence"],
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        },
    )

    assert report["run_status"] == "blocked_no_successful_vlm_judgments"
    assert report["manual_review_count"] == 1
    assert report["failed_judgment_count"] == 0
    assert results["judgments"] == []
    assert results["manual_review_judgments"][0]["winner_role"] == "manual_review"


def test_gemini_payload_is_blind_and_uses_inline_data_images(tmp_path: Path) -> None:
    from backend.scripts import comfyui_vlm_judge_runner

    packet = _packet(tmp_path)
    payload = comfyui_vlm_judge_runner.build_gemini_generate_content_payload(
        packet["judge_items"][0],
        packet_root=tmp_path,
    )

    serialized = json.dumps(payload, ensure_ascii=False)
    assert serialized.count('"inline_data"') == 2
    assert "mime_type" in serialized
    assert "human_labels" not in serialized
    assert "human_winner_role" not in serialized
    assert "human-reviewer" not in serialized


def test_gemini_response_parses_to_importable_real_judgment(tmp_path: Path) -> None:
    from backend.scripts import comfyui_vlm_judge_runner

    item = _packet(tmp_path)["judge_items"][0]
    response = {
        "responseId": "gemini-response-id",
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "ab_unit_id": "unit-1",
                                    "winner_role": "candidate",
                                    "confidence": 0.86,
                                    "rationale": "Candidate improves local region quality.",
                                    "risk_flags": ["artifact"],
                                }
                            )
                        }
                    ]
                }
            }
        ],
    }

    judgment = comfyui_vlm_judge_runner.parse_gemini_generate_content_judgment(
        response,
        item=item,
        provider="gemini_generate_content",
        model="gemini-2.5-flash",
    )

    assert judgment["ab_unit_id"] == "unit-1"
    assert judgment["winner_role"] == "candidate"
    assert judgment["judge_provider"] == "gemini_generate_content"
    assert judgment["judge_model"] == "gemini-2.5-flash"
    assert judgment["provider_response_id"] == "gemini-response-id"


def test_vertex_adc_blocks_without_project_location_or_token(tmp_path: Path) -> None:
    from backend.scripts import comfyui_vlm_judge_runner

    results, report = comfyui_vlm_judge_runner.run_vlm_judge(
        _packet(tmp_path),
        packet_root=tmp_path,
        env={
            "CASE_WORKBENCH_VLM_JUDGE_PROVIDER": "vertex_generate_content_adc",
            "CASE_WORKBENCH_VLM_JUDGE_MODEL": "gemini-2.5-flash",
        },
        token_provider=lambda: None,
    )

    assert report["run_status"] == "blocked_missing_vertex_project_config"
    assert results["judgments"] == []
    assert results["real_vlm_judge"] is False


def test_vertex_payload_is_blind_and_uses_inline_data_images(tmp_path: Path) -> None:
    from backend.scripts import comfyui_vlm_judge_runner

    packet = _packet(tmp_path)
    payload = comfyui_vlm_judge_runner.build_vertex_generate_content_payload(
        packet["judge_items"][0],
        packet_root=tmp_path,
    )

    serialized = json.dumps(payload, ensure_ascii=False)
    assert serialized.count('"inlineData"') == 2
    assert "mimeType" in serialized
    assert "human_labels" not in serialized
    assert "human_winner_role" not in serialized
    assert "human-reviewer" not in serialized


def test_vertex_response_parses_to_importable_real_judgment(tmp_path: Path) -> None:
    from backend.scripts import comfyui_vlm_judge_runner

    item = _packet(tmp_path)["judge_items"][0]
    response = {
        "responseId": "vertex-response-id",
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "ab_unit_id": "unit-1",
                                    "winner_role": "baseline",
                                    "confidence": 0.88,
                                    "rationale": "Baseline preserves identity better.",
                                    "risk_flags": ["identity_drift"],
                                }
                            )
                        }
                    ]
                }
            }
        ],
    }

    judgment = comfyui_vlm_judge_runner.parse_vertex_generate_content_judgment(
        response,
        item=item,
        provider="vertex_generate_content_adc",
        model="gemini-2.5-flash",
    )

    assert judgment["ab_unit_id"] == "unit-1"
    assert judgment["winner_role"] == "baseline"
    assert judgment["judge_provider"] == "vertex_generate_content_adc"
    assert judgment["judge_model"] == "gemini-2.5-flash"
    assert judgment["provider_response_id"] == "vertex-response-id"


def test_vertex_adc_uses_bearer_token_and_project_endpoint(tmp_path: Path) -> None:
    from backend.scripts import comfyui_vlm_judge_runner

    calls: list[dict] = []

    def post_json(url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        return {
            "responseId": "vertex-real-shape",
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "ab_unit_id": "unit-1",
                                        "winner_role": "candidate",
                                        "confidence": 0.79,
                                        "rationale": "Candidate has better local edit quality.",
                                        "risk_flags": [],
                                    }
                                )
                            }
                        ]
                    }
                }
            ],
        }

    results, report = comfyui_vlm_judge_runner.run_vlm_judge(
        _packet(tmp_path),
        packet_root=tmp_path,
        env={
            "CASE_WORKBENCH_VLM_JUDGE_PROVIDER": "vertex_generate_content_adc",
            "CASE_WORKBENCH_VLM_JUDGE_MODEL": "gemini-2.5-flash",
            "CASE_WORKBENCH_VERTEX_PROJECT": "project-id",
            "CASE_WORKBENCH_VERTEX_LOCATION": "us-central1",
        },
        token_provider=lambda: "adc-token",
        post_json=post_json,
    )

    assert report["run_status"] == "completed_real_vlm_judge"
    assert results["judgments"][0]["judge_provider"] == "vertex_generate_content_adc"
    assert calls[0]["headers"]["Authorization"] == "Bearer adc-token"
    assert "projects/project-id/locations/us-central1/publishers/google/models/gemini-2.5-flash:generateContent" in calls[0]["url"]


def test_vertex_paygo_reuses_formal_vision_provider_env_without_leaking_token(tmp_path: Path) -> None:
    from backend.scripts import comfyui_vlm_judge_runner

    calls: list[dict] = []

    def post_json(url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        return {
            "responseId": "vertex-paygo-response",
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "ab_unit_id": "unit-1",
                                        "winner_role": "baseline",
                                        "confidence": 0.93,
                                        "rationale": "Baseline has fewer artifacts.",
                                        "risk_flags": [],
                                    }
                                )
                            }
                        ]
                    }
                }
            ],
        }

    results, report = comfyui_vlm_judge_runner.run_vlm_judge(
        _packet(tmp_path),
        packet_root=tmp_path,
        env={
            "VISION_PROVIDER": "vertex_generate_content_adc",
            "VISION_API_MODEL": "gemini-2.5-flash",
            "GOOGLE_CLOUD_PROJECT": "project-id",
            "GOOGLE_CLOUD_LOCATION": "us-central1",
        },
        token_provider=lambda: "adc-token-should-not-be-serialized",
        post_json=post_json,
    )

    serialized_report = json.dumps(report, ensure_ascii=False)
    serialized_results = json.dumps(results, ensure_ascii=False)
    assert report["run_status"] == "completed_real_vlm_judge"
    assert report["provider"] == "vertex_generate_content_adc"
    assert report["model"] == "gemini-2.5-flash"
    assert report["billing_mode"] == "paygo"
    assert report["project"] == "project-id"
    assert report["location"] == "us-central1"
    assert results["billing_mode"] == "paygo"
    assert "adc-token-should-not-be-serialized" not in serialized_report
    assert "adc-token-should-not-be-serialized" not in serialized_results
    assert calls[0]["headers"]["Authorization"] == "Bearer adc-token-should-not-be-serialized"


def test_run_vlm_judge_uses_batch_concurrency_limit(tmp_path: Path) -> None:
    from backend.scripts import comfyui_vlm_judge_runner

    packet = _packet(tmp_path)
    first = packet["judge_items"][0]
    packet["judge_items"] = []
    for index in range(5):
        item = copy.deepcopy(first)
        item["ab_unit_id"] = f"unit-{index}"
        item["case_id"] = 200 + index
        packet["judge_items"].append(item)
    packet["judge_item_count"] = len(packet["judge_items"])

    active = 0
    max_active = 0
    lock = threading.Lock()

    def post_json(_url: str, _headers: dict[str, str], payload: dict, _timeout: float) -> dict:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        prompt = payload["contents"][0]["parts"][0]["text"]
        unit_id = next(line.split(":", 1)[1].strip() for line in prompt.splitlines() if line.startswith("ab_unit_id:"))
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "ab_unit_id": unit_id,
                                        "winner_role": "baseline",
                                        "confidence": 0.9,
                                        "visual_evidence_summary": "Baseline is cleaner.",
                                        "criterion_scores": {
                                            "medical aesthetic delivery quality": {"baseline": 4, "candidate": 3}
                                        },
                                        "rationale": "Baseline is cleaner.",
                                        "risk_flags": [],
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }

    results, report = comfyui_vlm_judge_runner.run_vlm_judge(
        packet,
        packet_root=tmp_path,
        env={
            "CASE_WORKBENCH_VLM_JUDGE_PROVIDER": "gemini_generate_content",
            "CASE_WORKBENCH_VLM_JUDGE_MODEL": "gemini-2.5-flash",
            "CASE_WORKBENCH_VLM_JUDGE_API_KEY": "local-test-key",
        },
        post_json=post_json,
        concurrency=2,
    )

    assert report["run_status"] == "completed_real_vlm_judge"
    assert report["successful_judgment_count"] == 5
    assert [judgment["ab_unit_id"] for judgment in results["judgments"]] == [f"unit-{index}" for index in range(5)]
    assert 1 < max_active <= 2


def test_run_vlm_judge_fail_closes_single_item_error_without_aborting_batch(tmp_path: Path) -> None:
    from backend.services.vlm_provider import VLMRequestError
    from backend.scripts import comfyui_vlm_judge_runner

    packet = _packet(tmp_path)
    first = packet["judge_items"][0]
    packet["judge_items"] = []
    for index in range(3):
        item = copy.deepcopy(first)
        item["ab_unit_id"] = f"unit-{index}"
        item["case_id"] = 300 + index
        packet["judge_items"].append(item)
    packet["judge_item_count"] = len(packet["judge_items"])

    def post_json(_url: str, _headers: dict[str, str], payload: dict, _timeout: float) -> dict:
        prompt = payload["contents"][0]["parts"][0]["text"]
        unit_id = next(line.split(":", 1)[1].strip() for line in prompt.splitlines() if line.startswith("ab_unit_id:"))
        if unit_id == "unit-1":
            raise VLMRequestError("rate limited", status_code=429, retry_after_seconds=0)
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "ab_unit_id": unit_id,
                                        "winner_role": "baseline",
                                        "confidence": 0.9,
                                        "visual_evidence_summary": "Baseline is cleaner.",
                                        "criterion_scores": {
                                            "medical aesthetic delivery quality": {"baseline": 4, "candidate": 3}
                                        },
                                        "rationale": "Baseline is cleaner.",
                                        "risk_flags": [],
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }

    results, report = comfyui_vlm_judge_runner.run_vlm_judge(
        packet,
        packet_root=tmp_path,
        env={
            "CASE_WORKBENCH_VLM_JUDGE_PROVIDER": "gemini_generate_content",
            "CASE_WORKBENCH_VLM_JUDGE_MODEL": "gemini-2.5-flash",
            "CASE_WORKBENCH_VLM_JUDGE_API_KEY": "local-test-key",
            "VLM_RETRY_MAX_ATTEMPTS": "1",
        },
        post_json=post_json,
        concurrency=3,
    )

    assert report["run_status"] == "partial_real_vlm_judge"
    assert report["successful_judgment_count"] == 2
    assert report["manual_review_count"] == 1
    assert report["failed_judgment_count"] == 0
    assert [judgment["ab_unit_id"] for judgment in results["judgments"]] == ["unit-0", "unit-2"]
    manual = results["manual_review_judgments"][0]
    assert manual["ab_unit_id"] == "unit-1"
    assert manual["winner_role"] == "manual_review"
    assert manual["fail_closed_reason"] == "rate limited"


def test_cli_exposes_concurrency_argument() -> None:
    from backend.scripts import comfyui_vlm_judge_runner

    args = comfyui_vlm_judge_runner.build_arg_parser().parse_args(
        [
            "--packet-json",
            "packet.json",
            "--results-output",
            "results.json",
            "--report-output",
            "report.json",
            "--concurrency",
            "7",
        ]
    )

    assert args.concurrency == 7


def _vertex_ok_post_json(url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
    return {
        "responseId": "vertex-real-shape",
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "ab_unit_id": "unit-1",
                                    "winner_role": "candidate",
                                    "confidence": 0.79,
                                    "rationale": "Candidate has better local edit quality.",
                                    "risk_flags": [],
                                }
                            )
                        }
                    ]
                }
            }
        ],
    }


def _vertex_env(extra: dict | None = None) -> dict:
    env = {
        "CASE_WORKBENCH_VLM_JUDGE_PROVIDER": "vertex_generate_content_adc",
        "CASE_WORKBENCH_VLM_JUDGE_MODEL": "gemini-2.5-flash",
        "CASE_WORKBENCH_VERTEX_PROJECT": "project-id",
        "CASE_WORKBENCH_VERTEX_LOCATION": "us-central1",
    }
    if extra:
        env.update(extra)
    return env


def test_runner_vertex_passes_live_token_provider_when_not_gcloud(tmp_path: Path) -> None:
    """In-process / 注入的 token provider 不得冻结 —— VLMProvider 每请求都向它取 token（长 batch 自动刷新）。"""
    from backend.scripts import comfyui_vlm_judge_runner

    counter = {"n": 0}

    def counting_provider() -> str:
        counter["n"] += 1
        return f"tok-{counter['n']}"

    _results, report = comfyui_vlm_judge_runner.run_vlm_judge(
        _packet(tmp_path),
        packet_root=tmp_path,
        env=_vertex_env(),
        token_provider=counting_provider,
        post_json=_vertex_ok_post_json,
        concurrency=1,
    )

    assert report["run_status"] == "completed_real_vlm_judge"
    # live：probe(1) + 每请求(1, 单 item) = 2；若被冻结只会停在 1
    assert counter["n"] == 2


def test_runner_vertex_freezes_gcloud_subprocess_provider(tmp_path: Path, monkeypatch) -> None:
    """gcloud 子进程路径保持冻结 —— 仅 probe 取一次 token，之后用冻结副本，不再每请求 spawn。"""
    from backend.scripts import comfyui_vlm_judge_runner

    counter = {"n": 0}

    def fake_gcloud() -> str:
        counter["n"] += 1
        return f"gtok-{counter['n']}"

    monkeypatch.setattr(comfyui_vlm_judge_runner, "_gcloud_adc_token", fake_gcloud)

    _results, report = comfyui_vlm_judge_runner.run_vlm_judge(
        _packet(tmp_path),
        packet_root=tmp_path,
        env=_vertex_env({"CASE_WORKBENCH_VERTEX_TOKEN_MODE": "gcloud"}),
        post_json=_vertex_ok_post_json,
        concurrency=1,
    )

    assert report["run_status"] == "completed_real_vlm_judge"
    # 冻结：gcloud 只在 probe 调一次，之后走冻结的嵌套函数，不再触达 fake_gcloud
    assert counter["n"] == 1
