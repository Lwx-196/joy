import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { useTranslation } from "react-i18next";
import {
  caseFileUrl,
  manualRenderPreviewFileUrl,
  simulationJobFileUrl,
  type FocusRegion,
  type ManualRenderImageInput,
  type ManualRenderPreviewResponse,
  type ManualTransform,
  type ManualRenderView,
  type SimulateAfterPayload,
  type SimulationJob,
} from "../api";
import {
  useCaseSimulationJobs,
  usePrepareManualRenderSources,
  usePreviewManualRender,
  useReviewSimulationJob,
  useRenderCase,
  usePsImageModelOptions,
  useSimulateCaseAfter,
} from "../hooks/queries";
import { Ico } from "./atoms";

type SlotKind = "before" | "after";
type SlotMode = "existing" | "upload";
type RenderTemplateChoice = "tri-compare" | "bi-compare" | "single-compare";

interface SlotState {
  mode: SlotMode;
  filename: string;
  file: File | null;
  previewUrl: string | null;
}

type ViewSlots = Record<SlotKind, SlotState>;
type SlotsByView = Record<ManualRenderView, ViewSlots>;
type FocusRegionsByView = Record<ManualRenderView, FocusRegion[]>;
type TransformsByView = Record<ManualRenderView, ManualTransform>;
type PreviewsByView = Partial<Record<ManualRenderView, ManualRenderPreviewResponse>>;

interface DrawingRegion {
  view: ManualRenderView;
  startX: number;
  startY: number;
  region: FocusRegion;
}

interface Props {
  caseId: number;
  allImages: string[];
  brand: string;
  seedRequest?: ManualRenderSeedRequest | null;
}

export interface ManualRenderSeedImage {
  filename: string;
  phase: "before" | "after" | null;
  view: ManualRenderView | null;
}

export interface ManualRenderSeedRequest {
  nonce: number;
  images: ManualRenderSeedImage[];
}

const ACCEPT = "image/jpeg,image/png,image/webp,image/heic,image/bmp";
const VIEWS: ManualRenderView[] = ["front", "oblique", "side"];
const TEMPLATE_CHOICES: RenderTemplateChoice[] = ["tri-compare", "bi-compare", "single-compare"];

const emptySlot = (): SlotState => ({
  mode: "existing",
  filename: "",
  file: null,
  previewUrl: null,
});

const emptyViewSlots = (): ViewSlots => ({
  before: emptySlot(),
  after: emptySlot(),
});

const emptySlotsByView = (): SlotsByView => ({
  front: emptyViewSlots(),
  oblique: emptyViewSlots(),
  side: emptyViewSlots(),
});

const emptyFocusRegionsByView = (): FocusRegionsByView => ({
  front: [],
  oblique: [],
  side: [],
});

const emptyTransform = (): ManualTransform => ({
  offset_x_pct: 0,
  offset_y_pct: 0,
  scale: 1,
});

const emptyTransformsByView = (): TransformsByView => ({
  front: emptyTransform(),
  oblique: emptyTransform(),
  side: emptyTransform(),
});

const clamp01 = (value: number) => Math.max(0, Math.min(1, value));
const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));

const isIdentityTransform = (transform: ManualTransform): boolean =>
  Math.abs(transform.offset_x_pct) < 0.0005 &&
  Math.abs(transform.offset_y_pct) < 0.0005 &&
  Math.abs(transform.scale - 1) < 0.0005;

const templateLimit = (template: RenderTemplateChoice): number =>
  template === "tri-compare" ? 3 : template === "bi-compare" ? 2 : 1;

const normalizeRenderViews = (
  template: RenderTemplateChoice,
  selected: ManualRenderView[],
  activeView: ManualRenderView,
): ManualRenderView[] => {
  if (template === "tri-compare") return VIEWS;
  const limit = templateLimit(template);
  const unique = Array.from(new Set([activeView, ...selected])).filter((v): v is ManualRenderView =>
    VIEWS.includes(v as ManualRenderView),
  );
  for (const candidate of VIEWS) {
    if (unique.length >= limit) break;
    if (!unique.includes(candidate)) unique.push(candidate);
  }
  return unique.slice(0, limit);
};

const transformStyle = (transform: ManualTransform) => ({
  transform: `translate(${transform.offset_x_pct * 100}%, ${transform.offset_y_pct * 100}%) scale(${transform.scale})`,
});

const regionFromPoints = (startX: number, startY: number, endX: number, endY: number): FocusRegion => {
  const x1 = clamp01(Math.min(startX, endX));
  const y1 = clamp01(Math.min(startY, endY));
  const x2 = clamp01(Math.max(startX, endX));
  const y2 = clamp01(Math.max(startY, endY));
  return {
    x: Number(x1.toFixed(4)),
    y: Number(y1.toFixed(4)),
    width: Number((x2 - x1).toFixed(4)),
    height: Number((y2 - y1).toFixed(4)),
  };
};

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("failed to read file"));
    reader.readAsDataURL(file);
  });
}

function slotReady(slot: SlotState): boolean {
  if (slot.mode === "existing") return !!slot.filename;
  return !!slot.file;
}

function simulationStatusText(status: string): string {
  if (status === "done") return "完成";
  if (status === "done_with_issues") return "有问题";
  if (status === "failed") return "失败";
  if (status === "running") return "生成中";
  if (status === "blocked") return "阻塞";
  return status;
}

function reviewText(job: SimulationJob): string {
  if (job.review_status === "approved") return "已审核可用";
  if (job.review_status === "needs_recheck") return "需复核";
  if (job.review_status === "rejected") return "已打回";
  return "待审核";
}

function simulationModelText(job: SimulationJob): string {
  const model = typeof job.model_plan.model_name === "string" ? job.model_plan.model_name.trim() : "";
  const provider = typeof job.model_plan.provider === "string" ? job.model_plan.provider.trim() : "";
  return model || provider || "ps_model_router";
}

