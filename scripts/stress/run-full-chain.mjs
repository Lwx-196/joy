#!/usr/bin/env node
import { spawn, spawnSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const defaultBackend = "http://127.0.0.1:5291";
const defaultFrontend = "http://127.0.0.1:5292";

const args = parseArgs(process.argv.slice(2));
const mode = args.mode || "readonly";
const backendUrl = args.backend || defaultBackend;
const frontendUrl = args.frontend || defaultFrontend;
const backendPort = new URL(backendUrl).port || "5291";
const frontendPort = new URL(frontendUrl).port || "5292";
const runId = args.runId || `stress-${new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14)}-${crypto.randomBytes(3).toString("hex")}`;
const resultRoot = path.resolve(repoRoot, "stress-results", runId);
const outputRoot = path.join(resultRoot, "artifacts");
const dbPath = path.join(resultRoot, "case-workbench-stress.db");
const casesRoot = path.join(resultRoot, "cases");
const screenshotsDir = path.join(resultRoot, "screenshots");
const sourceDb = path.resolve(repoRoot, args.sourceDb || "case-workbench.db");
const selectedLimit = Number(args.caseLimit || 10);
const renderBatchSize = Number(args.renderBatchSize || 1);
const concurrency = Number(args.concurrency || 5);
const timeoutMs = Number(args.timeoutMs || 240000);

const metrics = [];
const failures = [];
const children = [];
let selectedCaseIds = [];
let renderBatchResult = null;
let renderPreviewResult = null;
const codeVersion = gitVersion();

const phaseOrder = {
  readonly: ["readonly"],
  classification: ["classification"],
  supplement: ["supplement"],
  render: ["render"],
  quality: ["quality"],
  "ai-review": ["ai-review"],
  browser: ["browser"],
  full: ["readonly", "classification", "supplement", "render", "quality", "ai-review", "browser"],
}[mode];

if (!phaseOrder) {
  failFast(`unsupported mode: ${mode}`);
}

process.on("SIGINT", () => {
  cleanup();
  process.exit(130);
});
process.on("SIGTERM", () => {
  cleanup();
  process.exit(143);
});

await main();

async function main() {
  await fs.mkdir(resultRoot, { recursive: true });
  await fs.mkdir(outputRoot, { recursive: true });
  await fs.mkdir(screenshotsDir, { recursive: true });
  const prep = await prepareData();
  selectedCaseIds = prep.selected_case_ids || [];
  if (selectedCaseIds.length === 0) failFast("no real case directories were cloned for stress run");

  const usingExisting = await canUseExistingStressService();
  if (!usingExisting) {
    await startServices();
  }
  await preflight();

  for (const phase of phaseOrder) {
    await runPhase(phase);
  }

  await writeReport({ prep });
  cleanup();
  console.log(`stress run complete: ${resultRoot}`);
}

function parseArgs(items) {
  const out = {};
  for (const item of items) {
    if (!item.startsWith("--")) continue;
    const [key, rawValue] = item.slice(2).split("=", 2);
    out[key.replace(/-([a-z])/g, (_, ch) => ch.toUpperCase())] = rawValue ?? "1";
  }
  return out;
}

function failFast(message) {
  throw new Error(message);
}

async function prepareData() {
  const script = path.join(repoRoot, "scripts", "stress", "prepare_data.py");
  const proc = spawnSync("python3", [
    script,
    "--source-db", sourceDb,
    "--dest-db", dbPath,
    "--cases-root", casesRoot,
    "--limit", String(selectedLimit),
    ...(args.cases ? ["--case-ids", args.cases] : []),
  ], { cwd: repoRoot, encoding: "utf-8" });
  if (proc.status !== 0) {
    throw new Error(`prepare_data.py failed:\n${proc.stderr || proc.stdout}`);
  }
  const prep = JSON.parse(proc.stdout.trim());
  await fs.writeFile(path.join(resultRoot, "prepared-data.json"), JSON.stringify(prep, null, 2));
  return prep;
}

async function canUseExistingStressService() {
  if (args.startServices === "1") return false;
  try {
    const resp = await fetch(`${backendUrl}/api/stress/status`, { signal: AbortSignal.timeout(1200) });
    if (!resp.ok) return false;
    const status = await resp.json();
    return Boolean(status?.stress_mode && !status?.db_is_default && status?.output_root);
  } catch {
    return false;
  }
}

async function startServices() {
  if (await portResponds(`${backendUrl}/healthz`)) {
    throw new Error(`${backendUrl} is already running but is not a safe stress service. Stop it or start a stress service manually.`);
  }
  if (await portResponds(frontendUrl)) {
    throw new Error(`${frontendUrl} is already running. Stop it before running the self-starting stress command.`);
  }
  const env = {
    ...process.env,
    PORT: backendPort,
    CASE_WORKBENCH_DB_PATH: dbPath,
    CASE_WORKBENCH_OUTPUT_ROOT: outputRoot,
    CASE_WORKBENCH_STRESS_MODE: "1",
    CASE_WORKBENCH_STRESS_RUN_ID: runId,
    CASE_WORKBENCH_AI_ALLOW_EXTERNAL: args.allowExternalAi === "1" ? "1" : "0",
  };
  children.push(spawn("./start.sh", { cwd: repoRoot, env, stdio: ["ignore", "pipe", "pipe"] }));
  children.push(spawn("npm", ["run", "dev", "--", "--host", "127.0.0.1", "--port", frontendPort], {
    cwd: path.join(repoRoot, "frontend"),
    env: { ...env, VITE_API_PROXY_TARGET: backendUrl },
    stdio: ["ignore", "pipe", "pipe"],
  }));
  for (const child of children) {
    child.stdout?.on("data", (buf) => fs.appendFile(path.join(resultRoot, "services.log"), buf).catch(() => {}));
    child.stderr?.on("data", (buf) => fs.appendFile(path.join(resultRoot, "services.log"), buf).catch(() => {}));
  }
  await waitFor(`${backendUrl}/healthz`, 60000);
  await waitFor(frontendUrl, 60000);
}

async function preflight() {
  const status = await fetchJson(`${backendUrl}/api/stress/status`, { method: "GET" }, { metricName: "preflight.stress-status" });
  const expectedRoot = "/Users/a1234/Desktop/案例生成器/case-workbench";
  if (status.repo_root !== expectedRoot) failFast(`wrong project root: ${status.repo_root}`);
  if (!status.stress_mode) failFast("backend is not running with CASE_WORKBENCH_STRESS_MODE=1");
  if (status.db_is_default) failFast("backend is using the default DB; refusing to stress-test main DB");
  if (!status.output_root) failFast("CASE_WORKBENCH_OUTPUT_ROOT is required");
  if (!String(status.output_root).startsWith(outputRoot)) failFast(`backend output root mismatch: ${status.output_root}`);
  await fs.writeFile(path.join(resultRoot, "baseline.json"), JSON.stringify(status, null, 2));
}

async function runPhase(phase) {
  const started = performance.now();
  try {
    if (phase === "readonly") await phaseReadonly();
    if (phase === "classification") await phaseClassification();
    if (phase === "supplement") await phaseSupplement();
    if (phase === "render") await phaseRender();
    if (phase === "quality") await phaseQuality();
    if (phase === "ai-review") await phaseAiReview();
    if (phase === "browser") await phaseBrowser();
  } catch (error) {
    failures.push({ phase, message: String(error?.message || error), stack: String(error?.stack || "") });
  } finally {
    metrics.push({ phase, kind: "phase", duration_ms: Math.round(performance.now() - started) });
  }
}

async function phaseReadonly() {
  const endpoints = [
    "/api/stress/status",
    "/api/cases?page=1&page_size=20",
    "/api/cases/126",
    "/api/cases/126/render/latest",
    "/api/cases/126/render/jobs?limit=10",
    "/api/image-workbench/queue?status=review_needed&limit=50",
    "/api/render/quality-queue?status=review_required&limit=20",
    "/api/cases/simulation-jobs/quality-queue?status=all&limit=20",
    "/api/cases/quality-report?limit=100",
  ];
  await runConcurrent(
    Array.from({ length: Number(args.readIterations || 3) }, () => endpoints).flat(),
    concurrency,
    (endpoint) => fetchJson(`${backendUrl}${endpoint}`, { method: "GET" }, { metricName: endpoint }),
  );
}

async function phaseClassification() {
  const queue = await fetchJson(`${backendUrl}/api/image-workbench/queue?status=review_needed&limit=10`, { method: "GET" }, { metricName: "classification.queue" });
  const item = queue.items?.find((it) => selectedCaseIds.includes(it.case_id)) || queue.items?.[0];
  if (!item) return;
  await fetchJson(`${backendUrl}/api/image-workbench/batch`, {
    method: "POST",
    body: JSON.stringify({
      items: [{ case_id: item.case_id, filename: item.filename }],
      verdict: "deferred",
      reviewer: "stress-runner",
      note: `stress_run_id=${runId}`,
    }),
  }, { metricName: "classification.batch" });
}

async function phaseSupplement() {
  const targetCase = selectedCaseIds.find((id) => id !== 126) || selectedCaseIds[0];
  const data = await fetchJson(`${backendUrl}/api/image-workbench/supplement-candidates?target_case_id=${targetCase}&limit_per_gap=3`, { method: "GET" }, { metricName: "supplement.candidates" });
  const gap = data.gaps?.find((item) => item.candidates?.length);
  const candidate = gap?.candidates?.[0];
  if (!candidate) return;
  await fetchJson(`${backendUrl}/api/image-workbench/transfer`, {
    method: "POST",
    body: JSON.stringify({
      items: [{ case_id: candidate.case_id, filename: candidate.filename }],
      target_case_id: targetCase,
      mode: "copy",
      inherit_manual: true,
      inherit_review: true,
      require_target_review: true,
      reviewer: "stress-runner",
      note: `stress_run_id=${runId}; gap=${gap.view || ""}/${gap.role || ""}`,
    }),
  }, { metricName: "supplement.transfer" });
}

async function phaseRender() {
  const ids = selectedCaseIds.slice(0, Math.max(1, renderBatchSize));
  const preview = await fetchJson(`${backendUrl}/api/cases/render/batch/preview`, {
    method: "POST",
    body: JSON.stringify({ case_ids: ids, brand: "fumei", template: "tri-compare", semantic_judge: "auto" }),
  }, { metricName: "render.preview" });
  renderPreviewResult = preview;
  await fs.writeFile(path.join(resultRoot, "render-preview.json"), JSON.stringify(preview, null, 2));
  const enqueueIds = preview.valid_case_ids?.length ? preview.valid_case_ids : [];
  if (!enqueueIds.length) {
    renderBatchResult = { batch_id: null, total: 0, counts: {}, jobs: [] };
    await fs.writeFile(path.join(resultRoot, "render-batch.json"), JSON.stringify(renderBatchResult, null, 2));
    return;
  }
  const batch = await fetchJson(`${backendUrl}/api/cases/render/batch`, {
    method: "POST",
    body: JSON.stringify({ case_ids: enqueueIds, brand: "fumei", template: "tri-compare", semantic_judge: "auto" }),
  }, { metricName: "render.enqueue" });
  if (!batch.batch_id) return;
  const deadline = Date.now() + timeoutMs;
  let latest = null;
  while (Date.now() < deadline) {
    latest = await fetchJson(`${backendUrl}/api/render/batches/${batch.batch_id}`, { method: "GET" }, { metricName: "render.batch" });
    const counts = latest.counts || {};
    if (!counts.queued && !counts.running) break;
    await sleep(2000);
  }
  await fs.writeFile(path.join(resultRoot, "render-batch.json"), JSON.stringify(latest, null, 2));
  renderBatchResult = latest;
  const stuck = latest?.counts && (latest.counts.queued || latest.counts.running);
  if (stuck) throw new Error(`render batch stuck: ${JSON.stringify(latest.counts)}`);
  for (const job of latest?.jobs || []) {
    if (job.output_path && ["done", "done_with_issues"].includes(job.status)) {
      await fetchBytes(`${backendUrl}/api/render/jobs/${job.id}/file?kind=output`, { metricName: "render.output-file" });
    }
  }
}

async function phaseQuality() {
  await fetchJson(`${backendUrl}/api/cases/quality-report?limit=300`, { method: "GET" }, { metricName: "quality.report" });
  const queue = await fetchJson(`${backendUrl}/api/render/quality-queue?status=review_required&limit=10`, { method: "GET" }, { metricName: "quality.queue" });
  const item = queue.items?.find((entry) => entry.reviewable);
  if (!item) return;
  await fetchJson(`${backendUrl}/api/render-jobs/${item.job.id}/quality-review`, {
    method: "POST",
    body: JSON.stringify({ verdict: "needs_recheck", reviewer: "stress-runner", note: `stress_run_id=${runId}` }),
  }, { metricName: "quality.review" });
}

async function phaseAiReview() {
  const policy = await fetchJson(`${backendUrl}/api/cases/simulation-jobs/review-policy`, { method: "GET" }, { metricName: "ai.policy" });
  await fetchJson(`${backendUrl}/api/cases/simulation-jobs/review-policy/preview?limit=100`, {
    method: "POST",
    body: JSON.stringify(policy),
  }, { metricName: "ai.policy-preview" });
  const queue = await fetchJson(`${backendUrl}/api/cases/simulation-jobs/quality-queue?status=all&limit=10`, { method: "GET" }, { metricName: "ai.queue" });
  const item = queue.items?.find((entry) => entry.reviewable);
  if (!item) return;
  await fetchJson(`${backendUrl}/api/cases/simulation-jobs/${item.job.id}/review`, {
    method: "POST",
    body: JSON.stringify({ verdict: "needs_recheck", reviewer: "stress-runner", note: `stress_run_id=${runId}` }),
  }, { metricName: "ai.review" });
}

async function phaseBrowser() {
  const proc = spawnSync("npx", ["playwright", "test", "tests/e2e/stress-smoke.spec.ts"], {
    cwd: path.join(repoRoot, "frontend"),
    encoding: "utf-8",
    env: {
      ...process.env,
      PLAYWRIGHT_BASE_URL: frontendUrl,
      API_PROXY_TARGET: backendUrl,
      STRESS_SCREENSHOT_DIR: screenshotsDir,
    },
  });
  await fs.writeFile(path.join(resultRoot, "playwright-stress.log"), `${proc.stdout || ""}\n${proc.stderr || ""}`);
  metrics.push({ phase: "browser", kind: "playwright", status: proc.status ?? 1 });
  if (proc.status !== 0) {
    throw new Error("Playwright stress smoke failed; see playwright-stress.log");
  }
}

async function fetchJson(url, init = {}, opts = {}) {
  const resp = await measuredFetch(url, init, opts);
  return resp.json();
}

async function fetchBytes(url, opts = {}) {
  const resp = await measuredFetch(url, {}, opts);
  return resp.arrayBuffer();
}

async function measuredFetch(url, init = {}, opts = {}) {
  const started = performance.now();
  const metricName = opts.metricName || new URL(url).pathname;
  try {
    const resp = await fetch(url, {
      ...init,
      headers: { "content-type": "application/json", ...(init.headers || {}) },
    });
    const duration = Math.round(performance.now() - started);
    metrics.push({ kind: "request", name: metricName, method: init.method || "GET", status: resp.status, duration_ms: duration });
    if (!resp.ok && !opts.allowFailure) {
      const text = await resp.text();
      failures.push({ phase: metricName, status: resp.status, body: text.slice(0, 1000) });
      throw new Error(`${metricName} returned ${resp.status}: ${text.slice(0, 300)}`);
    }
    return resp;
  } catch (error) {
    metrics.push({ kind: "request", name: metricName, method: init.method || "GET", status: 0, duration_ms: Math.round(performance.now() - started) });
    if (!opts.allowFailure) throw error;
    return null;
  }
}

async function runConcurrent(items, workers, fn) {
  let index = 0;
  const pool = Array.from({ length: Math.max(1, workers) }, async () => {
    while (index < items.length) {
      const current = items[index++];
      await fn(current);
    }
  });
  await Promise.all(pool);
}

async function portResponds(url) {
  try {
    const resp = await fetch(url, { signal: AbortSignal.timeout(1000) });
    return resp.status < 500;
  } catch {
    return false;
  }
}

async function waitFor(url, timeout) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await portResponds(url)) return;
    await sleep(1000);
  }
  throw new Error(`timeout waiting for ${url}`);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function percentile(values, p) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const idx = Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1);
  return sorted[idx];
}

