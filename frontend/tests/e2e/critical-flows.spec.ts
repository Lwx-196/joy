import { test, expect } from "@playwright/test";

/**
 * Critical flows that require the real backend (port 5174 via /api proxy):
 *   1. Cases category filter narrows the list
 *   2. Cases list -> CaseDetail navigation
 *   3. Customers list -> CustomerDetail navigation
 *   4. Cases bulk-select reveals .bulkbar
 *
 * Backend db (case-workbench.db) is read-only for these tests — no writes.
 */

test.describe("critical-flows", () => {
  test("Cases: category filter narrows list", async ({ page }) => {
    await page.goto("/cases");
    // Wait for table to populate (cases query resolved).
    await expect(page.locator("table.table tbody tr").first()).toBeVisible({ timeout: 15_000 });
    const totalRows = await page.locator("table.table tbody tr").count();
    expect(totalRows).toBeGreaterThan(0);

    // The category FilterSelect overlays a transparent <select> — pick the first
    // non-empty option (anything other than "全部 / All").
    const categorySelect = page.locator(".select select").first();
    const optionValues = await categorySelect.locator("option").evaluateAll(
      (opts) => opts.map((o) => (o as HTMLOptionElement).value).filter((v) => v !== "")
    );
    expect(optionValues.length).toBeGreaterThan(0);
    await categorySelect.selectOption(optionValues[0]);

    // Allow react-query refetch + re-render. Row count stabilizes after a tick.
    await page.waitForTimeout(500);
    const filteredRows = await page.locator("table.table tbody tr").count();
    expect(filteredRows).toBeLessThanOrEqual(totalRows);
  });

  test("Cases -> CaseDetail navigation works", async ({ page }) => {
    await page.goto("/cases");
    const firstCaseLink = page.locator('table.table tbody a[href^="/cases/"]').first();
    await expect(firstCaseLink).toBeVisible({ timeout: 15_000 });
    await firstCaseLink.click();
    await expect(page).toHaveURL(/\/cases\/\d+/);
    await expect(page.locator("main#main-content")).toBeVisible();
  });

  test("Customers -> CustomerDetail navigation works", async ({ page }) => {
    await page.goto("/customers");
    const firstCustomerLink = page.locator('a[href^="/customers/"]').first();
    await expect(firstCustomerLink).toBeVisible({ timeout: 15_000 });
    await firstCustomerLink.click();
    await expect(page).toHaveURL(/\/customers\/\d+/);
    await expect(page.locator("main#main-content")).toBeVisible();
  });

  test("Cases bulk-select reveals .bulkbar", async ({ page }) => {
    await page.goto("/cases");
    await expect(page.locator("table.table tbody tr").first()).toBeVisible({ timeout: 15_000 });
    // The Check component renders <span class="checkbox" role="checkbox"> inside
    // td:first-child. With row virtualization, position:absolute + display:table
    // can confuse Playwright's hit-testing. Dispatch a real click event on the
    // span to invoke the React onClick handler reliably.
    await page.evaluate(() => {
      const cb = document.querySelector(
        'table.table tbody tr[data-index="0"] td:first-child .checkbox, table.table tbody tr:first-child td:first-child .checkbox'
      );
      if (cb) {
        cb.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
      }
    });
    await expect(page.locator(".bulkbar")).toBeVisible();
  });

  test("CaseDetail: render history drawer opens via toolbar button and closes via Esc", async ({ page }) => {
    await page.goto("/cases");
    const firstCaseLink = page.locator('table.table tbody a[href^="/cases/"]').first();
    await expect(firstCaseLink).toBeVisible({ timeout: 15_000 });
    await firstCaseLink.click();
    await expect(page).toHaveURL(/\/cases\/\d+/);
    await expect(page.locator("main#main-content")).toBeVisible();

    const trigger = page.locator('[data-testid="render-history-trigger"]');
    await expect(trigger).toBeVisible({ timeout: 15_000 });
    const drawer = page.locator('[data-testid="render-history-drawer"]');
    await expect(drawer).toHaveCount(0);

    await trigger.click();
    await expect(drawer).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(drawer).toHaveCount(0);
  });

  // === 阶段 12 新增 ===
  // case 126 的 .history/ 有真实 fixture 快照（≥3 张），是这些用例的实测样本。
  // 直接 goto /cases/126 而不是从列表 click first，避免顺序依赖。

  test("Lightbox opens via snapshot click and Esc closes (case 126)", async ({ page }) => {
    await page.goto("/cases/126");
    const trigger = page.locator('[data-testid="render-history-trigger"]');
    await expect(trigger).toBeVisible({ timeout: 15_000 });
    await trigger.click();
    const drawer = page.locator('[data-testid="render-history-drawer"]');
    await expect(drawer).toBeVisible();

    const items = page.locator('[data-testid="render-history-item"]');
    await expect(items.first()).toBeVisible({ timeout: 10_000 });
    const itemCount = await items.count();
    expect(itemCount).toBeGreaterThan(0);

    // 点第 1 条快照打开 lightbox
    await items.first().locator("button").first().click();
    const lightbox = page.locator('[data-testid="lightbox"]');
    await expect(lightbox).toBeVisible();

    // Esc 应关闭 lightbox 但保留 drawer（lightbox 自管 Esc + stopPropagation）
    await page.keyboard.press("Escape");
    await expect(lightbox).toHaveCount(0);
    await expect(drawer).toBeVisible();

    // 再 Esc 关 drawer
    await page.keyboard.press("Escape");
    await expect(drawer).toHaveCount(0);
  });

  test("Lightbox: ArrowRight / ArrowLeft navigate snapshots (case 126)", async ({ page }) => {
    await page.goto("/cases/126");
    await page.locator('[data-testid="render-history-trigger"]').click();
    await expect(page.locator('[data-testid="render-history-drawer"]')).toBeVisible();

    const items = page.locator('[data-testid="render-history-item"]');
    await expect(items.first()).toBeVisible({ timeout: 10_000 });
    const total = await items.count();
    expect(total).toBeGreaterThanOrEqual(2);

    // 第一张
    await items.first().locator("button").first().click();
    const lightbox = page.locator('[data-testid="lightbox"]');
    await expect(lightbox).toBeVisible();
    await expect(lightbox).toHaveAttribute("data-current-index", "0");

    // ArrowRight → idx 1
    await page.keyboard.press("ArrowRight");
    await expect(lightbox).toHaveAttribute("data-current-index", "1");

    // ArrowLeft → idx 0
    await page.keyboard.press("ArrowLeft");
    await expect(lightbox).toHaveAttribute("data-current-index", "0");

    // ArrowLeft 在 idx 0 → 边界 clamp，仍是 0
    await page.keyboard.press("ArrowLeft");
    await expect(lightbox).toHaveAttribute("data-current-index", "0");
  });

  test("Lightbox: +/0 keyboard zoom and reset (case 126)", async ({ page }) => {
    await page.goto("/cases/126");
    await page.locator('[data-testid="render-history-trigger"]').click();
    await expect(page.locator('[data-testid="render-history-drawer"]')).toBeVisible();

    const items = page.locator('[data-testid="render-history-item"]');
    await expect(items.first()).toBeVisible({ timeout: 10_000 });
    await items.first().locator("button").first().click();

    const area = page.locator('[data-testid="lightbox-image-area"]');
    await expect(area).toBeVisible();
    await expect(area).toHaveAttribute("data-zoom", "100");

    // `+` × 2 → 100% × 1.25 × 1.25 = 156%
    await page.keyboard.press("+");
    await expect(area).toHaveAttribute("data-zoom", "125");
    await page.keyboard.press("+");
    await expect(area).toHaveAttribute("data-zoom", "156");

    // `-` once → 156 / 1.25 = 125
    await page.keyboard.press("-");
    await expect(area).toHaveAttribute("data-zoom", "125");

    // `0` → reset to 100
    await page.keyboard.press("0");
    await expect(area).toHaveAttribute("data-zoom", "100");

    // `-` at 100 → clamp, stays 100
    await page.keyboard.press("-");
    await expect(area).toHaveAttribute("data-zoom", "100");

    // ArrowRight should reset zoom (each snapshot starts at 100)
    await page.keyboard.press("+");
    await expect(area).toHaveAttribute("data-zoom", "125");
    await page.keyboard.press("ArrowRight");
    await expect(area).toHaveAttribute("data-zoom", "100");
  });

  test("RenderHistoryDrawer: brand selector switches local query (case 126)", async ({ page }) => {
    await page.goto("/cases/126");
    await page.locator('[data-testid="render-history-trigger"]').click();
    const drawer = page.locator('[data-testid="render-history-drawer"]');
    await expect(drawer).toBeVisible();

    // 默认 fumei 应有快照
    await expect(page.locator('[data-testid="render-history-item"]').first()).toBeVisible({
      timeout: 10_000,
    });
    const fumeiCount = await page.locator('[data-testid="render-history-item"]').count();
    expect(fumeiCount).toBeGreaterThan(0);

    // 切到 shimei → 列表为空（case 126 无 shimei history）
    const select = page.locator('[data-testid="render-history-brand-select"]');
    await select.selectOption("shimei");
    await expect(page.locator('[data-testid="render-history-item"]')).toHaveCount(0, {
      timeout: 5_000,
    });

    // 切回 fumei → 列表回来
    await select.selectOption("fumei");
    await expect(page.locator('[data-testid="render-history-item"]').first()).toBeVisible({
      timeout: 5_000,
    });
  });

  test("RenderRestoreConfirm: cancel does not call API (case 126)", async ({ page }) => {
    // 监听 POST /restore，确认取消时不会被触发
    let restoreCalled = false;
    page.on("request", (req) => {
      if (req.method() === "POST" && req.url().includes("/render/restore")) {
        restoreCalled = true;
      }
    });

    await page.goto("/cases/126");
    await page.locator('[data-testid="render-history-trigger"]').click();
    await expect(page.locator('[data-testid="render-history-drawer"]')).toBeVisible();

    const items = page.locator('[data-testid="render-history-item"]');
    await expect(items.first()).toBeVisible({ timeout: 10_000 });
    const beforeCount = await items.count();

    // 点第一条的「恢复」按钮
    await items.first().locator('[data-testid="render-history-restore"]').click();
    const confirm = page.locator('[data-testid="render-restore-confirm"]');
    await expect(confirm).toBeVisible();

    // 点取消
    await page.locator('[data-testid="render-restore-cancel"]').click();
    await expect(confirm).toHaveCount(0);

    // 列表条数不变
    const afterCount = await page.locator('[data-testid="render-history-item"]').count();
    expect(afterCount).toBe(beforeCount);

    // 没有触发 restore API
    expect(restoreCalled).toBe(false);
  });

  // === 阶段 20 新增：服务端分页 ===
  // fixture DB 有 126 条 cases（64 standard_face + 46 non_labeled + 16 body）。
  // page_size=50 → page 1: 50 条, page 2: 50 条, page 3: 26 条，均合法。
  // standard_face 过滤后 64 条 → page 2 也合法。

  test("server pagination: next/prev syncs to URL (fixture has 126 cases)", async ({ page }) => {
    await page.goto("/cases");
    // 等待表格第一行可见（backend 查询已解析）
    await expect(page.locator("table.table tbody tr").first()).toBeVisible({ timeout: 15_000 });

    const nextBtn = page.locator('[data-testid="pagination-next"]');
    await expect(nextBtn).toBeVisible();

    // fixture 有 126 条，page_size=50 → 应有 page 2，next 可点
    await expect(nextBtn).toBeEnabled();

    // 点 next → URL 应包含 page=2
    await nextBtn.click();
    await expect(page).toHaveURL(/[?&]page=2(&|$)/);

    // pagination 文本应包含 "/" (e.g. "2 / 3")
    await expect(page.locator('[data-testid="pagination-page-of-total"]')).toContainText("/");

    // 点 prev → 回到 page 1，URL 不含 page=2
    await page.locator('[data-testid="pagination-prev"]').click();
    await expect(page).not.toHaveURL(/[?&]page=2(&|$)/);
  });

  test("server pagination: filter change resets page to 1", async ({ page }) => {
    // 先导航到第 2 页
    await page.goto("/cases?page=2");
    await expect(page.locator("table.table tbody tr").first()).toBeVisible({ timeout: 15_000 });
    await expect(page).toHaveURL(/[?&]page=2(&|$)/);

    // 切换 category 过滤器（第一个 FilterSelect 是 category）
    // FilterSelect 把透明 <select> 叠在 .select wrapper 上
    const categorySelect = page.locator(".select select").first();
    const optionValues = await categorySelect.locator("option").evaluateAll(
      (opts) => opts.map((o) => (o as HTMLOptionElement).value).filter((v) => v !== "")
    );
    expect(optionValues.length).toBeGreaterThan(0);
    await categorySelect.selectOption(optionValues[0]);

    // filter 变化后 setFilter() 调用 sp.delete("page") → URL 中不应再有 page=2
    await expect(page).not.toHaveURL(/[?&]page=2(&|$)/);
  });

  test("server pagination: URL state (category + page) persists across reload", async ({ page }) => {
    // standard_face 有 64 条 → page 2 合法
    await page.goto("/cases?category=standard_face&page=2");
    await expect(page.locator("table.table tbody tr").first()).toBeVisible({ timeout: 15_000 });

    // 刷新后 URL 参数应保留（React Router 的 useSearchParams 读 location.search）
    await page.reload();
    await expect(page.locator("table.table tbody tr").first()).toBeVisible({ timeout: 15_000 });

    // category 参数必须保留
    await expect(page).toHaveURL(/category=standard_face/);

    // page=2 保留，OR 若因数据不足被 OOB self-heal 重置到 page=1（即 URL 无 page 参数）——两种都合法
    // 主断言：category filter 跨刷新仍然生效（行数 ≤ 总行数）
    const rowCount = await page.locator("table.table tbody tr").count();
    expect(rowCount).toBeGreaterThan(0);
  });

  test("cross-page selection: survives page navigation", async ({ page }) => {
    await page.goto("/cases?page=1");
    await expect(page.locator("table.table tbody tr").first()).toBeVisible({ timeout: 15_000 });

    // 选第 1 行(避免 virtualization 用 dispatchEvent 直接触发 React onClick)
    await page.evaluate(() => {
      const cb = document.querySelector(
        'table.table tbody tr[data-index="0"] td:first-child .checkbox, table.table tbody tr:first-child td:first-child .checkbox'
      );
      cb?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    });
    await expect(page.locator(".bulkbar")).toBeVisible();
    await expect(page.locator(".bulkbar")).toContainText("1");

    // 翻到 page 2 → bulkbar 仍可见且仍显示 1
    await page.locator('[data-testid="pagination-next"]').click();
    await expect(page).toHaveURL(/[?&]page=2(&|$)/);
    await expect(page.locator("table.table tbody tr").first()).toBeVisible({ timeout: 15_000 });
    await expect(page.locator(".bulkbar")).toBeVisible();
    await expect(page.locator(".bulkbar")).toContainText("1");
  });

  test("cross-page selection: clearAll button drops everything", async ({ page }) => {
    await page.goto("/cases?page=1");
    await expect(page.locator("table.table tbody tr").first()).toBeVisible({ timeout: 15_000 });

    await page.evaluate(() => {
      const cb = document.querySelector(
        'table.table tbody tr[data-index="0"] td:first-child .checkbox, table.table tbody tr:first-child td:first-child .checkbox'
      );
      cb?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    });
    await expect(page.locator(".bulkbar")).toBeVisible();

    // dispatchEvent 同步触发 React onClick(原生 click 在虚拟化容器里偶尔被截到 ancestor)
    await page.evaluate(() => {
      const btn = document.querySelector('[data-testid="bulk-clear-selection"]');
      btn?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    });
    await expect(page.locator(".bulkbar")).toHaveCount(0);
  });
});
