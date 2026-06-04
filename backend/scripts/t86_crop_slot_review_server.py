"""Serve a local click-review UI for T80 crop/slot review decisions."""
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

DEFAULT_PACKET_DIR = Path("tasks/t80_crop_slot_review_packet")
DEFAULT_MANIFEST_JSON = DEFAULT_PACKET_DIR / "manifest.json"
DEFAULT_DECISIONS_JSON = DEFAULT_PACKET_DIR / "review_decisions_draft.json"

DEFAULT_CROP_ACTIONS = {
    "accept_current_pair",
    "needs_reselect_pair",
    "needs_replace_source",
    "defer_no_safe_alternative",
}
DEFAULT_SLOT_ACTIONS = {
    "manual_phase_view_override",
    "restore_or_add_source_photos",
    "bind_or_rescan_real_source",
    "template_policy_review",
    "defer",
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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _list_dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _allowed_actions(manifest: dict[str, Any], kind: str, unit: dict[str, Any]) -> list[str]:
    unit_actions = [str(item) for item in unit.get("allowed_actions") or [] if str(item)]
    if unit_actions:
        if kind == "crop" and "accept_current_pair" not in unit_actions:
            return ["accept_current_pair", *unit_actions]
        return unit_actions
    key = "crop_allowed_actions" if kind == "crop" else "slot_allowed_actions"
    default = DEFAULT_CROP_ACTIONS if kind == "crop" else DEFAULT_SLOT_ACTIONS
    manifest_actions = [str(item) for item in manifest.get(key) or [] if str(item)]
    return manifest_actions or sorted(default)


def _manifest_units(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for kind, key in (("crop", "crop_review_units"), ("slot", "slot_fill_units")):
        for unit in _list_dicts(manifest.get(key)):
            unit_id = _clean(unit.get("unit_id"))
            if unit_id:
                out[unit_id] = {"kind": kind, "unit": unit}
    return out


def _draft_items(draft: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    key = "crop_decisions" if kind == "crop" else "slot_decisions"
    return _list_dicts(draft.get(key))


def _decision_map(draft: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in _draft_items(draft, kind):
        unit_id = _clean(item.get("unit_id"))
        if unit_id:
            out[unit_id] = item
    return out


def _public_decision(decision: dict[str, Any] | None) -> dict[str, Any]:
    decision = decision or {}
    return {
        "reviewer": decision.get("reviewer"),
        "action": decision.get("action"),
        "note": decision.get("note"),
        "selected_before": decision.get("selected_before"),
        "selected_after": decision.get("selected_after"),
        "selected_assets": decision.get("selected_assets") if isinstance(decision.get("selected_assets"), list) else [],
        "reviewed_at": decision.get("reviewed_at"),
    }


def _asset_refs_from_crop(unit: dict[str, Any], role: str) -> list[dict[str, Any]]:
    current = (unit.get("current_pair") or {}).get(role)
    items: list[dict[str, Any]] = []
    if isinstance(current, dict):
        items.append(current)
    candidates = (unit.get("candidate_assets") or {}).get(role)
    items.extend(_list_dicts(candidates))
    return items


def _asset_key(asset: dict[str, Any]) -> str:
    return _clean(asset.get("asset_relative_path")) or _clean(asset.get("filename"))


def _selection_refs(unit: dict[str, Any], role: str) -> set[str]:
    refs: set[str] = set()
    for item in _asset_refs_from_crop(unit, role):
        filename = _clean(item.get("filename"))
        asset_path = _clean(item.get("asset_relative_path"))
        if filename:
            refs.add(filename)
        if asset_path:
            refs.add(asset_path)
    return refs


def _slot_selection_refs(unit: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for item in _list_dicts(unit.get("source_sample_assets")):
        for key in ("asset_relative_path", "filename", "source_path"):
            value = _clean(item.get(key))
            if value:
                refs.add(value)
    return refs


def _clean_list(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value] if value else []
    out: list[str] = []
    for item in values:
        cleaned = _clean(item)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def build_review_state(manifest_path: Path, draft_path: Path) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    draft = _load_json(draft_path)
    crop_decisions = _decision_map(draft, "crop")
    slot_decisions = _decision_map(draft, "slot")
    units: list[dict[str, Any]] = []
    for unit_id, wrapped in sorted(_manifest_units(manifest).items(), key=lambda item: (item[1]["kind"], item[0])):
        kind = str(wrapped["kind"])
        unit = wrapped["unit"]
        decision = crop_decisions.get(unit_id) if kind == "crop" else slot_decisions.get(unit_id)
        payload = {
            "kind": kind,
            "unit_id": unit_id,
            "case_id": unit.get("case_id"),
            "ticket_ids": unit.get("ticket_ids") or [],
            "reason_code": unit.get("reason_code"),
            "message": unit.get("message"),
            "recommended_action": unit.get("recommended_action"),
            "allowed_actions": _allowed_actions(manifest, kind, unit),
            "blocks_render": bool(unit.get("blocks_render")),
            "blocks_publish": bool(unit.get("blocks_publish")),
            "decision": _public_decision(decision),
        }
        if kind == "crop":
            payload["current_pair"] = unit.get("current_pair") or {}
            payload["candidate_assets"] = unit.get("candidate_assets") or {}
            payload["safe_alternative_pair_count"] = unit.get("safe_alternative_pair_count")
        else:
            payload["missing_slots"] = unit.get("missing_slots") or []
            payload["required_slots"] = unit.get("required_slots") or []
            payload["renderable_slots"] = unit.get("renderable_slots") or []
            payload["source_sample_assets"] = unit.get("source_sample_assets") or []
        units.append(payload)
    draft_items = [*_draft_items(draft, "crop"), *_draft_items(draft, "slot")]
    return {
        "generated_at": _now(),
        "scope": "t86_crop_slot_click_review_state_v1",
        "unit_count": len(units),
        "crop_unit_count": sum(1 for item in units if item["kind"] == "crop"),
        "slot_unit_count": sum(1 for item in units if item["kind"] == "slot"),
        "filled_reviewer_count": sum(1 for item in draft_items if _clean(item.get("reviewer"))),
        "filled_action_count": sum(1 for item in draft_items if _clean(item.get("action"))),
        "units": units,
    }


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": code, "error": message}


def _find_or_create_decision(draft: dict[str, Any], kind: str, unit: dict[str, Any], unit_id: str) -> dict[str, Any]:
    key = "crop_decisions" if kind == "crop" else "slot_decisions"
    decisions = _list_dicts(draft.get(key))
    draft[key] = decisions
    for item in decisions:
        if _clean(item.get("unit_id")) == unit_id:
            return item
    item = {
        "unit_id": unit_id,
        "case_id": unit.get("case_id"),
        "ticket_ids": unit.get("ticket_ids") or [],
        "reviewer": None,
        "action": None,
        "note": None,
    }
    if kind == "crop":
        item["selected_before"] = None
        item["selected_after"] = None
    else:
        item["selected_assets"] = []
    decisions.append(item)
    return item


def save_review_decision(manifest_path: Path, draft_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    draft = _load_json(draft_path)
    units = _manifest_units(manifest)
    unit_id = _clean(payload.get("unit_id"))
    reviewer = _clean(payload.get("reviewer"))
    action = _clean(payload.get("action"))
    kind_hint = _clean(payload.get("kind"))
    note = _clean(payload.get("note")) or None

    if not reviewer:
        return _error("missing_reviewer", "必须填写真实 reviewer。")
    wrapped = units.get(unit_id)
    if not wrapped:
        return _error("unknown_unit", "unit_id 不在当前 T80 manifest 中。")
    kind = str(wrapped["kind"])
    unit = wrapped["unit"]
    if kind_hint and kind_hint != kind:
        return _error("kind_mismatch", "kind 与 manifest 中的 unit 类型不一致。")
    allowed = set(_allowed_actions(manifest, kind, unit))
    if action not in allowed:
        return _error("invalid_action", "action 不在该 unit 的允许范围内。")

    selected_before = _clean(payload.get("selected_before")) or None
    selected_after = _clean(payload.get("selected_after")) or None
    if kind == "crop":
        before_refs = _selection_refs(unit, "before")
        after_refs = _selection_refs(unit, "after")
        if selected_before and selected_before not in before_refs:
            return _error("invalid_selected_before", "selected_before 不属于当前 crop unit 的真实候选。")
        if selected_after and selected_after not in after_refs:
            return _error("invalid_selected_after", "selected_after 不属于当前 crop unit 的真实候选。")
    else:
        selected_assets = _clean_list(payload.get("selected_assets"))
        slot_refs = _slot_selection_refs(unit)
        invalid_assets = [item for item in selected_assets if item not in slot_refs]
        if invalid_assets:
            return _error("invalid_selected_assets", "selected_assets 包含不属于当前 slot unit 的真实源图。")

    decision = _find_or_create_decision(draft, kind, unit, unit_id)
    decision["case_id"] = unit.get("case_id")
    decision["ticket_ids"] = unit.get("ticket_ids") or []
    decision["reviewer"] = reviewer
    decision["action"] = action
    decision["note"] = note
    decision["reviewed_at"] = _now()
    if kind == "crop":
        decision["selected_before"] = selected_before
        decision["selected_after"] = selected_after
    else:
        decision["selected_assets"] = selected_assets
    _write_json(draft_path, draft)
    return {
        "ok": True,
        "decision": _public_decision(decision),
        "state": build_review_state(manifest_path, draft_path),
    }


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
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>T80 Crop / Slot 点击审核</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;background:#f6f7f8;color:#20242a;}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid #d9dde3;padding:12px 18px;z-index:10;display:flex;gap:12px;align-items:center;flex-wrap:wrap;}
h1{font-size:18px;margin:0 12px 0 0;}label{font-size:13px;color:#596273;}input{height:32px;border:1px solid #c8d0db;border-radius:6px;padding:0 10px;min-width:180px;}
main{padding:18px}.unit{background:#fff;border:1px solid #d9dde3;border-radius:8px;margin-bottom:18px;padding:14px}.unit h2{font-size:16px;margin:0 0 4px}
.meta{font-size:13px;color:#596273;margin:0 0 10px;line-height:1.45}.pill{display:inline-block;border-radius:999px;padding:2px 8px;font-size:12px;background:#edf2f7;color:#344054;margin-right:6px}
.pair{display:grid;grid-template-columns:repeat(2,minmax(220px,1fr));gap:10px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
figure{margin:0;border:1px solid #e1e5ea;background:#eef1f4;border-radius:6px;padding:6px}figure.selectable{cursor:pointer;position:relative}figure.selectable:hover{border-color:#7aa7ff;background:#f8fbff}figure.selectable.chosen{border-color:#2563eb;background:#eff6ff;box-shadow:0 0 0 3px rgba(37,99,235,.24)}figure.selectable.chosen:after{content:'已选';position:absolute;right:8px;top:8px;background:#2563eb;color:#fff;border-radius:999px;padding:2px 7px;font-size:11px;font-weight:700}img{width:100%;height:180px;object-fit:contain;display:block}
figcaption{font-size:11px;color:#4c5565;line-height:1.35;word-break:break-all;margin-top:4px}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
button{border:1px solid #b9c1cc;background:#fff;border-radius:6px;height:34px;padding:0 10px;font-weight:650;cursor:pointer}button.selected{outline:3px solid #8ab4ff}
textarea,select{border:1px solid #c8d0db;border-radius:6px;padding:7px;box-sizing:border-box}textarea{width:100%;min-height:48px;margin-top:10px}.selectors{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.selectors label{display:flex;gap:6px;align-items:center}.selection-help{font-size:12px;color:#475467;background:#f8fafc;border:1px dashed #cbd5e1;border-radius:6px;padding:7px 9px;margin:8px 0}.selection-summary{font-size:12px;color:#344054;margin:8px 0}
.toast{position:fixed;right:16px;bottom:16px;background:#20242a;color:#fff;padding:10px 12px;border-radius:6px;font-size:13px;opacity:0;transition:.18s}.toast.show{opacity:1}
@media(max-width:900px){main{padding:10px}header{position:static}.pair{grid-template-columns:1fr}}
</style>
</head>
<body>
<header><h1>T80 Crop / Slot 点击审核</h1><label>Reviewer <input id="reviewer" placeholder="真实审核人"></label><span id="status" class="meta">加载中</span></header>
<main id="app"></main><div id="toast" class="toast"></div>
<script>
let state=null;
function esc(v){return String(v ?? '').replace(/[&<>"']/g,s=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));}
function toast(msg){const el=document.getElementById('toast');el.textContent=msg;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),1600);}
async function loadState(){const res=await fetch('/api/state');state=await res.json();render();}
function img(asset, alt){return asset ? `<img src="/${esc(asset)}" alt="${esc(alt)}">` : `<div class="meta">无法获取真实图片</div>`;}
function assetKey(item){return item.asset_relative_path || item.filename || '';}
function options(items, selected){return `<option value="">不指定</option>`+(items||[]).map(item=>{const v=assetKey(item);return `<option value="${esc(v)}" ${v===selected?'selected':''}>${esc(item.filename||v)}</option>`}).join('');}
function figure(item, opts={}){
  const key=assetKey(item||{}); const role=opts.role||''; const chosen=opts.selected?' chosen':''; const selectable=role&&key?' selectable':'';
  const attrs=role&&key?` data-select-role="${esc(role)}" data-asset-key="${esc(key)}"`:'';
  const label=opts.label?`<span class="pill">${esc(opts.label)}</span>`:'';
  return `<figure class="${selectable}${chosen}"${attrs}>${img(item?.asset_relative_path,item?.filename)}<figcaption>${label}${esc(item?.filename||key||'')}</figcaption></figure>`;
}
function renderCrop(unit){
  const before=(unit.current_pair||{}).before||{}, after=(unit.current_pair||{}).after||{}, c=unit.candidate_assets||{}, d=unit.decision||{};
  const beforeItems=[before,...(c.before||[])], afterItems=[after,...(c.after||[])];
  const sb=d.selected_before||'', sa=d.selected_after||'';
  return `<div class="selection-help">点击图片选择 before/after；也可以用下拉框。不选择图片时只记录动作决策。</div>
    <h3>当前阻断配对</h3><div class="pair">${figure(before,{role:'before',selected:assetKey(before)===sb,label:'current before'})}${figure(after,{role:'after',selected:assetKey(after)===sa,label:'current after'})}</div>
    <div class="selectors"><label>selected_before <select class="selected-before">${options(beforeItems,sb)}</select></label><label>selected_after <select class="selected-after">${options(afterItems,sa)}</select></label></div>
    <h3>候选 before</h3><div class="grid">${(c.before||[]).map(item=>figure(item,{role:'before',selected:assetKey(item)===sb,label:'candidate before'})).join('')||'<p class="meta">无候选</p>'}</div>
    <h3>候选 after</h3><div class="grid">${(c.after||[]).map(item=>figure(item,{role:'after',selected:assetKey(item)===sa,label:'candidate after'})).join('')||'<p class="meta">无候选</p>'}</div>`;
}
function renderSlot(unit){
  const selected=new Set((unit.decision||{}).selected_assets||[]);
  return `<p class="meta">missing ${esc(JSON.stringify(unit.missing_slots||[]))}</p>
    <div class="selection-help">点击源图样本进行多选；选择会写入 draft 的 selected_assets，供后续人工覆盖/补槽位使用。</div>
    <div class="selection-summary">已选 <span class="selected-assets-count">${selected.size}</span> 张</div>
    <div class="grid">${(unit.source_sample_assets||[]).map(item=>figure(item,{role:'slot-source',selected:selected.has(assetKey(item)),label:'source'})).join('')||'<p class="meta">无真实源图样本</p>'}</div>`;
}
function render(){
  document.getElementById('status').textContent=`${state.filled_action_count}/${state.unit_count} 已判定 · crop ${state.crop_unit_count} · slot ${state.slot_unit_count}`;
  document.getElementById('app').innerHTML=state.units.map(unit=>{
    const d=unit.decision||{}; const body=unit.kind==='crop'?renderCrop(unit):renderSlot(unit);
    const buttons=(unit.allowed_actions||[]).map(a=>`<button data-action="${esc(a)}" class="${d.action===a?'selected':''}">${esc(a)}</button>`).join('');
    return `<section class="unit" data-unit="${esc(unit.unit_id)}" data-kind="${esc(unit.kind)}">
      <h2>${esc(unit.unit_id)}</h2>
      <p class="meta"><span class="pill">${esc(unit.kind)}</span>case ${esc(unit.case_id)} · tickets ${esc(JSON.stringify(unit.ticket_ids||[]))} · recommended ${esc(unit.recommended_action)} · current ${esc(d.action||'未判定')} ${esc(d.reviewer||'')}</p>
      ${body}<textarea placeholder="审核备注，可空">${esc(d.note||'')}</textarea><div class="actions">${buttons}</div>
    </section>`;
  }).join('');
  document.querySelectorAll('.unit').forEach(section=>{
    section.querySelectorAll('button').forEach(btn=>btn.onclick=()=>saveDecision(section,btn.dataset.action));
    section.querySelectorAll('select').forEach(sel=>sel.onchange=()=>refreshSelection(section));
    section.querySelectorAll('figure.selectable').forEach(fig=>fig.onclick=()=>selectAsset(section,fig));
    refreshSelection(section);
  });
}
function selectAsset(section, fig){
  const role=fig.dataset.selectRole; const key=fig.dataset.assetKey; if(!role||!key)return;
  if(role==='before'||role==='after'){
    const selector=role==='before'?'.selected-before':'.selected-after';
    const select=section.querySelector(selector); if(select) select.value=key;
  }else if(role==='slot-source'){
    fig.classList.toggle('chosen');
  }
  refreshSelection(section);
}
function refreshSelection(section){
  const before=section.querySelector('.selected-before')?.value||''; const after=section.querySelector('.selected-after')?.value||'';
  section.querySelectorAll('figure[data-select-role="before"]').forEach(fig=>fig.classList.toggle('chosen',fig.dataset.assetKey===before&&!!before));
  section.querySelectorAll('figure[data-select-role="after"]').forEach(fig=>fig.classList.toggle('chosen',fig.dataset.assetKey===after&&!!after));
  const count=section.querySelectorAll('figure[data-select-role="slot-source"].chosen').length;
  const el=section.querySelector('.selected-assets-count'); if(el) el.textContent=String(count);
}
async function saveDecision(section, action){
  const reviewer=document.getElementById('reviewer').value.trim(); if(!reviewer){toast('先填写真实 reviewer');return;}
  const body={unit_id:section.dataset.unit,kind:section.dataset.kind,reviewer,action,note:section.querySelector('textarea')?.value||''};
  const before=section.querySelector('.selected-before')?.value || ''; const after=section.querySelector('.selected-after')?.value || '';
  if(before) body.selected_before=before; if(after) body.selected_after=after;
  const selectedAssets=[...section.querySelectorAll('figure[data-select-role="slot-source"].chosen')].map(fig=>fig.dataset.assetKey).filter(Boolean);
  if(selectedAssets.length) body.selected_assets=selectedAssets;
  const res=await fetch('/api/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await res.json(); if(!data.ok){toast(data.error||'保存失败');return;} state=data.state; render(); toast('已保存');
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
    parser = argparse.ArgumentParser(description="Run a local T80 crop/slot click-review server.")
    parser.add_argument("--packet-dir", type=Path, default=DEFAULT_PACKET_DIR)
    parser.add_argument("--manifest-json", type=Path, default=DEFAULT_MANIFEST_JSON)
    parser.add_argument("--review-decisions-json", type=Path, default=DEFAULT_DECISIONS_JSON)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    handler = make_handler(args.packet_dir, args.manifest_json, args.review_decisions_json)
    server = LocalReviewHTTPServer((args.host, int(args.port)), handler)
    print(f"Serving T80 crop/slot click-review UI at http://{args.host}:{args.port}/")
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
