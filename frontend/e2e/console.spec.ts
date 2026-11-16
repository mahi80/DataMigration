import { expect, test, type Page } from "@playwright/test";

const token = (role: string) => `synthetic-console-${role}-token-for-local-e2e-only`;
const origin = "http://127.0.0.1:3137";

async function expectNoPageOverflow(page: Page) {
  const layout = await page.evaluate(() => ({
    width: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
    overflowing: [...document.querySelectorAll("body *")].flatMap((element) => {
      const bounds = element.getBoundingClientRect(), style = getComputedStyle(element);
      if (!bounds.width || style.display === "none" || style.visibility === "hidden" || bounds.right <= innerWidth + 1) return [];
      return [{ tag: element.tagName, class: element.getAttribute("class"), width: Math.round(bounds.width), right: Math.round(bounds.right), overflowX: style.overflowX, parent: element.parentElement?.getAttribute("class") }];
    }).slice(0, 30),
  }));
  expect(layout.width, JSON.stringify(layout, null, 2)).toBeLessThanOrEqual(layout.viewport);
}

async function signIn(page: Page, role: string) {
  await page.goto("/");
  await page.getByLabel("Workspace access token").fill(token(role));
  await page.getByRole("button", { name: "Open workspace" }).click();
  await expect(page.getByRole("heading", { name: "Migration overview" })).toBeVisible();
}

