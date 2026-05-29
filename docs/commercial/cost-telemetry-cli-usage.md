# Cost Telemetry CLI 使用指南

> Phase C5.3 deliverable（Stream C）— 商业团队 self-service 操作手册。
> 配套脚本：`backend/scripts/aggregate_cost_telemetry.py`（只 SELECT，不写 DB）。
> 目的：A 流 C4.5 soft launch ≥ 1 周 telemetry 落地后，商业团队**自助**跑出真实聚合数据，
> 回填 `delivery/c5-cost-model.md` / `delivery/c5-sla-commitment.md` / `docs/commercial/sla-template.md` 的 `<<*_PENDING>>` placeholder。

---

## 0. 读者与边界

- **读者**：商业 / 运营 / Risk-Finance 团队（无需读 Python）
- **脚本边界**：纯只读 SELECT（`vlm_usage_log` / `render_jobs` / `simulation_jobs` / `candidate_lineage`），不 INSERT/UPDATE/DELETE/DDL，不改 schema
- **本脚本只产出 DB 内可聚合的数字**；GPU rental / 电力 / 硬件 / 人工 review 成本**在 DB 外**，由 finance 单独核算后相加（见 §5）

---

## 1. 前置条件

脚本随时可跑，但**输出有意义**需满足：

| 前置 | 来源 | 状态 |
|---|---|---|
| `vlm_usage_log` 有付费 VLM judge 调用记录（`cost_usd > 0`） | A 流 C2 + C4.5 路由真实 judge | ⏳ 当前 classifier 走本地 mlx，`cost_usd=0` |
| `render_jobs` 有真客户 case 完成记录 | A 流 C4.5 soft launch | ⏳ 等灰度 |
| 累计窗口 ≥ 7 天完整数据 | A 流 soft launch ≥ 1 周 | ⏳ plan v2 估 6-9 周内 |

> 在前置满足前跑出的数字是**主 worktree 历史样本**（含 classifier / drill），不能直接当 SLA 承诺值回填。

---

## 2. 命令

```bash
# (1) 默认 7 天窗口，JSON 打到 stdout
python -m backend.scripts.aggregate_cost_telemetry --window 7

# (2) 30 天窗口，原子写入 delivery 目录（推荐回填用）
python -m backend.scripts.aggregate_cost_telemetry --window 30 \
    --output delivery/c5-cost-telemetry-$(date +%F).json

# (3) 指定 DB（测试 / staging / 主 worktree 快照）
CASE_WORKBENCH_DB_PATH=/Users/a1234/Desktop/案例生成器/case-workbench/case-workbench.db \
    python -m backend.scripts.aggregate_cost_telemetry --window 30
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--window <int>` | `7` | 滑动窗口天数（按 `vlm_usage_log.created_at` / `render_jobs.enqueued_at` 截断）；必须正整数 |
| `--output <path>` | 无（打 stdout） | 给定则原子写（tmp + `os.replace`），自动建父目录 |
| `CASE_WORKBENCH_DB_PATH` (env) | worktree 内 `case-workbench.db` | 覆盖 DB 路径；本 worktree 默认 DB 为空，回填务必指向主 DB |

**退出码**：`0` 成功 / `2` 参数非法或读失败（如表不存在）。

---

## 3. 输出字段全表

```
{
  "schema_version": 1,
  "window_days": 30,
  "cutoff_iso": "...",          # UTC ISO-8601，= 生成时刻 - window
  "generated_at_iso": "...",    # UTC ISO-8601 生成时刻
  "vlm_usage": { ... },
  "render_jobs": { ... },
  "simulation_jobs": { ... },
  "candidate_lineage": { ... },
  "cost_per_case": { ... },
  "limitations": [ ... ]        # 脚本同步输出的已知盲区，回填前必读
}
```

