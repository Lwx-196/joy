"""Serve a local click-review UI for T65 formal render final-board decisions."""
from __future__ import annotations

import argparse
import html
import json
import mimetypes
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any
from urllib.parse import unquote, urlparse

VALID_DECISIONS = {
    "accept_template_downgrade",
    "needs_slot_fill",
    "needs_reselect",
    "needs_rerender",
    "reject",
}


class LocalReviewHTTPServer(ThreadingHTTPServer):
    """HTTPServer variant that avoids slow local reverse-DNS lookup."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _manifest_units(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for unit in manifest.get("review_units") or []:
        if isinstance(unit, dict):
            unit_id = str(unit.get("unit_id") or "").strip()
            if unit_id:
                out[unit_id] = unit
    return out


def _draft_decisions(draft: dict[str, Any]) -> list[dict[str, Any]]:
    raw = draft.get("decisions") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _decision_map(draft: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for decision in _draft_decisions(draft):
        unit_id = str(decision.get("unit_id") or "").strip()
        if unit_id:
            out[unit_id] = decision
    return out


def _public_decision(decision: dict[str, Any] | None) -> dict[str, Any]:
    decision = decision or {}
    return {
        "decision": decision.get("decision"),
        "reviewer": decision.get("reviewer"),
        "review_note": decision.get("review_note"),
        "reviewed_at": decision.get("reviewed_at"),
    }


def build_review_state(manifest_path: Path, draft_path: Path) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    draft = _load_json(draft_path)
    decisions = _decision_map(draft)
    units: list[dict[str, Any]] = []
    for unit_id, unit in sorted(_manifest_units(manifest).items(), key=lambda item: item[0]):
        units.append(
            {
                "unit_id": unit_id,
                "case_id": unit.get("case_id"),
                "job_id": unit.get("job_id"),
                "customer_raw": unit.get("customer_raw"),
                "template": unit.get("template"),
                "quality_score": unit.get("quality_score"),
                "ready_for_review": bool(unit.get("ready_for_review")),
                "image": unit.get("packet_final_board_relative_path"),
                "warnings": unit.get("warning_samples") or [],
                "decision": _public_decision(decisions.get(unit_id)),
            }
        )
    draft_items = _draft_decisions(draft)
    return {
        "generated_at": _now(),
        "scope": "t65_formal_render_click_review_state_v1",
        "unit_count": len(units),
        "filled_reviewer_count": sum(1 for item in draft_items if str(item.get("reviewer") or "").strip()),
        "filled_decision_count": sum(1 for item in draft_items if str(item.get("decision") or "").strip()),
        "allowed_decisions": sorted(VALID_DECISIONS),
        "units": units,
    }


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": code, "error": message}


def save_review_decision(manifest_path: Path, draft_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    draft = _load_json(draft_path)
    unit_id = str(payload.get("unit_id") or "").strip()
    reviewer = str(payload.get("reviewer") or "").strip()
    decision_value = str(payload.get("decision") or "").strip()
    review_note = str(payload.get("review_note") or "").strip() or None

    if not reviewer:
        return _error("missing_reviewer", "必须填写真实 reviewer。")
    if decision_value not in VALID_DECISIONS:
        return _error("invalid_decision", "decision 不在允许范围内。")
    units = _manifest_units(manifest)
    unit = units.get(unit_id)
    if not unit:
        return _error("unknown_unit", "unit_id 不在当前 manifest 中。")
    if not unit.get("ready_for_review"):
        return _error("unit_not_ready", "该 unit 缺少真实 final-board 或 manifest，不能保存决策。")

    decisions = _draft_decisions(draft)
    target = None
    for item in decisions:
        if str(item.get("unit_id") or "").strip() == unit_id:
            target = item
            break
    if target is None:
        target = {"unit_id": unit_id, "case_id": unit.get("case_id"), "job_id": unit.get("job_id")}
        decisions.append(target)
    target["case_id"] = unit.get("case_id")
    target["job_id"] = unit.get("job_id")
    target["reviewer"] = reviewer
    target["decision"] = decision_value
    target["review_note"] = review_note
    target["reviewed_at"] = _now()
    draft["decisions"] = decisions
    _write_json(draft_path, draft)
    return {"ok": True, "decision": _public_decision(target), "state": build_review_state(manifest_path, draft_path)}


def _json_response(handler: BaseHTTPRequestHandler, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _bytes_response(handler: BaseHTTPRequestHandler, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


def _render_html() -> str:
    options = "".join(f'<button data-decision="{_esc(item)}">{_esc(item)}</button>' for item in sorted(VALID_DECISIONS))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>T65 正式出图人工复核</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;background:#f6f7f8;color:#20242a;}}
header{{position:sticky;top:0;background:#fff;border-bottom:1px solid #d9dde3;padding:12px 18px;z-index:10;display:flex;gap:12px;align-items:center;flex-wrap:wrap;}}
h1{{font-size:18px;margin:0 12px 0 0;}}label{{font-size:13px;color:#596273;}}input{{height:32px;border:1px solid #c8d0db;border-radius:6px;padding:0 10px;min-width:180px;}}
main{{padding:18px;}}.unit{{background:#fff;border:1px solid #d9dde3;border-radius:8px;margin-bottom:18px;padding:14px;}}
.unit h2{{font-size:16px;margin:0 0 4px;}}.meta{{font-size:13px;color:#596273;margin:0 0 12px;}}figure{{margin:0;border:1px solid #e1e5ea;border-radius:6px;background:#eef1f4;padding:8px;}}
img{{display:block;width:100%;max-height:900px;object-fit:contain;background:#eef1f4;}}ul{{margin:10px 0 0 18px;padding:0;color:#4c5565;font-size:13px;line-height:1.55;}}
.actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;}}button{{border:1px solid #b9c1cc;background:#fff;border-radius:6px;height:34px;padding:0 10px;font-weight:650;cursor:pointer;}}
button.selected{{outline:3px solid #8ab4ff;}}textarea{{width:100%;min-height:48px;margin-top:10px;border:1px solid #c8d0db;border-radius:6px;padding:8px;box-sizing:border-box;}}
.toast{{position:fixed;right:16px;bottom:16px;background:#20242a;color:#fff;padding:10px 12px;border-radius:6px;font-size:13px;opacity:0;transition:.18s;}}.toast.show{{opacity:1;}}
@media(max-width:900px){{main{{padding:10px;}}header{{position:static;}}}}
</style>
</head>
<body>
<header><h1>T65 正式出图人工复核</h1><label>Reviewer <input id="reviewer" placeholder="真实审核人"></label><span id="status" class="meta">加载中</span></header>
<main id="app"></main><div id="toast" class="toast"></div>
<template id="buttons">{options}</template>
<script>
let state=null;
function esc(v){{return String(v ?? '').replace(/[&<>"']/g,s=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[s]));}}
function toast(msg){{const el=document.getElementById('toast');el.textContent=msg;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),1600);}}
async function loadState(){{const res=await fetch('/api/state');state=await res.json();render();}}
function render(){{
  document.getElementById('status').textContent=`${{state.filled_decision_count}}/${{state.unit_count}} 已判定`;
  const buttons=document.getElementById('buttons').innerHTML;
  document.getElementById('app').innerHTML=state.units.map(unit=>{{
    const d=unit.decision||{{}};
    const warnings=(unit.warnings||[]).map(w=>`<li>${{esc(w)}}</li>`).join('');
    return `<section class="unit" data-unit="${{esc(unit.unit_id)}}">
      <h2>${{esc(unit.unit_id)}}</h2><p class="meta">case ${{esc(unit.case_id)}} · job ${{esc(unit.job_id)}} · ${{esc(unit.customer_raw)}} · ${{esc(unit.template)}} · score ${{esc(unit.quality_score)}}</p>
      <figure><img src="/${{esc(unit.image)}}" alt="${{esc(unit.unit_id)}} final board"></figure>
      <ul>${{warnings || '<li>无 warning 样本</li>'}}</ul>
      <textarea placeholder="审核备注，可空">${{esc(d.review_note||'')}}</textarea>
      <div class="actions">${{buttons}}<span class="meta">当前：${{esc(d.decision||'未判定')}} ${{esc(d.reviewer||'')}}</span></div>
    </section>`;
  }}).join('');
  document.querySelectorAll('.unit').forEach(section=>{{
    const unitId=section.dataset.unit; const unit=state.units.find(u=>u.unit_id===unitId); const current=unit?.decision?.decision;
    section.querySelectorAll('button').forEach(btn=>{{ if(btn.dataset.decision===current) btn.classList.add('selected'); btn.onclick=()=>saveDecision(unitId,btn.dataset.decision,section); }});
  }});
}}
async function saveDecision(unitId, decision, section){{
  const reviewer=document.getElementById('reviewer').value.trim(); if(!reviewer){{toast('先填写真实 reviewer');return;}}
  const note=section.querySelector('textarea').value;
  const res=await fetch('/api/decision',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{unit_id:unitId,decision,reviewer,review_note:note}})}});
  const data=await res.json(); if(!data.ok){{toast(data.error||'保存失败');return;}} state=data.state; render(); toast('已保存');
}}
loadState().catch(err=>{{document.getElementById('app').textContent=err.message;}});
</script>
</body>
</html>
"""


