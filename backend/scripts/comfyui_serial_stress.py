"""Serial ComfyUI smoke/stress runner for real case-workbench cases.

Default mode is intentionally conservative: concurrency=1 and dry-run friendly.
Use real case ids and the local workbench API; no mock data is generated.
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request


ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
DEFAULT_API_BASE = "http://127.0.0.1:5291"
DEFAULT_OUTPUT_DIR = ROOT / "case-workbench-ai" / "stress_runs"
DEFAULT_MODEL = "local_region_enhance_v1@conservative"
DEFAULT_FOCUS_TARGETS = ["口角/下颌线局部轻量增强"]
DEFAULT_FOCUS_REGIONS = [{"x": 0.30, "y": 0.42, "width": 0.40, "height": 0.22, "label": "口角和下颌线"}]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run real serial ComfyUI stress jobs through case-workbench API.")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--case-id", type=int, action="append", dest="case_ids", required=True)
    parser.add_argument("--model-name", default=DEFAULT_MODEL)
    parser.add_argument("--provider", default="comfyui_local")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--allow-parallel", action="store_true", help="Explicitly allow concurrency > 1.")
    parser.add_argument("--allow-fallback", action="store_true", help="Count fallback runs as ok instead of stress failures.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve real cases and selected files without submitting jobs.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--focus-target", action="append", dest="focus_targets")
    parser.add_argument("--focus-region-json", default="")
    return parser


def validate_stress_args(args: argparse.Namespace) -> None:
    if int(args.concurrency) < 1:
        raise ValueError("concurrency must be >= 1")
    if int(args.concurrency) > 1 and not bool(args.allow_parallel):
        raise ValueError("当前 ComfyUI 压测默认只允许串行；如确需 parallel/concurrency > 1，必须显式传 --allow-parallel")


def _request_json(method: str, url: str, payload: dict[str, Any] | None = None, *, timeout: int = 30) -> dict[str, Any]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local operator API.
            return json.loads(resp.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {body[:1000]}") from exc


def _image_files(case_detail: dict[str, Any]) -> list[str]:
    meta = case_detail.get("meta") if isinstance(case_detail.get("meta"), dict) else {}
    raw = meta.get("image_files")
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item)]
    images = case_detail.get("images")
    if isinstance(images, list):
        return [str((item or {}).get("filename") or "") for item in images if str((item or {}).get("filename") or "")]
    return []


def select_front_pair(case_detail: dict[str, Any]) -> dict[str, str]:
    files = _image_files(case_detail)
    def has_phase(name: str, phase: str) -> bool:
        path = Path(name)
        basename = path.name
        parent = path.parent.name
        return phase in basename or parent == phase or parent.startswith(phase)

    before = [name for name in files if has_phase(name, "术前") and "正面" in Path(name).name]
    after = [name for name in files if has_phase(name, "术后") and "正面" in Path(name).name]
    before.sort(key=lambda name: ("手动" not in name, len(name), name))
    after.sort(key=lambda name: ("手动" not in name, len(name), name))
    if not before or not after:
        raise ValueError(f"case #{case_detail.get('id')} 未找到真实正面术前/术后文件，跳过")
    return {"before_image_path": before[0], "after_image_path": after[0]}


def _focus_regions(args: argparse.Namespace) -> list[dict[str, Any]]:
    if not args.focus_region_json:
        return list(DEFAULT_FOCUS_REGIONS)
    data = json.loads(args.focus_region_json)
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError("--focus-region-json must be a JSON array of objects")
    return data


def build_case_job(api_base: str, case_id: int, args: argparse.Namespace) -> dict[str, Any]:
    case_detail = _request_json("GET", f"{api_base}/api/cases/{case_id}")
    pair = select_front_pair(case_detail)
    return {
        "case_id": case_id,
        "customer_raw": case_detail.get("customer_raw"),
        "request": {
            **pair,
            "focus_targets": args.focus_targets or list(DEFAULT_FOCUS_TARGETS),
            "focus_regions": _focus_regions(args),
            "ai_generation_authorized": True,
            "provider": args.provider,
            "model_name": args.model_name,
            "note": "ComfyUI serial stress run",
        },
    }


def run_case_job(api_base: str, case_job: dict[str, Any], *, allow_fallback: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    record: dict[str, Any] = {
        "case_id": case_job["case_id"],
        "customer_raw": case_job.get("customer_raw"),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        response = _request_json(
            "POST",
            f"{api_base}/api/cases/{case_job['case_id']}/simulate-after",
            case_job["request"],
            timeout=900,
        )
        audit = response.get("audit") or {}
        fallback_used = bool(audit.get("fallback_used"))
        record.update(
            {
                "ok": not fallback_used or allow_fallback,
                "simulation_job_id": response.get("simulation_job_id"),
                "status": response.get("status"),
                "provider": response.get("provider"),
                "model_name": response.get("model_name"),
                "workflow_name": audit.get("workflow_name"),
                "fallback_used": fallback_used,
                "difference_analysis": audit.get("difference_analysis"),
                "qa_scores": audit.get("qa_scores"),
                "comfyui_concurrency": audit.get("comfyui_concurrency"),
            }
        )
        if fallback_used and not allow_fallback:
            record["error"] = "fallback_used=true; local ComfyUI stress treats fallback as failure"
    except Exception as exc:  # noqa: BLE001 - stress record should keep failure evidence.
        record.update({"ok": False, "error": str(exc)[:2000]})
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    return record


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed = [float(record.get("elapsed_seconds") or 0) for record in records]
    ok = [record for record in records if record.get("ok")]
    fallback = [record for record in ok if record.get("fallback_used")]
    return {
        "total": len(records),
        "ok": len(ok),
        "failed": len(records) - len(ok),
        "fallback_used": len(fallback),
        "elapsed_seconds_total": round(sum(elapsed), 3),
        "elapsed_seconds_max": round(max(elapsed), 3) if elapsed else 0,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    validate_stress_args(args)
    api_base = str(args.api_base).rstrip("/")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = args.output_dir / stamp
    jobs = [build_case_job(api_base, case_id, args) for case_id in args.case_ids]
    if args.dry_run:
        print(json.dumps({"dry_run": True, "jobs": jobs}, ensure_ascii=False, indent=2))
        return 0

    records: list[dict[str, Any]] = []
    if int(args.concurrency) == 1:
        for job in jobs:
            record = run_case_job(api_base, job, allow_fallback=bool(args.allow_fallback))
            records.append(record)
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    else:
        with ThreadPoolExecutor(max_workers=int(args.concurrency)) as pool:
            futures = [pool.submit(run_case_job, api_base, job, allow_fallback=bool(args.allow_fallback)) for job in jobs]
            for future in as_completed(futures):
                record = future.result()
                records.append(record)
                print(json.dumps(record, ensure_ascii=False, sort_keys=True))

    write_jsonl(output_dir / "records.jsonl", records)
    summary = summarize(records)
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "summary": summary}, ensure_ascii=False, indent=2))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
