import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  CATEGORY_LABEL,
  TIER_LABEL,
  caseFileUrl,
  resolveCandidates,
  type Category,
  type CandidateResult,
  type CaseUpdatePayload,
  type ManualRenderView,
  type ReviewStatus,
  type SkillImageMetadata,
  type SourceGroupImage,
  type SourceGroupSlot,
  type SupplementCandidate,
  type SupplementGap,
} from "../api";
import {
  useCaseDetail,
  useCaseRename,
  useCaseSourceGroup,
  useAcceptSourceGroupWarning,
  useClearSourceGroupSlotLock,
  useIssueDict,
  useLockSourceGroupSlot,
  useMergeCases,
  useRenderCase,
  useRescanCase,
  useRestoreCaseImage,
  useReviewCaseImage,
  useTrashCaseImage,
  useSupplementCandidates,
  useTransferImageWorkbenchImages,
  useUpdateImageOverride,
  useUpdateCase,
  useUpgradeCase,
} from "../hooks/queries";
import { useBrand } from "../lib/brand-context";
import { rememberCaseVisit } from "../lib/work-queue";
import {
  CategoryPill,
  Ico,
  ReviewPill,
  TierPill,
} from "../components/atoms";
import { EvaluateDialog } from "../components/EvaluateDialog";
import { ImageOverridePopover } from "../components/ImageOverridePopover";
import { ManualRenderPicker, type ManualRenderSeedImage, type ManualRenderSeedRequest } from "../components/ManualRenderPicker";
import { RenderHistoryDrawer } from "../components/RenderHistoryDrawer";
import { RenderStatusCard } from "../components/RenderStatusCard";
import { RevisionsDrawer } from "../components/RevisionsDrawer";
import { useHotkey } from "../hooks/useHotkey";

type SourceRole = "pre" | "post" | "unl";
type SourceViewKey = NonNullable<SkillImageMetadata["view_bucket"]> | "unknown";
type SourceRoleFilter = SourceRole | "all" | "needs" | "manual";
type SourceViewFilter = SourceViewKey | "all";
type SourceGroupFilter = "all" | "needs" | "missing_phase" | "missing_view" | "bound" | "excluded" | "missing_file";
type BulkPhaseAction = "before" | "after" | "clear";
type BulkViewAction = "front" | "oblique" | "side" | "clear";

const SOURCE_VIEW_ORDER: SourceViewKey[] = ["front", "oblique", "side", "unknown"];

