"""Serve a local click-review UI for ComfyUI A/B human decisions."""
from __future__ import annotations

import argparse
import json
import mimetypes
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any
from urllib.parse import unquote, urlparse

VALID_SAVE_ROLES = {"baseline", "candidate", "manual_review", "skip"}


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


def _unit_id(value: dict[str, Any]) -> str:
    return str(value.get("ab_unit_id") or value.get("unit_id") or value.get("case_id") or "").strip()


def _manifest_units(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for unit in manifest.get("review_units") or []:
        if isinstance(unit, dict):
            unit_id = _unit_id(unit)
            if unit_id:
                out[unit_id] = unit
    return out


def _draft_decisions(draft: dict[str, Any]) -> list[dict[str, Any]]:
    raw = draft.get("decisions") or []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _decision_map(draft: dict[str, Any]) -> dict[str, dict[str, Any]]:
    decisions: dict[str, dict[str, Any]] = {}
    for decision in _draft_decisions(draft):
        unit_id = _unit_id(decision)
        if unit_id:
            decisions[unit_id] = decision
    return decisions


def _roles(unit: dict[str, Any]) -> dict[str, dict[str, Any]]:
    roles: dict[str, dict[str, Any]] = {}
    for asset in unit.get("packet_assets") or []:
        if not isinstance(asset, dict):
            continue
        role = str(asset.get("role") or "").strip().lower()
        if role in {"baseline", "candidate"}:
            roles[role] = {
                "role": role,
                "variant": str(asset.get("variant") or "").strip(),
                "packet_relative_path": str(asset.get("packet_relative_path") or "").strip(),
                "source_path": str(asset.get("source_path") or "").strip(),
                "status": asset.get("status"),
                "simulation_job_id": asset.get("simulation_job_id"),
            }
    return roles


def _public_decision(decision: dict[str, Any] | None) -> dict[str, Any]:
    decision = decision or {}
    return {
        "winner_role": decision.get("winner_role"),
        "winner_variant": decision.get("winner_variant"),
        "reviewer": decision.get("reviewer"),
        "review_note": decision.get("review_note"),
        "review_status": decision.get("review_status"),
        "reviewed_at": decision.get("reviewed_at"),
    }


def build_review_state(manifest_path: Path, draft_path: Path) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    draft = _load_json(draft_path)
    decisions = _decision_map(draft)
    units: list[dict[str, Any]] = []
    for unit_id, unit in sorted(_manifest_units(manifest).items(), key=lambda item: item[0]):
        roles = _roles(unit)
        decision = decisions.get(unit_id, {})
        units.append(
            {
                "ab_unit_id": unit_id,
                "case_id": unit.get("case_id"),
                "view": unit.get("view"),
                "workflow": unit.get("workflow"),
                "disagreement_type": unit.get("disagreement_type"),
                "prior_human_winner_role": unit.get("prior_human_winner_role"),
                "vlm_winner_role": unit.get("vlm_winner_role"),
                "risk_assessment": unit.get("risk_assessment"),
                "visual_reaudit_note": unit.get("visual_reaudit_note"),
                "visual_evidence_summary": unit.get("visual_evidence_summary"),
                "rationale": unit.get("rationale"),
                "ready_for_review": bool(unit.get("ready_for_review")),
                "roles": roles,
                "decision": _public_decision(decision),
            }
        )
    draft_items = _draft_decisions(draft)
    filled_winner_count = sum(
        1
        for item in draft_items
        if str(item.get("winner_role") or "").strip().lower() in {"baseline", "candidate"}
        or (
            str(item.get("winner_variant") or "").strip()
            and str(item.get("winner_role") or "").strip().lower() in {"baseline", "candidate"}
        )
    )
    manual_review_count = sum(
        1
        for item in draft_items
        if str(item.get("winner_role") or item.get("review_status") or "").strip().lower() == "manual_review"
    )
    skipped_count = sum(
        1
        for item in draft_items
        if str(item.get("review_status") or "").strip().lower() == "skipped"
    )
    filled_review_status_count = sum(
        1
        for item in draft_items
        if str(item.get("winner_role") or item.get("winner_variant") or item.get("review_status") or "").strip()
    )
    return {
        "generated_at": _now(),
        "scope": "t48_comfyui_click_review_state_v1",
        "manifest_path": str(manifest_path),
        "draft_path": str(draft_path),
        "unit_count": len(units),
        "decision_count": len(draft_items),
        "filled_reviewer_count": sum(1 for item in draft_items if str(item.get("reviewer") or "").strip()),
        "filled_review_status_count": filled_review_status_count,
        "filled_winner_count": filled_winner_count,
        "manual_review_count": manual_review_count,
        "skipped_count": skipped_count,
        "units": units,
    }


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": code, "error": message}


def _variant_for_role(unit: dict[str, Any], role: str) -> str:
    role_asset = _roles(unit).get(role) or {}
    return str(role_asset.get("variant") or "").strip()


def save_review_decision(manifest_path: Path, draft_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    draft = _load_json(draft_path)
    unit_id = str(payload.get("ab_unit_id") or "").strip()
    reviewer = str(payload.get("reviewer") or "").strip()
    winner_role = str(payload.get("winner_role") or "").strip().lower()
    review_note = str(payload.get("review_note") or "").strip() or None

    if not reviewer:
        return _error("missing_reviewer", "必须填写真实 reviewer。")
    if winner_role not in VALID_SAVE_ROLES:
        return _error("invalid_winner_role", "winner_role 必须是 candidate、baseline、manual_review 或 skip。")

    units = _manifest_units(manifest)
    unit = units.get(unit_id)
    if not unit:
        return _error("unknown_ab_unit", "ab_unit_id 不在当前 review packet manifest 中。")

    winner_variant: str | None = None
    normalized_role: str | None = winner_role
    review_status = "reviewed"
    if winner_role == "skip":
        normalized_role = None
        review_status = "skipped"
    elif winner_role == "manual_review":
        normalized_role = "manual_review"
        review_status = "manual_review"
    else:
        winner_variant = _variant_for_role(unit, winner_role)
        if not winner_variant:
            return _error("missing_role_variant", f"manifest 中找不到 {winner_role} 对应的真实 variant。")

    decisions = _draft_decisions(draft)
    target = None
    for decision in decisions:
        if _unit_id(decision) == unit_id:
            target = decision
            break
    if target is None:
        target = {
            "ab_unit_id": unit_id,
            "case_id": unit.get("case_id"),
            "view": unit.get("view"),
            "workflow": unit.get("workflow"),
        }
        decisions.append(target)
    target["winner_role"] = normalized_role
    target["winner_variant"] = winner_variant
    target["reviewer"] = reviewer
    target["review_note"] = review_note
    target["review_status"] = review_status
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


def _render_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ComfyUI A/B 人工审核</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;background:#f6f6f2;color:#1f2328;}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid #d9d9d2;padding:12px 18px;z-index:10;display:flex;gap:12px;align-items:center;flex-wrap:wrap;}
h1{font-size:18px;margin:0 12px 0 0;}label{font-size:13px;color:#59606b;}input{height:32px;border:1px solid #c8c8c0;border-radius:6px;padding:0 10px;min-width:180px;}
main{padding:18px;}.summary{font-size:13px;color:#59606b;margin-bottom:14px;}.unit{background:#fff;border:1px solid #d9d9d2;border-radius:8px;margin-bottom:18px;padding:14px;}
.unit h2{font-size:15px;margin:0 0 4px;}.meta{font-size:13px;color:#59606b;margin:0 0 12px;}.review-note{background:#fff8e5;border:1px solid #ead28b;border-radius:6px;padding:8px 10px;font-size:13px;margin:8px 0 12px;}.review-note p{margin:4px 0;}.images{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;}
figure{margin:0;background:#fbfbf8;border:1px solid #e2e2dc;border-radius:6px;padding:8px;}figcaption{font-size:13px;font-weight:650;margin-bottom:8px;}figcaption span{display:block;font-weight:400;color:#59606b;overflow-wrap:anywhere;}
img{display:block;width:100%;max-height:680px;object-fit:contain;background:#ecece8;}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;}
button{border:1px solid #b9bbb2;background:#fff;border-radius:6px;height:34px;padding:0 12px;font-weight:650;cursor:pointer;}button.primary{background:#1f6feb;color:#fff;border-color:#1f6feb;}button.warn{background:#fff8e5;border-color:#d1a23a;}button.selected{outline:3px solid #8ab4ff;}
textarea{width:100%;min-height:46px;margin-top:10px;border:1px solid #c8c8c0;border-radius:6px;padding:8px;box-sizing:border-box;}.status{font-size:13px;margin-left:auto;color:#59606b;}
.toast{position:fixed;right:16px;bottom:16px;background:#1f2328;color:#fff;padding:10px 12px;border-radius:6px;font-size:13px;opacity:0;transition:.18s;}.toast.show{opacity:1;}
@media(max-width:900px){.images{grid-template-columns:1fr;}main{padding:10px;}header{position:static;}}
</style>
</head>
<body>
<header>
  <h1>ComfyUI A/B 人工审核</h1>
  <label>Reviewer <input id="reviewer" placeholder="真实审核人"></label>
  <span id="status" class="status">加载中</span>
</header>
<main id="app"></main>
<div id="toast" class="toast"></div>
<script>
let state=null;
function esc(v){return String(v ?? '').replace(/[&<>"']/g, s=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));}
function toast(msg){const el=document.getElementById('toast');el.textContent=msg;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),1500);}
async function loadState(){const res=await fetch('/api/state');state=await res.json();render();}
function render(){
  document.getElementById('status').textContent=`${state.filled_winner_count}/${state.unit_count} 可导入 winner，${state.manual_review_count||0} manual_review，${state.filled_reviewer_count}/${state.unit_count} reviewer`;
  const app=document.getElementById('app');
  app.innerHTML=`<div class="summary">点击只会写回本地 review_decisions_draft.json；不会自动 promote。</div>`+state.units.map(unit=>{
    const b=unit.roles.baseline||{}, c=unit.roles.candidate||{}, d=unit.decision||{};
    const selected=d.winner_role||d.review_status;
    return `<section class="unit" data-unit="${esc(unit.ab_unit_id)}">
      <h2>${esc(unit.ab_unit_id)}</h2>
      <p class="meta">case ${esc(unit.case_id)} · ${esc(unit.view)} · ${esc(unit.workflow)}</p>
      ${reviewMeta(unit)}
      <div class="images">
        ${figure('baseline',b,unit.ab_unit_id)}
        ${figure('candidate',c,unit.ab_unit_id)}
      </div>
      <textarea placeholder="审核备注，可空">${esc(d.review_note||'')}</textarea>
      <div class="actions">
        <button class="${selected==='baseline'?'selected':''}" onclick="saveDecision('${esc(unit.ab_unit_id)}','baseline',this)">Baseline 胜出</button>
        <button class="primary ${selected==='candidate'?'selected':''}" onclick="saveDecision('${esc(unit.ab_unit_id)}','candidate',this)">Candidate 胜出</button>
        <button class="warn ${selected==='manual_review'?'selected':''}" onclick="saveDecision('${esc(unit.ab_unit_id)}','manual_review',this)">Manual Review</button>
        <button class="warn ${selected==='skipped'?'selected':''}" onclick="saveDecision('${esc(unit.ab_unit_id)}','skip',this)">跳过</button>
        <span class="meta">当前：${esc(d.winner_role || d.review_status || '未选择')} ${esc(d.reviewer || '')}</span>
      </div>
    </section>`;
  }).join('');
}
function figure(role, asset, unitId){
  const src=asset.packet_relative_path?'/'+asset.packet_relative_path:'';
  return `<figure><figcaption>${esc(role)}<span>${esc(asset.variant||'无法获取')}</span></figcaption>${src?`<img src="${esc(src)}" alt="${esc(role)} ${esc(unitId)}">`:'<div>无法获取</div>'}</figure>`;
}
function reviewMeta(unit){
  if(!unit.disagreement_type && !unit.visual_reaudit_note && !unit.rationale){return '';}
  return `<div class="review-note">
    <p><b>${esc(unit.disagreement_type||'二次复审')}</b> · risk=${esc(unit.risk_assessment||'未标记')} · prior human=${esc(unit.prior_human_winner_role||'')} · VLM=${esc(unit.vlm_winner_role||'')}</p>
    ${unit.visual_reaudit_note?`<p>${esc(unit.visual_reaudit_note)}</p>`:''}
    ${unit.visual_evidence_summary?`<p><b>VLM evidence:</b> ${esc(unit.visual_evidence_summary)}</p>`:''}
    ${unit.rationale?`<p><b>VLM rationale:</b> ${esc(unit.rationale)}</p>`:''}
  </div>`;
}
async function saveDecision(unitId, role, button){
  const reviewer=document.getElementById('reviewer').value.trim();
  if(!reviewer){toast('先填写真实 reviewer');return;}
  const section=button.closest('.unit');
  const note=section.querySelector('textarea').value;
  const res=await fetch('/api/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ab_unit_id:unitId,winner_role:role,reviewer,review_note:note})});
  const data=await res.json();
  if(!data.ok){toast(data.error || '保存失败');return;}
  state=data.state;render();toast('已保存');
}
loadState().catch(err=>{document.getElementById('app').textContent=err.message;});
</script>
</body>
</html>
"""


def make_handler(packet_dir: Path, manifest_path: Path, draft_path: Path) -> type[BaseHTTPRequestHandler]:
    packet_dir = packet_dir.resolve()
    manifest_path = manifest_path.resolve()
    draft_path = draft_path.resolve()

    class ReviewHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib callback name.
            print(f"{self.address_string()} - {format % args}")

        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name.
            parsed = urlparse(self.path)
            if parsed.path == "/":
                _bytes_response(self, _render_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/state":
                _json_response(self, build_review_state(manifest_path, draft_path))
                return
            rel = unquote(parsed.path.lstrip("/"))
            target = (packet_dir / rel).resolve()
            if not str(target).startswith(str(packet_dir)) or not target.is_file():
                _json_response(self, _error("not_found", "文件不存在。"), HTTPStatus.NOT_FOUND)
                return
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            _bytes_response(self, target.read_bytes(), content_type)

        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name.
            parsed = urlparse(self.path)
            if parsed.path != "/api/decision":
                _json_response(self, _error("not_found", "接口不存在。"), HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    payload = {}
            except Exception as exc:  # noqa: BLE001 - local review API should return validation errors.
                _json_response(self, _error("invalid_json", str(exc)), HTTPStatus.BAD_REQUEST)
                return
            result = save_review_decision(manifest_path, draft_path, payload)
            status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST
            _json_response(self, result, status)

    return ReviewHandler


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local ComfyUI A/B click-review server.")
    parser.add_argument("--packet-dir", type=Path)
    parser.add_argument("--manifest-json", type=Path, default=Path("tasks/t46_human_review_packet/manifest.json"))
    parser.add_argument("--review-decisions-json", type=Path, default=Path("tasks/t46_human_review_packet/review_decisions_draft.json"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    packet_dir = args.packet_dir or args.manifest_json.parent
    handler = make_handler(packet_dir, args.manifest_json, args.review_decisions_json)
    server = LocalReviewHTTPServer((args.host, int(args.port)), handler)
    print(f"Serving ComfyUI A/B review UI at http://{args.host}:{args.port}/")
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