test("a planner maps actual data and a separate approver validates the result", async ({ page }, testInfo) => {
  await signIn(page, "planner");
  await page.screenshot({ path: testInfo.outputPath("overview-desktop.png"), fullPage: true });
  await page.getByRole("button", { name: "New migration", exact: true }).click();
  await page.getByLabel("Migration name", { exact: true }).fill("Customer transfer through console");
  await page.getByLabel("Source connection", { exact: true }).selectOption("demo-source");
  await page.getByLabel("Target connection", { exact: true }).selectOption("demo-target");
  await page.getByRole("button", { name: "Discover tables", exact: true }).click();
  await page.getByRole("checkbox", { name: "customers", exact: true }).check();
  await page.getByRole("button", { name: "Continue to mapping" }).click();
  await page.getByLabel("Target table for customers", { exact: true }).fill("customers_console");
  await page.getByLabel("Target column for customers.id", { exact: true }).fill("customer_id");
  await expect(page.getByLabel("Use id as key in customers", { exact: true })).toBeChecked();
  await page.getByLabel("Transform for customers.email", { exact: true }).selectOption("lower");
  await page.getByRole("button", { name: "Add check", exact: true }).click();
  await page.getByLabel("Quality column 1 for customers", { exact: true }).selectOption("name");
  await page.getByLabel("Quality check 1 for customers", { exact: true }).selectOption("non_blank");
  await page.getByLabel("Quality correction 1 for customers", { exact: true }).selectOption("trim");
  await page.screenshot({ path: testInfo.outputPath("mapping-desktop.png"), fullPage: true });
  await page.getByRole("button", { name: "Review plan", exact: true }).click();
  const create = page.getByRole("button", { name: "Create reviewable plan" });
  await expect(create).toBeDisabled();
  await page.getByRole("checkbox", { name: /My source is ready for a consistent snapshot/ }).check();
  await create.click();
  await expect(page.getByRole("heading", { name: "Customer transfer through console", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Ready for a careful review" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve and run", exact: true })).toHaveCount(0);

  const runId = new URL(page.url()).searchParams.get("run")!;
  const planResponse = await page.request.get(`/api/backend/runs/${runId}/plan`);
  expect(planResponse.ok()).toBeTruthy();
  const plan = await planResponse.json();
  expect(plan.spec.tables[0].keys).toEqual(["customer_id"]);
  expect(plan.spec.tables[0].quality_rules[0].correction).toBe("trim");
  const denied = await page.request.post(`/api/backend/runs/${runId}/approval`, {
    headers: { Origin: origin }, data: { approved: true, plan_hash: plan.plan_hash },
  });
  expect(denied.status()).toBe(403);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await signIn(page, "approver");
  await page.goto(`/?run=${runId}`);
  await expect(page.getByRole("heading", { name: "Ready for a careful review" })).toBeVisible();
  await page.getByRole("tab", { name: "Mappings & lineage" }).click();
  await expect(page.getByRole("heading", { name: "Follow every mapped column" })).toBeVisible();
  await expect(page.getByText("customer_id", { exact: true })).toBeVisible();
  await page.getByRole("checkbox", { name: /I have reviewed the source/ }).check();
  await page.getByRole("button", { name: "Approve and run", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText(plan.plan_hash);
  await page.getByRole("button", { name: "Confirm approval", exact: true }).click();
  await expect(page.getByText("Migration validated.", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Validation", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Mapped data reconciled" })).toBeVisible();
  const result = await (await page.request.get(`/api/backend/runs/${runId}`)).json();
  expect(result.status).toBe("COMPLETED");
  expect(result.validation.passed).toBe(true);
  expect(result.validation.tables[0]).toMatchObject({ expected_rows: 2, target_rows: 2, missing_keys: 0, mismatched_rows: 0 });
  const rules = Object.values(result.validation.tables[0].quality_rules) as Array<{ corrected: number }>;
  expect(rules[0].corrected).toBe(1);
  await page.screenshot({ path: testInfo.outputPath("validation-desktop.png"), fullPage: true });
});

test("viewer access is read only and the browser cannot read the session token", async ({ page, context }) => {
  await signIn(page, "viewer");
  await expect(page.getByRole("button", { name: "New migration", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Connections", exact: true })).toHaveCount(0);
  expect((await page.request.get("/api/backend/connections")).status()).toBe(403);
  expect((await page.request.get("/api/backend/runs/orders-validated/plan")).status()).toBe(403);
  const cookies = await context.cookies();
  const cookie = cookies.find((item) => item.name === "migration_console_session")!;
  expect(cookie.httpOnly).toBe(true);
  expect(cookie.sameSite).toBe("Lax");
  expect(cookie.value).not.toContain(token("viewer"));
  const browserStorage = await page.evaluate(() => ({ cookie: document.cookie, local: { ...localStorage }, session: { ...sessionStorage } }));
  expect(JSON.stringify(browserStorage)).not.toContain(token("viewer"));
  expect(browserStorage.cookie).not.toContain("migration_console_session");
  await page.goto("/?run=orders-validated");
  await page.getByRole("tab", { name: "Validation", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Mapped data reconciled" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Plan JSON", exact: true })).toHaveCount(0);
  expect((await page.request.post("/api/backend/runs/price-policy/retry", { headers: { Origin: origin }, data: {} })).status()).toBe(403);
});

test("rejection requires a deliberate confirmation and never executes the plan", async ({ page }) => {
  await signIn(page, "approver");
  await page.goto("/?run=products-review");
  await page.getByRole("button", { name: "Reject plan", exact: true }).click();
  await page.getByRole("dialog").press("Escape");
  expect((await (await page.request.get("/api/backend/runs/products-review")).json()).status).toBe("AWAITING_APPROVAL");
  await page.getByRole("button", { name: "Reject plan", exact: true }).click();
  await page.getByRole("button", { name: "Confirm rejection", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect((await (await page.request.get("/api/backend/runs/products-review")).json()).status).toBe("REJECTED");
  await expect(page.getByRole("button", { name: "Approve and run", exact: true })).toHaveCount(0);
});

test("mobile navigation and failure filters stay usable without page overflow", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page, "admin");
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("overview-mobile.png"), fullPage: true });
  await page.getByRole("button", { name: "Open navigation", exact: true }).click();
  await page.getByRole("button", { name: "Migrations", exact: false }).filter({ hasText: "Migrations" }).click();
  await page.getByLabel("Filter status", { exact: true }).selectOption("FAILED");
  await page.getByLabel("Search migrations").fill("Price policy");
  await page.getByRole("button", { name: "Price policy check", exact: true }).click();
  await expect(page.getByText("The run needs attention.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry run", exact: true })).toBeVisible();
  await expectNoPageOverflow(page);
});

test("cross-origin requests and invalid identities cannot establish or mutate a session", async ({ page }) => {
  const crossOrigin = await page.request.post("/api/session", { headers: { Origin: "https://untrusted.invalid" }, data: { token: token("admin") } });
  expect(crossOrigin.status()).toBe(403);
  const invalid = await page.request.post("/api/session", { headers: { Origin: origin }, data: { token: "synthetic-invalid-workspace-token-only" } });
  expect(invalid.status()).toBe(401);
  await signIn(page, "admin");
  const denied = await page.request.post("/api/backend/runs/customers-review/approval", {
    headers: { Origin: "https://untrusted.invalid" }, data: { approved: true, plan_hash: "0".repeat(64) },
  });
  expect(denied.status()).toBe(403);
  expect((await (await page.request.get("/api/backend/runs/customers-review")).json()).status).toBe("AWAITING_APPROVAL");
});