async function writeReport({ prep }) {
  const requestMetrics = metrics.filter((item) => item.kind === "request");
  const byName = new Map();
  for (const item of requestMetrics) {
    if (!byName.has(item.name)) byName.set(item.name, []);
    byName.get(item.name).push(item);
  }
  const requestSummary = {};
  for (const [name, items] of byName.entries()) {
    const durations = items.map((item) => item.duration_ms);
    requestSummary[name] = {
      count: items.length,
      success: items.filter((item) => item.status >= 200 && item.status < 400).length,
      failed: items.filter((item) => item.status >= 400 || item.status === 0).length,
      p50_ms: percentile(durations, 50),
      p95_ms: percentile(durations, 95),
      p99_ms: percentile(durations, 99),
      statuses: items.reduce((acc, item) => ({ ...acc, [item.status]: (acc[item.status] || 0) + 1 }), {}),
    };
  }
  const finalStatus = await fetchJson(`${backendUrl}/api/stress/status`, { method: "GET" }, { metricName: "final.stress-status", allowFailure: true }).catch(() => null);
  const qualityReport = await fetchJson(`${backendUrl}/api/cases/quality-report?limit=300`, { method: "GET" }, { metricName: "final.quality-report", allowFailure: true }).catch(() => null);
  const renderRootCauses = summarizeRenderRootCauses(renderBatchResult, renderPreviewResult);
  const metricsPayload = {
    run_id: runId,
    mode,
    code_version: codeVersion,
    backend_url: backendUrl,
    frontend_url: frontendUrl,
    result_root: resultRoot,
    selected_case_ids: selectedCaseIds,
    prepared_data: prep,
    request_summary: requestSummary,
    metrics,
    render_root_causes: renderRootCauses,
    quality_report: qualityReport,
    final_status: finalStatus,
  };
  await fs.writeFile(path.join(resultRoot, "metrics.json"), JSON.stringify(metricsPayload, null, 2));
  await fs.writeFile(path.join(resultRoot, "failures.json"), JSON.stringify(failures, null, 2));
  const total = requestMetrics.length;
  const failed = requestMetrics.filter((item) => item.status >= 400 || item.status === 0).length + failures.length;
  const summary = [
    `# Stress Run ${runId}`,
    "",
    `- Mode: ${mode}`,
    `- Backend: ${backendUrl}`,
    `- Frontend: ${frontendUrl}`,
    `- Code: ${codeVersion.commit}${codeVersion.dirty ? ` (dirty ${codeVersion.dirty_file_count})` : ""}`,
    `- Result root: ${resultRoot}`,
    `- Selected real cases: ${selectedCaseIds.join(", ")}`,
    `- Requests: ${total}`,
    `- Failed signals: ${failed}`,
    `- Render jobs: ${JSON.stringify(finalStatus?.render_jobs || {})}`,
    `- Simulation jobs: ${JSON.stringify(finalStatus?.simulation_jobs || {})}`,
    `- Output bytes: ${finalStatus?.output_root_size_bytes ?? "unknown"}`,
    `- Render batch: ${JSON.stringify(renderRootCauses.status_counts || {})}`,
    `- Current success rate: ${qualityReport?.render?.current_version_baseline?.renderer_success_rate_excluding_blocked ?? "unknown"}`,
    `- Final-board visible rate: ${qualityReport?.render?.artifact_visibility?.final_board_visible_rate ?? "unknown"}`,
    `- Classification completion rate: ${qualityReport?.classification?.completion_rate ?? "unknown"}`,
    "",
    "## Render Root Causes",
    "",
    ...(renderRootCauses.top_causes || []).map((item) => `- ${item.cause}: count=${item.count}, cases=${item.cases.join(", ")}`),
    (renderRootCauses.top_causes || []).length ? "" : "No render batch issues recorded.",
    "",
    "## Slowest Endpoints",
    "",
    ...Object.entries(requestSummary)
      .sort((a, b) => b[1].p95_ms - a[1].p95_ms)
      .slice(0, 12)
      .map(([name, stat]) => `- ${name}: count=${stat.count}, success=${stat.success}, failed=${stat.failed}, p95=${stat.p95_ms}ms, statuses=${JSON.stringify(stat.statuses)}`),
    "",
    "## Failures",
    "",
    failures.length ? "See `failures.json`." : "No phase-level failures recorded.",
    "",
  ].join("\n");
  await fs.writeFile(path.join(resultRoot, "summary.md"), summary);
}

