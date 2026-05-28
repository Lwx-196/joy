import { expect, test } from "@playwright/test";

/**
 * C3.0.3 — Playwright integration spec for the alerting endpoints.
 *
 * No UI is wired yet (alerts surface as audit-log rows + channel side
 * effects), so the spec drives the API directly via `page.request` to
 * verify the synthetic SLO → fire → ack → resolve lifecycle end-to-end
 * against the real running backend. This complements the pytest
 * integration tests by exercising the FastAPI router from a separate
 * HTTP client (catches CORS / proxy / middleware regressions that the
 * in-process TestClient cannot).
 */

test("alerting endpoints: SLO synthetic fire → ack → resolve cycle", async ({
  page,
}) => {
  // 1. Fire a synthetic SLO alert (dispatch=false so we never page on-call
  //    from a smoke run; the audit row still lands).
  const fire = await page.request.post("/api/render/ops/alerts/fire", {
    data: {
      source: "slo_report",
      slo_report: {
        recommendation: "rollback",
        violations: [{ dimension: "comfyui_failure_rate", delta: 0.12 }],
        sample_size: 64,
        window_hours: 24,
        evidence: { promotion_state: "p10" },
      },
      reviewer: "playwright-smoke",
      reason: "C3.0.3 e2e smoke",
      dispatch: false,
    },
  });
  expect(fire.status()).toBe(200);
  const fireBody = await fire.json();
  expect(fireBody.alert_compiled).toBe(true);
  expect(fireBody.event.severity).toBe("critical");
  expect(fireBody.event.source).toBe("slo_report");
  expect(fireBody.event.detail.recommendation).toBe("rollback");
  expect(fireBody.event.detail.promotion_state).toBe("p10");
  const correlationId: string = fireBody.correlation_id;
  expect(correlationId).toMatch(/^alert-[a-f0-9]+$/);

  // 2. Ack the alert by correlation_id.
  const ack = await page.request.post(
    `/api/render/ops/alerts/${correlationId}/ack`,
    {
      data: { operator: "playwright-on-call", note: "smoke ack" },
    }
  );
  expect(ack.status()).toBe(200);
  const ackBody = await ack.json();
  expect(ackBody.stage).toBe("acked");
  expect(ackBody.correlation_id).toBe(correlationId);

  // 3. Resolve the alert.
  const resolve = await page.request.post(
    `/api/render/ops/alerts/${correlationId}/resolve`,
    {
      data: { operator: "playwright-on-call", note: "smoke resolve" },
    }
  );
  expect(resolve.status()).toBe(200);
  const resolveBody = await resolve.json();
  expect(resolveBody.stage).toBe("resolved");
  expect(resolveBody.correlation_id).toBe(correlationId);
});

test("alerting endpoints: applier source replays applier correlation_id", async ({
  page,
}) => {
  const fire = await page.request.post("/api/render/ops/alerts/fire", {
    data: {
      source: "rollback_applier",
      applier_result: {
        request_id: "rb-cid-e2e",
        reason: "rollback_applied",
        outcome: "rollback_completed",
        from_state: "p10",
        to_state: "rolled_back",
        recommendation: "rollback",
      },
      reviewer: "playwright-smoke",
      reason: "C3.0.3 applier replay",
      dispatch: false,
    },
  });
  expect(fire.status()).toBe(200);
  const body = await fire.json();
  expect(body.alert_compiled).toBe(true);
  expect(body.correlation_id).toBe("rb-cid-e2e");
  expect(body.event.source).toBe("rollback_applier");
  expect(body.event.severity).toBe("critical");
});

test("alerting endpoints: benign recommendation returns alert_compiled=false", async ({
  page,
}) => {
  const fire = await page.request.post("/api/render/ops/alerts/fire", {
    data: {
      source: "slo_report",
      slo_report: {
        recommendation: "continue",
        violations: [],
        sample_size: 200,
        window_hours: 24,
      },
      reviewer: "playwright-smoke",
      reason: "benign no-alert path",
      dispatch: false,
    },
  });
  expect(fire.status()).toBe(200);
  const body = await fire.json();
  expect(body.alert_compiled).toBe(false);
  expect(body.note).toMatch(/benign/i);
});

test("alerting endpoints: unknown source is rejected with 400", async ({
  page,
}) => {
  const fire = await page.request.post("/api/render/ops/alerts/fire", {
    data: {
      source: "random_source",
      reviewer: "playwright-smoke",
      dispatch: false,
    },
  });
  expect(fire.status()).toBe(400);
  const body = await fire.json();
  expect(body.detail.error).toMatch(/source must be one of/);
});
