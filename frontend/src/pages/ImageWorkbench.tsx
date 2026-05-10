import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import {
  type ImageWorkbenchAngleSortGroup,
  type ImageWorkbenchBatchGroup,
  type ImageWorkbenchBatchPayload,
  type ImageWorkbenchCaseGroup,
  type ImageWorkbenchItem,
  type SourceGroupResponse,
  type SourceBindingCandidate,
} from "../api";
import {
  useBatchUpdateImageWorkbench,
  useBindSourceDirectories,
  useCaseSourceGroup,
  useConfirmImageWorkbenchSuggestions,
  useImageWorkbenchQueue,
  useSourceBindingCandidates,
  useTransferImageWorkbenchImages,
} from "../hooks/queries";
import { Ico } from "../components/atoms";

type IWT = TFunction<"imageWorkbench">;

type RenderView = "front" | "oblique" | "side";
const RENDER_VIEWS = new Set(["front", "oblique", "side"]);

function stateLabel(t: IWT, key: string) {
  return t(`stateLabels.${key}` as never, { defaultValue: key }) as string;
}
function phaseLabel(t: IWT, key: string) {
  return t(`phaseLabels.${key}` as never, { defaultValue: key }) as string;
}
function viewLabel(t: IWT, key: string) {
  return t(`viewLabels.${key}` as never, { defaultValue: key }) as string;
}
function bodyLabel(t: IWT, key: string) {
  return t(`bodyLabels.${key}` as never, { defaultValue: key }) as string;
}

function keyOf(item: Pick<ImageWorkbenchItem, "case_id" | "filename">) {
  return `${item.case_id}:${item.filename}`;
}

function scoreColor(score: number) {
  if (score >= 0.8) return "var(--ok)";
  if (score >= 0.65) return "var(--amber-ink)";
  return "var(--err)";
}

function stateClass(state: string) {
  if (state === "needs_manual" || state === "needs_repick" || state === "copied_review") return "block";
  if (state === "low_confidence") return "warn";
  if (state === "render_excluded" || state === "deferred") return "info";
  return "ok";
}

function taskLabel(t: IWT, task: string) {
  const value = t(`taskLabels.${task}` as never, { defaultValue: "" }) as string;
  return value || task;
}

function blockerClass(level: string | undefined) {
  if (level === "block") return "block";
  if (level === "review") return "warn";
  return "ok";
}

function compactReason(t: IWT, reason: string) {
  const lower = reason.toLowerCase();
  if (lower.includes("view_missing") || lower.includes("missing_view")) return t("compactReason.missingView");
  if (lower.includes("phase_missing") || lower.includes("missing_phase")) return t("compactReason.missingPhase");
  if (lower.includes("low_confidence") || reason.includes("低置信")) return t("compactReason.lowConfidence");
  if (lower.includes("path_or_filename_phase_token")) return t("compactReason.filenamePhase");
  if (lower.includes("path_or_filename_body_part_token")) return t("compactReason.pathBodyPart");
  if (lower.includes("body_part_missing")) return t("compactReason.missingBodyPart");
  if (reason.includes("待补充")) return t("compactReason.needsManual");
  return reason;
}

function sourceGroupStatusLabel(t: IWT, status: string) {
  const mapped = t(`sourceGroupStatus.${status}` as never, { defaultValue: "" }) as string;
  return mapped || status;
}

function asRenderView(value: unknown): RenderView | null {
  const text = String(value || "");
  return RENDER_VIEWS.has(text) ? (text as RenderView) : null;
}

function percentLabel(value: number | null | undefined) {
  if (value == null || Number.isNaN(value)) return "—";
  return `${Math.round(value * 100)}%`;
}

function missingRoleLabel(t: IWT, role: string) {
  const v = t(`missingRole.${role}` as never, { defaultValue: "" }) as string;
  return v || role;
}

function safeReasonLabel(t: IWT, reason: string | undefined) {
  if (!reason) return t("safeReason.fallback");
  const v = t(`safeReason.${reason}` as never, { defaultValue: "" }) as string;
  return v || reason;
}

function processingModeLabel(t: IWT, mode: string | undefined) {
  if (mode === "source_fix") return t("processingMode.source_fix");
  if (mode === "classify_or_bind") return t("processingMode.classify_or_bind");
  return t("processingMode.default");
}

function processingModeClass(mode: string | undefined) {
  if (mode === "source_fix") return "block";
  if (mode === "classify_or_bind") return "warn";
  return "ok";
}

function sourceReasonLabel(t: IWT, reason: string | null | undefined) {
  if (!reason) return t("sourceReason.default");
  const v = t(`sourceReason.${reason}` as never, { defaultValue: "" }) as string;
  return v || reason;
}

function formatCountMap(
  counts: Record<string, number> | undefined,
  resolveLabel: (key: string) => string,
) {
  if (!counts) return "—";
  const pairs = Object.entries(counts)
    .filter(([, count]) => count > 0)
    .map(([key, count]) => `${resolveLabel(key)} ${count}`);
  return pairs.length ? pairs.join(" / ") : "—";
}

function appendHash(path: string, hash: string) {
  if (!path) return "";
  return path.includes("#") ? path : `${path}${hash}`;
}

function selectedPatchFromControls(
  batchPhase: string,
  batchView: string,
  batchBody: string,
  batchVerdict: string,
  treatmentArea: string,
) {
  const payload: Omit<ImageWorkbenchBatchPayload, "items" | "reviewer"> = {};
  if (batchPhase) payload.manual_phase = batchPhase as ImageWorkbenchBatchPayload["manual_phase"];
  if (batchView) payload.manual_view = batchView as ImageWorkbenchBatchPayload["manual_view"];
  if (batchBody) payload.body_part = batchBody as ImageWorkbenchBatchPayload["body_part"];
  if (batchVerdict) payload.verdict = batchVerdict as ImageWorkbenchBatchPayload["verdict"];
  if (treatmentArea.trim()) payload.treatment_area = treatmentArea.trim();
  return payload;
}

function previewValue(current: string, next: string | null | undefined, allowed: Set<string>) {
  if (!next) return current;
  if (next === "clear") return "unknown";
  return allowed.has(next) ? next : current;
}

function buildBatchPreflight(
  t: IWT,
  selectedItems: ImageWorkbenchItem[],
  visibleItems: ImageWorkbenchItem[],
  activeCaseId: number | undefined,
  patch: Omit<ImageWorkbenchBatchPayload, "items" | "reviewer">,
) {
  const selectedKeys = new Set(selectedItems.map(keyOf));
  const caseIds = Array.from(new Set(selectedItems.map((item) => item.case_id)));
  const sourceReasonCounts = selectedItems.reduce<Record<string, number>>((acc, item) => {
    const reason = item.case_preflight?.reason || "normal";
    acc[reason] = (acc[reason] || 0) + 1;
    return acc;
  }, {});
  const sourcePhaseHintCounts = selectedItems.reduce<Record<string, number>>((acc, item) => {
    const hint = item.case_preflight?.source_phase_hint;
    if (hint === "before" || hint === "after") {
      acc[hint] = (acc[hint] || 0) + 1;
    }
    return acc;
  }, {});
  const fillsPhase = selectedItems.filter((item) => item.phase !== "before" && item.phase !== "after" && patch.manual_phase && patch.manual_phase !== "clear").length;
  const fillsView = selectedItems.filter((item) => !["front", "oblique", "side"].includes(item.view) && patch.manual_view && patch.manual_view !== "clear").length;
  const confirmsUsability = selectedItems.filter((item) => !item.review_state?.verdict && patch.verdict && patch.verdict !== "reopen").length;
  const clears = selectedItems.filter(
    () => patch.manual_phase === "clear" || patch.manual_view === "clear" || patch.body_part === "clear",
  ).length;
  const hasWrite = Boolean(patch.manual_phase || patch.manual_view || patch.body_part || patch.verdict || patch.treatment_area);
  const projectedSlots = activeCaseId
    ? ["front", "oblique", "side"].map((view) => {
        const before = visibleItems.filter((item) => {
          if (item.case_id !== activeCaseId || item.render_excluded) return false;
          const phase = selectedKeys.has(keyOf(item)) ? previewValue(item.phase, patch.manual_phase, new Set(["before", "after"])) : item.phase;
          const nextView = selectedKeys.has(keyOf(item)) ? previewValue(item.view, patch.manual_view, new Set(["front", "oblique", "side"])) : item.view;
          return phase === "before" && nextView === view;
        }).length;
        const after = visibleItems.filter((item) => {
          if (item.case_id !== activeCaseId || item.render_excluded) return false;
          const phase = selectedKeys.has(keyOf(item)) ? previewValue(item.phase, patch.manual_phase, new Set(["before", "after"])) : item.phase;
          const nextView = selectedKeys.has(keyOf(item)) ? previewValue(item.view, patch.manual_view, new Set(["front", "oblique", "side"])) : item.view;
          return phase === "after" && nextView === view;
        }).length;
        return {
          view,
          label: viewLabel(t, view),
          before,
          after,
          ready: before > 0 && after > 0,
        };
      })
    : [];
  const projectedNeedsManual = activeCaseId
    ? visibleItems.filter((item) => {
        if (item.case_id !== activeCaseId || item.render_excluded) return false;
        const phase = selectedKeys.has(keyOf(item)) ? previewValue(item.phase, patch.manual_phase, new Set(["before", "after"])) : item.phase;
        const view = selectedKeys.has(keyOf(item)) ? previewValue(item.view, patch.manual_view, new Set(["front", "oblique", "side"])) : item.view;
        return !["before", "after"].includes(phase) || !["front", "oblique", "side"].includes(view);
      }).length
    : null;
  return {
    caseIds,
    sourceReasonCounts,
    sourcePhaseHintCounts,
    fillsPhase,
    fillsView,
    confirmsUsability,
    clears,
    hasWrite,
    projectedSlots,
    projectedNeedsManual,
    sourceFixCount: (sourceReasonCounts.no_real_source_photos || 0) + (sourceReasonCounts.missing_source_files || 0) + (sourceReasonCounts.insufficient_source_photos || 0),
    pairMissingCount: sourceReasonCounts.missing_before_after_pair || 0,
  };
}

