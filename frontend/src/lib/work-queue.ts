/**
 * Work queue lanes: presentation metadata + assembly from the backend summary.
 *
 * Five lanes by ROI of "what should the user do next":
 *   1. todayNew      — cases whose last_modified is today (filesystem changes the user just made)
 *   2. missingLabel  — non_labeled category, needs term-1/post-1 renaming
 *   3. blockingOpen  — blocking_issue_count > 0 AND not yet reviewed
 *   4. unboundCustomer — customer_raw exists but customer_id is null (字典待规整)
 *   5. pendingReview — review_status = pending (someone started, needs to finish)
 *
 * Lane predicates live server-side in backend/routes/work_queue.py
 * (GET /api/work-queue/summary) — one lightweight aggregation instead of
 * shipping the full 2000-row case list to the client. Lanes with batch
 * actions (todayNew / pendingReview) also carry case_ids in id-DESC order,
 * matching the list endpoint so slice(0, MAX_BATCH) behaves as before.
 *
 * Each lane returns: count, route (with filter URL params), label, description.
 */
import type { CaseSummary, WorkQueueLaneKey, WorkQueueSummary } from "../api";

export type LaneKey = WorkQueueLaneKey;

export interface LaneDef {
  key: LaneKey;
  count: number;
  total: number; // baseline (e.g., total cases for percentage)
  label: string;
  desc: string;
  route: string; // /cases?... — Cases.tsx must handle these params
  tone: "cyan" | "amber" | "err" | "ok" | "ink";
}

/**
 * A case is "held" if held_until is in the future.
 * Held cases are excluded from work-queue lanes — that's the whole point of 挂起.
 */
export function isHeld(c: CaseSummary, now: Date = new Date()): boolean {
  if (!c.held_until) return false;
  const t = new Date(c.held_until);
  return !isNaN(t.getTime()) && t.getTime() > now.getTime();
}

const LANE_META: Array<Omit<LaneDef, "count" | "total">> = [
  {
    key: "todayNew",
    label: "今日新增",
    desc: "目录最后修改时间在今天",
    route: "/cases?since=today",
    tone: "cyan",
  },
  {
    key: "missingLabel",
    label: "缺命名",
    desc: "未识别 术前/术后 命名 · 重命名后可参与出图",
    route: "/cases?category=non_labeled",
    tone: "amber",
  },
  {
    key: "blockingOpen",
    label: "阻塞待处理",
    desc: "存在阻塞码且未审核",
    route: "/cases?blocking=open",
    tone: "err",
  },
  {
    key: "unboundCustomer",
    label: "客户待绑定",
    desc: "原始客户名未匹配到 canonical · 进字典处理",
    route: "/dict",
    tone: "ink",
  },
  {
    key: "pendingReview",
    label: "等审核确认",
    desc: "已分配但未完成 · 续做",
    route: "/cases?review=pending",
    tone: "ok",
  },
];

export function lanesFromSummary(summary: WorkQueueSummary | undefined): LaneDef[] {
  return LANE_META.map((meta) => ({
    ...meta,
    count: summary?.lanes[meta.key]?.count ?? 0,
    total: summary?.total ?? 0,
  }));
}

/**
 * Persistence key for "continue where you left off".
 * Updated by CaseDetail.tsx whenever a case is opened.
 */
const LAST_VISITED_KEY = "cw:last_case_id";

export function rememberCaseVisit(id: number) {
  try {
    localStorage.setItem(LAST_VISITED_KEY, String(id));
  } catch {
    /* ignore quota / private mode errors */
  }
}

export function readLastVisitedCase(): number | null {
  try {
    const v = localStorage.getItem(LAST_VISITED_KEY);
    if (!v) return null;
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? n : null;
  } catch {
    return null;
  }
}
