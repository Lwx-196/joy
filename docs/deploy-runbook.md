# Production Deploy Runbook — case-workbench promotion 灰度 + SLO Monitor

> 适用范围：case-workbench Wave 1-4 全交付后的 production 部署流程。Wave 4 release deploy gate (`994224e`) 后，placeholder baseline 会被 fail-closed 拒绝加载 — 本 runbook 是 operator 走完 5 步上线的标准路径。

---

## 前提

- `main` HEAD ≥ `994224e`（含 Wave 4 release deploy gate）
- DB schema 含 Wave 1-3 加的所有表：`simulation_jobs`、`candidate_lineage`、`ops_audit_log`、`promotion_audit_log`
- backend / frontend 已部署，但 promotion 状态机 **未进入 production 灰度**（即 `manifest.json` 缺失或 state=`shadow`）

如果上述任一不满足，先回到 owner WIP 收尾 + main rebase + schema migration，本 runbook 不能跳过。

---

## 5 步标准上线流程

### Step 1 — Schema migration + 验证

```bash
# 在 production 主机
cd /path/to/case-workbench
git fetch origin main
git checkout main
git reset --hard origin/main  # 仅在确认本地无 owner WIP 时
./scripts/migrate.sh           # 或者按你的迁移脚本

# 验证 Wave 1-3 后加的表存在
sqlite3 case-workbench.db ".tables" | tr ' ' '\n' | grep -E "ops_audit_log|promotion_audit_log|candidate_lineage|simulation_jobs"
# 期望输出 4 张表全部存在
```

**Gate**：4 表都在才能进 Step 2。缺任何一张回头跑 migration。

### Step 2 — 初始化 promotion manifest (shadow state)

`compute_manifest_hashes` 计算 source hash bindings，但要求 manifest 文件已存在。手工创建初始文件后再算 bindings：

```bash
# 手工创建初始 manifest（promotion_state=shadow，所有 case_id 都视作非 promoted）
mkdir -p case-workbench-ai/promotion
cat > case-workbench-ai/promotion/manifest.json <<'JSON'
{
  "promotion_state": "shadow",
  "bindings": {}
}
JSON

# 算 + 写入 source hash bindings
python -m backend.scripts.compute_manifest_hashes --write

# 验证
cat case-workbench-ai/promotion/manifest.json | jq '.promotion_state'
# 期望输出："shadow"
```

**Gate**：`manifest.json` 必须存在 + `promotion_state="shadow"`。

`promotion_manifest_loader.py` 默认 fail-closed — manifest 不存在或 `promotion_state` 未知 → 全部 case 走 shadow（不发布到 production），所以这一步安全可重复。

### Step 3 — Shadow mode 跑 ≥ 48h 产真数据

部署服务，让生产流量正常打过来，但**不切灰度**。所有 simulation_jobs / candidate_lineage / VLM judge / delivery_gate / ops_audit_log 数据会自然产生。

```bash
# 监控数据积累（每 6h 跑一次）
sqlite3 case-workbench.db "
SELECT 'simulation_jobs', COUNT(*) FROM simulation_jobs WHERE julianday(created_at) >= julianday('now', '-48 hours')
UNION ALL SELECT 'candidate_lineage', COUNT(*) FROM candidate_lineage WHERE julianday(created_at) >= julianday('now', '-48 hours')
UNION ALL SELECT 'ops_audit_log', COUNT(*) FROM ops_audit_log WHERE julianday(created_at) >= julianday('now', '-48 hours');
"
```

**Gate**：跑足够长（建议 48h）让样本累积 ≥ `minimum_sample_size`（默认 30），否则 Step 4 calibrate 写出来的 baseline 仍是低样本不可信。

业务流量小的项目可能要 7d+ — 这正是 Wave 4 W4-2 `PAUSED_STALE_DAYS=7d` 的设计依据（灰度 7d 仍没积够数据 → stop-loss halt 报警让 operator 介入）。

**Wave 5 #1 起**：`paused_stale_days` 可在 `slo_thresholds.json` 顶层调整（int / > 0），无需 redeploy。低流量项目可临时拉长到 14d / 30d；高流量可缩到 3d 更快感知。规则同 `minimum_sample_size` — JSON 字段缺失时回退模块常量 7。