function gitVersion() {
  const rev = spawnSync("git", ["rev-parse", "--short", "HEAD"], { cwd: repoRoot, encoding: "utf-8" });
  const status = spawnSync("git", ["status", "--short"], { cwd: repoRoot, encoding: "utf-8" });
  const dirtyLines = String(status.stdout || "").split("\n").filter((line) => line.trim());
  return {
    commit: String(rev.stdout || "unknown").trim() || "unknown",
    dirty: dirtyLines.length > 0,
    dirty_file_count: dirtyLines.length,
  };
}

function classifyRenderIssue(text) {
  const value = String(text || "");
  if (/不是案例源照片目录|成品图\/海报集合|没有可用于正式出图的真实源照片|已过滤生成图/.test(value)) return "no_real_source_photos";
  if (/真实源照片不足/.test(value)) return "insufficient_source_photos";
  if (/缺少术前\/术后配对|缺术前\/术后配对|missing_before_after_pair/.test(value)) return "missing_before_after_pair";
  if (/未闭环|待补充|低置信|需换片|补图待确认/.test(value)) return "classification_open";
  if (/正脸检测失败，已使用侧脸检测兜底/.test(value)) return "profile_expected_noise";
  if (/面部检测失败|未检测到面部|正脸检测失败/.test(value)) return "face_detection_review";
  if (/姿态差过大/.test(value)) return "pose_delta_review";
  if (/多个姿态推断候选/.test(value)) return "pose_candidate_noise";
  if (/缺少|未找到|未配齐|没有可渲染的角度槽位/.test(value)) return "missing_pair_or_slot";
  if (/Traceback|Exception|IndentationError|timeout|render failed/i.test(value)) return "render_exception";
  return "other";
}

