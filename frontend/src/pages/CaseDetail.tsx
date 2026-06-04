import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  caseFileUrl,
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
  Ico,
  ReviewPill,
} from "../components/atoms";
import { EvaluateDialog } from "../components/EvaluateDialog";
import { ImageOverridePopover } from "../components/ImageOverridePopover";
import { ManualRenderPicker, type ManualRenderSeedImage, type ManualRenderSeedRequest } from "../components/ManualRenderPicker";
import { RenderHistoryDrawer } from "../components/RenderHistoryDrawer";
import { RenderStatusCard } from "../components/RenderStatusCard";
import { RevisionsDrawer } from "../components/RevisionsDrawer";
import { DiagnosticsCard } from "../features/case-detail/DiagnosticsCard";
import { useCaseDetailDraft, useCustomerCandidates } from "../features/case-detail/hooks";
import { ManualEditCard } from "../features/case-detail/ManualEditCard";
import { SourceGroupPanel } from "../features/case-detail/SourceGroupPanel";
import { SourceImageThumb } from "../features/case-detail/SourceImageThumb";
import { SupplementCandidatesPanel } from "../features/case-detail/SupplementCandidatesPanel";
import {
  SOURCE_VIEW_ORDER,
  type BulkPhaseAction,
  type BulkViewAction,
  type SourceGroupFilter,
  type SourceRole,
  type SourceRoleFilter,
  type SourceViewFilter,
  type SourceViewKey,
} from "../features/case-detail/types";
import { useHotkey } from "../hooks/useHotkey";

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

  const [copied, setCopied] = useState(false);
  const [editing, setEditing] = useState(false);
  const candidates = useCustomerCandidates(data);
  const [draft, setDraft] = useCaseDetailDraft(data, editing);
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

  // Remember last visited case id for the dashboard "继续上次审核" affordance.
  useEffect(() => {
    if (caseId > 0) rememberCaseVisit(caseId);
  }, [caseId]);

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
  const renderSourceThumb = (name: string, role: SourceRole) => (
    <SourceImageThumb
      key={name}
      name={name}
      role={role}
      caseId={caseId}
      meta={skillMetaByFile.get(name)}
      view={sourceViewOf(name)}
      isManual={isManualSource(name)}
      needsManual={needsManualSource(name)}
      selected={selectedSourceImages.has(name)}
      trashPending={trashImageMut.isPending}
      onToggleSelection={toggleSourceSelection}
      onEdit={setOverrideTarget}
      onTrash={moveImageToTrash}
    />
  );

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
                <SupplementCandidatesPanel
                  isLoading={supplementQ.isLoading}
                  isError={supplementQ.isError}
                  gaps={supplementQ.data?.gaps ?? null}
                  message={supplementMessage}
                  isCopying={transferImageMut.isPending}
                  onCopyCandidate={copySupplementCandidate}
                />
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

          <SourceGroupPanel
            caseId={caseId}
            sourceGroup={sourceGroup}
            isLoading={sourceGroupQ.isLoading}
            isError={sourceGroupQ.isError}
            statusClass={sourceGroupStatusClass}
            missingSourceCount={sourceGroupMissingSourceCount}
            filter={sourceGroupFilter}
            filterItems={sourceGroupFilterItems}
            visibleImages={sourceGroupVisibleImages}
            selectedImages={selectedSourceGroupImages}
            selectedCount={selectedSourceGroupList.length}
            missingFiles={sourceGroupMissingFiles}
            allImages={allImages}
            actionBusy={sourceActionBusy}
            message={sourceGroupMessage}
            focusedSlot={focusedSourceGroupSlot}
            focusedIssueCode={focusedIssueCode}
            focusedIssueText={focusedIssueText}
            onFilterChange={setSourceGroupFilter}
            onSelectVisible={selectVisibleSourceGroupImages}
            onClearSelection={clearSourceGroupSelection}
            onBulkOverride={sourceGroupApplyBulkOverride}
            onReviewSelected={sourceGroupReviewSelected}
            onClearLock={sourceGroupClearLock}
            onAcceptWarning={sourceGroupAcceptWarning}
            onLockPair={sourceGroupLockPair}
            onToggleSelection={toggleSourceGroupSelection}
            onApplyOverride={sourceGroupApplyOverride}
            onReviewImage={sourceGroupReviewImage}
          />

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

          <ManualEditCard
            data={data}
            draft={draft}
            editing={editing}
            isOverridden={isOverridden}
            reviewKey={reviewKey}
            isHeldNow={isHeldNow}
            issueDict={issueDict}
            rescanning={rescanning}
            upgrading={upgrading}
            enqueueingRender={enqueueingRender}
            saving={saving}
            renderGateBlocked={renderGateBlocked}
            renderGateTitle={renderGateTitle}
            onDraftChange={setDraft}
            onToggleExtraBlocking={toggleExtraBlocking}
            onRescan={() => rescanMut.mutate(caseId)}
            onUpgrade={() => upgradeMut.mutate({ caseId, brand })}
            onRender={() =>
              renderMut.mutate({
                caseId,
                payload: { brand, template: "tri-compare", semantic_judge: "auto" },
              })
            }
            onSetEditing={setEditing}
            onClearOverrides={clearOverrides}
            onSaveEdits={saveEdits}
            onSetReview={setReview}
            onHoldCase={holdCase}
          />

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

          <DiagnosticsCard
            data={data}
            open={diagnosticsOpen}
            onToggle={() => setDiagnosticsOpen((value) => !value)}
          />

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