function angleGroupImages(group: ImageWorkbenchAngleSortGroup) {
  return group.images?.length ? group.images : group.sample_images;
}

function projectAngleGroupImpact(
  t: IWT,
  sourceGroup: SourceGroupResponse | null,
  group: ImageWorkbenchAngleSortGroup,
  nextView: RenderView,
) {
  if (!sourceGroup) return null;
  const targetFiles = new Set(group.filenames);
  const slots = (["front", "oblique", "side"] as const).map((view) => ({ view, label: viewLabel(t, view), before: 0, after: 0, ready: false }));
  let changedViewCount = 0;
  let projectedNeedsManual = 0;
  for (const source of sourceGroup.sources) {
    for (const image of source.images) {
      if (image.render_excluded) continue;
      const isTarget = image.case_id === group.case_id && targetFiles.has(image.filename);
      const phase = image.phase;
      const view = isTarget ? nextView : image.view;
      if (isTarget && image.view !== nextView) changedViewCount += 1;
      if (phase !== "before" && phase !== "after") {
        projectedNeedsManual += 1;
        continue;
      }
      if (view !== "front" && view !== "oblique" && view !== "side") {
        projectedNeedsManual += 1;
        continue;
      }
      const slot = slots.find((item) => item.view === view);
      if (slot) {
        if (phase === "before") slot.before += 1;
        if (phase === "after") slot.after += 1;
      }
    }
  }
  for (const slot of slots) {
    slot.ready = slot.before > 0 && slot.after > 0;
  }
  const missingSlots = slots
    .filter((slot) => !slot.ready)
    .map((slot) => ({
      view: slot.view,
      label: slot.label,
      missing: [
        ...(slot.before > 0 ? [] : ["before"]),
        ...(slot.after > 0 ? [] : ["after"]),
      ],
    }));
  return {
    changedViewCount,
    projectedNeedsManual,
    slots,
    missingSlots,
  };
}