### Step 4 — Calibrate baseline + 替换 placeholder thresholds

```bash
# Dry-run 先看观察值（不写文件）
python -m backend.scripts.calibrate_slo_baseline --window 48

# 检查输出
# - observed.sample_size 应 >= 30
# - 各维度 observed rate 是否在 reasonable 范围（comfyui_failure_rate < 0.3 / vlm_disagreement < 0.5 / 等）
# - patch.baseline_provenance.computed_by == "calibrate_cli"
# - patch.baseline_provenance.computed_at_main_sha 是真 sha 不是 "unknown"

# 如果 dry-run 输出合理，apply
python -m backend.scripts.calibrate_slo_baseline --window 48 --apply

# 验证写入
cat case-workbench-ai/promotion/slo_thresholds.json | jq '.baseline_provenance'
# computed_by 应为 "calibrate_cli"，sample_size > 0，computed_at_main_sha 是 git short sha
```

**Gate（W4-1 production deploy gate 把守这里）**：

- `computed_by` 必须在 `_LEGITIMATE_COMPUTED_BY = {"calibrate_cli"}` 白名单（K-4 hardening）
- `sample_size >= 1`（K-4 严格化后 placeholder + sample=0 完全绕不过）
- `baseline_provenance.measured_at` 必须是有时区 ISO8601

如果跑 backend / SLO monitor 时遇到 `ValueError: baseline 未校准，请跑 python -m backend.scripts.calibrate_slo_baseline ...` — 就是 W4-1 gate 在保护你，没替换 placeholder。回到本 Step 跑 calibrate。

### Step 5 — 灰度推进 + SLO monitor + auto-rollback 全链路守护

```bash
# 5.1 切到 p10（10% 流量灰度）
# 编辑 case-workbench-ai/promotion/manifest.json 把 promotion_state 改成 "p10"
# 或用 ops tool（如果有）

# 5.2 部署 SLO check cron（推荐每 15 min 一次）
# launchd plist 示例见下方"Cron 配置"段

# 5.3 监控 SLO report
python -m backend.scripts.promotion_slo_check --window 48
# 输出含 SLOReport markdown + violations 列表

# 5.4 ≥ 1 个完整 baseline window（48h）观察后，逐步推进
# p10 → p25 → p50 → p100
# 每一档至少跑 1 个 baseline window 看 SLO 全绿
```

**Auto-rollback 工作流**（已闭环，operator 不用手动）：

- `promotion_rollback_applier` 监听 SLO `recommendation`：
  - `continue` / `monitoring_paused` → 不动
  - `rollback` → 自动回滚 manifest 到 `rolled_back` + 写 audit
  - `stop_loss_halt`（K-5 Wave 4）→ **仅 alert 不动 manifest**，operator 必须介入

---

## Cron 配置（macOS launchd 示例）

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.case-workbench.slo-check</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>
            cd /path/to/case-workbench &&
            python -m backend.scripts.promotion_slo_check --window 48
            RC=$?
            case $RC in
                0) ;;                           # continue / monitoring_paused — noop
                1) /path/to/notify.sh rollback ;;  # 真 SLO breach（applier 已自动回滚）
                3) /path/to/notify.sh halt    ;;  # K-5 STOP_LOSS_HALT — operator 介入
                *) /path/to/notify.sh error   ;;
            esac
        </string>
    </array>
    <key>StartInterval</key>
    <integer>900</integer>  <!-- 15 min -->