| 字段 | 含义 | 数据源 |
|---|---|---|
| `vlm_usage.total_calls` | 窗口内 VLM 调用总数 | `vlm_usage_log` |
| `vlm_usage.total_cost_usd` | VLM API 总成本（vendor USD） | `vlm_usage_log.cost_usd` |
| `vlm_usage.cost_usd_per_call_avg` | 单次调用均价 | 派生 |
| `vlm_usage.latency_ms_p50` / `_p95` | **VLM 调用**延迟（**非**客户 render 延迟，见 §4 ⚠️） | `vlm_usage_log.latency_ms` |
| `vlm_usage.by_purpose` | 按用途拆（classifier / judge / ...） | `vlm_usage_log.purpose` |
| `vlm_usage.by_provider_model` | 按 provider+model 拆 calls / cost | `vlm_usage_log` |
| `render_jobs.total_finished` | `done` + `done_with_issues` 合计 | `render_jobs.status` |
| `render_jobs.duration_ms_p50` / `_p95` | **客户可见** render 时长（**SLA latency 源**） | `julianday(finished_at)-julianday(started_at)`，不含 queue wait |
| `render_jobs.by_status` | 各状态计数 | `render_jobs.status` |
| `simulation_jobs.by_status` / `drill_excluded` | ComfyUI sim 路径代理；drill 排除（best-effort 字符串嗅探，C3.0.4 后改 JOIN） | `simulation_jobs` |
| `candidate_lineage.attempts_per_case_avg` / `failure_reasons` | 重试次数 / 失败原因分布 | `candidate_lineage` |
| `cost_per_case.vlm_api_cost_usd` | VLM 成本 / 唯一完成 case | 派生（`total_cost_usd / estimated_eligible_cases`） |
| `cost_per_case.estimated_eligible_cases` | 窗口内有完成 render_job 的唯一 case 数 | 派生 |

---

## 4. 字段 → placeholder 映射（回填用核心表）

| 目标 placeholder | 文档 | 来自脚本字段 | 备注 |
|---|---|---|---|
| `<<P50_PENDING>>` / latency p50 | `c5-sla-commitment.md` / `sla-template.md` | `render_jobs.duration_ms_p50` ÷ 1000 → 秒 | ✅ 直接映射 |
| `<<P95_PENDING>>` / latency p95 | 同上 | `render_jobs.duration_ms_p95` ÷ 1000 → 秒 | ✅ 直接映射 |
| `<<P99_PENDING>>` / latency p99 | 同上 | **❌ 本脚本不算 p99** | 需扩脚本或 ad-hoc SQL；当前盲区 |
| `<<VLM_API_PENDING>>` 可变成本 | `c5-cost-model.md` | `cost_per_case.vlm_api_cost_usd` | ✅ 单 case VLM 成本 |
| `<<MONTHLY_CASES_PENDING>>` | `c5-cost-model.md` | `cost_per_case.estimated_eligible_cases` × (30 / window) | 按窗口外推月度量 |
| `<<MIN_SAMPLE_PENDING>>` / 样本量 | `c5-sla-commitment.md` | `render_jobs.total_finished` 作下界代理 | 正式样本量以 `slo_thresholds.json` 为准 |
| `<<WIN_RATE_PENDING>>` / VLM 胜率 | `c5-sla-commitment.md` / `sla-template.md` | **❌ 不来自本脚本** | 来自 `candidate_lineage.vlm_judge_result_json`（经 `promotion_slo_monitor`，A 流 C2 N=10 + C4.5） |
| `<<GPU_AMORT_PENDING>>` / GPU / 电力 / 硬件 / 人工 | `c5-cost-model.md` | **❌ DB 外** | finance 核算后相加（见 §5） |
| `<<AVAIL_PENDING>>` / 增强成功率 | `c5-sla-commitment.md` | 部分可由 `render_jobs.by_status` + `render_jobs.meta_json.ai_usage`（JSON 字段）推 | 非脚本单独输出，需配合 `simulation_jobs` / `promotion_audit_log` |

> **⚠️ 三个最易踩的坑**：
> 1. **`vlm_usage.latency_ms` ≠ 客户 SLA latency**。客户 SLA 的 p50/p95/p99 是 **`render_jobs.duration`**（端到端出图时长）。VLM latency 只是其中一段 judge 调用耗时，别混填。
> 2. **脚本只产 p50/p95，没有 p99**。`<<P99_PENDING>>` 不能用本脚本回填——需另跑 SQL 或扩脚本。
> 3. **胜率不来自本脚本**。`<<WIN_RATE_PENDING>>` 走 `candidate_lineage.vlm_judge_result_json`（A 流交付物，经 `promotion_slo_monitor`），不要从本脚本 `by_purpose` 里硬凑。

---

## 5. 本脚本给不了的（finance 必须在 DB 外补）

`cost_per_case._note` 与 `limitations` 字段已显式声明：

