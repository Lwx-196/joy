import type { EnqueueRenderPayload } from "../../api";

export const compareTemplateFromTier = (value: string | null | undefined): string | null => {
  const text = String(value ?? "").trim();
  if (text === "single" || text === "single-compare") return "single-compare";
  if (text === "bi" || text === "bi-compare") return "bi-compare";
  if (text === "tri" || text === "tri-compare") return "tri-compare";
  return null;
};

export const compareTemplateFromRenderableSlotCount = (
  count: number | null | undefined,
): string | null => {
  const value = Number(count ?? 0);
  if (!Number.isFinite(value) || value <= 0) return null;
  if (value === 1) return "single-compare";
  if (value === 2) return "bi-compare";
  return "tri-compare";
};

export const resolveFreshAiRenderTemplate = (
  effectiveTemplate: string | null | undefined,
  renderableSlotCount: number | null | undefined,
  latestJobTemplate: string | null | undefined,
): string => (
  // effectiveTemplate = 后端 preflight effective_template_hint（manual_template_tier
  // 优先于槽位数推断的契约在后端已生效）；槽位数只在无 tier 时兜底，否则手选
  // 模板会被（可能来自 stale job 计数的）槽位推断顶掉，与标准出图按钮漂移。
  compareTemplateFromTier(effectiveTemplate) ??
  compareTemplateFromRenderableSlotCount(renderableSlotCount) ??
  compareTemplateFromTier(latestJobTemplate) ??
  "tri-compare"
);

const positiveFiniteNumber = (value: number | null | undefined): number | null => {
  const numberValue = Number(value ?? 0);
  return Number.isFinite(numberValue) && numberValue > 0 ? numberValue : null;
};

export const resolveFreshAiSlotCount = (
  renderableSlotCount: number | null | undefined,
  renderSelectionSlotCount: number | null | undefined,
  cacheMissTotal: number | null | undefined,
  generatedArtifactCount: number | null | undefined,
): number => (
  positiveFiniteNumber(renderableSlotCount) ??
  positiveFiniteNumber(renderSelectionSlotCount) ??
  positiveFiniteNumber(cacheMissTotal) ??
  positiveFiniteNumber(generatedArtifactCount) ??
  1
);

export function buildCaseDetailRenderPayload(
  brand: string,
  effectiveTemplate: string | null | undefined,
  force: boolean,
): EnqueueRenderPayload {
  return {
    brand,
    template: compareTemplateFromTier(effectiveTemplate) ?? "tri-compare",
    semantic_judge: "auto",
    ...(force ? { force: true } : {}),
  };
}