function simulationDifference(job: SimulationJob): { full: number; nonTarget: number } | null {
  const diff = job.audit.difference_analysis;
  if (!diff || typeof diff !== "object") return null;
  const item = diff as Record<string, unknown>;
  const full = Number(item.full_frame_change_score);
  const nonTarget = Number(item.non_target_change_score);
  if (!Number.isFinite(full) || !Number.isFinite(nonTarget)) return null;
  return { full, nonTarget };
}

function simulationFile(job: SimulationJob, kind: string) {
  const canonical = kind === "comparison" ? "controlled_policy_comparison" : kind;
  return job.available_files?.find((file) => file.kind === canonical) ?? null;
}

function simulationDecisionTone(job: SimulationJob): string {
  const decision = job.review_decision;
  if (decision?.severity === "ok" || decision?.recommended_verdict === "approved") return "var(--ok)";
  if (decision?.severity === "block" || decision?.recommended_verdict === "rejected") return "var(--err)";
  return "var(--amber-ink)";
}

function simulationDecisionReasons(job: SimulationJob): string[] {
  const decision = job.review_decision;
  return [
    ...(decision?.blocking_reasons ?? []),
    ...(decision?.warning_reasons ?? []),
    ...(decision?.passing_reasons ?? []),
  ].filter(Boolean);
}