- **GPU rental / amortized**：`render_jobs.duration_ms × GPU hourly rate`，但 GPU hourly 是云账单/硬件折旧，DB 无
- **电力 / 散热**：DB 外
- **硬件折旧**：DB 外
- **人工 case review**（C5.6 hypercare 期）：DB 外

端到端单 case 成本 = `cost_per_case.vlm_api_cost_usd`（脚本）+ 以上四项（finance）。

---

## 6. 真实 sample output

实跑主 worktree DB（`case-workbench/case-workbench.db`，window=30，2026-05-28 captured，**含历史 classifier / drill 数据，非 SLA 承诺值**）：

```json
{
  "schema_version": 1,
  "window_days": 30,
  "vlm_usage": {
    "total_calls": 797,
    "total_cost_usd": 0.0,
    "latency_ms_p50": 10376.0,
    "latency_ms_p95": 14949.0,
    "by_provider_model": [
      { "provider": "openai_chat_completions", "model": "mlx-community/Qwen3-VL-4B-Instruct-4bit", "calls": 792, "cost_usd": 0.0 },
      { "provider": "vertex_generate_content_adc", "model": "gemini-2.5-flash", "calls": 5, "cost_usd": 0.0 }
    ]
  },
  "render_jobs": {
    "total_finished": 281,
    "duration_ms_p50": 27131.0,
    "duration_ms_p95": 82868.0,
    "by_status": { "failed": 45, "done_with_issues": 207, "done": 74, "blocked": 231, "cancelled": 11 }
  },
  "simulation_jobs": { "total": 975, "by_status": { "done": 805, "failed": 142, "done_with_issues": 27, "running": 1 }, "drill_excluded": 0 },
  "candidate_lineage": { "total_attempts": 0, "unique_cases": 0, "attempts_per_case_avg": 0.0 },
  "cost_per_case": { "vlm_api_cost_usd": 0.0, "estimated_eligible_cases": 56, "total_vlm_api_cost_usd": 0.0 }
}
```

**怎么读这份样本**：
- `total_cost_usd=0` 因为当前 classifier 走**本地 mlx 模型**（`Qwen3-VL-4B-Instruct-4bit`，零 API 成本，符合 PII 不出本机边界）。C4.5 路由付费 judge 后此值才非 0。
- `render_jobs.duration_ms_p50=27.1s / p95=82.9s` 是历史混合数据（含 layout-only + 各 brand），**不是** ComfyUI focal GA 期的客户 SLA 值。
- `candidate_lineage` 全 0 = 该表此窗口无数据（A 流 C0.5 schema 升级后才填）。

---

## 7. 回填工作流（商业团队 step-by-step）

1. 确认前置（§1）满足：A 流 soft launch ≥ 1 周 + 真客户 case 累计达 `slo_thresholds.json` 样本量
2. 跑命令 (2)，`--window` 取覆盖完整 soft launch 窗口的天数，`--output` 写 `delivery/c5-cost-telemetry-<date>.json`
3. 按 §4 映射表把脚本字段填进三个 doc 的 `<<*_PENDING>>`
4. p99 / 胜率 / GPU·电力·人工 按 §4/§5 从对应外部源补
5. finance（D4 = A）按 `raci-matrix.md` 签字
6. 自检：`grep -rn "PENDING" delivery/ docs/commercial/ docs/customer/` 应 0 残留
7. PR 合并，同步 `docs/commercial/billing-policy.md` / `docs/customer/billing.md` 数字

---

## 8. 已知 limitations（脚本 `limitations` 字段同步输出）

- `promotion_audit_log` JOIN 未落地（C3.0.4 deliverable）→ drill 排除当前用 `simulation_jobs.audit_json` 字符串嗅探，best-effort
- GPU rental / 电力 / 硬件 / 人工 review 成本在 DB 外
- `render_jobs.duration` 用 `julianday` 差（ms 粒度），**不含 queue wait time**
- 脚本只算 p50 / p95，**无 p99**

---

## 9. References

- 脚本：`backend/scripts/aggregate_cost_telemetry.py`（schema 见顶部 docstring）
- 成本模型：`delivery/c5-cost-model.md`（§6 已有简版用法 + 5-28 smoke）
- SLA：`delivery/c5-sla-commitment.md` / `docs/commercial/sla-template.md`
- RACI：`docs/commercial/raci-matrix.md`（D4 = Risk/Finance）
- Plan：`.claude/plan/comfyui-vlm-ga.md` Phase C5.3 / C5.3.1
