import { describe, expect, it } from "vitest";
import type { WorkQueueSummary } from "../api";
import { lanesFromSummary } from "./work-queue";

const SUMMARY: WorkQueueSummary = {
  total: 171,
  lanes: {
    todayNew: { count: 3, case_ids: [30, 20, 10] },
    missingLabel: { count: 12 },
    blockingOpen: { count: 5 },
    unboundCustomer: { count: 7 },
    pendingReview: { count: 2, case_ids: [40, 5] },
  },
};

describe("lanesFromSummary", () => {
  it("maps server counts onto the five lanes in display order", () => {
    const lanes = lanesFromSummary(SUMMARY);
    expect(lanes.map((l) => l.key)).toEqual([
      "todayNew",
      "missingLabel",
      "blockingOpen",
      "unboundCustomer",
      "pendingReview",
    ]);
    expect(lanes.map((l) => l.count)).toEqual([3, 12, 5, 7, 2]);
    expect(lanes.every((l) => l.total === 171)).toBe(true);
    // 展示元数据(route/label/tone)仍由前端持有
    expect(lanes[0].route).toBe("/cases?since=today");
    expect(lanes[2].tone).toBe("err");
  });

  it("renders all-zero lanes while the summary is still loading", () => {
    const lanes = lanesFromSummary(undefined);
    expect(lanes).toHaveLength(5);
    expect(lanes.every((l) => l.count === 0 && l.total === 0)).toBe(true);
  });
});