function causeFromPreviewReason(reason) {
  if (reason === "no_real_source_photos") return "no_real_source_photos";
  if (reason === "insufficient_source_photos") return "insufficient_source_photos";
  if (reason === "missing_before_after_pair") return "missing_before_after_pair";
  if (reason === "case_not_found") return "case_not_found";
  if (reason === "duplicate_in_batch") return "duplicate_in_batch";
  return "other";
}

function summarizeRenderRootCauses(batch, preview) {
  const statusCounts = {};
  const causeCounts = new Map();
  for (const invalid of preview?.invalid || []) {
    const cause = causeFromPreviewReason(invalid.reason);
    const current = causeCounts.get(cause) || { cause, count: 0, cases: new Set(), examples: [] };
    current.count += 1;
    current.cases.add(invalid.case_id);
    if (current.examples.length < 5) {
      current.examples.push({
        case_id: invalid.case_id,
        status: "preview_invalid",
        text: `${invalid.reason}: ${JSON.stringify(invalid.source_profile || {})}`.slice(0, 300),
      });
    }
    causeCounts.set(cause, current);
  }
  for (const job of batch?.jobs || []) {
    statusCounts[job.status] = (statusCounts[job.status] || 0) + 1;
    const texts = [
      ...(job.meta?.blocking_issues || []),
      ...(job.meta?.warnings || []),
      ...(job.quality?.metrics?.blocking_issues || []),
      ...(job.quality?.metrics?.warnings || []),
      job.error_message,
    ].filter(Boolean);
    if (!texts.length && job.status !== "done") {
      texts.push(`status=${job.status}`);
    }
    for (const text of texts) {
      const cause = classifyRenderIssue(text);
      const current = causeCounts.get(cause) || { cause, count: 0, cases: new Set(), examples: [] };
      current.count += 1;
      current.cases.add(job.case_id);
      if (current.examples.length < 5) {
        current.examples.push({ job_id: job.id, case_id: job.case_id, status: job.status, text: String(text).slice(0, 300) });
      }
      causeCounts.set(cause, current);
    }
  }
  const topCauses = [...causeCounts.values()]
    .sort((a, b) => b.count - a.count || a.cause.localeCompare(b.cause))
    .map((item) => ({ ...item, cases: [...item.cases].sort((a, b) => Number(a) - Number(b)) }));
  return { status_counts: statusCounts, top_causes: topCauses };
}

function cleanup() {
  for (const child of children) {
    try {
      child.kill("SIGTERM");
    } catch {
      // already gone
    }
  }
}
