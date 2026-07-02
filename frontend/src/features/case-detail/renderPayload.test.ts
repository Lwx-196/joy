import { describe, expect, it } from "vitest";

import {
  buildCaseDetailRenderPayload,
  compareTemplateFromRenderableSlotCount,
  compareTemplateFromTier,
  resolveFreshAiRenderTemplate,
  resolveFreshAiSlotCount,
} from "./renderPayload";

describe("case detail render payload", () => {
  it("uses the effective single template without disabling AI enhancement", () => {
    expect(buildCaseDetailRenderPayload("fumei", "single-compare", false)).toEqual({
      brand: "fumei",
      template: "single-compare",
      semantic_judge: "auto",
    });
  });

  it("preserves bi template and force flag", () => {
    expect(buildCaseDetailRenderPayload("fumei", "bi-compare", true)).toEqual({
      brand: "fumei",
      template: "bi-compare",
      semantic_judge: "auto",
      force: true,
    });
  });

  it("normalizes tier aliases", () => {
    expect(compareTemplateFromTier("single")).toBe("single-compare");
    expect(compareTemplateFromTier("bi")).toBe("bi-compare");
    expect(compareTemplateFromTier("tri")).toBe("tri-compare");
    expect(compareTemplateFromTier(null)).toBeNull();
  });

  it("falls back to the strongest publishable template from renderable slot count", () => {
    expect(compareTemplateFromRenderableSlotCount(1)).toBe("single-compare");
    expect(compareTemplateFromRenderableSlotCount(2)).toBe("bi-compare");
    expect(compareTemplateFromRenderableSlotCount(3)).toBe("tri-compare");
    expect(compareTemplateFromRenderableSlotCount(4)).toBe("tri-compare");
    expect(compareTemplateFromRenderableSlotCount(0)).toBeNull();
  });

  it("honors effective template tier over slot inference (backend manual_template_tier contract)", () => {
    // effectiveTemplate 来自活的 preflight effective_template_hint，后端已按
    // manual_template_tier > 槽位推断定序；fresh-AI 按钮不得用槽位数反超手选模板。
    expect(resolveFreshAiRenderTemplate("single-compare", 2, "single-compare")).toBe("single-compare");
    expect(resolveFreshAiRenderTemplate("bi-compare", 2, "single-compare")).toBe("bi-compare");
    expect(resolveFreshAiRenderTemplate("tri-compare", 2, "tri-compare")).toBe("tri-compare");
  });

  it("falls back to renderable slots then latest job template when no effective tier", () => {
    expect(resolveFreshAiRenderTemplate(null, 2, "single-compare")).toBe("bi-compare");
    expect(resolveFreshAiRenderTemplate(null, 0, "single-compare")).toBe("single-compare");
    expect(resolveFreshAiRenderTemplate(null, 0, null)).toBe("tri-compare");
  });

  it("uses current renderable slots for fresh AI cost instead of stale generated artifacts", () => {
    expect(resolveFreshAiSlotCount(2, 2, 0, 3)).toBe(2);
    expect(resolveFreshAiSlotCount(null, 2, 0, 3)).toBe(2);
    expect(resolveFreshAiSlotCount(null, null, 2, 3)).toBe(2);
    expect(resolveFreshAiSlotCount(null, null, null, 3)).toBe(3);
    expect(resolveFreshAiSlotCount(null, null, null, null)).toBe(1);
  });
});
