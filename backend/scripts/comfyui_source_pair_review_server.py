"""Serve a local review UI for choosing real before/after source pairs."""
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


VALID_VIEWS = {"front", "oblique", "side", "manual_review", "skip"}
IMPORTABLE_VIEWS = {"front", "oblique", "side"}


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
    return str(value.get("unit_id") or value.get("ab_unit_id") or "").strip()


def _manifest_units(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for unit in manifest.get("review_units") or []:
        if not isinstance(unit, dict):
            continue
        unit_id = _unit_id(unit)
        if unit_id:
            out[unit_id] = unit
    return out


def _draft_decisions(draft: dict[str, Any]) -> list[dict[str, Any]]:
    raw = draft.get("decisions") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _decision_map(draft: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in _draft_decisions(draft):
        unit_id = _unit_id(item)
        if unit_id:
            out[unit_id] = item
    return out


def _asset_map(unit: dict[str, Any]) -> dict[str, dict[str, Any]]:
    assets: dict[str, dict[str, Any]] = {}
    for key in ("before_assets", "after_assets", "all_assets"):
        for asset in unit.get(key) or []:
            if not isinstance(asset, dict):
                continue
            asset_id = str(asset.get("asset_id") or "").strip()
            if asset_id:
                assets[asset_id] = asset
    return assets


def _is_importable(decision: dict[str, Any]) -> bool:
    view = str(decision.get("selected_view") or "").strip()
    return (
        view in IMPORTABLE_VIEWS
        and bool(str(decision.get("reviewer") or "").strip())
        and bool(str(decision.get("selected_before") or "").strip())
        and bool(str(decision.get("selected_after") or "").strip())
        and str(decision.get("selected_before")) != str(decision.get("selected_after"))
    )


def _public_decision(decision: dict[str, Any] | None) -> dict[str, Any]:
    decision = decision or {}
    status = str(decision.get("review_status") or "").strip()
    if not status:
        status = "importable" if _is_importable(decision) else "pending"
    return {
        "reviewer": decision.get("reviewer"),
        "selected_before": decision.get("selected_before"),
        "selected_after": decision.get("selected_after"),
        "selected_view": decision.get("selected_view"),
        "review_note": decision.get("review_note"),
        "review_status": status,
        "reviewed_at": decision.get("reviewed_at"),
    }


def build_review_state(manifest_path: Path, draft_path: Path) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    draft = _load_json(draft_path)
    decisions = _decision_map(draft)
    units: list[dict[str, Any]] = []
    for unit_id, unit in sorted(_manifest_units(manifest).items(), key=lambda item: item[0]):
        decision = decisions.get(unit_id, {})
        units.append(
            {
                "unit_id": unit_id,
                "case_id": unit.get("case_id"),
                "customer_raw": unit.get("customer_raw"),
                "case_rel_path": unit.get("case_rel_path"),
                "case_abs_path": unit.get("case_abs_path"),
                "before_assets": unit.get("before_assets") or [],
                "after_assets": unit.get("after_assets") or [],
                "all_assets": unit.get("all_assets") or [],
                "allowed_views": unit.get("allowed_views") or ["front", "oblique", "side", "manual_review"],
                "decision": _public_decision(decision),
            }
        )
    draft_items = _draft_decisions(draft)
    importable_count = sum(1 for item in draft_items if _is_importable(item))
    manual_review_count = sum(
        1 for item in draft_items if str(item.get("selected_view") or "").strip() == "manual_review"
    )
    skipped_count = sum(1 for item in draft_items if str(item.get("selected_view") or "").strip() == "skip")
    filled_count = sum(
        1
        for item in draft_items
        if str(item.get("reviewer") or "").strip()
        and str(item.get("selected_view") or "").strip()
        and (
            str(item.get("selected_view") or "").strip() in {"manual_review", "skip"}
            or (
                str(item.get("selected_before") or "").strip()
                and str(item.get("selected_after") or "").strip()
            )
        )
    )
    return {
        "generated_at": _now(),
        "scope": "comfyui_source_pair_click_review_state_v1",
        "manifest_path": str(manifest_path),
        "draft_path": str(draft_path),
        "unit_count": len(units),
        "decision_count": len(draft_items),
        "filled_count": filled_count,
        "importable_count": importable_count,
        "manual_review_count": manual_review_count,
        "skipped_count": skipped_count,
        "units": units,
    }


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": code, "error": message}


def save_review_decision(manifest_path: Path, draft_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    draft = _load_json(draft_path)
    units = _manifest_units(manifest)
    unit_id = str(payload.get("unit_id") or "").strip()
    reviewer = str(payload.get("reviewer") or "").strip()
    selected_before = str(payload.get("selected_before") or "").strip()
    selected_after = str(payload.get("selected_after") or "").strip()
    selected_view = str(payload.get("selected_view") or "").strip()
    review_note = str(payload.get("review_note") or "").strip() or None

    if not reviewer:
        return _error("missing_reviewer", "必须填写真实 reviewer。")
    unit = units.get(unit_id)
    if not unit:
        return _error("unknown_unit", "unit_id 不在当前 source-pair manifest 中。")
    allowed_views = {str(item) for item in (unit.get("allowed_views") or [])}
    if selected_view not in VALID_VIEWS or (allowed_views and selected_view not in allowed_views):
        return _error("invalid_view", "selected_view 必须是 front、oblique、side、manual_review 或 skip。")

    assets = _asset_map(unit)
    if selected_view in IMPORTABLE_VIEWS:
        if not selected_before or not selected_after:
            return _error("missing_pair", "front/oblique/side 必须选择真实 before 与 after 图片。")
        if selected_before not in assets:
            return _error("invalid_selected_before", "selected_before 不属于当前真实素材。")
        if selected_after not in assets:
            return _error("invalid_selected_after", "selected_after 不属于当前真实素材。")
        if selected_before == selected_after:
            return _error("same_before_after", "before 和 after 不能是同一张图片。")

    decisions = _draft_decisions(draft)
    target = None
    for decision in decisions:
        if _unit_id(decision) == unit_id:
            target = decision
            break
    if target is None:
        target = {
            "unit_id": unit_id,
            "case_id": unit.get("case_id"),
            "customer_raw": unit.get("customer_raw"),
            "case_rel_path": unit.get("case_rel_path"),
        }
        decisions.append(target)
    target["reviewer"] = reviewer
    target["selected_before"] = selected_before or None
    target["selected_after"] = selected_after or None
    target["selected_view"] = selected_view
    target["review_note"] = review_note
    target["review_status"] = "importable" if _is_importable(target) else selected_view
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
<title>Source Pair Review</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:#f6f6f2;color:#1f2328}
header{position:sticky;top:0;z-index:10;background:#fff;border-bottom:1px solid #d8d8d0;padding:12px 18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0 12px 0 0}label{font-size:13px;color:#59606b}input,select{height:32px;border:1px solid #c8c8c0;border-radius:6px;padding:0 8px;background:#fff}input{min-width:180px}main{padding:16px}.summary{font-size:13px;color:#59606b;margin-bottom:12px}.unit{background:#fff;border:1px solid #d9d9d2;border-radius:8px;margin-bottom:18px;padding:14px}.unit h2{font-size:15px;margin:0 0 4px}.meta{font-size:13px;color:#59606b;margin:0 0 10px;overflow-wrap:anywhere}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.col h3{font-size:14px;margin:0 0 8px}.assets{display:grid;grid-template-columns:repeat(auto-fill,minmax(126px,1fr));gap:8px}.asset{border:1px solid #dddcd5;border-radius:6px;padding:6px;background:#fbfbf8;cursor:pointer}.asset.selected{outline:3px solid #1f6feb}.asset img{display:block;width:100%;height:146px;object-fit:contain;background:#ecece8}.asset div{font-size:12px;color:#59606b;margin-top:5px;overflow-wrap:anywhere}.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px}button{height:34px;border:1px solid #b8bab1;border-radius:6px;background:#fff;padding:0 12px;font-weight:650;cursor:pointer}button.primary{background:#1f6feb;border-color:#1f6feb;color:#fff}textarea{width:100%;min-height:44px;margin-top:10px;box-sizing:border-box;border:1px solid #c8c8c0;border-radius:6px;padding:8px}.pill{display:inline-block;background:#f1f1eb;border:1px solid #dddcd5;border-radius:999px;padding:2px 8px;margin-right:5px;font-size:12px}.toast{position:fixed;right:16px;bottom:16px;background:#1f2328;color:#fff;padding:10px 12px;border-radius:6px;font-size:13px;opacity:0;transition:.18s}.toast.show{opacity:1}@media(max-width:900px){.grid{grid-template-columns:1fr}main{padding:10px}header{position:static}}
</style>
</head>
<body>
<header><h1>Source Pair Review</h1><label>Reviewer <input id="reviewer" placeholder="真实审核人"></label><span id="status" class="summary">加载中</span></header>
<main id="app"></main><div id="toast" class="toast"></div>
<script>
let state=null;const selections={};
function esc(v){return String(v??'').replace(/[&<>"']/g,s=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));}
function toast(msg){const el=document.getElementById('toast');el.textContent=msg;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),1500);}
async function loadState(){const r=await fetch('/api/state');state=await r.json();render();}
function decisionFor(id){return (state.units||[]).find(u=>u.unit_id===id)?.decision||{};}
function setSel(id,role,assetId){selections[id]=selections[id]||{};selections[id][role]=assetId;render();}
function selected(id,role){return (selections[id]&&selections[id][role]) || decisionFor(id)[role==='before'?'selected_before':'selected_after'];}
function assetCard(unit,role,a){const on=selected(unit.unit_id,role)===a.asset_id;return `<div class="asset ${on?'selected':''}" onclick="setSel('${esc(unit.unit_id)}','${role}','${esc(a.asset_id)}')"><img src="/${esc(a.asset_relative_path)}" loading="lazy"><div>${esc(a.filename)} · ${esc(a.phase_guess||'source')}</div></div>`;}
async function save(unitId){const reviewer=document.getElementById('reviewer').value.trim();const view=document.getElementById('view-'+CSS.escape(unitId)).value;const note=document.getElementById('note-'+CSS.escape(unitId)).value.trim();const payload={unit_id:unitId,reviewer,selected_before:selected(unitId,'before'),selected_after:selected(unitId,'after'),selected_view:view,review_note:note};const r=await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await r.json();if(!data.ok){toast(data.error||'保存失败');return;}state=data.state;toast('已保存');render();}
function render(){document.getElementById('status').textContent=`${state.filled_count}/${state.unit_count} 已填 · importable ${state.importable_count} · manual ${state.manual_review_count}`;const rows=(state.units||[]).map(unit=>{const d=decisionFor(unit.unit_id);const before=(unit.before_assets||[]).map(a=>assetCard(unit,'before',a)).join('');const after=(unit.after_assets||[]).map(a=>assetCard(unit,'after',a)).join('');const view=d.selected_view||'';return `<section class="unit"><h2>${esc(unit.unit_id)}</h2><p class="meta">case ${esc(unit.case_id)} · ${esc(unit.customer_raw)} · ${esc(unit.case_rel_path)}</p><p><span class="pill">before ${unit.before_assets.length}</span><span class="pill">after ${unit.after_assets.length}</span><span class="pill">${esc(d.review_status||'pending')}</span></p><div class="grid"><div class="col"><h3>选择术前</h3><div class="assets">${before}</div></div><div class="col"><h3>选择术后</h3><div class="assets">${after}</div></div></div><div class="actions"><label>View <select id="view-${esc(unit.unit_id)}"><option value="">选择</option>${unit.allowed_views.map(v=>`<option value="${esc(v)}" ${view===v?'selected':''}>${esc(v)}</option>`).join('')}</select></label><button class="primary" onclick="save('${esc(unit.unit_id)}')">保存</button><span class="summary">${esc(d.selected_before||'')} ${d.selected_after?' + '+esc(d.selected_after):''}</span></div><textarea id="note-${esc(unit.unit_id)}" placeholder="备注，可空">${esc(d.review_note||'')}</textarea></section>`;}).join('');document.getElementById('app').innerHTML=`<div class="summary">每个案例选择一张真实术前、一张真实术后，并选择 front/oblique/side；不确定时选 manual_review，不会进入 A/B 补样。</div>${rows}`;}
loadState();
</script>
</body>
</html>"""


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
            if urlparse(self.path).path != "/api/save":
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
    parser = argparse.ArgumentParser(description="Run a local source-pair click-review server.")
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument("--manifest-json", type=Path, required=True)
    parser.add_argument("--review-decisions-json", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8781)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    handler = make_handler(args.packet_dir, args.manifest_json, args.review_decisions_json)
    server = LocalReviewHTTPServer((args.host, int(args.port)), handler)
    print(f"Serving source-pair review UI at http://{args.host}:{args.port}/")
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