</dict>
</plist>
```

**Exit code 表**（Wave 4 K-5 hardening 后）：

| Exit | recommendation | 含义 | operator 动作 |
|---|---|---|---|
| 0 | `continue` / `monitoring_paused` | SLO 全绿或样本不足 | 无 |
| 1 | `rollback` | SLO 真违反 | applier 已自动回滚，事后 review |
| 2 | (runtime error) | CLI / DB 异常 | 查 stderr |
| 3 | `stop_loss_halt` | 灰度 paused > 7d 没数据 | 介入：要么扩流量加速 paused 评估，要么手动 demote 回 shadow |

---

## 常见 W4-1 gate 错误 + 修复

| 错误消息 | 根因 | 修复 |
|---|---|---|
| `ValueError: baseline 未校准，请跑 calibrate_slo_baseline --apply` | placeholder baseline 未替换 | Step 4 跑 calibrate |
| `unknown computed_by 'ops_seed' — must be one of frozenset({'calibrate_cli'})` (K-4) | operator 手改 `computed_by` 字段试图绕 gate | 必须用 `calibrate_slo_baseline` CLI 写 provenance，不能手填 |
| `'measured_at' must be timezone-aware` | 手编 ISO8601 缺时区后缀 | 永远用 `+00:00` 或 `Z` UTC 后缀 |
| `'measured_at' is older than 60 days` (baseline_stale) | baseline 太老 | 重跑 calibrate（应在 W4-1 触发前自动周期性 recalibrate） |

---

## Wave 4 sidecar state files（不入 git）

- `case-workbench-ai/promotion/slo_paused_state.json` — W4-2 paused tracking
- `case-workbench-ai/promotion/slo_paused_state.json.lock` — K-1 fcntl lock 文件
- `case-workbench-ai/promotion/slo_paused_state.json.<pid>.<uuid>.tmp` — K-1 atomic write 临时文件

`.gitignore` (K-8 hardening) 已 ignore 这三类。`production` 部署目录这些路径 **必须可写**（容器 read-only fs 会让 paused state 写失败 → 触发 `paused_state_unreadable` STOP_LOSS_HALT，operator 介入）。

---

## 紧急回滚（手动）

如果自动 rollback 没触发但 operator 想强制：

```bash
# 1. 编辑 manifest.json 把 promotion_state 改 "rolled_back"
# 2. 写 ops_audit_log
sqlite3 case-workbench.db "
INSERT INTO ops_audit_log (endpoint, outcome, request_id, payload_json, created_at)
VALUES ('manual_rollback', 'ok', 'manual-$(uuidgen)', '{\"reason\": \"...\"}', datetime('now'));
"
# 3. 让 SLO monitor 下次 cron 跑感知到（or 立即跑 promotion_slo_check）
```

---

## Wave 4 hardening 参考

| Hardening | 文件 | 影响 |
|---|---|---|
| K-1 sidecar fcntl.flock | `promotion_slo_monitor.py:_paused_state_lock` | 多 cron 并发安全；BlockingIOError → 跳过 write log warning |
| K-2 `_merge_thresholds` fallback 限制 | `promotion_slo_monitor.py:_merge_thresholds` | 三分支：(a) `SLO_TEST_MODE=1` → 安全 fallback 到 code defaults / (b) user override 自带合法 `baseline_provenance` → fallback / (c) prod 模式 + 无 test_mode + override 无合法 provenance → raise |
| K-3 `_TEST_MODE_TRUTHY` 白名单 | `promotion_slo_monitor.py:_is_test_mode` | `SLO_TEST_MODE` 只接受 `1/true/True/yes/Yes/TRUE/YES`，其他全 prod |
| K-4 `_LEGITIMATE_COMPUTED_BY` | `promotion_slo_monitor.py:_validate_baseline_provenance` | 未知 producer 字符串拒绝 |
| K-5 `STOP_LOSS_HALT` 拆 ROLLBACK | `promotion_slo_monitor.py` + `promotion_rollback_applier.py` | stop-loss 不动 manifest 仅 audit alert |
| K-6 `_load_paused_state` 三态 | `promotion_slo_monitor.py:_load_paused_state` | OSError 不重置计时器，保守 STOP_LOSS_HALT |
| K-7 violation render 兼容 | `promotion_slo_check.py:_render_violation_row` | actual_days/threshold_days 自适应 |
| K-8 .gitignore sidecar | `.gitignore` | runtime state 不入 git |

---

## 不在本 runbook 范围

- DB 备份 / restore 策略（按各 ops 团队既有 SOP）
- 容器编排（k8s / nomad / docker-compose）配置
- 监控告警（pagerduty / 企微 / 飞书 webhook）接入
- Wave 4 hardening 内部实现细节（见 journal/2026-05-28.md Wave 4 续段）

---

**最后更新**：2026-05-28（Wave 4 + Wave 5 followup #1）