export default function ImageWorkbench() {
  const { t } = useTranslation("imageWorkbench");
  const [searchParams] = useSearchParams();
  const initialCaseId = Number(searchParams.get("case_id") || "");
  const initialSourceGroupCaseId = Number(searchParams.get("source_group_case_id") || "");
  const initialContextCaseId = Number.isInteger(initialCaseId) && initialCaseId > 0
    ? initialCaseId
    : Number.isInteger(initialSourceGroupCaseId) && initialSourceGroupCaseId > 0
      ? initialSourceGroupCaseId
      : 0;
  const initialFiles = searchParams.getAll("file").filter(Boolean);
  const [status, setStatus] = useState(searchParams.get("status") || "review_needed");
  const [phase, setPhase] = useState(searchParams.get("phase") || "all");
  const [view, setView] = useState(searchParams.get("view") || "all");
  const [bodyPart, setBodyPart] = useState(searchParams.get("body_part") || "all");
  const [caseFilter, setCaseFilter] = useState(initialContextCaseId > 0 ? String(initialContextCaseId) : "");
  const [q, setQ] = useState(searchParams.get("q") || "");
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(
      Number.isInteger(initialCaseId) && initialCaseId > 0
        ? initialFiles.map((filename) => `${initialCaseId}:${filename}`)
        : [],
    ),
  );
  const [batchPhase, setBatchPhase] = useState("");
  const [batchView, setBatchView] = useState("");
  const [batchBody, setBatchBody] = useState("");
  const [batchVerdict, setBatchVerdict] = useState("");
  const [treatmentArea, setTreatmentArea] = useState("");
  const [transferTargetCaseId, setTransferTargetCaseId] = useState("");
  const [transferNote, setTransferNote] = useState("");
  const [message, setMessage] = useState("");
  const [angleReview, setAngleReview] = useState<{
    group: ImageWorkbenchAngleSortGroup;
    nextView: RenderView | null;
  } | null>(null);
  const numericCaseFilter = Number(caseFilter);
  const activeCaseId = Number.isInteger(numericCaseFilter) && numericCaseFilter > 0 ? numericCaseFilter : undefined;
  const routeSourceGroupCaseId = Number.isInteger(initialSourceGroupCaseId) && initialSourceGroupCaseId > 0
    ? initialSourceGroupCaseId
    : undefined;
  const activeSourceGroupCaseId = routeSourceGroupCaseId && activeCaseId === routeSourceGroupCaseId
    ? routeSourceGroupCaseId
    : undefined;
  const focus = searchParams.get("focus") || "";
  const returnTo = searchParams.get("return") || (activeCaseId ? `/cases/${activeCaseId}` : "");
  const closureMode = focus === "classification_blockers" && activeCaseId != null;
  const initialFilesKey = initialFiles.join("\u0000");
  const params = {
    status,
    phase,
    view,
    body_part: bodyPart,
    q: q.trim() || undefined,
    case_id: activeCaseId,
    source_group_case_id: activeSourceGroupCaseId,
    limit: activeSourceGroupCaseId ? 240 : 160,
  };
  const queueQ = useImageWorkbenchQueue(params);
  const batchMut = useBatchUpdateImageWorkbench();
  const confirmSuggestionsMut = useConfirmImageWorkbenchSuggestions();
  const transferMut = useTransferImageWorkbenchImages();
  const sourceGroupQ = useCaseSourceGroup(activeCaseId ?? null);
  const sourceBindingQ = useSourceBindingCandidates(activeCaseId ?? null, { limit: 6 });
  const bindSourceMut = useBindSourceDirectories();
	  const items = useMemo(() => queueQ.data?.items ?? [], [queueQ.data?.items]);
	  const caseGroups = useMemo(() => queueQ.data?.case_groups ?? [], [queueQ.data?.case_groups]);
	  const batchGroups = useMemo(() => queueQ.data?.batch_groups ?? [], [queueQ.data?.batch_groups]);
	  const angleSortGroups = useMemo(() => queueQ.data?.angle_sort_groups ?? [], [queueQ.data?.angle_sort_groups]);
	  const taskQueues = useMemo(() => Object.values(queueQ.data?.task_queues ?? {}), [queueQ.data?.task_queues]);
	  const productionSummary = queueQ.data?.production_summary;
  const initialFileSet = useMemo(() => new Set(initialFilesKey ? initialFilesKey.split("\u0000") : []), [initialFilesKey]);
  const closureItems = useMemo(
    () => (closureMode && initialFileSet.size > 0 ? items.filter((item) => initialFileSet.has(item.filename)) : items),
    [closureMode, initialFileSet, items],
  );
  const closureTargetCount = initialFiles.length > 0 ? initialFiles.length : queueQ.data?.total ?? closureItems.length;
  const selectedItems = useMemo(
    () => items.filter((item) => selected.has(keyOf(item))),
    [items, selected],
  );
  const safeSelectedItems = useMemo(
    () => selectedItems.filter((item) => item.safe_confirm?.eligible),
    [selectedItems],
  );
  const batchPatch = useMemo(
    () => selectedPatchFromControls(batchPhase, batchView, batchBody, batchVerdict, treatmentArea),
    [batchPhase, batchView, batchBody, batchVerdict, treatmentArea],
  );
  const batchPreflight = useMemo(
    () => buildBatchPreflight(t, selectedItems, items, activeCaseId, batchPatch),
    [t, selectedItems, items, activeCaseId, batchPatch],
  );
  const allVisibleSelected = items.length > 0 && items.every((item) => selected.has(keyOf(item)));
  const sourceGroup = sourceGroupQ.data ?? null;
  const sourceBindingCandidates = useMemo(() => sourceBindingQ.data?.candidates ?? [], [sourceBindingQ.data?.candidates]);
  const completeSourceBindingCandidates = useMemo(
    () => sourceBindingCandidates.filter((candidate) => candidate.can_complete_pair && !candidate.already_bound),
    [sourceBindingCandidates],
  );
  const sourceGroupMissingSourceCount = sourceGroup?.preflight.missing_source_count ?? sourceGroup?.missing_image_count ?? 0;
  const sourceGroupNeedsManualCount = sourceGroup?.preflight.needs_manual_count ?? 0;
  const sourceGroupMissingSlots = sourceGroup?.preflight.missing_slots ?? [];
  const sourceGroupReady =
    sourceGroup != null &&
    sourceGroup.preflight.status === "ready" &&
    sourceGroupNeedsManualCount === 0 &&
    sourceGroupMissingSourceCount === 0 &&
    sourceGroupMissingSlots.length === 0;
  const returnToPreflight = appendHash(returnTo, "#source-group-preflight");
  const supplementHref = activeCaseId ? `/cases/${activeCaseId}#supplement-candidates` : returnToPreflight;
  const angleReviewImages = angleReview ? angleGroupImages(angleReview.group) : [];
  const angleReviewImpact = angleReview?.nextView
    ? projectAngleGroupImpact(t, sourceGroup, angleReview.group, angleReview.nextView)
    : null;

  const closureNextStep = (nextSourceGroup = sourceGroup) => {
    if (!activeCaseId) return "";
    if (!nextSourceGroup) return t("messages.preflightRefreshing");
    const missingSourceCount = nextSourceGroup.preflight.missing_source_count ?? nextSourceGroup.missing_image_count ?? 0;
    const needsManualCount = nextSourceGroup.preflight.needs_manual_count ?? 0;
    const missingSlotCount = nextSourceGroup.preflight.missing_slots.length;
    if (missingSourceCount > 0) return t("messages.stillMissingSource", { count: missingSourceCount });
    if (needsManualCount > 0) return t("messages.stillNeedsManual", { count: needsManualCount });
    if (missingSlotCount > 0) return t("messages.stillMissingSlots", { count: missingSlotCount });
    if (nextSourceGroup.preflight.status === "ready") return t("messages.sourceGroupReady");
    return t("messages.preflightRefreshed");
  };

  const toggleOne = (item: ImageWorkbenchItem) => {
    const key = keyOf(item);
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const toggleVisible = () => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) {
        items.forEach((item) => next.delete(keyOf(item)));
      } else {
        items.forEach((item) => next.add(keyOf(item)));
      }
      return next;
    });
  };

  const runBatchUpdate = (
    patch: Omit<ImageWorkbenchBatchPayload, "items" | "reviewer">,
    successLabel?: string,
    keepSelection = false,
  ) => {
    if (selectedItems.length === 0) return;
    const label = successLabel ?? t("messages.batchUpdated");
    const payload: ImageWorkbenchBatchPayload = {
      items: selectedItems.map((item) => ({ case_id: item.case_id, filename: item.filename })),
      reviewer: "operator",
      ...patch,
    };
    batchMut.mutate(payload, {
      onSuccess: async (data) => {
        let refreshedSourceGroup = sourceGroup;
        if (activeCaseId) {
          const [sourceResult] = await Promise.allSettled([
            sourceGroupQ.refetch(),
            queueQ.refetch(),
          ]);
          if (sourceResult.status === "fulfilled") {
            refreshedSourceGroup = sourceResult.value.data ?? refreshedSourceGroup;
          }
        }
        const skippedSuffix = data.skipped.length ? t("messages.skippedSuffix", { count: data.skipped.length }) : "";
        const nextStep = activeCaseId
          ? closureNextStep(refreshedSourceGroup)
          : returnTo
            ? t("messages.returnCaseRefresh")
            : "";
        setMessage(t("messages.batchSucceedRow", { label, updated: data.updated, skipped: skippedSuffix, nextStep }));
        if (!keepSelection) setSelected(new Set());
      },
      onError: (err) => {
        setMessage(err instanceof Error ? err.message : t("messages.batchUpdateError"));
      },
    });
  };

  const applyBatch = () => {
    const payload = batchPatch;
    if (!payload.manual_phase && !payload.manual_view && !payload.body_part && !payload.verdict && !payload.treatment_area) {
      setMessage(t("messages.selectAtLeastOne"));
      return;
    }
    if (batchPreflight.sourceFixCount > 0) {
      const ok = window.confirm(t("messages.sourceFixConfirm", { count: batchPreflight.sourceFixCount }));
      if (!ok) return;
    }
    runBatchUpdate(payload, t("messages.batchUpdated"));
  };

  const focusCaseGroup = (group: ImageWorkbenchCaseGroup) => {
    setCaseFilter(String(group.case_id));
    setStatus("review_needed");
    setPhase("all");
    setView("all");
    setBodyPart("all");
    setQ("");
    setSelected(new Set());
    setMessage(t("messages.focusedCase", { caseId: group.case_id, action: group.next_action }));
  };

  const selectBatchGroup = (group: ImageWorkbenchBatchGroup) => {
    setSelected((prev) => {
      const next = new Set(prev);
      group.filenames.forEach((filename) => next.add(`${group.case_id}:${filename}`));
      return next;
    });
    if (!activeCaseId || activeCaseId !== group.case_id) {
      setCaseFilter(String(group.case_id));
      setStatus("review_needed");
      setPhase("all");
      setView("all");
      setBodyPart("all");
      setQ("");
    }
    setMessage(t("messages.selectedBatchGroup", { caseId: group.case_id, bucket: group.filename_bucket, count: group.item_count }));
  };

  const applyGroupSuggestion = (group: ImageWorkbenchBatchGroup) => {
    const patch = group.recommended_patch;
    if (!patch || Object.keys(patch).length === 0) {
      setMessage(t("messages.groupNoSafeSuggestion"));
      return;
    }
    selectBatchGroup(group);
    if (patch.manual_phase) setBatchPhase(String(patch.manual_phase));
    if (patch.manual_view) setBatchView(String(patch.manual_view));
    setMessage(t("messages.groupSuggestionLoaded", { action: group.recommended_action }));
  };

  const selectAngleSortGroup = (group: ImageWorkbenchAngleSortGroup, nextView?: RenderView) => {
    setSelected(new Set(group.filenames.map((filename) => `${group.case_id}:${filename}`)));
    if (!activeCaseId || activeCaseId !== group.case_id) {
      setCaseFilter(String(group.case_id));
      setStatus("review_needed");
      setPhase("all");
      setView("all");
      setBodyPart("all");
      setQ("");
    }
    if (nextView) setBatchView(nextView);
    const prefill = nextView ? t("messages.anglePrefillSuffix", { label: viewLabel(t, nextView) }) : "";
    setMessage(t("messages.selectedAngleGroup", { count: group.item_count, prefill }));
  };

  const openAngleReview = (group: ImageWorkbenchAngleSortGroup, nextView: RenderView | null = null) => {
    selectAngleSortGroup(group, nextView ?? undefined);
    setAngleReview({ group, nextView });
  };

  const saveAngleSortGroup = (group: ImageWorkbenchAngleSortGroup, nextView: RenderView) => {
    const impact = projectAngleGroupImpact(t, sourceGroup, group, nextView);
    const patchPhase = group.recommended_patch?.manual_phase;
    const nextPhase = patchPhase === "before" || patchPhase === "after" ? patchPhase : null;
    const titleLine = t("messages.angleConfirmTitle", { count: group.item_count, label: viewLabel(t, nextView) });
    const phaseLine = nextPhase ? t("messages.anglePhaseHintLine", { label: phaseLabel(t, nextPhase) }) : "";
    const sampleLine = t("messages.angleSampleLine", {
      filenames: group.filenames.slice(0, 6).join("、"),
      ellipsis: group.filenames.length > 6 ? " ..." : "",
    });
    const impactLine = impact
      ? t("messages.angleImpactLine", {
          changed: impact.changedViewCount,
          remaining: impact.projectedNeedsManual,
          slots: impact.missingSlots.length,
        })
      : "";
    const auditLine = t("messages.angleAuditNote");
    const ok = window.confirm(`${titleLine}${phaseLine}${sampleLine}${impactLine}${auditLine}`);
    if (!ok) return;
    const selectedKeys = new Set(group.filenames.map((filename) => `${group.case_id}:${filename}`));
    setSelected(selectedKeys);
    setBatchView(nextView);
    batchMut.mutate(
      {
        items: group.filenames.map((filename) => ({ case_id: group.case_id, filename })),
        manual_view: nextView,
        manual_phase: nextPhase,
        reviewer: "operator-angle-sort",
      },
      {
        onSuccess: async (data) => {
          let refreshedSourceGroup = sourceGroup;
          const [sourceResult] = await Promise.allSettled([
            sourceGroupQ.refetch(),
            sourceBindingQ.refetch(),
            queueQ.refetch(),
          ]);
          if (sourceResult.status === "fulfilled") {
            refreshedSourceGroup = sourceResult.value.data ?? refreshedSourceGroup;
          }
          const skippedSuffix = data.skipped.length ? t("messages.skippedSuffix", { count: data.skipped.length }) : "";
          setMessage(
            t("messages.angleSavedSuccess", {
              count: data.updated,
              label: viewLabel(t, nextView),
              skipped: skippedSuffix,
              nextStep: closureNextStep(refreshedSourceGroup),
            }),
          );
          setAngleReview(null);
        },
        onError: (err) => {
          setMessage(err instanceof Error ? err.message : t("messages.angleSaveError"));
        },
      },
    );
  };

  const bindSourceCandidate = (candidate: SourceBindingCandidate) => {
    if (!activeCaseId) return;
    const preview = candidate.projected_preflight;
    const blockersText =
      preview && preview.hard_blockers.length > 0
        ? preview.hard_blockers.map((blocker) => blocker.code).slice(0, 3).join(" / ")
        : t("messages.bindBlockerNone");
    const previewText = preview
      ? t("messages.bindConfirmPreviewLine", {
          status: sourceGroupStatusLabel(t, preview.status),
          score: preview.readiness_score ?? "—",
          needs: preview.needs_manual_count ?? "—",
          slots: preview.missing_slots.length,
          blockers: blockersText,
        })
      : "";
    const headerLine = t("messages.bindConfirmHeader", {
      caseId: candidate.case_id,
      title: candidate.case_title,
      activeCaseId,
    });
    const pathLine = t("messages.bindConfirmPath", { path: candidate.abs_path });
    const reasonsLine = t("messages.bindConfirmReasons", { reasons: candidate.match_reasons.join(" / ") });
    const footerLine = t("messages.bindConfirmFooter");
    const ok = window.confirm(`${headerLine}${pathLine}${reasonsLine}${previewText}${footerLine}`);
    if (!ok) return;
    bindSourceMut.mutate(
      {
        caseId: activeCaseId,
        sourceCaseIds: [candidate.case_id],
        note: t("messages.bindNote", { reasons: candidate.match_reasons.join(" / ") }),
      },
      {
        onSuccess: async (data) => {
          const [sourceResult] = await Promise.allSettled([
            sourceGroupQ.refetch(),
            sourceBindingQ.refetch(),
            queueQ.refetch(),
          ]);
          const refreshedSourceGroup = sourceResult.status === "fulfilled" ? sourceResult.value.data : sourceGroup;
          setMessage(
            t("messages.bindSuccess", {
              caseId: candidate.case_id,
              ids: data.bound_case_ids.map((caseId) => `#${caseId}`).join("、"),
              nextStep: closureNextStep(refreshedSourceGroup),
            }),
          );
        },
        onError: (err) => {
          setMessage(err instanceof Error ? err.message : t("messages.bindError"));
        },
      },
    );
  };

  const confirmSuggestions = (targets: ImageWorkbenchItem[], label: string) => {
    const safeTargets = targets.filter((item) => item.safe_confirm?.eligible);
    if (safeTargets.length === 0) {
      const reason = targets[0]?.safe_confirm?.reason;
      const suffix = reason ? t("messages.noSafeConfirmReasonSuffix", { reason: safeReasonLabel(t, reason) }) : "";
      setMessage(`${t("messages.noSafeConfirm")}${suffix}`);
      return;
    }
    const skipped = targets.length - safeTargets.length;
    const skipSuffix = skipped > 0 ? t("messages.confirmSafeSkipSuffix", { count: skipped }) : "";
    const ok = window.confirm(t("messages.confirmSafeDialog", { count: safeTargets.length, skipSuffix }));
    if (!ok) return;
    confirmSuggestionsMut.mutate(
      {
        items: safeTargets.map((item) => ({ case_id: item.case_id, filename: item.filename })),
        min_confidence: 0.85,
        reviewer: "operator",
        note: t("messages.confirmSafeNote", { label }),
        mark_usable: true,
      },
      {
        onSuccess: async (data) => {
          if (closureMode) {
            await Promise.allSettled([sourceGroupQ.refetch(), queueQ.refetch()]);
          }
          const skippedSuffix = data.skipped.length ? t("messages.skippedSuffix", { count: data.skipped.length }) : "";
          setMessage(t("messages.confirmSafeSuccess", { count: data.updated, skipped: skippedSuffix }));
          setSelected(new Set());
        },
        onError: (err) => {
          setMessage(err instanceof Error ? err.message : t("messages.confirmSafeError"));
        },
      },
    );
  };

  const copyToCase = () => {
    if (selectedItems.length === 0) return;
    const targetCaseId = Number(transferTargetCaseId);
    if (!Number.isInteger(targetCaseId) || targetCaseId <= 0) {
      setMessage(t("messages.invalidTargetCase"));
      return;
    }
    transferMut.mutate(
      {
        target_case_id: targetCaseId,
        mode: "copy",
        inherit_manual: true,
        inherit_review: true,
        reviewer: "operator",
        note: transferNote.trim() || null,
        items: selectedItems.map((item) => ({ case_id: item.case_id, filename: item.filename })),
      },
      {
        onSuccess: (data) => {
          const first = data.items[0]?.target_filename;
          const firstSuffix = first ? t("messages.copyFirstSuffix", { filename: first }) : "";
          const skippedSuffix = data.skipped.length ? t("messages.skippedSuffix", { count: data.skipped.length }) : "";
          setMessage(
            t("messages.copySuccess", {
              count: data.copied,
              targetCaseId: data.target_case_id,
              firstSuffix,
              skipped: skippedSuffix,
            }),
          );
          setSelected(new Set());
        },
        onError: (err) => {
          setMessage(err instanceof Error ? err.message : t("messages.copyError"));
        },
      },
    );
  };

  return (
    <div className={`image-workbench ${closureMode ? "with-assist" : ""}`}>
      <div className="page-header">
        <div>
          <h1 className="page-title">
            {t("title")}
            <span style={{ fontFamily: "var(--mono)", color: "var(--ink-3)", fontSize: 14, fontWeight: 500, marginLeft: 6 }}>
              {queueQ.data?.total ?? 0}
            </span>
          </h1>
          <div className="page-sub">{t("subtitle")}</div>
          {closureMode && (
            <div className="image-workbench-context" data-testid="image-workbench-closure-context">
              <span>{t("header.closureContext", { caseId: activeCaseId })}</span>
              <b>{t("header.closureCount", { count: closureTargetCount })}</b>
              {returnToPreflight && <Link to={returnToPreflight}>{t("header.returnPreflight")}</Link>}
            </div>
          )}
        </div>
        <div className="image-workbench-filters">
          <label className="search image-workbench-case-filter">
            <span>{t("header.caseLabel")}</span>
            <input value={caseFilter} onChange={(e) => setCaseFilter(e.target.value.replace(/[^\d]/g, ""))} placeholder={t("header.casePlaceholder")} inputMode="numeric" />
          </label>
          <label className="select">
            <span>{t("header.queueLabel")}</span>
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              {["review_needed", "missing_phase", "missing_view", "missing_usability", "blocked_case", "needs_manual", "low_confidence", "manual", "identified", "used_in_render", "ready_for_render", "usable", "deferred", "needs_repick", "render_excluded", "all"].map((value) => (
                <option key={value} value={value}>{stateLabel(t, value)}</option>
              ))}
            </select>
          </label>
          <label className="select">
            <span>{t("header.phaseLabel")}</span>
            <select value={phase} onChange={(e) => setPhase(e.target.value)}>
              {["all", "before", "after", "unknown"].map((value) => <option key={value} value={value}>{phaseLabel(t, value)}</option>)}
            </select>
          </label>
          <label className="select">
            <span>{t("header.viewLabel")}</span>
            <select value={view} onChange={(e) => setView(e.target.value)}>
              {["all", "front", "oblique", "side", "unknown"].map((value) => <option key={value} value={value}>{viewLabel(t, value)}</option>)}
            </select>
          </label>
          <label className="select">
            <span>{t("header.bodyLabel")}</span>
            <select value={bodyPart} onChange={(e) => setBodyPart(e.target.value)}>
              {["all", "face", "body", "unknown"].map((value) => <option key={value} value={value}>{bodyLabel(t, value)}</option>)}
            </select>
          </label>
          <label className="search image-workbench-search">
            <Ico name="search" />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("header.searchPlaceholder")} />
          </label>
        </div>
	      </div>

	      {taskQueues.length > 0 && (
	        <section className="image-workbench-case-queue" data-testid="image-workbench-task-queues">
	          <div className="image-workbench-case-queue-head">
	            <div>
	              <b>{t("taskQueues.title")}</b>
	              <span>
	                {productionSummary
	                  ? t("taskQueues.summary", {
	                      review: productionSummary.review_needed_total,
	                      blocking: productionSummary.blocking_image_count,
	                      bulk: productionSummary.bulk_group_count,
	                    })
	                  : t("taskQueues.fallback")}
	              </span>
	            </div>
	            {productionSummary && (
	              <span className="badge">{t("taskQueues.policyBadge", { percent: Math.round(productionSummary.policy.high_confidence_threshold * 100) })}</span>
	            )}
	          </div>
	          <div className="image-workbench-case-queue-grid">
	            {taskQueues
	              .filter((lane) => lane.count > 0 || lane.key === "angle_sort_groups")
	              .slice(0, 7)
	              .map((lane) => (
	                <article key={lane.key} className={`image-workbench-case-queue-card ${lane.blocks_render ? "blocked" : "ready"}`}>
	                  <div className="image-workbench-case-queue-title">
	                    <b>{lane.label}</b>
	                    <span>{lane.count}</span>
	                  </div>
	                  <div className="image-workbench-case-queue-metrics">
	                    <span>{t("taskQueues.imageCount", { count: lane.item_count })}</span>
	                    <span>{lane.blocks_render ? t("taskQueues.blocksRender") : t("taskQueues.auditAssist")}</span>
	                  </div>
	                  <div className="image-workbench-case-queue-action">{lane.recommended_action}</div>
	                  <div className="image-workbench-case-queue-buttons">
	                    {lane.key === "angle_sort_groups" ? (
	                      <button className="btn sm" type="button" onClick={() => setStatus("review_needed")}>{t("taskQueues.viewAngleGroups")}</button>
	                    ) : (
	                      <button className="btn sm" type="button" onClick={() => setStatus(lane.key)}>{t("taskQueues.enterQueue")}</button>
	                    )}
	                  </div>
	                </article>
	              ))}
	          </div>
	        </section>
	      )}

	      {caseGroups.length > 0 && (
	        <section className="image-workbench-case-queue" data-testid="image-workbench-case-queue">
          <div className="image-workbench-case-queue-head">
            <div>
              <b>{t("caseQueue.title")}</b>
              <span>{t("caseQueue.subtitle")}</span>
            </div>
            <button
              className="btn sm"
              type="button"
              onClick={() => confirmSuggestions(items, t("messages.filterScopeCurrent"))}
              disabled={items.every((item) => !item.safe_confirm?.eligible) || confirmSuggestionsMut.isPending}
            >
              <Ico name="check" size={11} />
              {t("caseQueue.confirmCurrent")}
            </button>
          </div>
          <div className="image-workbench-case-queue-grid">
            {caseGroups.slice(0, 8).map((group) => {
              const visibleSafe = items.filter((item) => item.case_id === group.case_id && item.safe_confirm?.eligible);
              return (
                <article key={group.case_id} className={`image-workbench-case-queue-card ${group.preflight_status === "ready" ? "ready" : "blocked"}`}>
                  <div className="image-workbench-case-queue-title">
                    <Link to={`/cases/${group.case_id}`}>#{group.case_id} {group.case_title}</Link>
                    <span>{group.readiness_score}</span>
                  </div>
                  <div className="image-workbench-case-queue-metrics">
                    <span>{t("caseQueue.metrics.current", { count: group.filtered_count })}</span>
                    <span>{t("caseQueue.metrics.needsManual", { count: group.needs_manual_count })}</span>
                    <span>{t("caseQueue.metrics.missingView", { count: group.missing_view_count })}</span>
                    <span>{t("caseQueue.metrics.lowConfidence", { count: group.low_confidence_count })}</span>
                    <span>{t("caseQueue.metrics.safeConfirm", { count: group.safe_confirm_count })}</span>
                    <span>{group.processing_mode_label || processingModeLabel(t, group.processing_mode)}</span>
                  </div>
                  {group.missing_slots.length > 0 && (
                    <div className="image-workbench-case-queue-gaps">
                      {group.missing_slots.slice(0, 3).map((slot) => (
                        <span key={slot.view}>{t("caseQueue.missingSlot", { label: slot.label, missing: slot.missing.map((r) => missingRoleLabel(t, r)).join("/") })}</span>
                      ))}
                    </div>
                  )}
                  <div className="image-workbench-case-queue-action">{group.next_action}</div>
                  <div className="image-workbench-case-queue-buttons">
                    {group.processing_mode === "source_fix" ? (
                      <Link className="btn sm primary" to={group.queue_url}>{t("caseQueue.viewSourceDir")}</Link>
                    ) : (
                      <button className="btn sm" type="button" onClick={() => focusCaseGroup(group)}>
                        {t("caseQueue.processQueue")}
                      </button>
                    )}
                    {group.classification_url && group.processing_mode === "source_fix" && (
                      <Link className="btn sm" to={group.classification_url}>{t("caseQueue.viewImages")}</Link>
                    )}
                    <button
                      className="btn sm"
                      type="button"
                      onClick={() => confirmSuggestions(visibleSafe, `case #${group.case_id}`)}
                      disabled={visibleSafe.length === 0 || confirmSuggestionsMut.isPending}
                      title={group.safe_confirm_count > visibleSafe.length ? t("caseQueue.confirmHighConfidenceLoadedOnly") : undefined}
                    >
                      {t("caseQueue.confirmHighConfidence", { count: visibleSafe.length || group.safe_confirm_count })}
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      )}

      {angleSortGroups.length > 0 && (
        <section className="image-workbench-batch-groups image-workbench-angle-sort" data-testid="image-workbench-angle-sort">
          <div className="image-workbench-batch-groups-head">
            <div>
              <b>{t("angleSort.title")}</b>
              <span>{t("angleSort.subtitle")}</span>
            </div>
            <span className="badge">{t("angleSort.groupCount", { scope: activeSourceGroupCaseId ? t("angleSort.scopeCurrentSourceGroup") : t("angleSort.scopeCurrentCase"), count: angleSortGroups.length })}</span>
          </div>
          <div className="image-workbench-batch-groups-grid">
            {angleSortGroups.map((group) => {
              const suggestedView = asRenderView(group.suggested_view);
              return (
              <article key={group.id} className="image-workbench-batch-group-card image-workbench-angle-sort-card">
                <div className="image-workbench-batch-group-title">
                  <div>
                    <Link to={`/cases/${group.case_id}`}>#{group.case_id} {group.case_title}</Link>
                    <span>{group.composition_summary}</span>
                  </div>
                  <b>{group.item_count}</b>
                </div>
                <div className="image-workbench-batch-group-samples">
                  {group.sample_images.slice(0, 6).map((sample) => (
                    <img key={`${sample.case_id}:${sample.filename}`} src={sample.preview_url} alt={sample.filename} loading="lazy" />
                  ))}
                </div>
                <div className="image-workbench-batch-group-badges">
                  <span className="preflight-state warn">{group.orientation_label}</span>
                  <span className="badge">{group.sequence_range}</span>
                  <span className="badge">{t("angleSort.similarityScore", { score: group.metrics.similarity_score })}</span>
                  <span className="badge">{t("angleSort.brightness", { value: Math.round(group.metrics.brightness_avg * 100) })}</span>
                  <span className="badge">{t("angleSort.edgeDensity", { value: Math.round(group.metrics.edge_density_avg * 100) })}</span>
                  {suggestedView && (
                    <span className="preflight-state ready">
                      {t("angleSort.suggestedView", { label: viewLabel(t, suggestedView), percent: percentLabel(group.suggested_view_confidence) })}
                    </span>
                  )}
                  {group.can_quick_confirm_angle && <span className="badge">{t("angleSort.quickConfirm")}</span>}
                  {group.suggested_phase && group.missing_phase_count ? (
                    <span className="badge">{t("angleSort.addPhase", { label: group.suggested_phase_label ?? phaseLabel(t, group.suggested_phase) })}</span>
                  ) : null}
                </div>
                <div className="image-workbench-batch-group-action">{group.recommended_action}</div>
                <div className="image-workbench-batch-group-meta">
                  <span>{(group.angle_evidence_labels?.length ? group.angle_evidence_labels : group.reason_labels).join(" / ")}</span>
                  <span>{group.filenames.slice(0, 6).join("、")}{group.filenames.length > 6 ? " ..." : ""}</span>
                </div>
                {group.local_angle_votes && (
                  <div className="image-workbench-batch-group-meta">
                    <span>
                      {t("angleSort.localVotes", { front: group.local_angle_votes.front ?? 0, oblique: group.local_angle_votes.oblique ?? 0, side: group.local_angle_votes.side ?? 0 })}
                    </span>
                    <span>{t("angleSort.agreement", { percent: percentLabel(group.suggested_view_agreement) })}</span>
                  </div>
                )}
                <div className="image-workbench-batch-group-buttons">
                  <button className="btn sm primary" type="button" onClick={() => selectAngleSortGroup(group)}>
                    {t("angleSort.selectGroup")}
                  </button>
                  <button className="btn sm" type="button" onClick={() => openAngleReview(group, suggestedView)}>
                    {suggestedView ? t("angleSort.reviewBySuggestion", { label: viewLabel(t, suggestedView) }) : t("angleSort.reviewLargeImage")}
                  </button>
                  <button className="btn sm" type="button" onClick={() => selectAngleSortGroup(group, "front")}>{t("angleSort.presetFront")}</button>
                  <button className="btn sm" type="button" onClick={() => selectAngleSortGroup(group, "oblique")}>{t("angleSort.presetOblique")}</button>
                  <button className="btn sm" type="button" onClick={() => selectAngleSortGroup(group, "side")}>{t("angleSort.presetSide")}</button>
                </div>
                <div className="image-workbench-angle-save-buttons">
                  {suggestedView && group.can_quick_confirm_angle && (
                    <button className="btn sm primary" type="button" onClick={() => saveAngleSortGroup(group, suggestedView)} disabled={batchMut.isPending}>
                      {t("angleSort.confirmAfterReview")}
                    </button>
                  )}
                  <button className="btn sm cyan" type="button" onClick={() => openAngleReview(group, "front")} disabled={batchMut.isPending}>{t("angleSort.reviewSaveFront")}</button>
                  <button className="btn sm cyan" type="button" onClick={() => openAngleReview(group, "oblique")} disabled={batchMut.isPending}>{t("angleSort.reviewSaveOblique")}</button>
                  <button className="btn sm cyan" type="button" onClick={() => openAngleReview(group, "side")} disabled={batchMut.isPending}>{t("angleSort.reviewSaveSide")}</button>
                </div>
              </article>
              );
            })}
          </div>
        </section>
      )}

      {activeCaseId && (sourceBindingQ.isLoading || sourceBindingCandidates.length > 0 || sourceGroup?.effective_source_profile.after_count === 0) && (
        <section className="image-workbench-source-bindings" data-testid="image-workbench-source-bindings">
          <div className="image-workbench-batch-groups-head">
            <div>
              <b>{t("sourceBindings.title")}</b>
              <span>{t("sourceBindings.subtitle")}</span>
            </div>
            <span className="badge">
              {t("sourceBindings.stats", { complete: completeSourceBindingCandidates.length, total: sourceBindingCandidates.length })}
            </span>
          </div>
          {sourceGroup && (
            <div className="image-workbench-source-binding-summary">
              <span>{t("sourceBindings.currentBefore", { count: sourceGroup.effective_source_profile.before_count })}</span>
              <span>{t("sourceBindings.currentAfter", { count: sourceGroup.effective_source_profile.after_count })}</span>
              <span>{t("sourceBindings.boundDirs", { count: sourceGroup.bound_case_ids.length })}</span>
              <span>{t("sourceBindings.needsManual", { count: sourceGroup.preflight.needs_manual_count })}</span>
              <span>{t("sourceBindings.missingSlots", { count: sourceGroup.preflight.missing_slots.length })}</span>
            </div>
          )}
          {sourceBindingQ.isLoading && <div className="empty">{t("sourceBindings.loading")}</div>}
          {!sourceBindingQ.isLoading && sourceBindingCandidates.length === 0 && (
            <div className="image-workbench-source-binding-empty">
              {t("sourceBindings.empty")}
            </div>
          )}
          {sourceBindingCandidates.length > 0 && (
            <div className="image-workbench-source-binding-grid">
              {sourceBindingCandidates.slice(0, 6).map((candidate) => (
                <article key={candidate.case_id} className={`image-workbench-source-binding-card ${candidate.can_complete_pair ? "complete" : ""}`}>
                  <div className="image-workbench-source-binding-title">
                    <div>
                      <Link to={candidate.case_url}>#{candidate.case_id} {candidate.case_title}</Link>
                      <span>{candidate.abs_path}</span>
                    </div>
                    <b>{candidate.score}</b>
                  </div>
                  <div className="image-workbench-source-binding-metrics">
                    <span>{t("sourceBindings.candidateBefore", { count: candidate.source_profile.before_count })}</span>
                    <span>{t("sourceBindings.candidateAfter", { count: candidate.source_profile.after_count })}</span>
                    <span>{t("sourceBindings.mergedBefore", { count: candidate.merged_source_profile.before_count })}</span>
                    <span>{t("sourceBindings.mergedAfter", { count: candidate.merged_source_profile.after_count })}</span>
                    <span>{candidate.can_complete_pair ? t("sourceBindings.complete") : t("sourceBindings.stillReview")}</span>
                  </div>
                  {candidate.projected_preflight && (
                    <div className={`image-workbench-binding-preflight ${candidate.projected_preflight.status === "ready" ? "ready" : "blocked"}`}>
                      <div className="image-workbench-binding-preflight-head">
                        <b>{t("sourceBindings.preflightTitle")}</b>
                        <span>{t("sourceBindings.preflightStatus", { status: sourceGroupStatusLabel(t, candidate.projected_preflight.status), score: candidate.projected_preflight.readiness_score ?? "—" })}</span>
                      </div>
                      <div className="image-workbench-binding-preflight-metrics">
                        <span>{t("sourceBindings.preflightNeedsManual", { count: candidate.projected_preflight.needs_manual_count ?? 0 })}</span>
                        <span>{t("sourceBindings.preflightMissingSlots", { count: candidate.projected_preflight.missing_slots.length })}</span>
                        <span>{t("sourceBindings.preflightSelected", { selected: candidate.projected_preflight.selected_count ?? 0 })}</span>
                        <span>{t("sourceBindings.preflightMissingSource", { count: candidate.projected_preflight.missing_source_count ?? 0 })}</span>
                      </div>
                      <div className="image-workbench-binding-preflight-slots">
                        {candidate.projected_preflight.slots.map((slot) => (
                          <span key={slot.view} className={slot.ready ? "ready" : "blocked"}>
                            {slot.label} {slot.before_count}/{slot.after_count}
                          </span>
                        ))}
                      </div>
                      {candidate.projected_preflight.hard_blockers.length > 0 && (
                        <div className="image-workbench-binding-preflight-blockers">
                          {candidate.projected_preflight.hard_blockers.slice(0, 2).map((blocker) => (
                            <span key={blocker.code}>{blocker.message || blocker.code}</span>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                  <div className="image-workbench-source-binding-reasons">
                    {candidate.match_reasons.join(" / ")}
                  </div>
                  <div className="image-workbench-source-binding-actions">
                    <button
                      type="button"
                      className={`btn sm ${candidate.can_complete_pair ? "primary" : ""}`}
                      onClick={() => bindSourceCandidate(candidate)}
                      disabled={bindSourceMut.isPending || candidate.already_bound}
                    >
                      {candidate.already_bound ? t("sourceBindings.alreadyBound") : t("sourceBindings.bindThisDir")}
                    </button>
                    <Link className="btn sm" to={candidate.case_url}>{t("sourceBindings.viewCandidate")}</Link>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>
      )}

      {batchGroups.length > 0 && (
        <section className="image-workbench-batch-groups" data-testid="image-workbench-batch-groups">
          <div className="image-workbench-batch-groups-head">
            <div>
              <b>{t("batchGroups.title")}</b>
              <span>{t("batchGroups.subtitle")}</span>
            </div>
            <span className="badge">{t("batchGroups.currentPageCount", { count: batchGroups.length })}</span>
          </div>
          <div className="image-workbench-batch-groups-grid">
            {batchGroups.slice(0, 12).map((group) => {
              const sourceFix = group.processing_mode === "source_fix";
              return (
                <article key={group.id} className={`image-workbench-batch-group-card ${sourceFix ? "source-fix" : ""}`}>
                  <div className="image-workbench-batch-group-title">
                    <div>
                      <Link to={`/cases/${group.case_id}`}>#{group.case_id} {group.case_title}</Link>
                      <span>{group.filename_bucket}</span>
                    </div>
                    <b>{group.item_count}</b>
                  </div>
                  <div className="image-workbench-batch-group-samples">
                    {group.sample_images.slice(0, 5).map((sample) => (
                      <img key={`${sample.case_id}:${sample.filename}`} src={sample.preview_url} alt={sample.filename} loading="lazy" />
                    ))}
                  </div>
                  <div className="image-workbench-batch-group-badges">
                    <span className={`preflight-state ${processingModeClass(group.processing_mode)}`}>{group.processing_mode_label || processingModeLabel(t, group.processing_mode)}</span>
                    <span className="badge">{sourceReasonLabel(t, group.source_reason)}</span>
                    {group.source_phase_hint && <span className="badge">{t("batchGroups.directoryPhase", { label: group.source_phase_hint_label || phaseLabel(t, group.source_phase_hint) })}</span>}
                    <span className="badge">{t("batchGroups.missingPhase", { count: group.missing_phase_count })}</span>
                    <span className="badge">{t("batchGroups.missingView", { count: group.missing_view_count })}</span>
                    <span className="badge">{t("batchGroups.lowConfidence", { count: group.low_confidence_count })}</span>
                    <span className="badge">{t("batchGroups.safeConfirm", { count: group.safe_confirm_count })}</span>
                  </div>
                  <div className="image-workbench-batch-group-meta">
                    <span>{t("batchGroups.phaseRow", { value: formatCountMap(group.phase_counts, (k) => phaseLabel(t, k)) })}</span>
                    <span>{t("batchGroups.viewRow", { value: formatCountMap(group.view_counts, (k) => viewLabel(t, k)) })}</span>
                    <span>{t("batchGroups.confidenceRow", { min: Math.round(group.confidence_min * 100), avg: Math.round(group.confidence_avg * 100) })}</span>
                  </div>
                  <div className="image-workbench-batch-group-action">
                    {group.recommended_action}
                  </div>
                  <div className="image-workbench-batch-group-buttons">
                    {sourceFix ? (
                      <Link className="btn sm primary" to={group.source_fix_url}>{t("batchGroups.viewSourceDir")}</Link>
                    ) : (
                      <button className="btn sm primary" type="button" onClick={() => selectBatchGroup(group)}>
                        {t("batchGroups.selectGroup")}
                      </button>
                    )}
                    <button
                      className="btn sm"
                      type="button"
                      onClick={() => applyGroupSuggestion(group)}
                      disabled={!group.recommended_patch}
                      title={!group.recommended_patch ? t("batchGroups.applySuggestionDisabled") : undefined}
                    >
                      {t("batchGroups.applySuggestion")}
                    </button>
                    <Link className="btn sm" to={group.classification_url}>{t("batchGroups.enterCaseQueue")}</Link>
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      )}

      <section className="image-workbench-summary">
        <span className="badge">{t("summary.needsManual", { count: queueQ.data?.summary.needs_manual ?? 0 })}</span>
        <span className="badge">{t("summary.missingPhase", { count: queueQ.data?.summary.missing_phase ?? 0 })}</span>
        <span className="badge">{t("summary.missingView", { count: queueQ.data?.summary.missing_view ?? 0 })}</span>
        <span className="badge">{t("summary.missingUsability", { count: queueQ.data?.summary.missing_usability ?? 0 })}</span>
        <span className="badge">{t("summary.lowConfidence", { count: queueQ.data?.summary.low_confidence ?? 0 })}</span>
        <span className="badge">{t("summary.manual", { count: queueQ.data?.summary.manual ?? 0 })}</span>
        <span className="badge">{t("summary.identified", { count: queueQ.data?.summary.identified ?? 0 })}</span>
        <span className="badge">{t("summary.usedInRender", { count: queueQ.data?.summary.used_in_render ?? 0 })}</span>
        <span className="badge">{t("summary.renderExcluded", { count: queueQ.data?.summary.render_excluded ?? 0 })}</span>
        <span className="badge">{t("summary.needsRepick", { count: queueQ.data?.summary.needs_repick ?? 0 })}</span>
        <span className="badge">{t("summary.copiedReview", { count: queueQ.data?.summary.copied_review ?? 0 })}</span>
        <span className="badge">{t("summary.blockedCase", { count: queueQ.data?.summary.blocked_case ?? 0 })}</span>
      </section>

      {closureMode && (
        <section className="manual-angle-assist" data-testid="manual-angle-assist-panel">
          <div className="manual-angle-assist-head">
            <div className="manual-angle-assist-title">
              <span>{t("manualAngleAssist.title")}</span>
              <b>{t("manualAngleAssist.blockingCount", { count: closureItems.length })}</b>
              <em>{t("manualAngleAssist.hint")}</em>
            </div>
            <div className="manual-angle-assist-actions">
              <button
                className="btn sm"
                onClick={() => runBatchUpdate({ manual_view: "front" }, t("messages.successFront"), true)}
                disabled={selectedItems.length === 0 || batchMut.isPending}
              >
                <Ico name="eye" size={11} />
                {t("manualAngleAssist.btnFront")}
              </button>
              <button
                className="btn sm"
                onClick={() => runBatchUpdate({ manual_view: "oblique" }, t("messages.successOblique"), true)}
                disabled={selectedItems.length === 0 || batchMut.isPending}
              >
                <Ico name="split" size={11} />
                {t("manualAngleAssist.btnOblique")}
              </button>
              <button
                className="btn sm"
                onClick={() => runBatchUpdate({ manual_view: "side" }, t("messages.successSide"), true)}
                disabled={selectedItems.length === 0 || batchMut.isPending}
              >
                <Ico name="arrow-r" size={11} />
                {t("manualAngleAssist.btnSide")}
              </button>
              <button
                className="btn sm primary"
                onClick={() => runBatchUpdate({ verdict: "usable" }, t("messages.successConfirmed"), true)}
                disabled={selectedItems.length === 0 || batchMut.isPending}
              >
                <Ico name="check" size={11} />
                {t("manualAngleAssist.btnUsable")}
              </button>
              <button
                className="btn sm"
                onClick={() => runBatchUpdate({ verdict: "deferred" }, t("messages.successDeferred"), true)}
                disabled={selectedItems.length === 0 || batchMut.isPending}
              >
                <Ico name="flag" size={11} />
                {t("manualAngleAssist.btnDeferred")}
              </button>
              <button
                className="btn sm"
                onClick={() => runBatchUpdate({ verdict: "excluded" }, t("messages.successExcluded"), true)}
                disabled={selectedItems.length === 0 || batchMut.isPending}
              >
                <Ico name="x" size={11} />
                {t("manualAngleAssist.btnExcluded")}
              </button>
              {returnTo && (
                <Link className="btn sm" to={returnToPreflight}>
                  {t("manualAngleAssist.returnPreflight")}
                </Link>
              )}
            </div>
          </div>
          <div className={`closure-preflight-strip ${sourceGroupReady ? "ready" : sourceGroup?.preflight.status === "blocked" ? "blocked" : "review"}`} data-testid="closure-preflight-strip">
            <div className="closure-preflight-main">
              <span>{t("preflightStrip.savedAutoRecheck")}</span>
              <b>
                {sourceGroupQ.isLoading && !sourceGroup
                  ? t("preflightStrip.loading")
                  : sourceGroup
                    ? sourceGroupStatusLabel(t, sourceGroup.preflight.status)
                    : t("preflightStrip.notRead")}
              </b>
              {sourceGroupQ.isFetching && <em>{t("preflightStrip.refreshing")}</em>}
            </div>
            <div className="closure-preflight-metrics">
              <span>{t("preflightStrip.remainingNeedsManual", { count: sourceGroupNeedsManualCount })}</span>
              <span>{t("preflightStrip.missingSlots", { count: sourceGroupMissingSlots.length })}</span>
              <span>{t("preflightStrip.missingSourceFiles", { count: sourceGroupMissingSourceCount })}</span>
              <span>{t("preflightStrip.sourceImages", { count: sourceGroup?.image_count ?? "—" })}</span>
            </div>
            {sourceGroup && (
              <div className="closure-preflight-slots">
                {sourceGroup.preflight.slots.map((slot) => (
                  <span key={slot.view} className={slot.ready ? "ready" : "blocked"}>
                    {slot.label} {slot.before_count}/{slot.after_count}
                  </span>
                ))}
              </div>
            )}
            {sourceGroupMissingSlots.length > 0 && (
              <div className="closure-preflight-gaps">
                {sourceGroupMissingSlots.slice(0, 3).map((slot) => (
                  <span key={slot.view}>{t("preflightStrip.missingSlot", { label: slot.label, missing: slot.missing.map((r) => missingRoleLabel(t, r)).join(" / ") })}</span>
                ))}
                <Link className="btn sm" to={supplementHref}>{t("preflightStrip.goSupplement")}</Link>
              </div>
            )}
            {sourceGroupReady && (
              <div className="closure-preflight-gaps">
                <span>{t("preflightStrip.allReady")}</span>
                <Link className="btn sm primary" to={returnToPreflight}>{t("preflightStrip.returnRender")}</Link>
              </div>
            )}
          </div>
          <div className="manual-angle-assist-grid">
            {closureItems.map((item) => {
              const isSelected = selected.has(keyOf(item));
              const reasons = item.reasons.map((r) => compactReason(t, r));
              return (
                <article key={keyOf(item)} className={`manual-angle-assist-card ${isSelected ? "selected" : ""}`}>
                  <div className="manual-angle-assist-preview">
                    <button className="image-workbench-check" onClick={() => toggleOne(item)} aria-label={t("manualAngleAssist.selectAria")}>
                      {isSelected ? <Ico name="check" size={12} /> : null}
                    </button>
                    <a href={item.preview_url} target="_blank" rel="noreferrer" title={t("manualAngleAssist.openPreviewTitle")}>
                      <img src={item.preview_url} alt={item.filename} loading="lazy" />
                    </a>
                  </div>
                  <div className="manual-angle-assist-info">
                    <div className="manual-angle-assist-name" title={item.filename}>{item.filename}</div>
                    <div className="manual-angle-assist-badges">
                      <span className={`preflight-state ${stateClass(item.queue_state)}`}>{stateLabel(t, item.queue_state)}</span>
                      <span className={`preflight-state ${blockerClass(item.blocker_level)}`}>{item.blocker_level === "block" ? t("blockerLevel.block") : item.blocker_level === "review" ? t("blockerLevel.review") : t("blockerLevel.candidate")}</span>
                      <span className="badge">{phaseLabel(t, item.phase)}</span>
                      <span className="badge">{viewLabel(t, item.view)}</span>
                      <span className="badge">{bodyLabel(t, item.body_part)}</span>
                      {item.used_in_render && <span className="badge">{t("manualAngleAssist.alreadyRendered")}</span>}
                    </div>
                    <dl className="manual-angle-assist-meta">
                      <div>
                        <dt>{t("manualAngleAssist.metaConfidence")}</dt>
                        <dd style={{ color: scoreColor(item.confidence) }}>{Math.round(item.confidence * 100)}</dd>
                      </div>
                      <div>
                        <dt>{t("manualAngleAssist.metaSource")}</dt>
                        <dd>{item.phase_source}/{item.view_source}</dd>
                      </div>
                      <div>
                        <dt>{t("manualAngleAssist.metaTreatment")}</dt>
                        <dd>{item.treatment_area || t("manualAngleAssist.treatmentFallback")}</dd>
                      </div>
                    </dl>
                    <div className="manual-angle-assist-reasons" title={item.reasons.join(" / ")}>
                      {reasons.slice(0, 4).join(" / ") || t("manualAngleAssist.reasonFallback")}
                    </div>
                    {item.recommended_actions && item.recommended_actions.length > 0 && (
                      <div className="manual-angle-assist-reasons" title={item.recommended_actions.map((action) => action.label).join(" / ")}>
                        {t("manualAngleAssist.nextStep", { value: item.recommended_actions.slice(0, 2).map((action) => action.label).join(" / ") })}
                      </div>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      )}

      {selectedItems.length > 0 && (
        <section className={`image-workbench-batch-preflight ${batchPreflight.sourceFixCount > 0 ? "source-fix" : ""}`} data-testid="image-workbench-batch-preflight">
          <div className="image-workbench-batch-preflight-head">
            <div>
              <b>{t("batchPreflight.title")}</b>
              <span>
                {t("batchPreflight.summary", {
                  selected: selectedItems.length,
                  cases: batchPreflight.caseIds.length,
                  noWrite: !batchPreflight.hasWrite ? t("batchPreflight.noWriteSuffix") : "",
                })}
              </span>
            </div>
            {activeCaseId && (
              <Link className="btn sm" to={`/cases/${activeCaseId}#source-group-preflight`}>{t("batchPreflight.returnCasePreflight")}</Link>
            )}
          </div>
          <div className="image-workbench-batch-preflight-metrics">
            <span>{t("batchPreflight.fillsPhase", { count: batchPreflight.fillsPhase })}</span>
            <span>{t("batchPreflight.fillsView", { count: batchPreflight.fillsView })}</span>
            <span>{t("batchPreflight.confirmsUsability", { count: batchPreflight.confirmsUsability })}</span>
            <span>{t("batchPreflight.clears", { count: batchPreflight.clears })}</span>
            <span>{t("batchPreflight.pairMissing", { count: batchPreflight.pairMissingCount })}</span>
            <span>{t("batchPreflight.sourceFix", { count: batchPreflight.sourceFixCount })}</span>
          </div>
          {Object.keys(batchPreflight.sourceReasonCounts).length > 0 && (
            <div className="image-workbench-batch-preflight-reasons">
              {Object.entries(batchPreflight.sourceReasonCounts).map(([reason, count]) => (
                <span key={reason}>{sourceReasonLabel(t, reason === "normal" ? null : reason)} {count}</span>
              ))}
            </div>
          )}
          {Object.keys(batchPreflight.sourcePhaseHintCounts).length > 0 && (
            <div className="image-workbench-batch-preflight-reasons">
              {Object.entries(batchPreflight.sourcePhaseHintCounts).map(([hint, count]) => (
                <span key={hint}>{t("batchPreflight.directoryPhaseHint", { label: phaseLabel(t, hint), count })}</span>
              ))}
            </div>
          )}
          {batchPreflight.projectedSlots.length > 0 && (
            <div className="image-workbench-batch-preflight-slots">
              {batchPreflight.projectedSlots.map((slot) => (
                <span key={slot.view} className={slot.ready ? "ready" : "blocked"}>
                  {slot.label} {slot.before}/{slot.after}
                </span>
              ))}
              <span className={batchPreflight.projectedNeedsManual === 0 ? "ready" : "blocked"}>
                {t("batchPreflight.estimatedRemaining", { count: batchPreflight.projectedNeedsManual })}
              </span>
            </div>
          )}
          {batchPreflight.sourceFixCount > 0 && (
            <div className="image-workbench-batch-preflight-note">
              {t("batchPreflight.sourceFixNote")}
            </div>
          )}
          {!batchPreflight.hasWrite && (
            <div className="image-workbench-batch-preflight-note">{t("batchPreflight.noWriteNote")}</div>
          )}
        </section>
      )}

      <section className="image-workbench-bulk">
        <button className="btn sm" onClick={toggleVisible} disabled={items.length === 0}>
          <Ico name={allVisibleSelected ? "x" : "check"} size={11} />
          {allVisibleSelected ? t("bulk.deselectVisible") : t("bulk.selectVisible")}
        </button>
        <span className="badge">{t("bulk.selectedCount", { count: selectedItems.length })}</span>
        <select value={batchPhase} onChange={(e) => setBatchPhase(e.target.value)}>
          <option value="">{t("bulk.phasePlaceholder")}</option>
          <option value="before">{t("phaseLabels.before")}</option>
          <option value="after">{t("phaseLabels.after")}</option>
          <option value="clear">{t("bulk.phaseClear")}</option>
        </select>
        <select value={batchView} onChange={(e) => setBatchView(e.target.value)}>
          <option value="">{t("bulk.viewPlaceholder")}</option>
          <option value="front">{t("viewLabels.front")}</option>
          <option value="oblique">{t("viewLabels.oblique")}</option>
          <option value="side">{t("viewLabels.side")}</option>
          <option value="clear">{t("bulk.viewClear")}</option>
        </select>
        <select value={batchBody} onChange={(e) => setBatchBody(e.target.value)}>
          <option value="">{t("bulk.bodyPlaceholder")}</option>
          <option value="face">{t("bodyLabels.face")}</option>
          <option value="body">{t("bodyLabels.body")}</option>
          <option value="unknown">{t("bulk.bodyUnknown")}</option>
          <option value="clear">{t("bulk.bodyClear")}</option>
        </select>
        <input value={treatmentArea} onChange={(e) => setTreatmentArea(e.target.value)} placeholder={t("bulk.treatmentPlaceholder")} />
        <select value={batchVerdict} onChange={(e) => setBatchVerdict(e.target.value)}>
          <option value="">{t("bulk.verdictPlaceholder")}</option>
          <option value="usable">{t("bulk.verdictUsable")}</option>
          <option value="deferred">{t("bulk.verdictDeferred")}</option>
          <option value="needs_repick">{t("bulk.verdictNeedsRepick")}</option>
          <option value="excluded">{t("bulk.verdictExcluded")}</option>
          <option value="reopen">{t("bulk.verdictReopen")}</option>
        </select>
        <button className="btn sm primary" onClick={applyBatch} disabled={selectedItems.length === 0 || batchMut.isPending}>
          <Ico name="check" size={11} />
          {batchMut.isPending ? t("bulk.applyProcessing") : closureMode ? t("bulk.applySaveRecheck") : t("bulk.apply")}
        </button>
        <button
          className="btn sm"
          onClick={() => confirmSuggestions(selectedItems, t("messages.selectedScope"))}
          disabled={selectedItems.length === 0 || safeSelectedItems.length === 0 || confirmSuggestionsMut.isPending}
          title={safeSelectedItems.length === 0 && selectedItems.length > 0 ? t("bulk.confirmHighConfidenceTitle") : undefined}
        >
          <Ico name="check" size={11} />
          {t("bulk.confirmHighConfidence", { count: safeSelectedItems.length || "" })}
        </button>
        <span className="image-workbench-divider" />
        <input
          value={transferTargetCaseId}
          onChange={(e) => setTransferTargetCaseId(e.target.value)}
          inputMode="numeric"
          placeholder={t("bulk.transferTargetPlaceholder")}
          style={{ width: 118 }}
        />
        <input
          value={transferNote}
          onChange={(e) => setTransferNote(e.target.value)}
          placeholder={t("bulk.transferNotePlaceholder")}
          style={{ width: 150 }}
        />
        <button className="btn sm" onClick={copyToCase} disabled={selectedItems.length === 0 || transferMut.isPending}>
          <Ico name="copy" size={11} />
          {transferMut.isPending ? t("bulk.copyProcessing") : t("bulk.copySupplement")}
        </button>
        {message && <span className="image-workbench-message">{message}</span>}
      </section>

      <main className="image-workbench-grid">
        {queueQ.isLoading && <div className="empty">{t("grid.loading")}</div>}
        {!queueQ.isLoading && items.length === 0 && <div className="empty">{t("grid.empty")}</div>}
	        {items.map((item) => {
	          const localVisual = item.classification_suggestion?.classification_layers.local_visual as
	            | {
	                decision?: string;
	                confidence_band?: string;
	                view_suggestion?: {
	                  suggested_view?: string | null;
	                  suggested_view_label?: string | null;
	                  confidence?: number | null;
	                  confidence_band?: string | null;
	                };
	              }
	            | undefined;
	          const renderGate = item.classification_suggestion?.render_gate;
	          const localViewSuggestion = localVisual?.view_suggestion;
	          return (
	          <article key={keyOf(item)} className={`image-workbench-card ${selected.has(keyOf(item)) ? "selected" : ""}`}>
	            <button className="image-workbench-check" onClick={() => toggleOne(item)} aria-label={t("grid.selectAria")}>
	              {selected.has(keyOf(item)) ? <Ico name="check" size={12} /> : null}
            </button>
            <div className="image-workbench-thumb">
              <img src={item.preview_url} alt={item.filename} loading="lazy" />
            </div>
            <div className="image-workbench-card-body">
              <div className="image-workbench-card-title" title={item.filename}>{item.filename}</div>
              <div className="image-workbench-badges">
                <span className={`preflight-state ${stateClass(item.queue_state)}`}>{stateLabel(t, item.queue_state)}</span>
                <span className={`preflight-state ${blockerClass(item.blocker_level)}`}>{item.blocker_level === "block" ? t("blockerLevel.block") : item.blocker_level === "review" ? t("blockerLevel.review") : t("blockerLevel.candidate")}</span>
                <span className="badge">{phaseLabel(t, item.phase)}</span>
                <span className="badge">{viewLabel(t, item.view)}</span>
                <span className="badge">{bodyLabel(t, item.body_part)}</span>
                {item.used_in_render && <span className="badge">{t("grid.alreadyRendered")}</span>}
              </div>
              <div className="image-workbench-meta">
                <span style={{ color: scoreColor(item.confidence) }}>{Math.round(item.confidence * 100)}</span>
                <span>{item.phase_source}/{item.view_source}</span>
                {item.treatment_area && <span>{item.treatment_area}</span>}
              </div>
              <div className="image-workbench-reasons">{item.reasons.slice(0, 3).join(" / ") || t("grid.reasonFallback")}</div>
	              <div className="image-workbench-reasons">
	                {(item.task_groups ?? []).slice(0, 3).map((task) => taskLabel(t, task)).join(" / ")}
	                {item.recommended_actions?.[0]?.label ? ` · ${item.recommended_actions[0].label}` : ""}
	              </div>
	              {(localVisual || renderGate) && (
	                <div className="image-workbench-reasons">
	                  {localVisual ? t("grid.localVisual", { decision: localVisual.decision ?? "—", band: localVisual.confidence_band ?? "—" }) : ""}
	                  {localViewSuggestion?.suggested_view ? t("grid.suggestedViewWithLabel", { label: localViewSuggestion.suggested_view_label ?? viewLabel(t, localViewSuggestion.suggested_view), percent: percentLabel(localViewSuggestion.confidence) }) : ""}
	                  {renderGate?.blocks_render ? t("grid.blocksRender") : renderGate ? t("grid.candidate") : ""}
	                </div>
	              )}
	              {item.case_preflight?.reason_label && (
	                <div className="image-workbench-reasons">
	                  {t("grid.casePreflightLine", { label: item.case_preflight.reason_label, action: item.case_preflight.recommended_action })}
                </div>
              )}
              <div className="image-workbench-case">
                <Link to={`/cases/${item.case_id}`}>{item.case_title}</Link>
	              </div>
	            </div>
	          </article>
	          );
	        })}
	      </main>

      {angleReview && (
        <div className="image-workbench-review-modal" role="dialog" aria-modal="true" aria-label={t("angleReview.title")}>
          <div className="image-workbench-review-panel">
            <div className="image-workbench-review-head">
              <div>
                <b>{t("angleReview.title")}</b>
                <span>
                  {t("angleReview.headerInfo", { caseId: angleReview.group.case_id, count: angleReview.group.item_count, summary: angleReview.group.composition_summary })}
                </span>
              </div>
              <button className="btn sm" type="button" onClick={() => setAngleReview(null)}>{t("angleReview.close")}</button>
            </div>
            <div className="image-workbench-review-toolbar">
              <button
                className={`btn sm ${angleReview.nextView === "front" ? "primary" : ""}`}
                type="button"
                onClick={() => {
                  setBatchView("front");
                  setAngleReview({ ...angleReview, nextView: "front" });
                }}
              >
                {t("angleReview.judgeFront")}
              </button>
              <button
                className={`btn sm ${angleReview.nextView === "oblique" ? "primary" : ""}`}
                type="button"
                onClick={() => {
                  setBatchView("oblique");
                  setAngleReview({ ...angleReview, nextView: "oblique" });
                }}
              >
                {t("angleReview.judgeOblique")}
              </button>
              <button
                className={`btn sm ${angleReview.nextView === "side" ? "primary" : ""}`}
                type="button"
                onClick={() => {
                  setBatchView("side");
                  setAngleReview({ ...angleReview, nextView: "side" });
                }}
              >
                {t("angleReview.judgeSide")}
              </button>
              <span>{t("angleReview.reviewHint")}</span>
            </div>
            {(angleReview.group.suggested_view || angleReview.group.local_angle_votes) && (
              <div className="image-workbench-review-impact-note">
                {t("angleReview.localSuggestionPrefix")}
                {angleReview.group.suggested_view
                  ? t("angleReview.localSuggestionDetail", {
                      label: viewLabel(t, angleReview.group.suggested_view),
                      confidence: percentLabel(angleReview.group.suggested_view_confidence),
                      agreement: percentLabel(angleReview.group.suggested_view_agreement),
                    })
                  : t("angleReview.localSuggestionNone")}
                {angleReview.group.local_angle_votes
                  ? t("angleReview.voteSuffix", {
                      front: angleReview.group.local_angle_votes.front ?? 0,
                      oblique: angleReview.group.local_angle_votes.oblique ?? 0,
                      side: angleReview.group.local_angle_votes.side ?? 0,
                    })
                  : ""}
                {angleReview.group.suggested_phase && angleReview.group.missing_phase_count
                  ? t("angleReview.phaseSuggestionSuffix", { label: angleReview.group.suggested_phase_label ?? phaseLabel(t, angleReview.group.suggested_phase) })
                  : ""}
                {angleReview.group.angle_evidence_labels?.length ? t("angleReview.evidenceSuffix", { value: angleReview.group.angle_evidence_labels.join(" / ") }) : ""}
              </div>
            )}
            {angleReviewImpact ? (
              <div className="image-workbench-review-impact" data-testid="angle-review-impact">
                <div className="image-workbench-review-impact-metrics">
                  <span>{t("angleReview.impactGroupViews", { count: angleReviewImpact.changedViewCount })}</span>
                  <span>{t("angleReview.impactProjectedNeedsManual", { count: angleReviewImpact.projectedNeedsManual })}</span>
                  <span>{t("angleReview.impactProjectedMissingSlots", { count: angleReviewImpact.missingSlots.length })}</span>
                </div>
                <div className="image-workbench-review-impact-slots">
                  {angleReviewImpact.slots.map((slot) => (
                    <span key={slot.view} className={slot.ready ? "ready" : "blocked"}>
                      {slot.label} {slot.before}/{slot.after}
                    </span>
                  ))}
                </div>
                {angleReviewImpact.missingSlots.length > 0 && (
                  <div className="image-workbench-review-impact-note">
                    {t("angleReview.impactStillMissing", { value: angleReviewImpact.missingSlots.map((slot) => `${slot.label}${slot.missing.map((r) => missingRoleLabel(t, r)).join("/")}`).join(" / ") })}
                  </div>
                )}
              </div>
            ) : (
              <div className="image-workbench-review-impact-note">
                {t("angleReview.impactPlaceholder")}
              </div>
            )}
            <div className="image-workbench-review-grid">
              {angleReviewImages.map((image) => (
                <a key={`${image.case_id}:${image.filename}`} href={image.preview_url} target="_blank" rel="noreferrer" className="image-workbench-review-image">
                  <img src={image.preview_url} alt={image.filename} loading="lazy" />
                  <span title={image.filename}>{image.filename}</span>
                </a>
              ))}
            </div>
            <div className="image-workbench-review-footer">
              <span>
                {t("angleReview.footerInfo", { filenames: `${angleReview.group.filenames.slice(0, 3).join("、")}${angleReview.group.filenames.length > 3 ? "…" : ""}` })}
              </span>
              <button
                className="btn sm primary"
                type="button"
                disabled={!angleReview.nextView || batchMut.isPending}
                onClick={() => {
                  if (angleReview.nextView) saveAngleSortGroup(angleReview.group, angleReview.nextView);
                }}
              >
                {batchMut.isPending ? t("angleReview.saveProcessing") : angleReview.nextView ? t("angleReview.confirmWriteWithLabel", { label: viewLabel(t, angleReview.nextView) }) : t("angleReview.selectViewFirst")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
