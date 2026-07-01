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
  compareTemplateFromRenderableSlotCount(renderableSlotCount) ??
  compareTemplateFromTier(effectiveTemplate) ??
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
