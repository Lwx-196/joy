import type { SkillImageMetadata } from "../../api";

export type SourceRole = "pre" | "post" | "unl";
export type SourceViewKey = NonNullable<SkillImageMetadata["view_bucket"]> | "unknown";
export type SourceRoleFilter = SourceRole | "all" | "needs" | "manual";
export type SourceViewFilter = SourceViewKey | "all";
export type SourceGroupFilter = "all" | "needs" | "missing_phase" | "missing_view" | "bound" | "excluded" | "missing_file";
export type BulkPhaseAction = "before" | "after" | "clear";
export type BulkViewAction = "front" | "oblique" | "side" | "clear";

export const SOURCE_VIEW_ORDER: SourceViewKey[] = ["front", "oblique", "side", "unknown"];