export default function CaseDetail() {
  const { t } = useTranslation(["caseDetail", "common", "renderHistory"]);
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const caseId = Number(id);

  const detailQ = useCaseDetail(caseId || null);
  const renameQ = useCaseRename(caseId || null);
  const issueDictQ = useIssueDict();
  const updateMut = useUpdateCase();
  const mergeMut = useMergeCases();
  const rescanMut = useRescanCase();
  const upgradeMut = useUpgradeCase();
  const renderMut = useRenderCase();
  const trashImageMut = useTrashCaseImage();
  const restoreImageMut = useRestoreCaseImage();
  const updateImageOverrideMut = useUpdateImageOverride();
  const reviewImageMut = useReviewCaseImage();
  const transferImageMut = useTransferImageWorkbenchImages();
  const lockSourceGroupSlotMut = useLockSourceGroupSlot();
  const clearSourceGroupSlotLockMut = useClearSourceGroupSlotLock();
  const acceptSourceGroupWarningMut = useAcceptSourceGroupWarning();
  const brand = useBrand();

  const data = detailQ.data ?? null;
  const renameHint = renameQ.data ?? null;
  const issueDict = issueDictQ.data ?? [];
  const merging = mergeMut.isPending;
  const saving = updateMut.isPending;
  const rescanning = rescanMut.isPending;
  const upgrading = upgradeMut.isPending;
  const enqueueingRender = renderMut.isPending;

  const [candidates, setCandidates] = useState<CandidateResult | null>(null);
  const [copied, setCopied] = useState(false);
  const [editing, setEditing] = useState(false);
  const [sourceRoleFilter, setSourceRoleFilter] = useState<SourceRoleFilter>("all");
  const [sourceViewFilter, setSourceViewFilter] = useState<SourceViewFilter>("all");
  const [selectedSourceImages, setSelectedSourceImages] = useState<Set<string>>(() => new Set());
  const [sourceBulkBusy, setSourceBulkBusy] = useState(false);
  const [sourceBulkPhase, setSourceBulkPhase] = useState<BulkPhaseAction | "">("");
  const [sourceBulkView, setSourceBulkView] = useState<BulkViewAction | "">("");
  const [sourceBatchMessage, setSourceBatchMessage] = useState<string | null>(null);
  const [sourceGroupMessage, setSourceGroupMessage] = useState<string | null>(null);
  const [sourceGroupFilter, setSourceGroupFilter] = useState<SourceGroupFilter>("all");
  const [selectedSourceGroupImages, setSelectedSourceGroupImages] = useState<Set<string>>(() => new Set());
  const [sourceGroupBulkBusy, setSourceGroupBulkBusy] = useState(false);
  const [supplementOpen, setSupplementOpen] = useState(false);
  const [supplementMessage, setSupplementMessage] = useState<string | null>(null);
  const [manualSeedRequest, setManualSeedRequest] = useState<ManualRenderSeedRequest | null>(null);
  const manualSeedNonceRef = useRef(0);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [customerOpen, setCustomerOpen] = useState(false);
  const [revisionsOpen, setRevisionsOpen] = useState(false);
  const [evaluateOpen, setEvaluateOpen] = useState(false);
  const [renderHistoryOpen, setRenderHistoryOpen] = useState(false);
  const [trashMessage, setTrashMessage] = useState<{ case_id: number; text: string } | null>(null);
  const [lastTrashed, setLastTrashed] = useState<{
    case_id: number;
    original_filename: string;
    trash_path: string;
  } | null>(null);
  const [hiddenTrashedImagesByCase, setHiddenTrashedImagesByCase] = useState<Record<number, string[]>>({});
  // Stage B: 当前打开的 image override popover (filename + anchor element)
  const [overrideTarget, setOverrideTarget] = useState<
    { filename: string; anchor: HTMLElement } | null
  >(null);
  const supplementQ = useSupplementCandidates(caseId || null, { enabled: supplementOpen, limitPerGap: 6 });
  const sourceGroupQ = useCaseSourceGroup(caseId || null);

  useHotkey("h", () => setRenderHistoryOpen((v) => !v), { ignoreInEditable: true });
  const [draft, setDraft] = useState({
    manual_category: "" as "" | Category,
    manual_template_tier: "",
    notes: "",
    tags: "",
    extra_blocking: [] as string[],
  });

  // Sync draft from server data when it (re)loads.
  // Only resets when caseId changes or when not editing — preserves edits during save.
  useEffect(() => {
    if (!data) return;
    if (editing) return;
    setDraft({
      manual_category: (data.manual_category as Category | null) ?? "",
      manual_template_tier: data.manual_template_tier ?? "",
      notes: data.notes ?? "",
      tags: data.tags.join(", "),
      extra_blocking: data.manual_blocking_codes,
    });
  }, [data, editing]);

  // Remember last visited case id for the dashboard "继续上次审核" affordance.
  useEffect(() => {
    if (caseId > 0) rememberCaseVisit(caseId);
  }, [caseId]);

  // Resolve customer candidates (one-shot, not cached — depends on detail).
  useEffect(() => {
    if (!data) return;
    if (!data.customer_id && data.customer_raw) {
      let cancelled = false;
      resolveCandidates(data.customer_raw).then((res) => {
        if (!cancelled) setCandidates(res);
      });
      return () => {
        cancelled = true;
      };
    }
    setCandidates(null);
  }, [data?.customer_id, data?.customer_raw]);

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* ignore */
    }
  };

  const bindToCustomer = (customerId: number) => {
    mergeMut.mutate({ customerId, caseIds: [caseId] });
  };

  const saveEdits = () => {
    const clear: string[] = [];
    const payload: CaseUpdatePayload = {
      notes: draft.notes,
      tags: draft.tags
        .split(/[,，\n]/)
        .map((s) => s.trim())
        .filter(Boolean),
      manual_blocking_codes: draft.extra_blocking,
    };
    if (draft.manual_category === "") clear.push("manual_category");
    else payload.manual_category = draft.manual_category;
    if (draft.manual_template_tier === "") clear.push("manual_template_tier");
    else payload.manual_template_tier = draft.manual_template_tier;
    if (clear.length) payload.clear_fields = clear;
    updateMut.mutate(
      { id: caseId, payload },
      { onSuccess: () => setEditing(false) }
    );
  };

  const setReview = (status: ReviewStatus) => {
    updateMut.mutate({ id: caseId, payload: { review_status: status } });
  };

  const clearOverrides = () => {
    updateMut.mutate(
      {
        id: caseId,
        payload: { clear_fields: ["manual_category", "manual_template_tier"] },
      },
      { onSuccess: () => setEditing(false) }
    );
  };

  const holdCase = () => {
    const reason = window.prompt(
      t("dialogs.holdPrompt"),
      t("dialogs.holdDefault")
    );
    if (reason == null) return; // cancel
    // 默认挂起到 90 天后；用户可在表里手动改时间。"挂起"语义本就是"我现在不想动它"。
    const until = new Date();
    until.setDate(until.getDate() + 90);
    updateMut.mutate({
      id: caseId,
      payload: {
        held_until: until.toISOString(),
        hold_reason: reason.trim() || t("dialogs.holdDefault"),
      },
    });
  };

  const unholdCase = () => {
    updateMut.mutate({
      id: caseId,
      payload: { clear_fields: ["held_until", "hold_reason"] },
    });
  };

  const toggleExtraBlocking = (code: string) => {
    setDraft((d) => ({
      ...d,
      extra_blocking: d.extra_blocking.includes(code)
        ? d.extra_blocking.filter((c) => c !== code)
        : [...d.extra_blocking, code],
    }));
  };

  const toggleSourceSelection = (filename: string) => {
    setSelectedSourceImages((prev) => {
      const next = new Set(prev);
      if (next.has(filename)) next.delete(filename);
      else next.add(filename);
      return next;
    });
  };

  const moveImageToTrash = async (filename: string) => {
    if (!window.confirm(t("dialogs.trashConfirm"))) return;
    setTrashMessage(null);
    setHiddenTrashedImagesByCase((prev) => ({
      ...prev,
      [caseId]: Array.from(new Set([...(prev[caseId] ?? []), filename])),
    }));
    try {
      const result = await trashImageMut.mutateAsync({ caseId, filename });
      setLastTrashed({
        case_id: caseId,
        original_filename: result.original_filename,
        trash_path: result.trash_path,
      });
      setTrashMessage({ case_id: caseId, text: t("trash.trashed", { name: result.original_filename }) });
    } catch (e) {
      setHiddenTrashedImagesByCase((prev) => {
        const next = new Set(prev[caseId] ?? []);
        next.delete(filename);
        return { ...prev, [caseId]: Array.from(next) };
      });
      setTrashMessage({ case_id: caseId, text: `${t("trash.error")}：${e instanceof Error ? e.message : String(e)}` });
    }
  };

  const restoreLastImage = async () => {
    if (!lastTrashed || lastTrashed.case_id !== caseId) return;
    setTrashMessage(null);
    try {
      const result = await restoreImageMut.mutateAsync({
        caseId,
        trashPath: lastTrashed.trash_path,
      });
      setHiddenTrashedImagesByCase((prev) => {
        const next = new Set(prev[caseId] ?? []);
        next.delete(lastTrashed.original_filename);
        next.delete(result.restored_filename);
        return { ...prev, [caseId]: Array.from(next) };
      });
      setTrashMessage({ case_id: caseId, text: t("trash.restored", { name: result.restored_filename }) });
      setLastTrashed(null);
    } catch (e) {
      setTrashMessage({ case_id: caseId, text: `${t("trash.error")}：${e instanceof Error ? e.message : String(e)}` });
    }
  };

  if (!data) return <div className="empty">{t("common:common.loading")}</div>;

  const segments = data.abs_path.split("/");
  const caseName = segments[segments.length - 1];
  const parents = segments.slice(-3, -1);
  const reviewKey = (data.review_status ?? "unreviewed") as ReviewStatus | "unreviewed";
  const customerLabel = data.customer_canonical ?? data.customer_raw ?? t("customer.unbound");
  const isOverridden = data.manual_category != null || data.manual_template_tier != null;
  // 挂起判定：held_until 在未来即认为挂起。
  const heldUntilDate = data.held_until ? new Date(data.held_until) : null;
  const isHeldNow =
    heldUntilDate != null && !isNaN(heldUntilDate.getTime()) && heldUntilDate.getTime() > Date.now();
  const hiddenTrashedImages = new Set(hiddenTrashedImagesByCase[caseId] ?? []);
  const activeLastTrashed = lastTrashed?.case_id === caseId ? lastTrashed : null;
  const activeTrashMessage = trashMessage?.case_id === caseId ? trashMessage.text : null;

  // Group images. Stage A: prefer skill_image_metadata.phase (skill 已识别
  // before/after/null,比文件名启发式准确，比如「术后即刻10.jpeg」会被识别成
  // after,但旧的正则 /术后|after|post/i 也能匹配 — 真正修的是「文件名没线索
  // 但 skill 通过姿态对比确定 phase」的场景)。fallback 保留文件名启发式以兼容
  // 未升级到 v3 的 case。
  const allImages = (data.meta.image_files ?? []).filter((name) => !hiddenTrashedImages.has(name));
  const skillMetaByFile = new Map<string, SkillImageMetadata>();
  for (const m of data.skill_image_metadata ?? []) {
    if (m.filename) skillMetaByFile.set(m.filename, m);
  }
  const phaseOf = (file: string): SourceRole => {
    const meta = skillMetaByFile.get(file);
    if (meta && meta.phase === "before") return "pre";
    if (meta && meta.phase === "after") return "post";
    if (meta && meta.phase === null) {
      // skill 见过但识别为 null,信任 skill,不再用文件名兜底
      return "unl";
    }
    // 未升级 / skill 未输出该文件:回退文件名
    const lower = file.toLowerCase();
    if (/术前|before|pre/i.test(lower)) return "pre";
    if (/术后|after|post/i.test(lower)) return "post";
    return "unl";
  };
  const groups: { role: SourceRole; label: string; files: string[] }[] = [
    { role: "pre", label: t("images.groupPreOp"), files: [] },
    { role: "post", label: t("images.groupPostOp"), files: [] },
    { role: "unl", label: t("images.groupUnlabeled"), files: [] },
  ];
  for (const f of allImages) {
    const role = phaseOf(f);
    if (role === "pre") groups[0].files.push(f);
    else if (role === "post") groups[1].files.push(f);
    else groups[2].files.push(f);
  }
  const viewLabel = (bucket: SkillImageMetadata["view_bucket"]): string => {
    if (bucket === "front") return t("images.viewFront");
    if (bucket === "oblique") return t("images.viewOblique");
    if (bucket === "side") return t("images.viewSide");
    return "";
  };
  const sourceViewLabel = (bucket: SourceViewKey): string => (
    bucket === "unknown" ? t("images.viewUnknownGroup") : viewLabel(bucket)
  );
  const sourceViewOf = (file: string): SourceViewKey => {
    const meta = skillMetaByFile.get(file);
    if (meta?.view_bucket) return meta.view_bucket;
    if (meta?.angle === "front" || meta?.angle === "oblique" || meta?.angle === "side") {
      return meta.angle;
    }
    if (/(3\/4|34|45|45°|微侧|斜侧|半侧)/i.test(file)) return "oblique";
    if (/(侧面|侧脸)/i.test(file)) return "side";
    if (/(正面|正脸|front)/i.test(file)) return "front";
    return "unknown";
  };
  const isManualSource = (file: string): boolean => {
    const meta = skillMetaByFile.get(file);
    return meta?.phase_override_source === "manual" || meta?.view_override_source === "manual";
  };
  const needsManualSource = (file: string): boolean => phaseOf(file) === "unl" || sourceViewOf(file) === "unknown";
  const filteredImages = allImages.filter((file) => {
    const role = phaseOf(file);
    const sourceView = sourceViewOf(file);
    const roleMatches =
      sourceRoleFilter === "all" ||
      (sourceRoleFilter === "needs" && needsManualSource(file)) ||
      (sourceRoleFilter === "manual" && isManualSource(file)) ||
      role === sourceRoleFilter;
    return (
      roleMatches &&
      (sourceViewFilter === "all" || sourceView === sourceViewFilter)
    );
  });
  const roleFilterItems: { key: SourceRoleFilter; label: string; count: number; tone?: "pre" | "post" | "unl" | "manual" }[] = [
    { key: "all", label: t("images.all"), count: allImages.length },
    { key: "needs", label: t("images.needsManual"), count: allImages.filter(needsManualSource).length, tone: "unl" },
    { key: "manual", label: t("images.manual"), count: allImages.filter(isManualSource).length, tone: "manual" },
    { key: "pre", label: t("images.preOp"), count: groups[0].files.length, tone: "pre" },
    { key: "post", label: t("images.postOp"), count: groups[1].files.length, tone: "post" },
    { key: "unl", label: t("images.unlabeled"), count: groups[2].files.length, tone: "unl" },
  ];
  const viewFilterItems: { key: SourceViewFilter; label: string; count: number }[] = [
    { key: "all", label: t("images.all"), count: allImages.length },
    ...SOURCE_VIEW_ORDER.map((bucket) => ({
      key: bucket,
      label: sourceViewLabel(bucket),
      count: allImages.filter((file) => sourceViewOf(file) === bucket).length,
    })),
  ];
  const selectedSourceList = allImages.filter((name) => selectedSourceImages.has(name));
  const selectedFilteredList = filteredImages.filter((name) => selectedSourceImages.has(name));
  const sourceActionBusy = sourceBulkBusy || sourceGroupBulkBusy || updateImageOverrideMut.isPending || trashImageMut.isPending || reviewImageMut.isPending || transferImageMut.isPending || lockSourceGroupSlotMut.isPending || clearSourceGroupSlotLockMut.isPending || acceptSourceGroupWarningMut.isPending;
  const sourceGroup = sourceGroupQ.data ?? null;
  const sourceGroupImageKey = (image: SourceGroupImage): string => `${image.case_id}::${image.filename}`;
  const sourceGroupImages = sourceGroup
    ? sourceGroup.sources.flatMap((source) =>
        source.images.map((image) => ({
          ...image,
          source_role: source.role,
          source_title: source.case_title,
          source_abs_path: source.abs_path,
        })),
      )
    : [];
  const sourceGroupMissingFiles = sourceGroup
    ? sourceGroup.sources.flatMap((source) =>
        source.missing_image_samples.map((filename) => ({
          case_id: source.case_id,
          source_role: source.role,
          source_title: source.case_title,
          filename,
        })),
      )
    : [];
  const sourceGroupVisibleImages = sourceGroupImages.filter((image) => {
    if (sourceGroupFilter === "needs") return image.needs_manual;
    if (sourceGroupFilter === "missing_phase") return image.phase == null;
    if (sourceGroupFilter === "missing_view") return image.view == null;
    if (sourceGroupFilter === "bound") return image.source_role !== "primary";
    if (sourceGroupFilter === "excluded") return image.render_excluded;
    if (sourceGroupFilter === "missing_file") return false;
    return true;
  });
  const selectedSourceGroupList = sourceGroupImages.filter((image) => selectedSourceGroupImages.has(sourceGroupImageKey(image)));
  const sourceGroupFilterItems: { key: SourceGroupFilter; label: string; count: number }[] = [
    { key: "all", label: "全部", count: sourceGroupImages.length },
    { key: "needs", label: "待补充", count: sourceGroupImages.filter((image) => image.needs_manual).length },
    { key: "missing_phase", label: "缺阶段", count: sourceGroupImages.filter((image) => image.phase == null).length },
    { key: "missing_view", label: "缺角度", count: sourceGroupImages.filter((image) => image.view == null).length },
    { key: "bound", label: "绑定目录", count: sourceGroupImages.filter((image) => image.source_role !== "primary").length },
    { key: "excluded", label: "已排除", count: sourceGroupImages.filter((image) => image.render_excluded).length },
    { key: "missing_file", label: "缺失文件", count: sourceGroup?.missing_image_count ?? 0 },
  ];
  const sourceGroupStatus = sourceGroup?.preflight.status ?? "review";
  const sourceGroupStatusClass =
    sourceGroupStatus === "ready" ? "ok" : sourceGroupStatus === "blocked" ? "block" : "warn";
  const sourceGroupFocusParams = new URLSearchParams(window.location.search);
  const focusedSourceGroupSlot = sourceGroupFocusParams.get("source_group_focus") ?? "";
  const focusedIssueCode = sourceGroupFocusParams.get("issue_code") ?? "";
  const focusedIssueText = sourceGroupFocusParams.get("issue_text") ?? "";
  const isFocusedSlot = (slot: SourceGroupSlot): boolean => focusedSourceGroupSlot === slot.view;
  const warningContainsFromCode = (code: string, text?: string): string => {
    if (text) {
      if (text.includes("方向不一致")) return "方向不一致";
      if (text.includes("姿态差")) return "姿态差";
      if (text.includes("清晰度")) return "清晰度";
      if (text.includes("侧面人脸检测失败")) return "侧面人脸检测失败";
      if (text.includes("构图")) return "构图";
      if (text.includes("兜底")) return "兜底";
    }
    if (code === "direction_mismatch") return "方向不一致";
    if (code === "pose_delta_large") return "姿态差";
    if (code === "sharpness_delta") return "清晰度";
    if (code === "side_face_alignment_fallback") return "侧面人脸检测失败";
    return text || code;
  };
  const sourceKindLabel = (kind?: string | null): string => {
    if (kind === "ready_source") return "源图配齐";
    if (kind === "missing_before_after_pair") return "缺术前/术后配对";
    if (kind === "insufficient_source_photos") return "真实源图不足";
    if (kind === "generated_output_collection") return "成品集合";
    if (kind === "manual_not_case_source_directory") return "素材归档";
    if (kind === "missing_source_files") return "源文件缺失";
    if (kind === "unknown_not_scanned") return "未扫描";
    return kind || "未知";
  };
  const sourceGroupPhaseLabel = (phase: SourceGroupImage["phase"]): string =>
    phase === "before" ? t("images.preOp") : phase === "after" ? t("images.postOp") : t("images.unlabeled");
  const sourceGroupViewLabel = (view: SourceGroupImage["view"]): string =>
    view === "front"
      ? t("images.viewFront")
      : view === "oblique"
        ? t("images.viewOblique")
        : view === "side"
          ? t("images.viewSide")
          : t("images.viewUnknown");
  const pairQualityLabel = (label?: string | null): string => {
    if (label === "strong") return "候选稳";
    if (label === "review") return "需复核";
    if (label === "risky") return "高风险";
    return "未评分";
  };
  const candidateLine = (candidate: NonNullable<SourceGroupSlot["selected_before"]> | null, role: "前" | "后"): string =>
    candidate ? `${role} #${candidate.case_id} ${candidate.filename} · ${candidate.selection_score}` : `${role} 未选`;
  const sourceGroupApplyOverride = (
    image: SourceGroupImage,
    kind: "phase" | "view",
    value: string,
  ) => {
    setSourceGroupMessage(null);
    updateImageOverrideMut.mutate(
      {
        caseId: image.case_id,
        filename: image.filename,
        payload: kind === "phase" ? { manual_phase: value } : { manual_view: value },
      },
      {
        onSuccess: () => {
          setSourceGroupMessage(`已更新 #${image.case_id} / ${image.filename}`);
          void sourceGroupQ.refetch();
          if (image.case_id !== caseId) void detailQ.refetch();
        },
        onError: (err) => {
          setSourceGroupMessage(err instanceof Error ? err.message : "源组分类保存失败");
        },
      },
    );
  };
  const sourceGroupReviewImage = (
    image: SourceGroupImage,
    verdict: "usable" | "deferred" | "needs_repick" | "excluded" | "reopen",
  ) => {
    setSourceGroupMessage(null);
    reviewImageMut.mutate(
      {
        caseId: image.case_id,
        filename: image.filename,
        payload: {
          verdict,
          reviewer: "source-group-workbench",
          note: image.case_id === caseId ? "统一源组整理" : `从 case #${caseId} 统一源组整理`,
          layer: "source_group",
        },
      },
      {
        onSuccess: () => {
          setSourceGroupMessage(`已复核 #${image.case_id} / ${image.filename}`);
          void sourceGroupQ.refetch();
          if (image.case_id !== caseId) void detailQ.refetch();
        },
        onError: (err) => {
          setSourceGroupMessage(err instanceof Error ? err.message : "源组复核保存失败");
        },
      },
    );
  };
  const sourceGroupLockPair = (
    slot: SourceGroupSlot,
    before: NonNullable<SourceGroupSlot["selected_before"]>,
    after: NonNullable<SourceGroupSlot["selected_after"]>,
  ) => {
    setSourceGroupMessage(null);
    lockSourceGroupSlotMut.mutate(
      {
        caseId,
        view: slot.view,
        before: { case_id: before.case_id, filename: before.filename },
        after: { case_id: after.case_id, filename: after.filename },
        reason: `人工从 source-group 锁定 ${slot.label} 配对`,
      },
      {
        onSuccess: () => {
          setSourceGroupMessage(`已锁定 ${slot.label}：${before.filename} / ${after.filename}`);
          void sourceGroupQ.refetch();
        },
        onError: (err) => {
          setSourceGroupMessage(err instanceof Error ? err.message : "锁定配对失败");
        },
      },
    );
  };
  const sourceGroupClearLock = (slot: SourceGroupSlot) => {
    setSourceGroupMessage(null);
    clearSourceGroupSlotLockMut.mutate(
      { caseId, view: slot.view },
      {
        onSuccess: () => {
          setSourceGroupMessage(`已解除 ${slot.label} 锁片`);
          void sourceGroupQ.refetch();
        },
        onError: (err) => {
          setSourceGroupMessage(err instanceof Error ? err.message : "解除锁片失败");
        },
      },
    );
  };
  const sourceGroupAcceptWarning = (slot: SourceGroupSlot, code: string, message?: string) => {
    const contains = warningContainsFromCode(code, message);
    setSourceGroupMessage(null);
    acceptSourceGroupWarningMut.mutate(
      {
        caseId,
        slot: slot.view,
        code,
        jobId: preflight?.latest_render?.job_id ?? null,
        messageContains: contains,
        note: `人工确认 ${slot.label} ${contains} 可接受，保留审计`,
      },
      {
        onSuccess: () => {
          setSourceGroupMessage(`已确认 ${slot.label} ${contains} 为可接受复核`);
          void sourceGroupQ.refetch();
        },
        onError: (err) => {
          setSourceGroupMessage(err instanceof Error ? err.message : "确认可接受 warning 失败");
        },
      },
    );
  };
  const toggleSourceGroupSelection = (image: SourceGroupImage) => {
    const key = sourceGroupImageKey(image);
    setSelectedSourceGroupImages((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };
  const selectVisibleSourceGroupImages = () => {
    setSelectedSourceGroupImages((prev) => {
      const next = new Set(prev);
      for (const image of sourceGroupVisibleImages) next.add(sourceGroupImageKey(image));
      return next;
    });
    setSourceGroupMessage(`已选择 ${sourceGroupVisibleImages.length} 张源组图片`);
  };
  const clearSourceGroupSelection = () => {
    setSelectedSourceGroupImages(new Set());
    setSourceGroupMessage(null);
  };
  const sourceGroupApplyBulkOverride = async (kind: "phase" | "view", value: string) => {
    const targets = selectedSourceGroupList;
    if (!value || targets.length === 0) return;
    setSourceGroupBulkBusy(true);
    setSourceGroupMessage(null);
    try {
      for (const image of targets) {
        await updateImageOverrideMut.mutateAsync({
          caseId: image.case_id,
          filename: image.filename,
          payload: kind === "phase"
            ? { manual_phase: value === "clear" ? "" : value }
            : { manual_view: value === "clear" ? "" : value },
        });
      }
      setSourceGroupMessage(`已批量更新 ${targets.length} 张源组图片`);
      setSelectedSourceGroupImages(new Set());
      await sourceGroupQ.refetch();
      if (targets.some((image) => image.case_id === caseId)) void detailQ.refetch();
    } catch (e) {
      setSourceGroupMessage(`源组批量分类失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSourceGroupBulkBusy(false);
    }
  };
  const sourceGroupReviewSelected = async (
    verdict: "usable" | "deferred" | "needs_repick" | "excluded" | "reopen",
  ) => {
    const targets = selectedSourceGroupList;
    if (targets.length === 0) return;
    setSourceGroupBulkBusy(true);
    setSourceGroupMessage(null);
    try {
      for (const image of targets) {
        await reviewImageMut.mutateAsync({
          caseId: image.case_id,
          filename: image.filename,
          payload: {
            verdict,
            reviewer: "source-group-workbench",
            note: image.case_id === caseId ? "统一源组批量整理" : `从 case #${caseId} 统一源组批量整理`,
            layer: "source_group",
          },
        });
      }
      setSourceGroupMessage(`已批量复核 ${targets.length} 张源组图片`);
      setSelectedSourceGroupImages(new Set());
      await sourceGroupQ.refetch();
      if (targets.some((image) => image.case_id === caseId)) void detailQ.refetch();
    } catch (e) {
      setSourceGroupMessage(`源组批量复核失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSourceGroupBulkBusy(false);
    }
  };
  const preflight = data.classification_preflight;
  const preflightReviewItems = preflight?.classification?.review_items ?? [];
  const preflightItemByFile = new Map(preflightReviewItems.map((item) => [item.filename, item]));
  const preflightBlockingItems = preflightReviewItems.filter((item) => item.severity === "block");
  const preflightReviewOnlyItems = preflightReviewItems.filter(
    (item) => item.severity !== "block" && !["profile_expected", "render_excluded"].includes(item.layer ?? ""),
  );
  const preflightNoiseItems = preflightReviewItems.filter((item) => item.layer === "profile_expected");
  const preflightReviewLayers = preflight?.classification?.review_layers ?? [];
  const preflightLatestLayers = preflight?.latest_render?.warning_layers ?? [];
  const preflightReviewLayerKeys = new Set(preflightReviewLayers.map((layer) => layer.key));
  const preflightLatestUniqueLayers = preflightLatestLayers.filter(
    (layer) => !preflightReviewLayerKeys.has(layer.key),
  );
  const preflightStatus = preflight?.render?.status ?? "ready";
  const sourceGroupGatePending = sourceGroupQ.isLoading && !sourceGroup;
  const sourceGroupMissingSourceCount = sourceGroup?.preflight.missing_source_count ?? sourceGroup?.missing_image_count ?? 0;
  const sourceGroupMissingSlotCount = sourceGroup?.preflight.missing_slots.length ?? 0;
  const sourceGroupNeedsManualCount = sourceGroup?.preflight.needs_manual_count ?? 0;
  const sourceGroupGateBlocked = Boolean(
    sourceGroup &&
      (
        sourceGroup.preflight.status === "blocked" ||
        sourceGroupMissingSourceCount > 0 ||
        sourceGroupMissingSlotCount > 0 ||
        sourceGroupNeedsManualCount > 0
      ),
  );
  const renderGateBlocked = preflightStatus === "blocked" || sourceGroupGatePending || sourceGroupGateBlocked;
  const renderGateTitle = preflightStatus === "blocked"
    ? "仍有待补充/低置信/需换片照片，已阻断正式出图"
    : sourceGroupGatePending
      ? "正在读取源组门禁，完成后才能正式出图"
      : sourceGroupMissingSourceCount > 0
        ? `源组有 ${sourceGroupMissingSourceCount} 个历史图片文件在当前磁盘不可读，已阻断正式出图`
        : sourceGroupMissingSlotCount > 0
          ? `三联槽位仍有 ${sourceGroupMissingSlotCount} 个缺口，已阻断正式出图`
          : sourceGroupNeedsManualCount > 0
            ? `源组还有 ${sourceGroupNeedsManualCount} 张待补阶段/角度，已阻断正式出图`
            : t("buttons.renderTooltip", { brand });
  const preflightLatest = preflight?.latest_render ?? null;
  const preflightAiUsage = preflightLatest?.ai_usage;
  const preflightRenderGaps = preflight?.render?.gaps ?? [];
  const preflightStatusLabel = (status: string): string => {
    if (status === "blocked") return t("preflight.statusBlocked");
    if (status === "review") return t("preflight.statusReview");
    if (status === "ready") return t("preflight.statusReady");
    return status;
  };
  const preflightReasonLabel = (reason: string): string =>
    t(`preflight.reasons.${reason}`, { defaultValue: reason });
  const preflightMissingLabel = (role: string): string =>
    role === "before" ? t("images.preOp") : role === "after" ? t("images.postOp") : role;
  const preflightPhaseLabel = (phase: "before" | "after" | null): string =>
    phase === "before" ? t("images.preOp") : phase === "after" ? t("images.postOp") : t("images.unlabeled");
  const preflightViewLabel = (view: "front" | "oblique" | "side" | null): string =>
    view === "front" ? t("images.viewFront") : view === "oblique" ? t("images.viewOblique") : view === "side" ? t("images.viewSide") : t("images.viewUnknownGroup");
  const useSourceGroupClassificationQueue = Boolean(sourceGroup);
  const sourceGroupManualSamples = sourceGroup?.preflight.needs_manual_samples ?? [];
  const sourceGroupSelectedCount = sourceGroup?.preflight.formal_candidate_manifest?.selected_count ?? 0;
  const sourceGroupDisplaySlots = (sourceGroup?.preflight.slots ?? []).map((slot) => ({
    view: slot.view,
    label: slot.label,
    before_count: slot.before_count,
    after_count: slot.after_count,
    ready: slot.ready,
  }));
  const sourceGroupRenderGaps = (sourceGroup?.preflight.missing_slots ?? []).flatMap((slot) =>
    slot.missing.map((role) => ({
      view: `${slot.view}-${role}`,
      view_label: slot.label,
      role_label: preflightMissingLabel(role),
    })),
  );
  const activePreflightStatus = sourceGroup?.preflight.status ?? preflightStatus;
  const activePreflightSlots = sourceGroup ? sourceGroupDisplaySlots : preflight?.render.slots ?? [];
  const activePreflightRenderGaps = sourceGroup ? sourceGroupRenderGaps : preflightRenderGaps;
  const activePreflightBlockingItems = sourceGroup ? [] : preflightBlockingItems;
  const activePreflightRenderBlocking = sourceGroup ? [] : preflight?.render.blocking ?? [];
  const activePreflightReviewLayers = sourceGroup ? [] : preflightReviewLayers;
  const imageWorkbenchBlockerParams = new URLSearchParams({
    case_id: String(caseId),
    status: "review_needed",
    focus: "classification_blockers",
    return: `/cases/${caseId}`,
  });
  if (useSourceGroupClassificationQueue) {
    imageWorkbenchBlockerParams.set("source_group_case_id", String(caseId));
  } else {
    for (const item of preflightBlockingItems) {
      imageWorkbenchBlockerParams.append("file", item.filename);
    }
  }
  const imageWorkbenchBlockerHref = `/images?${imageWorkbenchBlockerParams.toString()}`;
  const classificationBlockerCount = useSourceGroupClassificationQueue
    ? sourceGroupNeedsManualCount
    : preflightBlockingItems.length;
  const classificationBlockerPreviewItems = useSourceGroupClassificationQueue
    ? sourceGroupManualSamples.map((item) => ({
      case_id: item.case_id,
      filename: item.filename,
      phaseLabel: item.missing.includes("phase") ? "待补阶段" : "阶段已确认",
      viewLabel: item.missing.includes("view") ? "待判角" : "角度已确认",
      reasonText: item.missing.map((missing) => (missing === "phase" ? "缺阶段" : missing === "view" ? "缺角度" : missing)).join(" / "),
    }))
    : preflightBlockingItems.map((item) => ({
      case_id: caseId,
      filename: item.filename,
      phaseLabel: preflightPhaseLabel(item.phase),
      viewLabel: preflightViewLabel(item.view),
      reasonText: item.reasons.map(preflightReasonLabel).join(" / "),
    }));
  const copySupplementCandidate = (gap: SupplementGap, candidate: SupplementCandidate) => {
    setSupplementMessage(null);
    transferImageMut.mutate(
      {
        target_case_id: caseId,
        mode: "copy",
        inherit_manual: true,
        inherit_review: true,
        require_target_review: true,
        reviewer: "operator",
        note: `补齐 ${gap.view_label} ${gap.role_label}`,
        items: [{ case_id: candidate.case_id, filename: candidate.filename }],
      },
      {
        onSuccess: (result) => {
          const copiedName = result.items[0]?.target_filename;
          setSupplementMessage(
            copiedName
              ? `已复制 ${copiedName}，请在目标案例确认后再正式出图`
              : `未复制，跳过 ${result.skipped.length} 张`,
          );
        },
        onError: (err) => {
          setSupplementMessage(err instanceof Error ? err.message : "补图复制失败");
        },
      },
    );
  };
  const selectPreflightItems = (items: typeof preflightReviewItems) => {
    const names = items.map((item) => item.filename).filter((filename) => allImages.includes(filename));
    setSourceRoleFilter("all");
    setSourceViewFilter("all");
    setSelectedSourceImages(new Set(names));
    setSourceBatchMessage(t("preflight.selectedReviewItems", { count: names.length }));
  };
  const selectPreflightLayer = (filenames: string[]) => {
    const names = filenames.filter((filename) => allImages.includes(filename));
    setSourceRoleFilter("all");
    setSourceViewFilter("all");
    setSelectedSourceImages(new Set(names));
    setSourceBatchMessage(t("preflight.selectedReviewItems", { count: names.length }));
  };
  const reviewSelectedImages = async (verdict: "usable" | "deferred" | "needs_repick" | "excluded" | "reopen") => {
    const targets = selectedSourceList;
    if (targets.length === 0) return;
    setSourceBatchMessage(null);
    setSourceBulkBusy(true);
    try {
      for (const filename of targets) {
        await reviewImageMut.mutateAsync({
          caseId,
          filename,
          payload: {
            verdict,
            reviewer: "operator",
            layer: preflightItemByFile.get(filename)?.layer ?? null,
          },
        });
      }
      setSourceBatchMessage(t("preflight.reviewSaved", { count: targets.length }));
      setSelectedSourceImages(new Set());
    } catch (e) {
      setSourceBatchMessage(`${t("preflight.reviewError")}：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSourceBulkBusy(false);
    }
  };
  const selectFilteredImages = () => {
    setSourceBatchMessage(null);
    setSelectedSourceImages((prev) => {
      const next = new Set(prev);
      for (const name of filteredImages) next.add(name);
      return next;
    });
  };
  const clearSourceSelection = () => {
    setSourceBatchMessage(null);
    setSelectedSourceImages(new Set());
  };
  const applyBulkOverride = async (kind: "phase" | "view") => {
    const targets = selectedSourceList;
    if (targets.length === 0) return;
    const value = kind === "phase" ? sourceBulkPhase : sourceBulkView;
    if (!value) return;
    setSourceBatchMessage(null);
    setSourceBulkBusy(true);
    try {
      for (const filename of targets) {
        await updateImageOverrideMut.mutateAsync({
          caseId,
          filename,
          payload: kind === "phase"
            ? { manual_phase: value === "clear" ? "" : value }
            : { manual_view: value === "clear" ? "" : value },
        });
      }
      setSourceBatchMessage(
        kind === "phase"
          ? t("images.batchPhaseDone", { count: targets.length })
          : t("images.batchViewDone", { count: targets.length }),
      );
      setSelectedSourceImages(new Set());
    } catch (e) {
      setSourceBatchMessage(`${t("images.batchError")}：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSourceBulkBusy(false);
    }
  };
  const bulkMoveSelectedToTrash = async () => {
    const targets = selectedSourceList;
    if (targets.length === 0) return;
    if (!window.confirm(t("dialogs.trashBatchConfirm", { count: targets.length }))) return;
    setSourceBatchMessage(null);
    setSourceBulkBusy(true);
    setHiddenTrashedImagesByCase((prev) => ({
      ...prev,
      [caseId]: Array.from(new Set([...(prev[caseId] ?? []), ...targets])),
    }));
    const failed: string[] = [];
    let lastResult: Awaited<ReturnType<typeof trashImageMut.mutateAsync>> | null = null;
    for (const filename of targets) {
      try {
        lastResult = await trashImageMut.mutateAsync({ caseId, filename });
      } catch {
        failed.push(filename);
      }
    }
    if (failed.length > 0) {
      setHiddenTrashedImagesByCase((prev) => {
        const failedSet = new Set(failed);
        const next = (prev[caseId] ?? []).filter((name) => !failedSet.has(name));
        return { ...prev, [caseId]: next };
      });
    }
    setSelectedSourceImages((prev) => {
      const next = new Set(prev);
      for (const filename of targets) {
        if (!failed.includes(filename)) next.delete(filename);
      }
      return next;
    });
    if (lastResult) {
      setLastTrashed({
        case_id: caseId,
        original_filename: lastResult.original_filename,
        trash_path: lastResult.trash_path,
      });
    }
    const successCount = targets.length - failed.length;
    setTrashMessage({
      case_id: caseId,
      text: failed.length > 0
        ? t("trash.batchPartial", { count: successCount, failed: failed.length })
        : t("trash.batchTrashed", { count: successCount }),
    });
    setSourceBatchMessage(
      failed.length > 0
        ? t("images.batchTrashPartial", { count: successCount, failed: failed.length })
        : t("images.batchTrashDone", { count: successCount }),
    );
    setSourceBulkBusy(false);
  };
  const sendSelectedToManualRender = () => {
    if (selectedSourceList.length === 0) return;
    const images: ManualRenderSeedImage[] = selectedSourceList.map((filename) => {
      const role = phaseOf(filename);
      const view = sourceViewOf(filename);
      return {
        filename,
        phase: role === "pre" ? "before" : role === "post" ? "after" : null,
        view: view === "unknown" ? null : (view as ManualRenderView),
      };
    });
    manualSeedNonceRef.current += 1;
    setManualSeedRequest({ nonce: manualSeedNonceRef.current, images });
    setSourceBatchMessage(t("images.sentToManual", { count: selectedSourceList.length }));
  };
  const renderSourceThumb = (name: string, role: SourceRole) => {
    const meta = skillMetaByFile.get(name);
    const view = sourceViewOf(name);
    const hasView = view !== "unknown";
    const viewText = view === "unknown" ? "" : viewLabel(view);
    const phaseTxt =
      role === "pre" ? t("images.preOp") : role === "post" ? t("images.postOp") : t("images.unlabeled");
    const rejection = meta?.rejection_reason ? `\n${t("images.rejectionTitle")}: ${meta.rejection_reason}` : "";
    const isManualPhase = meta?.phase_override_source === "manual";
    const isManualView = meta?.view_override_source === "manual";
    const isManual = isManualSource(name);
    const needsManual = needsManualSource(name);
    const reviewState = meta?.review_state ?? null;
    const selected = selectedSourceImages.has(name);
    return (
      <div
        key={name}
        className={`thumb${selected ? " selected" : ""}`}
        draggable
        data-source-file={name}
        data-view={hasView ? view : ""}
        data-manual={isManual ? "1" : "0"}
        data-needs-manual={needsManual ? "1" : "0"}
        data-review-excluded={reviewState?.render_excluded ? "1" : "0"}
        data-selected={selected ? "1" : "0"}
        onDragStart={(e) => {
          e.dataTransfer.effectAllowed = "copy";
          e.dataTransfer.setData("application/x-case-image", name);
          e.dataTransfer.setData("text/plain", name);
        }}
        title={t("images.thumbnailTitle", {
          name,
          phase: phaseTxt,
          view: viewText || t("images.viewUnknown"),
          rejection,
        })}
        style={{ position: "relative" }}
      >
        <button
          type="button"
          className="thumb-select-btn"
          aria-label={selected ? t("images.unselectImage") : t("images.selectImage")}
          title={selected ? t("images.unselectImage") : t("images.selectImage")}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            toggleSourceSelection(name);
          }}
        >
          {selected ? <Ico name="check" size={11} /> : null}
        </button>
        <a
          href={caseFileUrl(caseId, name)}
          target="_blank"
          rel="noreferrer"
          style={{ display: "block", color: "inherit" }}
        >
          <img src={caseFileUrl(caseId, name)} alt={name} loading="lazy" />
        </a>
        <span className={`role ${role}`}>
          {role === "pre" ? "PRE" : role === "post" ? "POST" : "UNL"}
          {isManualPhase && (
            <span
              data-testid="phase-manual-marker"
              style={{ marginLeft: 3, fontSize: 8, opacity: 0.85 }}
              title={t("images.manualBadge")}
            >
              ✎
            </span>
          )}
        </span>
        {hasView && (
          <span className={`view ${view}`}>
            {viewText}
            {isManualView && (
              <span
                data-testid="view-manual-marker"
                style={{ marginLeft: 2, fontSize: 8 }}
                title={t("images.manualBadge")}
              >
                ✎
              </span>
            )}
          </span>
        )}
        {meta?.rejection_reason && (
          <span className="reject" title={meta.rejection_reason}>
            {meta.rejection_reason}
          </span>
        )}
        {needsManual && (
          <span className="class-state" title={t("images.needsManual")}>
            {t("images.needsManualShort")}
          </span>
        )}
        {reviewState && (
          <span
            className={`review-state ${reviewState.verdict ?? (reviewState.copied_requires_review ? "copied-review" : "")}`}
            title={reviewState.note || reviewState.label || reviewState.verdict || ""}
          >
            {reviewState.label || (reviewState.verdict ? t(`preflight.reviewVerdicts.${reviewState.verdict}`, { defaultValue: reviewState.verdict }) : "待确认")}
          </span>
        )}
        <button
          type="button"
          className="thumb-edit-btn"
          data-testid="thumb-edit-btn"
          aria-label={t("images.editOverride")}
          title={t("images.editOverride")}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            setOverrideTarget({ filename: name, anchor: e.currentTarget });
          }}
        >
          <Ico name="edit" size={11} />
        </button>
        <button
          type="button"
          className="thumb-trash-btn"
          data-testid="thumb-trash-btn"
          aria-label={t("trash.button")}
          title={t("trash.button")}
          disabled={trashImageMut.isPending}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            moveImageToTrash(name);
          }}
        >
          <Ico name="x" size={11} />
        </button>
        <div className="name">{name}</div>
      </div>
    );
  };

  return (
    <div style={{ height: "100%", display: "grid", gridTemplateRows: "auto 1fr", overflow: "hidden" }}>
      {/* Header — breadcrumb + layered title */}
      <div style={{ padding: "14px 24px 12px", borderBottom: "1px solid var(--line-2)", background: "var(--panel)" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            fontSize: 11.5,
            color: "var(--ink-3)",
            marginBottom: 6,
          }}
        >
          <Ico name="arrow-r" size={10} style={{ transform: "rotate(180deg)" }} />
          <Link to="/cases" style={{ color: "var(--ink-1)" }}>{t("header.backLink")}</Link>
          {parents.map((p, i) => (
            <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span style={{ color: "var(--ink-5)" }}>/</span>
              <span style={{ color: data.customer_id || i < parents.length - 1 ? "var(--ink-3)" : "var(--err)" }}>{p}</span>
            </span>
          ))}
        </div>
        <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 12 }}>
          <div style={{ minWidth: 0 }}>
            <h1 style={{ fontSize: 17, fontWeight: 600, marginBottom: 2, marginTop: 0 }}>{caseName}</h1>
            <div
              style={{
                fontFamily: "var(--mono)",
                fontSize: 11,
                color: "var(--ink-3)",
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
                maxWidth: 700,
              }}
              title={data.abs_path}
            >
              {data.abs_path}
            </div>
            <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center", flexWrap: "wrap" }}>
              <ReviewPill status={reviewKey} />
              {data.meta?.source === "skill_v3" && (
                <span
                  className="badge"
                  style={{
                    background: "var(--cyan-50)",
                    color: "var(--cyan-ink)",
                    borderColor: "var(--cyan-200)",
                    fontFamily: "var(--mono)",
                    fontSize: 10.5,
                    fontWeight: 600,
                    letterSpacing: 0.4,
                  }}
                  title={t("header.v3Tooltip")}
                >
                  V3
                </span>
              )}
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
            <button
              className="btn sm"
              onClick={() => setEvaluateOpen(true)}
              title={t("buttons.evaluateTooltip")}
            >
              <Ico name="check" size={12} />
              {t("buttons.evaluate")}
            </button>
            <button
              className="btn sm"
              onClick={() => setRevisionsOpen(true)}
              title={t("buttons.revisionsTooltip")}
            >
              <Ico name="list" size={12} />
              {t("buttons.revisions")}
            </button>
            <button
              type="button"
              className="btn sm"
              onClick={() => setRenderHistoryOpen(true)}
              title={t("renderHistory:buttonTitle")}
              data-testid="render-history-trigger"
            >
              <Ico name="recheck" size={12} />
              {t("renderHistory:buttonLabel")}
            </button>
            <button className="btn sm" onClick={() => copy(data.abs_path)}>
              <Ico name="copy" size={12} />
              {copied ? t("buttons.copied") : t("buttons.copyPath")}
            </button>
            <button className="btn sm" onClick={() => navigate(-1)}>
              <Ico name="arrow-r" size={11} style={{ transform: "rotate(180deg)" }} />
              {t("buttons.back")}
            </button>
          </div>
        </div>
        {isHeldNow && (
          <div
            style={{
              marginTop: 10,
              padding: "8px 12px",
              background: "var(--bg-2)",
              border: "1px dashed var(--line)",
              borderRadius: 6,
              display: "flex",
              alignItems: "center",
              gap: 10,
              fontSize: 12,
            }}
          >
            <Ico name="dot" size={9} style={{ color: "var(--ink-4)" }} />
            <span style={{ color: "var(--ink-2)" }}>
              {t("hold.status")}
              {data.hold_reason ? (
                <>：<span style={{ color: "var(--ink-1)", fontWeight: 500 }}>{data.hold_reason}</span></>
              ) : null}
            </span>
            <span style={{ color: "var(--ink-3)", fontSize: 11 }}>
              {t("hold.until")}{" "}
              <span style={{ fontFamily: "var(--mono)" }}>
                {heldUntilDate?.toLocaleDateString("zh-CN")}
              </span>
            </span>
            <button
              className="btn sm ghost"
              style={{ marginLeft: "auto" }}
              onClick={unholdCase}
              disabled={saving}
            >
              <Ico name="x" size={11} />
              {t("hold.cancel")}
            </button>
          </div>
        )}
        {/* Phase 3: render status card — shows latest render job + live SSE updates. */}
        <RenderStatusCard caseId={caseId} caseAbsPath={data.abs_path} />
      </div>

      {/* Body 60/40 */}
      <div style={{ display: "grid", gridTemplateColumns: "60% 40%", minHeight: 0, overflow: "hidden" }}>
        {/* Sources grid */}
        <div
          style={{
            padding: "16px 18px 16px 24px",
            overflow: "auto",
            borderRight: "1px solid var(--line-2)",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: 10,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>
                {t("images.title")}{" "}
                <span style={{ fontFamily: "var(--mono)", color: "var(--ink-3)", fontWeight: 500 }}>
                  {allImages.length}
                </span>
              </div>
              <span
                className="badge"
                style={{ background: "var(--cyan-50)", color: "var(--cyan-ink)", borderColor: "var(--cyan-200)" }}
              >
                <span style={{ width: 6, height: 6, background: "var(--cyan)", borderRadius: "50%" }}></span>
                {t("images.preOp")} {groups[0].files.length}
              </span>
              <span
                className="badge"
                style={{ background: "var(--amber-50)", color: "var(--amber-ink)", borderColor: "var(--amber-200)" }}
              >
                <span style={{ width: 6, height: 6, background: "var(--amber)", borderRadius: "50%" }}></span>
                {t("images.postOp")} {groups[1].files.length}
              </span>
              <span className="badge">
                <span style={{ width: 6, height: 6, background: "var(--ink-4)", borderRadius: "50%" }}></span>
                {t("images.unlabeled")} {groups[2].files.length}
              </span>
            </div>
            {activeTrashMessage && (
              <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11.5 }}>
                <span style={{ color: activeTrashMessage.startsWith(t("trash.error")) ? "var(--err)" : "var(--ink-3)" }}>
                  {activeTrashMessage}
                </span>
                {activeLastTrashed && (
                  <button
                    type="button"
                    className="btn sm ghost"
                    onClick={restoreLastImage}
                    disabled={restoreImageMut.isPending}
                  >
                    <Ico name="refresh" size={11} />
                    {restoreImageMut.isPending ? t("trash.restoring") : t("trash.restore")}
                  </button>
                )}
              </div>
            )}
          </div>

          {preflight && (
            <section className={`preflight-panel ${activePreflightStatus}`} data-testid="classification-preflight-panel">
              <div className="preflight-head">
                <div className="preflight-title">
                  <Ico name="scan" size={13} />
                  <span>{t("preflight.title")}</span>
                </div>
                <span className={`preflight-state ${activePreflightStatus}`}>
                  {preflightStatusLabel(activePreflightStatus)}
                </span>
              </div>
              <div className="preflight-metrics">
                <div>
                  <b>
                    {sourceGroup
                      ? `${sourceGroup.image_count - sourceGroupNeedsManualCount}/${sourceGroup.image_count}`
                      : `${preflight.classification.classified_count}/${preflight.classification.source_count}`}
                  </b>
                  <span>{t("preflight.metrics.classified")}</span>
                </div>
                <div>
                  <b>{sourceGroup ? sourceGroupNeedsManualCount : preflight.classification.needs_manual_count}</b>
                  <span>{t("preflight.metrics.needsManual")}</span>
                </div>
                <div>
                  <b>{sourceGroup ? sourceGroup.bound_case_ids.length : preflight.classification.manual_override_count}</b>
                  <span>{sourceGroup ? "绑定目录" : t("preflight.metrics.manual")}</span>
                </div>
                <div>
                  <b>{sourceGroup ? `${sourceGroupSelectedCount}/6` : preflight.classification.actionable_review_count ?? preflight.classification.review_count}</b>
                  <span>{sourceGroup ? "正式候选" : t("preflight.metrics.actionableReview")}</span>
                </div>
              </div>
              <div className="preflight-slots">
                {activePreflightSlots.map((slot) => (
                  <div key={slot.view} className={`preflight-slot ${slot.ready ? "ready" : "blocked"}`}>
                    <span>{slot.label}</span>
                    <b>{slot.before_count}/{slot.after_count}</b>
                    <em>{slot.ready ? t("preflight.slotReady") : t("preflight.slotMissing")}</em>
                  </div>
                ))}
              </div>
              {classificationBlockerCount > 0 && (
                <div className="classification-blocker-panel" data-testid="classification-blocker-panel">
                  <div className="classification-blocker-head">
                    <div>
                      <b>分类阻断任务 {classificationBlockerCount}</b>
                      <span>这些照片缺少正式出图所需的阶段/角度确认，补齐后回到本页刷新预检。</span>
                    </div>
                    <Link className="btn sm primary" to={imageWorkbenchBlockerHref} data-testid="classification-blocker-open-workbench">
                      <Ico name="list" size={11} />
                      去照片分类补齐
                    </Link>
                  </div>
                  <div className="classification-blocker-grid">
                    {classificationBlockerPreviewItems.slice(0, 8).map((item) => (
                      <article key={`${item.case_id}:${item.filename}`} className="classification-blocker-card">
                        <a href={caseFileUrl(item.case_id, item.filename)} target="_blank" rel="noreferrer">
                          <img src={caseFileUrl(item.case_id, item.filename)} alt={item.filename} loading="lazy" />
                        </a>
                        <div>
                          <b title={item.filename}>{item.filename}</b>
                          <span>
                            当前 {item.phaseLabel} / {item.viewLabel}
                          </span>
                          <em>{item.reasonText}</em>
                        </div>
                      </article>
                    ))}
                    {classificationBlockerCount > Math.min(8, classificationBlockerPreviewItems.length) && (
                      <div className="classification-blocker-more">
                        还有 {classificationBlockerCount - Math.min(8, classificationBlockerPreviewItems.length)} 张，请到照片分类队列继续处理
                      </div>
                    )}
                  </div>
                </div>
              )}
              {activePreflightRenderGaps.length > 0 && (
                <div className="supplement-gap-strip" id="supplement-candidates">
                  <div>
                    <b>缺口 {activePreflightRenderGaps.length}</b>
                    <span>
                      {activePreflightRenderGaps.map((gap) => `${gap.view_label}${gap.role_label}`).join("、")}
                    </span>
                  </div>
                  <button
                    type="button"
                    className="btn sm"
                    onClick={() => {
                      setSupplementOpen((open) => !open);
                      setSupplementMessage(null);
                    }}
                    disabled={sourceActionBusy}
                  >
                    <Ico name="search" size={11} />
                    {supplementOpen ? "收起补图候选" : "查找可补图"}
                  </button>
                </div>
              )}
              {supplementOpen && (
                <div className="supplement-panel">
                  {supplementQ.isLoading && <div className="empty">正在从全局真实照片队列查找候选…</div>}
                  {supplementQ.isError && <div className="empty">补图候选加载失败</div>}
                  {supplementQ.data && supplementQ.data.gaps.length === 0 && (
                    <div className="empty">当前三联槽位已配齐，无需跨案例补图</div>
                  )}
                  {supplementQ.data?.gaps.map((gap) => (
                    <div key={gap.key} className="supplement-gap">
                      <div className="supplement-gap-head">
                        <div>
                          <b>{gap.view_label} · {gap.role_label}</b>
                          <span>
                            {gap.body_part === "body" ? "身体" : gap.body_part === "face" ? "面部" : "部位未识别"}
                            {gap.treatment_area ? ` / ${gap.treatment_area}` : ""}
                          </span>
                        </div>
                        <em>{gap.candidate_count ?? 0} 个候选</em>
                      </div>
                      <div className="supplement-candidate-grid">
                        {(gap.candidates ?? []).map((candidate) => (
                          <article key={`${gap.key}-${candidate.case_id}-${candidate.filename}`} className="supplement-candidate-card">
                            <img src={candidate.preview_url} alt={candidate.filename} loading="lazy" />
                            <div className="supplement-candidate-body">
                              <b title={candidate.filename}>{candidate.filename}</b>
                              <span>{candidate.case_title}</span>
                              <em>{candidate.match_reasons.join(" / ")}</em>
                              <div>
                                <Link to={`/cases/${candidate.case_id}`}>来源 #{candidate.case_id}</Link>
                                <button
                                  type="button"
                                  className="btn sm primary"
                                  onClick={() => copySupplementCandidate(gap, candidate)}
                                  disabled={transferImageMut.isPending}
                                  title="复制到当前案例，并标记为补图待确认"
                                >
                                  {transferImageMut.isPending ? "复制中…" : "复制到本案"}
                                </button>
                              </div>
                            </div>
                          </article>
                        ))}
                        {(gap.candidates ?? []).length === 0 && (
                          <div className="empty">没有找到安全候选：低置信、需换片、已排除出图的照片已被过滤</div>
                        )}
                      </div>
                    </div>
                  ))}
                  {supplementMessage && <div className="supplement-message">{supplementMessage}</div>}
                </div>
              )}
              {(activePreflightBlockingItems.length > 0 || activePreflightRenderBlocking.length > 0) && (
                <div className="preflight-issues">
                  {activePreflightBlockingItems.slice(0, 4).map((item) => (
                    <span key={item.filename} title={item.filename}>
                      {item.filename}: {item.reasons.map(preflightReasonLabel).join(" / ")}
                    </span>
                  ))}
                  {activePreflightRenderBlocking.map((item) => (
                    <span key={item.view}>
                      {item.label}: {item.missing.map(preflightMissingLabel).join(" / ")}
                    </span>
                  ))}
                </div>
              )}
              {preflightLatest && (
                <div className="preflight-latest">
                  <span>#{preflightLatest.job_id}</span>
                  <span>{preflightLatest.quality_status ?? preflightLatest.job_status}</span>
                  <span>{t("preflight.latestWarnings", { count: preflightLatest.warning_buckets.actionable_count ?? preflightLatest.warning_count })}</span>
                  {(preflightLatest.blocking_warning_count ?? 0) > 0 && (
                    <span>阻断 {preflightLatest.blocking_warning_count}</span>
                  )}
                  {(preflightLatest.acceptable_warning_count ?? 0) > 0 && (
                    <span>可接受复核 {preflightLatest.acceptable_warning_count}</span>
                  )}
                  {(preflightLatest.warning_buckets.noise_count ?? 0) > 0 && (
                    <span>{t("preflight.latestNoise", { count: preflightLatest.warning_buckets.noise_count })}</span>
                  )}
                  {(preflight.classification.reviewed_count ?? 0) > 0 && (
                    <span>{t("preflight.reviewedCount", { count: preflight.classification.reviewed_count })}</span>
                  )}
                  {(preflight.classification.render_excluded_count ?? 0) > 0 && (
                    <span>{t("preflight.excludedCount", { count: preflight.classification.render_excluded_count })}</span>
                  )}
                  <span>{preflightLatest.can_publish ? t("preflight.publishable") : t("preflight.notPublishable")}</span>
                  <span>
                    {preflightAiUsage?.used_after_enhancement
                      ? t("preflight.aiUsed")
                      : t("preflight.aiNotUsed")}
                  </span>
                </div>
              )}
              {(activePreflightReviewLayers.length > 0 || preflightLatestUniqueLayers.length > 0) && (
                <div className="preflight-layer-list">
                  {activePreflightReviewLayers.map((layer) => (
                    <button
                      key={`image-${layer.key}`}
                      type="button"
                      className={`preflight-layer ${layer.severity}`}
                      onClick={() => selectPreflightLayer(layer.filenames)}
                      disabled={layer.filenames.length === 0 || sourceActionBusy}
                      title={layer.action}
                    >
                      <b>{layer.count}</b>
                      <span>{layer.label}</span>
                      <em>{layer.action}</em>
                    </button>
                  ))}
                  {preflightLatestUniqueLayers.map((layer) => (
                    <button
                      key={`render-${layer.key}`}
                      type="button"
                      className={`preflight-layer ${layer.severity}`}
                      onClick={() => selectPreflightLayer(layer.filenames)}
                      disabled={layer.filenames.length === 0 || sourceActionBusy}
                      title={layer.action}
                    >
                      <b>{layer.count}</b>
                      <span>{layer.label}</span>
                      <em>{layer.action}</em>
                    </button>
                  ))}
                </div>
              )}
              {preflightLatestUniqueLayers.some((layer) => (layer.slots?.length ?? 0) > 0) && (
                <div className="preflight-slot-detail-list">
                  {preflightLatestUniqueLayers.flatMap((layer) =>
                    (layer.slots ?? []).map((slot) => (
                      <button
                        key={`${layer.key}-${slot.key}`}
                        type="button"
                        className="preflight-slot-detail"
                        onClick={() => selectPreflightLayer(slot.filenames)}
                        disabled={slot.filenames.length === 0 || sourceActionBusy}
                        title={`${slot.before ?? "—"} / ${slot.after ?? "—"}`}
                      >
                        <b>{slot.label}</b>
                        <span>{t("preflight.poseIssueCount", { count: slot.count })}</span>
                        <em>{slot.before ?? "—"} / {slot.after ?? "—"}</em>
                      </button>
                    )),
                  )}
                </div>
              )}
              <div className="preflight-actions">
                <button
                  type="button"
                  className="btn sm"
                  onClick={() => selectPreflightItems(activePreflightBlockingItems)}
                  disabled={activePreflightBlockingItems.length === 0 || sourceActionBusy}
                >
                  {t("preflight.selectBlocking")}
                </button>
                <button
                  type="button"
                  className="btn sm ghost"
                  onClick={() => selectPreflightItems(preflightReviewOnlyItems)}
                  disabled={preflightReviewOnlyItems.length === 0 || sourceActionBusy}
                >
                  {t("preflight.selectAllReview")}
                </button>
                {preflightReviewOnlyItems.length > 0 && (
                  <span>{t("preflight.reviewOnlyCount", { count: preflightReviewOnlyItems.length })}</span>
                )}
                {preflightNoiseItems.length > 0 && (
                  <span>{t("preflight.noiseCount", { count: preflightNoiseItems.length })}</span>
                )}
              </div>
            </section>
          )}

          <section className="source-group-panel" id="source-group-preflight" data-testid="source-group-panel">
            <div className="source-group-head">
              <div>
                <b>绑定源组</b>
                <span>
                  {sourceGroup
                    ? `${sourceGroup.source_count} 个目录 / ${sourceGroup.image_count} 张源图`
                    : sourceGroupQ.isLoading
                      ? "正在读取真实来源目录…"
                      : "未读取"}
                </span>
              </div>
              <span className={`preflight-state ${sourceGroupStatusClass}`}>
                {sourceGroup
                  ? sourceGroup.preflight.status === "ready"
                    ? "预检 ready"
                    : sourceGroup.preflight.status === "blocked"
                      ? "硬门禁阻断"
                      : "需复检"
                  : "加载中"}
              </span>
            </div>
            {sourceGroupQ.isError && <div className="empty">绑定源组加载失败</div>}
            {sourceGroup && (
              <>
                <div className="source-group-metrics">
                  <span>主目录 #{sourceGroup.case_id}</span>
                  <span>绑定 {sourceGroup.bound_case_ids.length}</span>
                  <span>术前 {sourceGroup.effective_source_profile.before_count}</span>
                  <span>术后 {sourceGroup.effective_source_profile.after_count}</span>
                  <span>待补 {sourceGroup.preflight.needs_manual_count}</span>
                  <span>已排除 {sourceGroup.preflight.render_excluded_count}</span>
                  <span>ready 分 {sourceGroup.preflight.readiness_score ?? "—"}</span>
                  <span>正式候选 {sourceGroup.preflight.formal_candidate_manifest?.selected_count ?? 0}/6</span>
                  {sourceGroupMissingSourceCount > 0 && <span>文件缺失 {sourceGroupMissingSourceCount}</span>}
                </div>
                {sourceGroup.preflight.hard_blockers?.length ? (
                  <div className="source-group-warnings">
                    {sourceGroup.preflight.hard_blockers.slice(0, 4).map((blocker) => (
                      <span key={blocker.code}>
                        {blocker.message} · {blocker.recommended_action}
                      </span>
                    ))}
                  </div>
                ) : (
                  <div className="source-group-audit">
                    <span>正式候选 manifest</span>
                    <b>{sourceGroup.preflight.formal_candidate_manifest?.policy ?? "source_selection_v1"} · 三联槽位已闭环</b>
                  </div>
                )}
                {(focusedSourceGroupSlot || (sourceGroup.preflight.accepted_warnings?.length ?? 0) > 0) && (
                  <div className="source-group-audit">
                    <span>质检闭环</span>
                    <b>
                      {focusedSourceGroupSlot
                        ? `正在处理 ${sourceGroupViewLabel(focusedSourceGroupSlot as SourceGroupSlot["view"])} · ${focusedIssueCode || "issue"}`
                        : `已确认可接受 ${sourceGroup.preflight.accepted_warnings?.length ?? 0} 条`}
                    </b>
                  </div>
                )}
                <div className="source-group-filter-row" data-testid="source-group-filter-row">
                  {sourceGroupFilterItems.map((item) => (
                    <button
                      key={item.key}
                      type="button"
                      className={`source-group-filter-btn ${sourceGroupFilter === item.key ? "active" : ""}`}
                      onClick={() => setSourceGroupFilter(item.key)}
                    >
                      <span>{item.label}</span>
                      <b>{item.count}</b>
                    </button>
                  ))}
                </div>
                <div className="source-group-bulk-bar" data-testid="source-group-bulk-bar">
                  <span>已选 {selectedSourceGroupList.length}</span>
                  <button
                    type="button"
                    className="btn sm"
                    onClick={selectVisibleSourceGroupImages}
                    disabled={sourceGroupVisibleImages.length === 0 || sourceActionBusy}
                  >
                    选择当前
                  </button>
                  <button
                    type="button"
                    className="btn sm ghost"
                    onClick={clearSourceGroupSelection}
                    disabled={selectedSourceGroupList.length === 0 || sourceActionBusy}
                  >
                    清空
                  </button>
                  <button type="button" className="btn sm" onClick={() => sourceGroupApplyBulkOverride("phase", "before")} disabled={selectedSourceGroupList.length === 0 || sourceActionBusy}>批设术前</button>
                  <button type="button" className="btn sm" onClick={() => sourceGroupApplyBulkOverride("phase", "after")} disabled={selectedSourceGroupList.length === 0 || sourceActionBusy}>批设术后</button>
                  <button type="button" className="btn sm ghost" onClick={() => sourceGroupApplyBulkOverride("phase", "clear")} disabled={selectedSourceGroupList.length === 0 || sourceActionBusy}>清阶段</button>
                  <button type="button" className="btn sm" onClick={() => sourceGroupApplyBulkOverride("view", "front")} disabled={selectedSourceGroupList.length === 0 || sourceActionBusy}>批设正面</button>
                  <button type="button" className="btn sm" onClick={() => sourceGroupApplyBulkOverride("view", "oblique")} disabled={selectedSourceGroupList.length === 0 || sourceActionBusy}>批设45°</button>
                  <button type="button" className="btn sm" onClick={() => sourceGroupApplyBulkOverride("view", "side")} disabled={selectedSourceGroupList.length === 0 || sourceActionBusy}>批设侧面</button>
                  <button type="button" className="btn sm ghost" onClick={() => sourceGroupApplyBulkOverride("view", "clear")} disabled={selectedSourceGroupList.length === 0 || sourceActionBusy}>清角度</button>
                  <button type="button" className="btn sm" onClick={() => sourceGroupReviewSelected("usable")} disabled={selectedSourceGroupList.length === 0 || sourceActionBusy}>可用</button>
                  <button type="button" className="btn sm ghost" onClick={() => sourceGroupReviewSelected("deferred")} disabled={selectedSourceGroupList.length === 0 || sourceActionBusy}>低优先</button>
                  <button type="button" className="btn sm ghost" onClick={() => sourceGroupReviewSelected("needs_repick")} disabled={selectedSourceGroupList.length === 0 || sourceActionBusy}>需换片</button>
                  <button type="button" className="btn sm danger" onClick={() => sourceGroupReviewSelected("excluded")} disabled={selectedSourceGroupList.length === 0 || sourceActionBusy}>排除出图</button>
                  <button type="button" className="btn sm ghost" onClick={() => sourceGroupReviewSelected("reopen")} disabled={selectedSourceGroupList.length === 0 || sourceActionBusy}>重开</button>
                </div>
                {sourceGroup.audit.binding_note && (
                  <div className="source-group-audit">
                    <span>绑定备注</span>
                    <b>{sourceGroup.audit.binding_note}</b>
                  </div>
                )}
                <div className="source-group-slots">
	                  {sourceGroup.preflight.slots.map((slot) => {
	                    const manifestSlot = sourceGroup.preflight.formal_candidate_manifest?.slots?.[slot.view];
	                    const qualityPrediction = manifestSlot?.quality_prediction;
	                    return (
	                    <div key={slot.view} className={`source-group-slot ${slot.ready ? "ready" : "blocked"} ${isFocusedSlot(slot) ? "focused" : ""}`}>
                      <span>{slot.label}</span>
                      <b>{slot.before_count}/{slot.after_count}</b>
                      <em>
                        {slot.source_case_ids.length > 0
                          ? slot.source_case_ids.map((sourceCaseId) => `#${sourceCaseId}`).join(" / ")
                          : "未配"}
                      </em>
                      <div className="source-group-candidates">
                        <span title={slot.selected_before ? `${slot.selected_before.case_title} / ${slot.selected_before.filename} / ${slot.selected_before.selection_reasons.join("、")}` : ""}>
                          {candidateLine(slot.selected_before, "前")}
                        </span>
                        <span title={slot.selected_after ? `${slot.selected_after.case_title} / ${slot.selected_after.filename} / ${slot.selected_after.selection_reasons.join("、")}` : ""}>
                          {candidateLine(slot.selected_after, "后")}
                        </span>
                      </div>
                      {(slot.selected_before || slot.selected_after) && (
                        <div className="source-group-selection-reasons">
                          {slot.selected_before?.selection_reasons?.[0] && <span>前：{slot.selected_before.selection_reasons[0]}</span>}
                          {slot.selected_after?.selection_reasons?.[0] && <span>后：{slot.selected_after.selection_reasons[0]}</span>}
                        </div>
                      )}
	                      {slot.pair_quality && (
	                        <div className={`source-group-pair ${slot.pair_quality.severity}`}>
	                          <b>{slot.pair_quality.score}</b>
	                          <span>{pairQualityLabel(slot.pair_quality.label)}</span>
	                          <em>{slot.pair_quality.reasons[0] ?? "首选候选已配齐"}</em>
	                        </div>
	                      )}
	                      {qualityPrediction && (
	                        <div className="source-group-selection-reasons">
	                          <span>
	                            出图预测：{qualityPrediction.decision === "render" ? "入选" : qualityPrediction.decision === "drop" ? "降级移除" : "阻断"}
	                            {qualityPrediction.pair_score != null ? ` · pair ${qualityPrediction.pair_score}` : ""}
	                          </span>
	                          <span>{qualityPrediction.recommended_action}</span>
	                        </div>
	                      )}
                      {slot.pair_quality?.warnings?.length ? (
                        <div className="source-group-pair-warnings">
                          {slot.pair_quality.warnings.slice(0, 2).map((warning) => (
                            <span key={`${slot.view}-${warning.code}`}>{warning.message}</span>
                          ))}
                        </div>
                      ) : null}
                      {slot.selection_lock?.locked && (
                        <div className="source-group-lock-note">
                          <span>已锁片 · {slot.selection_lock.reviewer ?? "operator"}</span>
                          <button type="button" className="btn sm ghost" onClick={() => sourceGroupClearLock(slot)} disabled={sourceActionBusy}>解除</button>
                        </div>
                      )}
                      {isFocusedSlot(slot) && focusedIssueCode && (
                        <div className="source-group-lock-note">
                          <span>来自质检：{focusedIssueText || focusedIssueCode}</span>
                          <button
                            type="button"
                            className="btn sm"
                            onClick={() => sourceGroupAcceptWarning(slot, focusedIssueCode, focusedIssueText)}
                            disabled={sourceActionBusy}
                          >
                            确认可接受
                          </button>
                        </div>
                      )}
                      {slot.selected_before && slot.selected_after && (
                        <div className="source-group-lock-panel">
                          <div>
                            <b>候选重选</b>
                            <span>当前入选可锁定；也可从备选组合里改锁片</span>
                          </div>
                          <button
                            type="button"
                            className="btn sm"
                            onClick={() => sourceGroupLockPair(slot, slot.selected_before!, slot.selected_after!)}
                            disabled={sourceActionBusy}
                          >
                            锁定当前
                          </button>
                          <div className="source-group-pair-grid">
                            {slot.before_candidates.slice(0, 3).flatMap((before) =>
                              slot.after_candidates.slice(0, 3).map((after) => (
                                <button
                                  key={`${slot.view}-${before.case_id}-${before.filename}-${after.case_id}-${after.filename}`}
                                  type="button"
                                  className="source-group-pair-option"
                                  title={`前：${before.selection_reasons.join("、")} / 后：${after.selection_reasons.join("、")}`}
                                  onClick={() => sourceGroupLockPair(slot, before, after)}
                                  disabled={sourceActionBusy}
                                >
                                  <span>#{before.case_id} {before.filename}</span>
                                  <span>#{after.case_id} {after.filename}</span>
                                </button>
                              )),
                            )}
                          </div>
                        </div>
                      )}
	                    </div>
	                    );
	                  })}
                </div>
                {(sourceGroup.preflight.needs_manual_count > 0 || sourceGroup.preflight.missing_slots.length > 0) && (
                  <div className="source-group-warnings">
                    {sourceGroup.preflight.missing_slots.slice(0, 3).map((slot) => (
                      <span key={slot.view}>
                        {slot.label} 缺 {slot.missing.map((role) => role === "before" ? "术前" : "术后").join(" / ")}
                      </span>
                    ))}
                    {sourceGroup.preflight.needs_manual_count > 0 && (
                      <span>{sourceGroup.preflight.needs_manual_count} 张需要补阶段/角度</span>
                    )}
                  </div>
                )}
                {sourceGroup.missing_bound_case_ids.length > 0 && (
                  <div className="source-group-warnings">
                    <span>绑定目录已失效：{sourceGroup.missing_bound_case_ids.map((sourceCaseId) => `#${sourceCaseId}`).join("、")}</span>
                  </div>
                )}
                {sourceGroupFilter === "missing_file" && (
                  <div className="source-group-missing-files">
                    {sourceGroupMissingFiles.length > 0
                      ? sourceGroupMissingFiles.map((item) => (
                          <span key={`${item.case_id}-${item.filename}`}>
                            #{item.case_id} {item.source_title} / {item.filename}
                          </span>
                        ))
                      : <span>当前源组没有缺失文件</span>}
                  </div>
                )}
                <div className="source-group-source-list">
                  {sourceGroup.sources.map((source) => (
                    <article key={`${source.role}-${source.case_id}`} className="source-group-source">
                      <div className="source-group-source-head">
                        <div>
                          <b>
                            {source.role === "primary" ? "主目录" : "绑定目录"} #{source.case_id} · {source.case_title}
                          </b>
                          <span title={source.abs_path}>{source.abs_path}</span>
                        </div>
                        <div>
                          <span>{sourceKindLabel(source.source_profile.source_kind)}</span>
                          {source.missing_image_count > 0 && <span>缺失 {source.missing_image_count}</span>}
                          {source.case_id !== caseId && <Link to={`/cases/${source.case_id}`}>打开</Link>}
                        </div>
                      </div>
                      <div className="source-group-grid">
                        {source.images
                          .filter((image) => sourceGroupVisibleImages.some((visible) => sourceGroupImageKey(visible) === sourceGroupImageKey(image)))
                          .map((image) => {
                            const selected = selectedSourceGroupImages.has(sourceGroupImageKey(image));
                            return (
                          <div
                            key={`${image.case_id}-${image.filename}`}
                            className={`source-group-card ${image.render_excluded ? "excluded" : ""} ${selected ? "selected" : ""}`}
                            draggable={image.case_id === caseId && allImages.includes(image.filename)}
                            data-source-file={image.filename}
                            onDragStart={(e) => {
                              if (image.case_id !== caseId || !allImages.includes(image.filename)) {
                                e.preventDefault();
                                return;
                              }
                              e.dataTransfer.effectAllowed = "copy";
                              e.dataTransfer.setData("application/x-case-image", image.filename);
                              e.dataTransfer.setData("text/plain", image.filename);
                            }}
                            title={
                              image.case_id === caseId && allImages.includes(image.filename)
                                ? "可拖到右侧『人工整理与出图』槽位"
                                : "互补目录或非当前 case 的图，无法拖到出图槽"
                            }
                          >
                            <button
                              type="button"
                              className="source-group-select-toggle"
                              onClick={() => toggleSourceGroupSelection(image)}
                              aria-pressed={selected}
                              disabled={sourceActionBusy}
                            >
                              {selected ? "已选" : "选择"}
                            </button>
                            <a href={image.preview_url} target="_blank" rel="noreferrer">
                              <img src={image.preview_url} alt={image.filename} loading="lazy" />
                            </a>
                            <div className="source-group-card-body">
                              <b title={image.filename}>{image.filename}</b>
                              <div className="source-group-tags">
                                <span className={image.phase === "before" ? "pre" : image.phase === "after" ? "post" : ""}>
                                  {sourceGroupPhaseLabel(image.phase)}
                                  {image.phase_source === "manual" ? " / 人工" : image.phase_source === "directory" ? " / 目录" : ""}
                                </span>
                                <span className={image.view ?? ""}>
                                  {sourceGroupViewLabel(image.view)}
                                  {image.view_source === "manual" ? " / 人工" : ""}
                                </span>
                                {image.review_state?.label && <span>{image.review_state.label}</span>}
                              </div>
                              <div className="source-group-card-quality">
                                <span>{image.manual ? "人工整理优先" : "自动识别"}</span>
                                {image.angle_confidence != null && <span>角度 {Math.round(image.angle_confidence * 100)}</span>}
                                {image.rejection_reason === "face_detection_failure" && <span className="warn">面检复核</span>}
                              </div>
                              <div className="source-group-controls">
                                <select
                                  value={image.phase ?? ""}
                                  onChange={(e) => sourceGroupApplyOverride(image, "phase", e.target.value)}
                                  disabled={sourceActionBusy}
                                  aria-label="源组阶段"
                                >
                                  <option value="">阶段</option>
                                  <option value="before">术前</option>
                                  <option value="after">术后</option>
                                </select>
                                <select
                                  value={image.view ?? ""}
                                  onChange={(e) => sourceGroupApplyOverride(image, "view", e.target.value)}
                                  disabled={sourceActionBusy}
                                  aria-label="源组角度"
                                >
                                  <option value="">角度</option>
                                  <option value="front">正面</option>
                                  <option value="oblique">45°</option>
                                  <option value="side">侧面</option>
                                </select>
                              </div>
                              <div className="source-group-actions">
                                <button
                                  type="button"
                                  className="btn sm"
                                  onClick={() => sourceGroupReviewImage(image, "usable")}
                                  disabled={sourceActionBusy}
                                >
                                  可用
                                </button>
                                <button
                                  type="button"
                                  className="btn sm ghost"
                                  onClick={() => sourceGroupReviewImage(image, "deferred")}
                                  disabled={sourceActionBusy}
                                >
                                  低优先
                                </button>
                                <button
                                  type="button"
                                  className="btn sm danger"
                                  onClick={() => sourceGroupReviewImage(image, "excluded")}
                                  disabled={sourceActionBusy}
                                >
                                  排除
                                </button>
                              </div>
                            </div>
                          </div>
                            );
                          })}
                        {source.images.filter((image) => sourceGroupVisibleImages.some((visible) => sourceGroupImageKey(visible) === sourceGroupImageKey(image))).length === 0 && (
                          <div className="empty">
                            {source.images.length === 0 ? "该目录没有可用于正式出图的真实源图" : "当前筛选没有可整理图片"}
                          </div>
                        )}
                      </div>
                    </article>
                  ))}
                </div>
                {sourceGroupMessage && <div className="source-group-message">{sourceGroupMessage}</div>}
              </>
            )}
          </section>

          <div className="source-filter-panel">
            <div className="source-filter-row">
              <span className="source-filter-label">{t("images.phaseFilter")}</span>
              {roleFilterItems.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  className={`source-filter-btn ${sourceRoleFilter === item.key ? "active" : ""} ${item.tone ?? ""}`}
                  onClick={() => setSourceRoleFilter(item.key)}
                >
                  <span>{item.label}</span>
                  <b>{item.count}</b>
                </button>
              ))}
            </div>
            <div className="source-filter-row">
              <span className="source-filter-label">{t("images.viewFilter")}</span>
              {viewFilterItems.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  className={`source-filter-btn ${sourceViewFilter === item.key ? "active" : ""}`}
                  onClick={() => setSourceViewFilter(item.key)}
                >
                  <span>{item.label}</span>
                  <b>{item.count}</b>
                </button>
              ))}
            </div>
          </div>

          <div className="source-bulk-bar" data-testid="source-bulk-bar">
            <div className="source-bulk-summary">
              {t("images.selectedCount", { count: selectedSourceList.length })}
              {selectedFilteredList.length > 0 && selectedFilteredList.length !== selectedSourceList.length && (
                <span>{t("images.selectedInFilter", { count: selectedFilteredList.length })}</span>
              )}
            </div>
            <button
              type="button"
              className="btn sm"
              onClick={selectFilteredImages}
              disabled={filteredImages.length === 0 || sourceActionBusy}
            >
              <Ico name="check" size={11} />
              {t("images.selectFiltered")}
            </button>
            <button
              type="button"
              className="btn sm ghost"
              onClick={clearSourceSelection}
              disabled={selectedSourceList.length === 0 || sourceActionBusy}
            >
              <Ico name="x" size={11} />
              {t("images.clearSelection")}
            </button>
            <select
              value={sourceBulkPhase}
              onChange={(e) => setSourceBulkPhase(e.target.value as BulkPhaseAction | "")}
              disabled={selectedSourceList.length === 0 || sourceActionBusy}
              aria-label={t("images.batchPhaseLabel")}
            >
              <option value="">{t("images.batchPhaseLabel")}</option>
              <option value="before">{t("images.preOp")}</option>
              <option value="after">{t("images.postOp")}</option>
              <option value="clear">{t("images.clearPhaseOverride")}</option>
            </select>
            <button
              type="button"
              className="btn sm"
              onClick={() => applyBulkOverride("phase")}
              disabled={selectedSourceList.length === 0 || !sourceBulkPhase || sourceActionBusy}
            >
              {t("images.applyPhase")}
            </button>
            <select
              value={sourceBulkView}
              onChange={(e) => setSourceBulkView(e.target.value as BulkViewAction | "")}
              disabled={selectedSourceList.length === 0 || sourceActionBusy}
              aria-label={t("images.batchViewLabel")}
            >
              <option value="">{t("images.batchViewLabel")}</option>
              <option value="front">{t("images.viewFront")}</option>
              <option value="oblique">{t("images.viewOblique")}</option>
              <option value="side">{t("images.viewSide")}</option>
              <option value="clear">{t("images.clearViewOverride")}</option>
            </select>
            <button
              type="button"
              className="btn sm"
              onClick={() => applyBulkOverride("view")}
              disabled={selectedSourceList.length === 0 || !sourceBulkView || sourceActionBusy}
            >
              {t("images.applyView")}
            </button>
            <button
              type="button"
              className="btn sm primary"
              onClick={sendSelectedToManualRender}
              disabled={selectedSourceList.length === 0 || sourceActionBusy}
              title={t("images.sendToManualTitle")}
            >
              <Ico name="arrow-r" size={11} />
              {t("images.sendToManual")}
            </button>
            <button
              type="button"
              className="btn sm"
              onClick={() => reviewSelectedImages("usable")}
              disabled={selectedSourceList.length === 0 || sourceActionBusy}
            >
              {t("preflight.reviewUsable")}
            </button>
            <button
              type="button"
              className="btn sm ghost"
              onClick={() => reviewSelectedImages("deferred")}
              disabled={selectedSourceList.length === 0 || sourceActionBusy}
            >
              {t("preflight.reviewDeferred")}
            </button>
            <button
              type="button"
              className="btn sm ghost"
              onClick={() => reviewSelectedImages("needs_repick")}
              disabled={selectedSourceList.length === 0 || sourceActionBusy}
            >
              {t("preflight.reviewNeedsRepick")}
            </button>
            <button
              type="button"
              className="btn sm danger"
              onClick={() => reviewSelectedImages("excluded")}
              disabled={selectedSourceList.length === 0 || sourceActionBusy}
              title={t("preflight.reviewExcludeTitle")}
            >
              {t("preflight.reviewExclude")}
            </button>
            <button
              type="button"
              className="btn sm ghost"
              onClick={() => reviewSelectedImages("reopen")}
              disabled={selectedSourceList.length === 0 || sourceActionBusy}
            >
              {t("preflight.reviewReopen")}
            </button>
            <button
              type="button"
              className="btn sm danger"
              onClick={bulkMoveSelectedToTrash}
              disabled={selectedSourceList.length === 0 || sourceActionBusy}
            >
              <Ico name="x" size={11} />
              {t("images.trashSelected")}
            </button>
            {sourceBatchMessage && (
              <span className="source-bulk-message">{sourceBatchMessage}</span>
            )}
          </div>

          {allImages.length === 0 ? (
            <div className="empty">{t("images.empty")}</div>
          ) : filteredImages.length === 0 ? (
            <div className="empty">{t("images.noMatch")}</div>
          ) : (
            <div className="source-wall">
              {filteredImages.map((name) => renderSourceThumb(name, phaseOf(name)))}
            </div>
          )}
        </div>

        {/* Right rail */}
        <div
          style={{
            padding: "16px 24px 16px 16px",
            overflow: "auto",
            display: "grid",
            gap: 12,
            alignContent: "start",
          }}
        >
          <ManualRenderPicker
            caseId={caseId}
            allImages={allImages}
            brand={brand}
            seedRequest={manualSeedRequest}
          />

          {/* Manual edit card */}
          <div
            className="card"
            style={{ borderColor: "var(--cyan-200)", boxShadow: "0 0 0 3px rgba(8,145,178,.04)" }}
          >
            <div
              className="card-h"
              style={{ background: "var(--cyan-50)", borderBottom: "1px solid var(--cyan-200)" }}
            >
              <div className="t">
                <Ico name="scan" size={13} style={{ color: "var(--cyan-ink)" }} />
                <span style={{ color: "var(--cyan-ink)" }}>{t("edit.cardTitle")}</span>
                {isOverridden && !editing && (
                  <span
                    className="badge"
                    style={{
                      background: "var(--amber-100)",
                      color: "var(--amber-ink)",
                      borderColor: "var(--amber-200)",
                    }}
                  >
                    {t("edit.hasOverride")}
                  </span>
                )}
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
                {!editing ? (
                  <>
                    <button
                      className="btn sm"
                      onClick={() => rescanMut.mutate(caseId)}
                      disabled={rescanning || upgrading || enqueueingRender}
                      title={t("buttons.rescanTooltip")}
                    >
                      <Ico name="refresh" size={11} />
                      {rescanning ? t("buttons.rescanning") : t("edit.autoJudge")}
                    </button>
                    <button
                      className="btn sm"
                      onClick={() => upgradeMut.mutate({ caseId, brand })}
                      disabled={rescanning || upgrading || enqueueingRender}
                      title={t("buttons.upgradeTooltip")}
                      style={{ borderColor: "var(--cyan-200)", color: "var(--cyan-ink)" }}
                    >
                      <Ico name="scan" size={11} />
                      {upgrading ? t("buttons.upgrading") : t("edit.deepJudge")}
                    </button>
                    <button
                      className="btn sm primary"
                      onClick={() =>
                        renderMut.mutate({
                          caseId,
                          payload: { brand, template: "tri-compare", semantic_judge: "auto" },
                        })
                      }
                      disabled={enqueueingRender || renderGateBlocked}
                      title={renderGateTitle}
                    >
                      <Ico name="image" size={11} />
                      {enqueueingRender ? t("buttons.enqueuing") : t("edit.autoRender")}
                    </button>
                    <button className="btn sm ghost" onClick={() => setEditing(true)}>
                      <Ico name="edit" size={11} />
                      {t("edit.editButton")}
                    </button>
                  </>
                ) : (
                  <>
                    <button className="btn sm ghost" onClick={() => setEditing(false)} disabled={saving}>
                      {t("edit.cancel")}
                    </button>
                    <button className="btn sm danger" onClick={clearOverrides} disabled={saving}>
                      {t("edit.clearOverride")}
                    </button>
                    <button className="btn sm amber" onClick={saveEdits} disabled={saving}>
                      <Ico name="check" size={11} />
                      {saving ? t("edit.saving") : t("edit.save")}
                    </button>
                  </>
                )}
              </div>
            </div>
            <div className="card-b" style={{ display: "grid", gap: 12 }}>
              {!editing && (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    flexWrap: "wrap",
                    gap: 8,
                    padding: "8px 10px",
                    border: "1px solid var(--cyan-200)",
                    borderRadius: 6,
                    background: "var(--cyan-50)",
                    color: "var(--cyan-ink)",
                    fontSize: 11.5,
                  }}
                >
                  <span>{t("edit.autoSummary")}</span>
                  <div style={{ display: "flex", gap: 6, flexShrink: 0, flexWrap: "wrap" }}>
                    {reviewKey !== "reviewed" && (
                      <button
                        className="btn sm"
                        style={{
                          background: "#fff",
                          borderColor: "var(--ok-100)",
                          color: "var(--ok)",
                        }}
                        onClick={() => setReview("reviewed")}
                      >
                        <Ico name="check" size={11} />
                        {t("edit.markReviewed")}
                      </button>
                    )}
                    {reviewKey !== "needs_recheck" && (
                      <button className="btn sm danger" onClick={() => setReview("needs_recheck")}>
                        <Ico name="alert" size={11} />
                        {t("edit.needsRecheck")}
                      </button>
                    )}
                    {!isHeldNow && (
                      <button
                        className="btn sm ghost"
                        onClick={holdCase}
                        disabled={saving}
                        title={t("edit.holdTooltip")}
                        style={{ borderStyle: "dashed", color: "var(--ink-3)", background: "#fff" }}
                      >
                        <Ico name="dot" size={11} />
                        {t("edit.hold")}
                      </button>
                    )}
                  </div>
                </div>
              )}
              {editing && (
                <div
                  style={{ fontSize: 11, color: "var(--ink-3)", display: "flex", alignItems: "center", gap: 6 }}
                >
                  <Ico name="dot" size={8} style={{ color: "var(--amber)" }} />
                  {t("edit.modeWarning")}
                </div>
              )}

              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "76px 1fr",
                  gap: "10px 12px",
                  alignItems: "center",
                }}
              >
                <label style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{t("edit.categoryLabel")}</label>
                {editing ? (
                  <select
                    value={draft.manual_category}
                    onChange={(e) =>
                      setDraft({ ...draft, manual_category: e.target.value as Category | "" })
                    }
                    style={{ background: "#fff", borderColor: "var(--amber-200)" }}
                  >
                    <option value="">{t("edit.followAuto")}</option>
                    {Object.entries(CATEGORY_LABEL).map(([k, v]) => (
                      <option key={k} value={k}>{v}</option>
                    ))}
                  </select>
                ) : (
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      padding: "5px 9px",
                      borderRadius: 6,
                      border: "1px solid var(--line)",
                      background: "var(--panel-2)",
                    }}
                  >
                    {data.manual_category ? (
                      <CategoryPill value={data.manual_category} />
                    ) : (
                      <span className="layer-chip empty" style={{ height: 20 }}>
                        <span className="lab">manual</span>
                        {t("edit.noOverride")}
                      </span>
                    )}
                    <span
                      style={{
                        marginLeft: "auto",
                        fontFamily: "var(--mono)",
                        fontSize: 10.5,
                        color: "var(--ink-4)",
                      }}
                    >
                      auto: {CATEGORY_LABEL[data.auto_category]}
                    </span>
                  </div>
                )}

                <label style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{t("edit.templateLabel")}</label>
                {editing ? (
                  <select
                    value={draft.manual_template_tier}
                    onChange={(e) => setDraft({ ...draft, manual_template_tier: e.target.value })}
                    style={{ background: "#fff", borderColor: "var(--amber-200)" }}
                  >
                    <option value="">{t("edit.followAuto")}</option>
                    {Object.entries(TIER_LABEL).map(([k, v]) => (
                      <option key={k} value={k}>{v}</option>
                    ))}
                  </select>
                ) : (
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      padding: "5px 9px",
                      borderRadius: 6,
                      border: "1px solid var(--line)",
                      background: "var(--panel-2)",
                    }}
                  >
                    {data.manual_template_tier ? (
                      <TierPill value={data.manual_template_tier} />
                    ) : (
                      <span className="layer-chip empty" style={{ height: 20 }}>
                        <span className="lab">manual</span>
                        {t("edit.noOverride")}
                      </span>
                    )}
                    <span
                      style={{
                        marginLeft: "auto",
                        fontFamily: "var(--mono)",
                        fontSize: 10.5,
                        color: "var(--ink-4)",
                      }}
                    >
                      auto: {data.auto_template_tier ? TIER_LABEL[data.auto_template_tier] : "—"}
                    </span>
                  </div>
                )}

                <label style={{ fontSize: 11.5, color: "var(--ink-3)", alignSelf: "flex-start", marginTop: 6 }}>
                  {t("edit.notesLabel")}
                </label>
                {editing ? (
                  <textarea
                    style={{
                      minHeight: 56,
                      borderColor: "var(--amber-200)",
                      background: "#fff",
                    }}
                    value={draft.notes}
                    onChange={(e) => setDraft({ ...draft, notes: e.target.value })}
                    placeholder={t("edit.notesPlaceholder")}
                  />
                ) : (
                  <div
                    style={{
                      fontSize: 12.5,
                      color: data.notes ? "var(--ink-1)" : "var(--ink-4)",
                      padding: "6px 0",
                    }}
                  >
                    {data.notes || <span style={{ fontStyle: "italic" }}>—</span>}
                  </div>
                )}

                <label style={{ fontSize: 11.5, color: "var(--ink-3)", alignSelf: "flex-start", marginTop: 4 }}>
                  {t("edit.tagsLabel")}
                </label>
                {editing ? (
                  <input
                    value={draft.tags}
                    onChange={(e) => setDraft({ ...draft, tags: e.target.value })}
                    placeholder={t("edit.tagsPlaceholder")}
                    style={{ borderColor: "var(--amber-200)", background: "#fff" }}
                  />
                ) : data.tags.length > 0 ? (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                    {data.tags.map((t) => (
                      <span key={t} className="chip on">
                        {t}
                      </span>
                    ))}
                  </div>
                ) : (
                  <span style={{ color: "var(--ink-4)", fontStyle: "italic" }}>—</span>
                )}

                <label style={{ fontSize: 11.5, color: "var(--ink-3)", alignSelf: "flex-start", marginTop: 4 }}>
                  {t("edit.blockingLabel")}
                </label>
                <div style={{ display: "grid", gap: 6 }}>
                  {editing && (
                    <select
                      value=""
                      onChange={(e) => {
                        if (e.target.value) toggleExtraBlocking(e.target.value);
                      }}
                      style={{ background: "#fff", borderColor: "var(--amber-200)" }}
                    >
                      <option value="">{t("edit.blockingSelectPlaceholder")}</option>
                      {issueDict.map((iss) => (
                        <option
                          key={iss.code}
                          value={iss.code}
                          disabled={draft.extra_blocking.includes(iss.code)}
                        >
                          {iss.zh}
                        </option>
                      ))}
                    </select>
                  )}
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 4, minHeight: 24, alignItems: "center" }}>
                    {(editing ? draft.extra_blocking : data.manual_blocking_codes).length > 0 ? (
                      (editing ? draft.extra_blocking : data.manual_blocking_codes).map((code) => {
                        const iss = issueDict.find((item) => item.code === code);
                        return (
                          <button
                            key={code}
                            type="button"
                            className="chip danger on"
                            onClick={() => editing && toggleExtraBlocking(code)}
                            style={{ cursor: editing ? "pointer" : "default" }}
                            title={code}
                          >
                            {iss?.zh ?? code}
                            {editing && <Ico name="x" size={10} />}
                          </button>
                        );
                      })
                    ) : (
                      <span style={{ color: "var(--ink-4)", fontSize: 12 }}>{t("edit.noExtraBlocking")}</span>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Customer binding card */}
          <div className="card">
            <div className="card-h">
              <div className="t">
                <Ico name="user" size={13} style={{ color: "var(--ink-3)" }} />
                {t("customer.cardTitle")}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                {data.customer_id ? (
                  <span
                    className="badge"
                    style={{ background: "var(--ok-50)", color: "var(--ok)", borderColor: "var(--ok-100)" }}
                  >
                    <Ico name="check" size={10} />
                    {data.customer_canonical ?? t("customer.bound")}
                  </span>
                ) : (
                  <span
                    className="badge"
                    style={{ background: "var(--err-50)", color: "var(--err)", borderColor: "var(--err-100)" }}
                  >
                    <Ico name="alert" size={10} />
                    {t("customer.unbound")}
                  </span>
                )}
                <button type="button" className="btn sm ghost" onClick={() => setCustomerOpen((v) => !v)}>
                  <Ico name={customerOpen ? "down" : "arrow-r"} size={11} />
                  {customerOpen ? t("customer.collapse") : t("customer.expand")}
                </button>
              </div>
            </div>
            {customerOpen && <div className="card-b" style={{ display: "grid", gap: 8 }}>
              {data.customer_id ? (
                <div>
                  <Link
                    to={`/customers/${data.customer_id}`}
                    style={{ color: "var(--ink-1)", fontSize: 16, fontWeight: 600 }}
                  >
                    {data.customer_canonical}
                  </Link>
                  <div style={{ fontSize: 11.5, color: "var(--ink-3)", marginTop: 4 }}>
                    {t("customer.rawName")} <span style={{ fontFamily: "var(--mono)", color: "var(--ink-2)" }}>{data.customer_raw}</span>
                  </div>
                </div>
              ) : (
                <>
                  <div style={{ fontSize: 11.5, color: "var(--ink-3)" }}>
                    {t("customer.rawNameUnbound")}{" "}
                    <span style={{ fontFamily: "var(--mono)", color: "var(--ink-4)" }}>
                      {data.customer_raw ?? t("customer.noName")}
                    </span>
                    {candidates && candidates.candidates.length > 0 && t("customer.candidatesIntro")}
                  </div>
                  {candidates ? (
                    candidates.candidates.length > 0 ? (
                      <>
                        {candidates.candidates.map((c, i) => (
                          <button
                            key={c.id}
                            className={`cand-btn${i === 0 && candidates.decision === "matched" ? " suggest" : ""}`}
                            onClick={() => bindToCustomer(c.id)}
                            disabled={merging}
                          >
                            <span>
                              <span style={{ fontWeight: 600 }}>{c.canonical_name}</span>
                              <span style={{ fontSize: 11, color: "var(--ink-3)" }}>{t("customer.caseCountSuffix", { n: c.case_count })}</span>
                            </span>
                            <span className="sim">
                              {c.similarity != null ? t("customer.similarity", { similarity: Math.round((c.similarity ?? 0) * 100) }) : t("customer.knownAlias")}
                            </span>
                          </button>
                        ))}
                        <div className="divider" />
                      </>
                    ) : (
                      <div
                        style={{
                          fontSize: 11.5,
                          color: "var(--amber-ink)",
                          fontStyle: "italic",
                          padding: "6px 0",
                        }}
                      >
                        {candidates.suggestion}
                      </div>
                    )
                  ) : (
                    <div className="muted" style={{ fontSize: 12 }}>{t("customer.resolving")}</div>
                  )}
                  <Link
                    to={`/dict?prefill=${encodeURIComponent(candidates?.normalized ?? data.customer_raw ?? "")}`}
                    className="btn sm"
                    style={{ justifyContent: "center" }}
                  >
                    <Ico name="plus" size={11} />
                    {t("customer.createAndBind")}
                  </Link>
                </>
              )}
            </div>}
          </div>

          {/* Diagnostics */}
          {data.blocking_issues.length > 0 && (() => {
            const blocks = data.blocking_issues.filter((i) => (i.severity ?? "block") === "block");
            const warns = data.blocking_issues.filter((i) => i.severity === "warn");
            const autoCodes = new Set(data.auto_blocking_issues.map((i) => i.code));
            const renderIssue = (issue: typeof data.blocking_issues[number], i: number) => {
              const isManual = data.manual_blocking_codes.includes(issue.code);
              const isAuto = autoCodes.has(issue.code);
              const isBlock = (issue.severity ?? "block") === "block";
              const accent = isManual
                ? "var(--amber-ink)"
                : isBlock
                  ? "var(--err)"
                  : "var(--amber-ink)";
              const bg = isManual
                ? "var(--amber-50)"
                : isBlock
                  ? "var(--err-50)"
                  : "var(--amber-50)";
              const border = isManual
                ? "var(--amber-200)"
                : isBlock
                  ? "var(--err-100)"
                  : "var(--amber-200)";
              return (
                <div
                  key={`${issue.code}-${i}`}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "auto 1fr auto",
                    gap: 8,
                    padding: 8,
                    background: bg,
                    border: `1px solid ${border}`,
                    borderRadius: 6,
                  }}
                >
                  <Ico
                    name={isBlock ? "alert" : "dot"}
                    size={14}
                    style={{ color: accent, flexShrink: 0, marginTop: 2 }}
                  />
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 12.5, fontWeight: 500, color: accent }}>
                      {issue.zh}
                    </div>
                    <div
                      style={{
                        fontFamily: "var(--mono)",
                        fontSize: 10.5,
                        color: "var(--ink-3)",
                        marginTop: 1,
                      }}
                    >
                      {issue.code} · {t("diagnostics.suggestionPrefix")}{issue.next}
                    </div>
                    {issue.files && issue.files.length > 0 && (
                      <div
                        style={{
                          marginTop: 6,
                          display: "flex",
                          flexWrap: "wrap",
                          gap: 4,
                          alignItems: "center",
                        }}
                      >
                        <span
                          style={{
                            fontSize: 10.5,
                            color: "var(--ink-3)",
                            marginRight: 2,
                          }}
                        >
                          {t("diagnostics.affectedFiles")}
                        </span>
                        {issue.files.slice(0, 6).map((fn) => (
                          <button
                            key={fn}
                            type="button"
                            onClick={() => {
                              const sel = `[data-source-file="${CSS.escape(fn)}"]`;
                              const el = document.querySelector<HTMLElement>(sel);
                              if (el) {
                                el.scrollIntoView({ behavior: "smooth", block: "center" });
                                el.classList.add("flash-highlight");
                                setTimeout(() => el.classList.remove("flash-highlight"), 1500);
                              }
                            }}
                            className="chip"
                            style={{
                              fontFamily: "var(--mono)",
                              fontSize: 10.5,
                              cursor: "pointer",
                              border: "1px solid var(--line)",
                              padding: "2px 6px",
                            }}
                            title={t("diagnostics.highlightTooltip")}
                          >
                            {fn}
                          </button>
                        ))}
                        {issue.files.length > 6 && (
                          <span style={{ fontSize: 10.5, color: "var(--ink-4)" }}>
                            … +{issue.files.length - 6}
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                  <span
                    className="badge"
                    style={{
                      background: isManual ? "var(--amber-100)" : "var(--cyan-50)",
                      borderColor: isManual ? "var(--amber-200)" : "var(--cyan-200)",
                      color: isManual ? "var(--amber-ink)" : "var(--cyan-ink)",
                      height: 18,
                      alignSelf: "start",
                    }}
                  >
                    {isManual && isAuto
                      ? t("diagnostics.autoManual")
                      : isManual
                        ? t("diagnostics.manual")
                        : t("diagnostics.auto")}
                  </span>
                </div>
              );
            };
            return (
              <div className="card" style={{ borderColor: blocks.length > 0 ? "var(--err-100)" : "var(--amber-200)" }}>
                <div
                  className="card-h"
                  style={{
                    background: blocks.length > 0 ? "var(--err-50)" : "var(--amber-50)",
                    borderBottom: `1px solid ${blocks.length > 0 ? "var(--err-100)" : "var(--amber-200)"}`,
                  }}
                >
                  <div className="t" style={{ color: blocks.length > 0 ? "var(--err)" : "var(--amber-ink)" }}>
                    <Ico name="alert" size={13} />
                    {t("diagnostics.cardTitle")}
                    {blocks.length > 0 && <span>{t("diagnostics.blockingCount", { count: blocks.length })}</span>}
                    {warns.length > 0 && <span>{t("diagnostics.warningCount", { count: warns.length })}</span>}
                  </div>
                  <button type="button" className="btn sm ghost" onClick={() => setDiagnosticsOpen((v) => !v)}>
                    <Ico name={diagnosticsOpen ? "down" : "arrow-r"} size={11} />
                    {diagnosticsOpen ? t("diagnostics.collapse") : t("diagnostics.expand")}
                  </button>
                </div>
                <div className="card-b" style={{ display: "grid", gap: 8 }}>
                  <div
                    style={{
                      display: "grid",
                      gap: 4,
                      padding: "8px 10px",
                      border: "1px solid var(--line-2)",
                      borderRadius: 6,
                      background: "var(--panel-2)",
                      color: "var(--ink-3)",
                      fontSize: 11.5,
                    }}
                  >
                    <div>{t("diagnostics.scopeHint")}</div>
                    <div style={{ fontFamily: "var(--mono)", color: "var(--ink-4)" }}>
                      {t("diagnostics.caseDiagnosisSummary", {
                        auto: data.auto_blocking_issues.length,
                        manual: data.manual_blocking_codes.length,
                      })}
                      {data.latest_render_status && (
                        <span>
                          {" · "}
                          {t("diagnostics.latestRender", {
                            status: data.latest_render_status,
                            quality: data.latest_render_quality_status ?? "—",
                          })}
                        </span>
                      )}
                    </div>
                  </div>
                  {diagnosticsOpen && (
                    <>
                      {blocks.length > 0 && (
                        <>
                          {blocks.length > 0 && warns.length > 0 && (
                            <div
                              style={{
                                fontSize: 10.5,
                                color: "var(--err)",
                                textTransform: "uppercase",
                                letterSpacing: 0.4,
                              }}
                            >
                              {t("diagnostics.blockingSection", { count: blocks.length })}
                            </div>
                          )}
                          {blocks.map(renderIssue)}
                        </>
                      )}
                      {warns.length > 0 && (
                        <>
                          {blocks.length > 0 && warns.length > 0 && (
                            <div
                              style={{
                                fontSize: 10.5,
                                color: "var(--amber-ink)",
                                textTransform: "uppercase",
                                letterSpacing: 0.4,
                                marginTop: 4,
                              }}
                            >
                              {t("diagnostics.warningSection", { count: warns.length })}
                            </div>
                          )}
                          {warns.map(renderIssue)}
                        </>
                      )}
                    </>
                  )}
                </div>
              </div>
            );
          })()}

          {/* Rename suggestion */}
          {renameHint?.command && (
            <div className="card">
              <div className="card-h">
                <div className="t">
                  <Ico name="folder" size={13} style={{ color: "var(--ink-3)" }} />
                  {t("rename.cardTitle")}
                  <span
                    className="badge"
                    style={{
                      background: "var(--cyan-50)",
                      color: "var(--cyan-ink)",
                      borderColor: "var(--cyan-200)",
                      fontFamily: "var(--mono)",
                    }}
                  >
                    DRY-RUN
                  </span>
                </div>
                <button className="btn sm" onClick={() => copy(renameHint.command!)}>
                  <Ico name="copy" size={11} />
                  {copied ? t("rename.copied") : t("rename.copyAll")}
                </button>
              </div>
              <div className="card-b" style={{ padding: 0 }}>
                <div
                  style={{
                    padding: "8px 12px",
                    fontSize: 11.5,
                    color: "var(--ink-3)",
                    background: "var(--panel-2)",
                    borderBottom: "1px solid var(--line-2)",
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                  }}
                >
                  <Ico name="alert" size={11} style={{ color: "var(--cyan-ink)" }} />
                  <span>
                    {t("rename.affectsLabel")}{" "}
                    <span style={{ fontFamily: "var(--mono)", color: "var(--ink-1)", fontWeight: 500 }}>
                      {renameHint.affected_count}
                    </span>{" "}
                    {t("rename.disclaimerSuffix")}<span style={{ color: "var(--ok)" }}>{t("rename.wontModify")}</span>{t("rename.templateOnly")}
                  </span>
                </div>
                <pre className="code" style={{ borderRadius: 0, margin: 0 }}>
                  <span className="cmt"># {renameHint.note}</span>
                  {"\n"}
                  {renameHint.command}
                </pre>
              </div>
            </div>
          )}
          {/* show customer label discreetly when not bound and no candidates */}
          {!data.customer_id && !candidates && (
            <div style={{ fontSize: 11, color: "var(--ink-4)", textAlign: "center" }}>
              {customerLabel}
            </div>
          )}
        </div>
      </div>
      <EvaluateDialog
        open={evaluateOpen}
        onClose={() => setEvaluateOpen(false)}
        subjectKind="case"
        subjectId={caseId}
        caseId={caseId}
        subjectSummary={`#${caseId} · ${data.customer_canonical || data.customer_raw || "—"} · ${data.category}`}
      />
      <RevisionsDrawer
        caseId={caseId}
        open={revisionsOpen}
        onClose={() => setRevisionsOpen(false)}
      />
      <RenderHistoryDrawer
        caseId={caseId}
        brand={brand}
        template="tri-compare"
        open={renderHistoryOpen}
        onClose={() => setRenderHistoryOpen(false)}
      />
      {overrideTarget && (
        <ImageOverridePopover
          caseId={caseId}
          filename={overrideTarget.filename}
          meta={skillMetaByFile.get(overrideTarget.filename)}
          anchorEl={overrideTarget.anchor}
          onClose={() => setOverrideTarget(null)}
        />
      )}
    </div>
  );
}
