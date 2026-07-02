#!/bin/zsh
# 4K 批量监控 + 桌面同步（脱会话 nohup，不随 Claude 会话死）
# 批量: PID 1104 / output /tmp/batch-ai-enhance-20260614-4k / CASE_WORKBENCH_ADAPTIVE_4K=1
OUT=/tmp/batch-ai-enhance-20260614-4k
LOG=/tmp/batch-ai-enhance-20260614-4k.log
DESK="$HOME/Desktop/批量增强-20260614-4K边跑边看"
AUDIT="$DESK/_provider审计.txt"
MONLOG=/tmp/batch-4k-monitor.log
TMPK=/tmp/_4k_okvia.txt
mkdir -p "$DESK"

mlog(){ echo "[$(date '+%m-%d %H:%M:%S')] $*" >> "$MONLOG"; }
mlog "monitor started"

write_audit(){
  grep -aE '\[fullres\].*OK via' "$LOG" 2>/dev/null | sed -E 's/.*\[fullres\] //' > "$TMPK"
  local total rsta tuzi flash boards
  total=$(wc -l < "$TMPK" | tr -d ' ')
  rsta=$(grep -c 'via rsta' "$TMPK" 2>/dev/null); rsta=${rsta:-0}
  tuzi=$(grep -c 'via tuzi' "$TMPK" 2>/dev/null); tuzi=${tuzi:-0}
  flash=$(grep -c 'via flashapi' "$TMPK" 2>/dev/null); flash=${flash:-0}
  boards=$(ls "$OUT"/*_ai_enhanced.jpg 2>/dev/null | wc -l | tr -d ' ')
  {
    echo "# Provider 审计 (live) — $(date '+%m-%d %H:%M:%S')"
    echo
    echo "板完成: ${boards}/47   已增强槽: ${total}"
    echo "rsta(真4K): ${rsta}   tuzi(低清fallback): ${tuzi}   flashapi(低清fallback): ${flash}"
    echo
    echo "## fallback 低清槽 — 需删 cache 重烧成真 4K:"
    grep -anE 'via tuzi|via flashapi' "$TMPK" 2>/dev/null || echo "(暂无 — 全 rsta 真 4K)"
    echo
    echo "## 横版源图槽（确认比例不拉伸：OK via 行 WxH=源图尺寸即未拉伸）:"
    grep -aE '\[fullres\].*源图' "$LOG" 2>/dev/null | sed -E 's/.*\[fullres\] //' \
      | awk -F'源图 |x| ' '{ if($2>$3) print }' | head -40 || true
  } > "$AUDIT"
}

while true; do
  rsync -a --include='*_ai_enhanced.jpg' --exclude='*' "$OUT"/ "$DESK"/ 2>/dev/null
  write_audit
  if ! pgrep -f render_ai_enhanced_boards >/dev/null 2>&1; then
    mlog "batch gone -> final sync"
    rsync -a --include='*_ai_enhanced.jpg' --exclude='*' "$OUT"/ "$DESK"/ 2>/dev/null
    cp "$OUT/boards_manifest.json" "$DESK/boards_manifest.json" 2>/dev/null
    write_audit
    echo "" >> "$AUDIT"; echo "===== DONE $(date '+%m-%d %H:%M:%S') =====" >> "$AUDIT"
    touch "$DESK/_DONE_$(date '+%H%M').flag"
    mlog "done, exit"
    break
  fi
  sleep 180
done