def make_handler(packet_dir: Path, manifest_path: Path, draft_path: Path) -> type[BaseHTTPRequestHandler]:
    packet_dir = packet_dir.resolve()
    manifest_path = manifest_path.resolve()
    draft_path = draft_path.resolve()

    class ReviewHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            print(f"{self.address_string()} - {format % args}")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                _bytes_response(self, _render_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/state":
                _json_response(self, build_review_state(manifest_path, draft_path))
                return
            target = (packet_dir / unquote(parsed.path.lstrip("/"))).resolve()
            if not str(target).startswith(str(packet_dir)) or not target.is_file():
                _json_response(self, _error("not_found", "文件不存在。"), HTTPStatus.NOT_FOUND)
                return
            _bytes_response(self, target.read_bytes(), mimetypes.guess_type(str(target))[0] or "application/octet-stream")

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/api/decision":
                _json_response(self, _error("not_found", "接口不存在。"), HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    payload = {}
            except Exception as exc:  # noqa: BLE001
                _json_response(self, _error("invalid_json", str(exc)), HTTPStatus.BAD_REQUEST)
                return
            result = save_review_decision(manifest_path, draft_path, payload)
            _json_response(self, result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)

    return ReviewHandler


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local T65 formal render click-review server.")
    parser.add_argument("--packet-dir", type=Path, default=Path("tasks/t65_formal_review_packet"))
    parser.add_argument("--manifest-json", type=Path, default=Path("tasks/t65_formal_review_packet/manifest.json"))
    parser.add_argument("--review-decisions-json", type=Path, default=Path("tasks/t65_formal_review_packet/review_decisions_draft.json"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    handler = make_handler(args.packet_dir, args.manifest_json, args.review_decisions_json)
    server = LocalReviewHTTPServer((args.host, int(args.port)), handler)
    print(f"Serving T65 formal render review UI at http://{args.host}:{args.port}/")
    print(f"Writing decisions to {args.review_decisions_json.resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