function SimulationThumb({ href, src, alt }: { href: string; src: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  return (
    <a href={href} target="_blank" rel="noreferrer" style={{ width: "100%", height: "100%", display: "grid", placeItems: "center" }}>
      {failed ? (
        <span style={{ display: "grid", justifyItems: "center", gap: 4, fontSize: 10.5, color: "var(--err)" }}>
          <Ico name="image" size={18} />
          加载失败
        </span>
      ) : (
        <img
          src={src}
          alt={alt}
          loading="lazy"
          onError={() => setFailed(true)}
          style={{ width: "100%", height: "100%", objectFit: "contain" }}
        />
      )}
    </a>
  );
}

function SimulationFileLinks({ caseId, job }: { caseId: number; job: SimulationJob }) {
  const specs = [
    { kind: "original_after", label: "原术后" },
    { kind: "ai_after_simulation", label: "增强图" },
    { kind: "difference_heatmap", label: "热区" },
    { kind: "controlled_policy_comparison", label: "对比" },
  ];
  const files = specs.filter((spec) => simulationFile(job, spec.kind));
  if (files.length === 0) {
    return <div style={{ fontSize: 11, color: "var(--err)" }}>输出文件不可见，请检查任务目录</div>;
  }
  return (
    <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
      {files.map((file) => (
        <a
          key={file.kind}
          className="badge"
          href={simulationJobFileUrl(caseId, job.id, file.kind)}
          target="_blank"
          rel="noreferrer"
          style={{ textDecoration: "none" }}
        >
          {file.label}
        </a>
      ))}
    </div>
  );
}

export function ManualRenderPicker({ caseId, allImages, brand, seedRequest }: Props) {
  const { t } = useTranslation("caseDetail");
  const prepareMut = usePrepareManualRenderSources();
  const previewMut = usePreviewManualRender();
  const renderMut = useRenderCase();
  const simulateMut = useSimulateCaseAfter();
  const simulationsQ = useCaseSimulationJobs(caseId, 6);
  const modelOptionsQ = usePsImageModelOptions();
  const reviewMut = useReviewSimulationJob();
  const previewUrlsRef = useRef<Set<string>>(new Set());
  const lastSeedNonceRef = useRef<number | null>(null);
  const [slotsByView, setSlotsByView] = useState<SlotsByView>(() => emptySlotsByView());
  const [focusRegionsByView, setFocusRegionsByView] = useState<FocusRegionsByView>(() => emptyFocusRegionsByView());
  const [transformsByView, setTransformsByView] = useState<TransformsByView>(() => emptyTransformsByView());
  const [previewsByView, setPreviewsByView] = useState<PreviewsByView>({});
  const [drawingRegion, setDrawingRegion] = useState<DrawingRegion | null>(null);
  const [view, setView] = useState<ManualRenderView>("front");
  const [renderTemplate, setRenderTemplate] = useState<RenderTemplateChoice>("tri-compare");
  const [selectedRenderViews, setSelectedRenderViews] = useState<ManualRenderView[]>(() => VIEWS);
  const [dragOver, setDragOver] = useState<SlotKind | null>(null);
  const [focusText, setFocusText] = useState("");
  const [modelName, setModelName] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const imageOptions = useMemo(() => [...allImages].sort(), [allImages]);
  const imageNameSet = useMemo(() => new Set(imageOptions), [imageOptions]);
  const modelChoices = useMemo(() => modelOptionsQ.data?.options ?? [], [modelOptionsQ.data?.options]);
  const selectedModelName = modelChoices.some((option) => option.value === modelName)
    ? modelName
    : modelChoices[0]?.value ?? "";
  const effectiveSlotsByView = useMemo(() => {
    const next: SlotsByView = emptySlotsByView();
    for (const targetView of VIEWS) {
      for (const kind of ["before", "after"] as SlotKind[]) {
        const slot = slotsByView[targetView][kind];
        next[targetView][kind] =
          slot.mode === "existing" && slot.filename && !imageNameSet.has(slot.filename)
            ? emptySlot()
            : slot;
      }
    }
    return next;
  }, [imageNameSet, slotsByView]);
  const focusTargets = useMemo(
    () => focusText.split(/[，,;\n]/).map((x) => x.trim()).filter(Boolean),
    [focusText],
  );
  const activeSlots = effectiveSlotsByView[view];
  const activeTransform = transformsByView[view];
  const activeTransformChanged = !isIdentityTransform(activeTransform);
  const activePreview = previewsByView[view];
  const activeFocusRegions = slotReady(activeSlots.after) ? focusRegionsByView[view] : [];
  const readyViews = useMemo(
    () => VIEWS.filter((v) => slotReady(effectiveSlotsByView[v].before) && slotReady(effectiveSlotsByView[v].after)),
    [effectiveSlotsByView],
  );
  const selectedRenderViewSet = useMemo(() => new Set(selectedRenderViews), [selectedRenderViews]);
  const missingSelectedViews = useMemo(
    () => selectedRenderViews.filter((v) => !readyViews.includes(v)),
    [readyViews, selectedRenderViews],
  );
  const orderedReadyViews = useMemo(
    () => (selectedRenderViews.includes(view) ? [view, ...selectedRenderViews.filter((v) => v !== view)] : selectedRenderViews),
    [selectedRenderViews, view],
  );
  const busy = prepareMut.isPending || previewMut.isPending || renderMut.isPending || simulateMut.isPending;
  const canSubmit = selectedRenderViews.length > 0 && missingSelectedViews.length === 0 && !busy;
  const canPreview = slotReady(activeSlots.before) && slotReady(activeSlots.after) && !busy;
  const hasSimulationTarget = activeFocusRegions.length > 0;
  const simulationInputProblem = useMemo(() => {
    if (!slotReady(activeSlots.after)) return t("manualRender.simAfterRequired");
    if (modelOptionsQ.isError && modelChoices.length === 0) return t("manualRender.modelLoadFailed");
    if (!selectedModelName) {
      return modelOptionsQ.isLoading ? t("manualRender.modelLoading") : t("manualRender.modelRequired");
    }
    return null;
  }, [activeSlots.after, modelChoices.length, modelOptionsQ.isError, modelOptionsQ.isLoading, selectedModelName, t]);
  const canSimulate = !simulationInputProblem && !busy;

  useEffect(() => {
    const previewUrls = previewUrlsRef.current;
    return () => {
      for (const url of previewUrls) URL.revokeObjectURL(url);
      previewUrls.clear();
    };
  }, []);

  const viewText = (v: ManualRenderView): string =>
    v === "front" ? t("manualRender.viewFront") : v === "oblique" ? t("manualRender.viewOblique") : t("manualRender.viewSide");

  const templateText = (template: RenderTemplateChoice): string =>
    template === "tri-compare"
      ? t("manualRender.templateTri")
      : template === "bi-compare"
        ? t("manualRender.templateBi")
        : t("manualRender.templateSingle");

  const changeRenderTemplate = (next: RenderTemplateChoice) => {
    setRenderTemplate(next);
    setSelectedRenderViews((prev) => normalizeRenderViews(next, prev, view));
  };

  const chooseView = (nextView: ManualRenderView) => {
    setView(nextView);
    setSelectedRenderViews((prev) => {
      if (renderTemplate === "tri-compare") return VIEWS;
      if (renderTemplate === "single-compare") return [nextView];
      if (prev.includes(nextView)) return prev;
      return normalizeRenderViews(renderTemplate, [...prev, nextView], nextView);
    });
  };

  const revokePreview = (url: string | null) => {
    if (!url) return;
    URL.revokeObjectURL(url);
    previewUrlsRef.current.delete(url);
  };

  const updateSlot = (targetView: ManualRenderView, kind: SlotKind, patch: Partial<SlotState>) => {
    setSlotsByView((prev) => ({
      ...prev,
      [targetView]: {
        ...prev[targetView],
        [kind]: { ...prev[targetView][kind], ...patch },
      },
    }));
  };

  const updateTransform = (targetView: ManualRenderView, patch: Partial<ManualTransform>) => {
    setTransformsByView((prev) => ({
      ...prev,
      [targetView]: {
        ...prev[targetView],
        ...patch,
      },
    }));
    setPreviewsByView((prev) => ({ ...prev, [targetView]: undefined }));
  };

  const resetTransform = (targetView: ManualRenderView = view) => {
    setTransformsByView((prev) => ({ ...prev, [targetView]: emptyTransform() }));
    setPreviewsByView((prev) => ({ ...prev, [targetView]: undefined }));
  };

  const selectExisting = (
    kind: SlotKind,
    filename: string,
    targetView: ManualRenderView = view,
    options: { preserveTransform?: boolean } = {},
  ) => {
    const current = slotsByView[targetView][kind];
    revokePreview(current.previewUrl);
    updateSlot(targetView, kind, {
      mode: "existing",
      filename,
      file: null,
      previewUrl: null,
    });
    setPreviewsByView((prev) => ({ ...prev, [targetView]: undefined }));
    if (kind === "before" && !options.preserveTransform) resetTransform(targetView);
    if (kind === "after") setFocusRegionsByView((prev) => ({ ...prev, [targetView]: [] }));
  };

  useEffect(() => {
    if (!seedRequest || lastSeedNonceRef.current === seedRequest.nonce) return;
    lastSeedNonceRef.current = seedRequest.nonce;
    const timer = window.setTimeout(() => {
    setMessage(null);
    setError(null);

    const replacements: Partial<Record<ManualRenderView, Partial<Record<SlotKind, string>>>> = {};
    let skipped = 0;
    for (const image of seedRequest.images) {
      if (!imageNameSet.has(image.filename) || !image.phase || !image.view) {
        skipped += 1;
        continue;
      }
      const kind: SlotKind = image.phase === "before" ? "before" : "after";
      const byView = replacements[image.view] ?? {};
      if (byView[kind]) {
        skipped += 1;
        continue;
      }
      replacements[image.view] = { ...byView, [kind]: image.filename };
    }

    const changedViews = new Set<ManualRenderView>();
    const previewUrlsToRevoke: (string | null)[] = [];
    let appliedSlots = 0;
    for (const targetView of VIEWS) {
      const byView = replacements[targetView];
      if (!byView) continue;
      for (const kind of ["before", "after"] as SlotKind[]) {
        if (!byView[kind]) continue;
        previewUrlsToRevoke.push(slotsByView[targetView][kind].previewUrl);
        appliedSlots += 1;
        changedViews.add(targetView);
      }
    }
    for (const url of previewUrlsToRevoke) revokePreview(url);

    setSlotsByView((prev) => {
      const next: SlotsByView = {
        front: {
          before: { ...prev.front.before },
          after: { ...prev.front.after },
        },
        oblique: {
          before: { ...prev.oblique.before },
          after: { ...prev.oblique.after },
        },
        side: {
          before: { ...prev.side.before },
          after: { ...prev.side.after },
        },
      };
      for (const targetView of VIEWS) {
        const byView = replacements[targetView];
        if (!byView) continue;
        for (const kind of ["before", "after"] as SlotKind[]) {
          const filename = byView[kind];
          if (!filename) continue;
          next[targetView][kind] = {
            mode: "existing",
            filename,
            file: null,
            previewUrl: null,
          };
        }
      }
      return next;
    });

    if (changedViews.size > 0) {
      setPreviewsByView((prev) => {
        const next = { ...prev };
        for (const targetView of changedViews) next[targetView] = undefined;
        return next;
      });
      setTransformsByView((prev) => {
        const next: TransformsByView = {
          front: { ...prev.front },
          oblique: { ...prev.oblique },
          side: { ...prev.side },
        };
        for (const targetView of changedViews) {
          if (replacements[targetView]?.before) next[targetView] = emptyTransform();
        }
        return next;
      });
      setFocusRegionsByView((prev) => {
        const next: FocusRegionsByView = {
          front: [...prev.front],
          oblique: [...prev.oblique],
          side: [...prev.side],
        };
        for (const targetView of changedViews) {
          if (replacements[targetView]?.after) next[targetView] = [];
        }
        return next;
      });
    }

    const mergedSlots: SlotsByView = {
      front: {
        before: { ...slotsByView.front.before },
        after: { ...slotsByView.front.after },
      },
      oblique: {
        before: { ...slotsByView.oblique.before },
        after: { ...slotsByView.oblique.after },
      },
      side: {
        before: { ...slotsByView.side.before },
        after: { ...slotsByView.side.after },
      },
    };
    for (const targetView of VIEWS) {
      const byView = replacements[targetView];
      if (!byView) continue;
      if (byView.before) mergedSlots[targetView].before = { mode: "existing", filename: byView.before, file: null, previewUrl: null };
      if (byView.after) mergedSlots[targetView].after = { mode: "existing", filename: byView.after, file: null, previewUrl: null };
    }
    const completeViews = VIEWS.filter((targetView) => (
      slotReady(mergedSlots[targetView].before) && slotReady(mergedSlots[targetView].after)
    ));
    if (completeViews.length > 0) {
      const nextTemplate: RenderTemplateChoice =
        completeViews.length >= 3 ? "tri-compare" : completeViews.length === 2 ? "bi-compare" : "single-compare";
      const nextSelectedViews = nextTemplate === "tri-compare" ? VIEWS : completeViews.slice(0, templateLimit(nextTemplate));
      setRenderTemplate(nextTemplate);
      setSelectedRenderViews(nextSelectedViews);
      setView(nextSelectedViews[0]);
    }

    if (appliedSlots === 0) {
      setError(t("manualRender.seedNoUsable"));
    } else {
      setMessage(t("manualRender.seedApplied", {
        slots: appliedSlots,
        views: completeViews.length,
        skippedText: skipped > 0 ? t("manualRender.seedSkippedSuffix", { count: skipped }) : "",
      }));
    }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [imageNameSet, seedRequest, slotsByView, t]);

  const selectUpload = (kind: SlotKind, file: File | null, targetView: ManualRenderView = view) => {
    const current = slotsByView[targetView][kind];
    revokePreview(current.previewUrl);
    const previewUrl = file ? URL.createObjectURL(file) : null;
    if (previewUrl) previewUrlsRef.current.add(previewUrl);
    updateSlot(targetView, kind, {
      mode: "upload",
      file,
      filename: "",
      previewUrl,
    });
    setPreviewsByView((prev) => ({ ...prev, [targetView]: undefined }));
    if (kind === "before") resetTransform(targetView);
    if (kind === "after") setFocusRegionsByView((prev) => ({ ...prev, [targetView]: [] }));
  };

  const clearSlot = (kind: SlotKind, targetView: ManualRenderView = view) => {
    const current = slotsByView[targetView][kind];
    revokePreview(current.previewUrl);
    updateSlot(targetView, kind, emptySlot());
    setPreviewsByView((prev) => ({ ...prev, [targetView]: undefined }));
    if (kind === "before") resetTransform(targetView);
    if (kind === "after") setFocusRegionsByView((prev) => ({ ...prev, [targetView]: [] }));
  };

  const pointFromPointer = (e: ReactPointerEvent<HTMLElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    return {
      x: clamp01((e.clientX - rect.left) / Math.max(rect.width, 1)),
      y: clamp01((e.clientY - rect.top) / Math.max(rect.height, 1)),
    };
  };

  const startFocusRegion = (e: ReactPointerEvent<HTMLElement>, src: string | null) => {
    if (!src || busy) return;
    e.preventDefault();
    e.stopPropagation();
    const point = pointFromPointer(e);
    e.currentTarget.setPointerCapture(e.pointerId);
    setDrawingRegion({
      view,
      startX: point.x,
      startY: point.y,
      region: regionFromPoints(point.x, point.y, point.x, point.y),
    });
  };

  const moveFocusRegion = (e: ReactPointerEvent<HTMLElement>) => {
    if (!drawingRegion || drawingRegion.view !== view) return;
    e.preventDefault();
    e.stopPropagation();
    const point = pointFromPointer(e);
    setDrawingRegion((current) => {
      if (!current || current.view !== view) return current;
      return {
        ...current,
        region: regionFromPoints(current.startX, current.startY, point.x, point.y),
      };
    });
  };

  const finishFocusRegion = (e: ReactPointerEvent<HTMLElement>) => {
    if (!drawingRegion || drawingRegion.view !== view) return;
    e.preventDefault();
    e.stopPropagation();
    const region = drawingRegion.region;
    setDrawingRegion(null);
    if (region.width < 0.035 || region.height < 0.035) {
      return;
    }
    setFocusRegionsByView((prev) => ({
      ...prev,
      [view]: [
        ...prev[view],
        {
          ...region,
          label: focusTargets[prev[view].length] || focusTargets.join("；") || null,
        },
      ],
    }));
  };

  const clearFocusRegions = () => {
    setDrawingRegion(null);
    setFocusRegionsByView((prev) => ({ ...prev, [view]: [] }));
  };

  const removeFocusRegion = (index: number) => {
    setFocusRegionsByView((prev) => ({
      ...prev,
      [view]: prev[view].filter((_, i) => i !== index),
    }));
  };

  const toInput = async (slot: SlotState): Promise<ManualRenderImageInput> => {
    if (slot.mode === "existing") {
      if (!slot.filename) throw new Error(t("manualRender.missingExisting"));
      return { kind: "existing", filename: slot.filename };
    }
    if (!slot.file) throw new Error(t("manualRender.missingUpload"));
    return {
      kind: "upload",
      upload_name: slot.file.name,
      data_url: await readFileAsDataUrl(slot.file),
    };
  };

  const savePair = async (shouldRender: boolean) => {
    setMessage(null);
    setError(null);
    try {
      if (selectedRenderViews.length === 0) throw new Error(t("manualRender.missing"));
      if (missingSelectedViews.length > 0) {
        throw new Error(t("manualRender.selectedViewsMissing", { views: missingSelectedViews.map(viewText).join(" / ") }));
      }
      const createdFiles: string[] = [];
      for (const targetView of orderedReadyViews) {
        const slots = effectiveSlotsByView[targetView];
        const result = await prepareMut.mutateAsync({
          caseId,
          payload: {
            before: await toInput(slots.before),
            after: await toInput(slots.after),
            view: targetView,
            before_transform: isIdentityTransform(transformsByView[targetView])
              ? null
              : transformsByView[targetView],
          },
        });
        createdFiles.push(...result.created_files);
        const [beforeName, afterName] = result.created_files;
        if (beforeName) selectExisting("before", beforeName, targetView, { preserveTransform: true });
        if (afterName) selectExisting("after", afterName, targetView);
      }
      if (shouldRender) {
        await renderMut.mutateAsync({
          caseId,
          payload: { brand, template: renderTemplate, semantic_judge: "off" },
        });
        setMessage(t("manualRender.enqueued", { files: createdFiles.join(" / ") }));
      } else {
        setMessage(t("manualRender.saved", { files: createdFiles.join(" / ") }));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const previewCurrentPair = async () => {
    setMessage(null);
    setError(null);
    try {
      if (!slotReady(activeSlots.before) || !slotReady(activeSlots.after)) {
        throw new Error(t("manualRender.missing"));
      }
      const result = await previewMut.mutateAsync({
        caseId,
        payload: {
          before: await toInput(activeSlots.before),
          after: await toInput(activeSlots.after),
          view,
          brand,
          before_transform: isIdentityTransform(activeTransform) ? null : activeTransform,
        },
      });
      setPreviewsByView((prev) => ({ ...prev, [view]: result }));
      setMessage(t("manualRender.previewDone"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const buildSimulationInputs = async (): Promise<
    Pick<SimulateAfterPayload, "after_image_path" | "after_image" | "before_image_path" | "before_image">
  > => {
    const slots = effectiveSlotsByView[view];
    if (!slotReady(slots.after)) {
      throw new Error(t("manualRender.simAfterRequired"));
    }
    const afterInput = await toInput(slots.after);
    const beforeInput = slotReady(slots.before) ? await toInput(slots.before) : null;
    const inputPayload: Pick<
      SimulateAfterPayload,
      "after_image_path" | "after_image" | "before_image_path" | "before_image"
    > = {};
    if (afterInput.kind === "existing") {
      inputPayload.after_image_path = afterInput.filename;
    } else {
      inputPayload.after_image = afterInput;
    }
    if (beforeInput?.kind === "existing") {
      inputPayload.before_image_path = beforeInput.filename;
    } else if (beforeInput) {
      inputPayload.before_image = beforeInput;
    }
    return inputPayload;
  };

  const runSimulation = async () => {
    setMessage(null);
    setError(null);
    try {
      if (simulationInputProblem) throw new Error(simulationInputProblem);
      if (!window.confirm(t("manualRender.simConfirm"))) return;
      const simulationInputs = await buildSimulationInputs();
      const focusRegions = activeFocusRegions.map((region, index) => ({
        ...region,
        label: region.label || focusTargets[index] || focusTargets.join("；") || null,
      }));
      const result = await simulateMut.mutateAsync({
        caseId,
        payload: {
          ...simulationInputs,
          focus_targets: focusTargets,
          focus_regions: focusRegions,
          ai_generation_authorized: true,
          provider: "ps_model_router",
          model_name: selectedModelName.trim(),
          note: "由案例详情页人工整理与出图面板创建",
        },
      });
      if (result.status === "failed") {
        setError(result.error_message || t("manualRender.simFailed"));
      } else {
        setMessage(t("manualRender.simDone", { id: result.simulation_job_id, status: result.status }));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const reviewSimulation = async (job: SimulationJob, verdict: "approved" | "needs_recheck" | "rejected") => {
    const reviewer = window.prompt(t("manualRender.reviewReviewerPrompt"), "doctor");
    if (!reviewer) return;
    const note = window.prompt(t("manualRender.reviewNotePrompt"), "") ?? "";
    try {
      await reviewMut.mutateAsync({
        caseId,
        jobId: job.id,
        payload: { verdict, reviewer, note },
      });
      setMessage(t("manualRender.reviewDone"));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const previewSrc = (slot: SlotState) => {
    if (slot.mode === "existing" && slot.filename) return caseFileUrl(caseId, slot.filename);
    return slot.previewUrl;
  };

  const focusRegionText = (region: FocusRegion) =>
    t("manualRender.focusRegionSummary", {
      x: Math.round(region.x * 100),
      y: Math.round(region.y * 100),
      w: Math.round(region.width * 100),
      h: Math.round(region.height * 100),
    });

  const renderSlot = (kind: SlotKind, slot: SlotState) => {
    const label = kind === "before" ? t("manualRender.beforeLabel") : t("manualRender.afterLabel");
    const src = previewSrc(slot);
    const displayRegions =
      kind === "after"
        ? [
            ...activeFocusRegions.map((region, index) => ({ region, index, drawing: false })),
            ...(drawingRegion?.view === view
              ? [{ region: drawingRegion.region, index: activeFocusRegions.length, drawing: true }]
              : []),
          ]
        : [];
    return (
      <div
        className={`manual-image-slot${dragOver === kind ? " drag-over" : ""}${src ? " has-image" : ""}`}
        data-testid={`manual-slot-${kind}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(kind);
        }}
        onDragLeave={() => setDragOver((v) => (v === kind ? null : v))}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(null);
          const filename =
            e.dataTransfer.getData("application/x-case-image") ||
            e.dataTransfer.getData("text/plain");
          if (filename && imageNameSet.has(filename)) selectExisting(kind, filename);
        }}
      >
        <div className="manual-image-slot-title">
          <span>{label}</span>
          {slotReady(slot) && (
            <button
              type="button"
              className="manual-image-slot-clear"
              onClick={(e) => {
                e.stopPropagation();
                clearSlot(kind);
              }}
              disabled={busy}
              title={t("manualRender.clearSlot")}
            >
              <Ico name="x" size={11} />
            </button>
          )}
        </div>
        <div className="manual-image-slot-canvas">
          {src ? (
            <img
              src={src}
              alt={t("manualRender.previewAlt", { label })}
              style={kind === "before" ? transformStyle(activeTransform) : undefined}
            />
          ) : (
            <div className="manual-image-slot-empty">
              <Ico name="image" size={22} />
              <span>{t("manualRender.dropHint")}</span>
            </div>
          )}
          {kind === "after" && src && (
            <div
              className="manual-focus-layer"
              data-testid="manual-focus-layer"
              onPointerDown={(e) => startFocusRegion(e, src)}
              onPointerMove={moveFocusRegion}
              onPointerUp={finishFocusRegion}
              onPointerCancel={() => setDrawingRegion(null)}
              title={t("manualRender.focusRegionDrawHint")}
            >
              {displayRegions.map(({ region, index, drawing }) => region.width > 0 && region.height > 0 && (
                <span
                  key={`${index}-${drawing ? "drawing" : "saved"}`}
                  className={`manual-focus-box${drawing ? " drawing" : ""}`}
                  data-index={index + 1}
                  style={{
                    left: `${region.x * 100}%`,
                    top: `${region.y * 100}%`,
                    width: `${region.width * 100}%`,
                    height: `${region.height * 100}%`,
                  }}
                />
              ))}
            </div>
          )}
        </div>
        <div className="manual-image-slot-footer">
          <select
            value={slot.mode === "existing" ? slot.filename : ""}
            onChange={(e) => selectExisting(kind, e.target.value)}
            disabled={busy}
            aria-label={t("manualRender.sourceSelectLabel")}
          >
            <option value="">{t("manualRender.existingPlaceholder")}</option>
            {imageOptions.map((name) => (
              <option key={name} value={name}>{name}</option>
            ))}
          </select>
          <label className={`btn sm ghost manual-upload-btn${busy ? " disabled" : ""}`}>
            <Ico name="plus" size={11} />
            {t("manualRender.upload")}
            <input
              type="file"
              accept={ACCEPT}
              disabled={busy}
              onChange={(e) => selectUpload(kind, e.target.files?.[0] ?? null)}
            />
          </label>
        </div>
        <div className="manual-image-slot-name" title={slot.mode === "upload" ? slot.file?.name : slot.filename}>
          {slot.mode === "upload" && slot.file
            ? slot.file.name
            : slot.filename || t("manualRender.noImageSelected")}
        </div>
      </div>
    );
  };

  return (
    <div
      className="card"
      style={{ borderColor: "var(--cyan-200)", boxShadow: "0 0 0 3px rgba(8,145,178,.04)" }}
      data-testid="manual-render-picker"
    >
      <div
        className="card-h"
        style={{ background: "var(--cyan-50)", borderBottom: "1px solid var(--cyan-200)" }}
      >
        <div className="t" style={{ color: "var(--cyan-ink)" }}>
          <Ico name="image" size={13} />
          {t("manualRender.cardTitle")}
        </div>
        <div className="manual-template-control">
          <span>{t("manualRender.outputTypeLabel")}</span>
          <select
            value={renderTemplate}
            onChange={(e) => changeRenderTemplate(e.target.value as RenderTemplateChoice)}
            disabled={busy}
            aria-label={t("manualRender.outputTypeLabel")}
            title={t("manualRender.outputTypeHint")}
          >
            {TEMPLATE_CHOICES.map((template) => (
              <option key={template} value={template}>
                {templateText(template)}
              </option>
            ))}
          </select>
          <span className="badge" style={{ background: "#fff", color: "var(--cyan-ink)", borderColor: "var(--cyan-200)" }}>
            manual
          </span>
        </div>
      </div>
      <div className="card-b" style={{ display: "grid", gap: 12 }}>
        <div className="manual-template-help">
          {t("manualRender.outputTypeSummary", {
            type: templateText(renderTemplate),
            views: selectedRenderViews.map(viewText).join(" / "),
          })}
        </div>
        <div className="manual-view-tabs" aria-label={t("manualRender.viewLabel")}>
          {VIEWS.map((v) => {
            const complete = slotReady(slotsByView[v].before) && slotReady(slotsByView[v].after);
            const included = selectedRenderViewSet.has(v);
            return (
              <button
                key={v}
                type="button"
                className={`manual-view-tab${view === v ? " active" : ""}${complete ? " ready" : ""}${included ? " included" : ""}`}
                onClick={() => chooseView(v)}
                disabled={busy}
                title={
                  included
                    ? complete
                      ? t("manualRender.viewReady")
                      : t("manualRender.viewMissingForTemplate")
                    : t("manualRender.viewClickToInclude")
                }
              >
                <span>{viewText(v)}</span>
                <span className="manual-view-tab-dot" />
              </button>
            );
          })}
        </div>

        <div className="manual-slot-grid">
          {renderSlot("before", activeSlots.before)}
          {renderSlot("after", activeSlots.after)}
        </div>

        {slotReady(activeSlots.after) && (
          <div className="manual-focus-row manual-focus-row-wide">
            <div className="manual-focus-list">
              {activeFocusRegions.length > 0 ? (
                activeFocusRegions.map((region, index) => (
                  <button
                    type="button"
                    key={index}
                    className="manual-focus-chip"
                    onClick={() => removeFocusRegion(index)}
                    disabled={busy}
                    title={t("manualRender.focusRegionRemove")}
                  >
                    <span>{t("manualRender.focusRegionItem", { n: index + 1 })}</span>
                    <span>{focusRegionText(region)}</span>
                    <Ico name="x" size={9} />
                  </button>
                ))
              ) : (
                <span>{t("manualRender.focusRegionHint")}</span>
              )}
            </div>
            {activeFocusRegions.length > 0 && (
              <button type="button" className="btn sm ghost" onClick={clearFocusRegions} disabled={busy}>
                <Ico name="x" size={10} />
                {t("manualRender.focusRegionClearAll")}
              </button>
            )}
          </div>
        )}

        <div className={`manual-transform-panel${activeTransformChanged ? " changed" : ""}`}>
          <div className="manual-transform-head">
            <span>{t("manualRender.transformTitle")}</span>
            <button
              type="button"
              className="btn sm ghost"
              onClick={() => resetTransform()}
              disabled={busy || !activeTransformChanged}
            >
              <Ico name="refresh" size={10} />
              {t("manualRender.transformReset")}
            </button>
          </div>
          <div className="manual-transform-grid">
            <label>
              <span>{t("manualRender.transformX")}</span>
              <input
                type="range"
                min="-12"
                max="12"
                step="1"
                value={Math.round(activeTransform.offset_x_pct * 100)}
                disabled={busy || !slotReady(activeSlots.before)}
                onChange={(e) => updateTransform(view, { offset_x_pct: clamp(Number(e.target.value), -12, 12) / 100 })}
              />
              <strong>{Math.round(activeTransform.offset_x_pct * 100)}%</strong>
            </label>
            <label>
              <span>{t("manualRender.transformY")}</span>
              <input
                type="range"
                min="-12"
                max="12"
                step="1"
                value={Math.round(activeTransform.offset_y_pct * 100)}
                disabled={busy || !slotReady(activeSlots.before)}
                onChange={(e) => updateTransform(view, { offset_y_pct: clamp(Number(e.target.value), -12, 12) / 100 })}
              />
              <strong>{Math.round(activeTransform.offset_y_pct * 100)}%</strong>
            </label>
            <label>
              <span>{t("manualRender.transformScale")}</span>
              <input
                type="range"
                min="90"
                max="110"
                step="1"
                value={Math.round(activeTransform.scale * 100)}
                disabled={busy || !slotReady(activeSlots.before)}
                onChange={(e) => updateTransform(view, { scale: clamp(Number(e.target.value), 90, 110) / 100 })}
              />
              <strong>{Math.round(activeTransform.scale * 100)}%</strong>
            </label>
          </div>
          <div className="manual-transform-hint">
            {t("manualRender.transformHint")}
          </div>
        </div>

        <div className="manual-preview-panel" data-testid="manual-render-preview-panel">
          <div className="manual-preview-head">
            <span>{t("manualRender.previewTitle")}</span>
            <button
              type="button"
              className="btn sm"
              onClick={previewCurrentPair}
              disabled={!canPreview}
            >
              <Ico name="eye" size={11} />
              {previewMut.isPending ? t("manualRender.previewWorking") : t("manualRender.previewButton")}
            </button>
          </div>
          {activePreview ? (
            <a
              className="manual-preview-image"
              href={manualRenderPreviewFileUrl(caseId, activePreview.preview_id)}
              target="_blank"
              rel="noreferrer"
              title={t("manualRender.previewOpen")}
            >
              <img
                src={`${manualRenderPreviewFileUrl(caseId, activePreview.preview_id)}?t=${activePreview.preview_id}`}
                alt={t("manualRender.previewResultAlt")}
              />
            </a>
          ) : (
            <div className="manual-preview-empty">
              {t("manualRender.previewEmpty")}
            </div>
          )}
        </div>

        {selectedRenderViews.length > 0 && (
          <div style={{ fontSize: 11, color: "var(--ink-3)", minWidth: 0 }}>
            {missingSelectedViews.length > 0
              ? t("manualRender.missingSelectedSummary", { views: missingSelectedViews.map(viewText).join(" / ") })
              : t("manualRender.readySummary", {
                  type: templateText(renderTemplate),
                  views: orderedReadyViews.map(viewText).join(" / "),
                })}
          </div>
        )}
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", alignItems: "center" }}>
          <button
            type="button"
            className="btn sm"
            onClick={() => savePair(false)}
            disabled={!canSubmit}
            title={!canSubmit && missingSelectedViews.length > 0 ? t("manualRender.selectedViewsMissing", { views: missingSelectedViews.map(viewText).join(" / ") }) : undefined}
          >
            <Ico name="check" size={11} />
            {prepareMut.isPending ? t("manualRender.saving") : t("manualRender.saveOnly")}
          </button>
          <button
            type="button"
            className="btn sm primary"
            onClick={() => savePair(true)}
            disabled={!canSubmit}
            title={!canSubmit && missingSelectedViews.length > 0 ? t("manualRender.selectedViewsMissing", { views: missingSelectedViews.map(viewText).join(" / ") }) : undefined}
          >
            <Ico name="image" size={11} />
            {busy ? t("manualRender.working") : t("manualRender.saveAndRender")}
          </button>
        </div>

        <div className="divider" />
        <div style={{ display: "grid", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--ink-1)" }}>
              {t("manualRender.simTitle")}
            </div>
            <span
              className="badge"
              style={{ background: "var(--amber-50)", color: "var(--amber-ink)", borderColor: "var(--amber-200)" }}
            >
              PS model-router
            </span>
          </div>
          <input
            value={focusText}
            disabled={busy}
            onChange={(e) => setFocusText(e.target.value)}
            placeholder={t("manualRender.focusPlaceholder")}
            style={{ fontSize: 12 }}
          />
          <div
            className="manual-sim-hint"
            style={{ color: simulationInputProblem ? "var(--amber-ink)" : undefined }}
          >
            {simulationInputProblem ?? t("manualRender.focusRegionLocked", { count: activeFocusRegions.length })}
          </div>
          <div className="manual-sim-hint">
            {t("manualRender.simIsolated")}
          </div>
          <select
            value={selectedModelName}
            disabled={busy || modelOptionsQ.isLoading || modelChoices.length === 0}
            onChange={(e) => setModelName(e.target.value)}
            aria-label={t("manualRender.modelSelectLabel")}
            style={{ fontSize: 12 }}
          >
            {modelChoices.length === 0 && (
              <option value="">{t("manualRender.modelLoading")}</option>
            )}
            {modelChoices.map((option) => (
              <option key={option.value} value={option.value}>
                {option.is_default
                  ? t("manualRender.modelPrimary", { model: option.label })
                  : option.source === "fallback"
                    ? t("manualRender.modelFallback", { model: option.label })
                    : option.label}
              </option>
            ))}
          </select>
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <button
              type="button"
              className="btn sm amber"
              onClick={runSimulation}
              disabled={!canSimulate}
              title={simulationInputProblem ?? t("manualRender.simTitle")}
            >
              <Ico name="scan" size={11} />
              {simulateMut.isPending ? t("manualRender.simWorking") : t("manualRender.simButton")}
            </button>
          </div>
        </div>

        <div style={{ display: "grid", gap: 8 }} data-testid="simulation-jobs">
          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--ink-1)" }}>
            {t("manualRender.simHistoryTitle")}
          </div>
          {simulationsQ.isLoading && (
            <div style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{t("manualRender.simHistoryLoading")}</div>
          )}
          {!simulationsQ.isLoading && (simulationsQ.data?.length ?? 0) === 0 && (
            <div style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{t("manualRender.simHistoryEmpty")}</div>
          )}
          {simulationsQ.data?.map((job) => {
              const imageFile = simulationFile(job, "ai_after_simulation");
              const diff = simulationDifference(job);
              const decisionReasons = simulationDecisionReasons(job);
              const reviewTone =
                job.review_status === "approved"
                  ? "var(--ok)"
                  : job.review_status === "rejected"
                    ? "var(--err)"
                    : "var(--amber-ink)";
              return (
                <div
                  key={job.id}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "82px 1fr",
                    gap: 10,
                    padding: 8,
                    border: "1px solid var(--line)",
                    borderRadius: 6,
                    background: "#fff",
                  }}
                >
                  <div
                    style={{
                      width: 82,
                      aspectRatio: "1 / 1",
                      background: "var(--panel-2)",
                      border: "1px solid var(--line)",
                      borderRadius: 6,
                      overflow: "hidden",
                      display: "grid",
                      placeItems: "center",
                    }}
                  >
                    {imageFile ? (
                      <SimulationThumb
                        href={simulationJobFileUrl(caseId, job.id, "ai_after_simulation")}
                        src={simulationJobFileUrl(caseId, job.id, "ai_after_simulation")}
                        alt={t("manualRender.simPreviewAlt")}
                      />
                    ) : (
                      <span style={{ display: "grid", justifyItems: "center", gap: 4, fontSize: 10.5, color: "var(--err)" }}>
                        <Ico name="image" size={18} />
                        图不可见
                      </span>
                    )}
                  </div>
                  <div style={{ display: "grid", gap: 6, minWidth: 0 }}>
                    <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                      <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--ink-3)" }}>
                        #{job.id}
                      </span>
                      <span className="badge">{simulationStatusText(job.status)}</span>
                      <span className="badge" style={{ color: reviewTone }}>
                        {reviewText(job)}
                      </span>
                      <span className="badge">
                        {t("manualRender.modelCurrent", { model: simulationModelText(job) })}
                      </span>
                      <span className="badge" style={{ color: simulationDecisionTone(job) }}>
                        {job.review_decision?.label ?? "待系统建议"}
                      </span>
                      {diff && (
                        <span className="badge">
                          {t("manualRender.simChangeScore", { score: diff.full.toFixed(1), nonTarget: diff.nonTarget.toFixed(1) })}
                        </span>
                      )}
                      {job.watermarked && <span className="badge">watermark</span>}
                    </div>
                    <div
                      style={{
                        fontSize: 11.5,
                        color: "var(--ink-3)",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                      title={job.focus_targets.join("，")}
                    >
                      {job.focus_targets.join("，") || "—"}
                    </div>
                    <SimulationFileLinks caseId={caseId} job={job} />
                    {decisionReasons.length > 0 && (
                      <div style={{ fontSize: 11, color: "var(--ink-3)" }}>
                        {decisionReasons.slice(0, 2).join("；")}
                      </div>
                    )}
                    {job.error_message && (
                      <div style={{ fontSize: 11, color: "var(--err)" }}>{job.error_message}</div>
                    )}
                    {job.status !== "failed" && (
                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                        <button
                          type="button"
                          className="btn sm"
                          onClick={() => reviewSimulation(job, "approved")}
                          disabled={reviewMut.isPending || job.status === "running" || job.review_decision?.can_approve === false}
                          title={job.review_decision?.can_approve === false ? "存在硬性拒绝项，不能审核通过" : undefined}
                        >
                          {t("manualRender.reviewApprove")}
                        </button>
                        <button
                          type="button"
                          className="btn sm"
                          onClick={() => reviewSimulation(job, "needs_recheck")}
                          disabled={reviewMut.isPending || job.status === "running"}
                        >
                          {t("manualRender.reviewRecheck")}
                        </button>
                        <button
                          type="button"
                          className="btn sm danger"
                          onClick={() => reviewSimulation(job, "rejected")}
                          disabled={reviewMut.isPending || job.status === "running"}
                        >
                          {t("manualRender.reviewReject")}
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              );
          })}
        </div>

        {message && (
          <div style={{ fontSize: 11.5, color: "var(--ok)" }} data-testid="manual-render-message">
            {message}
          </div>
        )}
        {error && (
          <div style={{ fontSize: 11.5, color: "var(--err)" }} data-testid="manual-render-error">
            {t("manualRender.error")}: {error}
          </div>
        )}
      </div>
    </div>
  );
}
