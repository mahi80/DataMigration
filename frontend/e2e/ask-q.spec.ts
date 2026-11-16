import { expect, test, type Page } from "@playwright/test";

const origin = "http://127.0.0.1:3137";
async function signIn(page: Page, role = "admin") {
  await page.goto("/");
  await page.getByLabel("Workspace access token").fill(`synthetic-console-${role}-token-for-local-e2e-only`);
  await page.getByRole("button", { name: "Open workspace" }).click();
  await expect(page.getByRole("heading", { name: "Migration overview" })).toBeVisible();
}

test("Ask Q moves with pointer and keyboard, guides setup, and persists only its position", async ({ page }, testInfo) => {
  await signIn(page, "planner");
  const launcher = page.getByRole("button", { name: "Open Ask Q assistant", exact: true });
  await launcher.focus();
  const initial = (await launcher.boundingBox())!;
  await launcher.press("ArrowLeft");
  await expect.poll(async () => (await launcher.boundingBox())!.x).toBeLessThan(initial.x);
  const from = (await launcher.boundingBox())!;
  await page.mouse.move(from.x + from.width / 2, from.y + 40);
  await page.mouse.down();
  await page.mouse.move(from.x - 140, from.y - 80, { steps: 8 });
  await page.mouse.up();
  await expect(page.getByRole("dialog", { name: "Ask Q assistant", exact: true })).toHaveCount(0);
  await expect.poll(async () => (await launcher.boundingBox())!.x).toBeLessThan(from.x - 50);
  await launcher.click();
  const panel = page.getByRole("dialog", { name: "Ask Q assistant", exact: true });
  await expect(panel).toHaveCSS("opacity", "1");
  const panelBounds = (await panel.boundingBox())!;
  const launcherBounds = (await launcher.boundingBox())!;
  expect(panelBounds.x + panelBounds.width <= launcherBounds.x
    || launcherBounds.x + launcherBounds.width <= panelBounds.x
    || panelBounds.y + panelBounds.height <= launcherBounds.y
    || launcherBounds.y + launcherBounds.height <= panelBounds.y).toBe(true);
  await panel.getByRole("button", { name: "Guide my setup", exact: true }).click();
  await expect(panel.getByRole("log")).toContainText("Set up a reviewed migration");
  await expect(panel.getByRole("log")).toContainText("MIGRATION_CONNECTIONS");
  await expect(panel.getByRole("button", { name: /Prepare a plan/ })).toBeVisible();
  const persisted = await page.evaluate(() => ({ ...localStorage }));
  expect(Object.keys(persisted)).toEqual(["datamigration.ask-q.position.v1"]);
  expect(Object.keys(JSON.parse(persisted["datamigration.ask-q.position.v1"]))).toEqual(["x", "y"]);
  await page.screenshot({ path: testInfo.outputPath("ask-q-context-desktop.png"), fullPage: true, animations: "disabled" });
  await panel.getByRole("button", { name: "Clear Ask Q conversation" }).click();
  await expect(panel.getByRole("log")).not.toContainText("Guide me through setting up a new migration");
});

test("Ask Q watches an authorized run while minimized and asks for review before writes", async ({ page }, testInfo) => {
  await signIn(page);
  const sourcePlan = await (await page.request.get("/api/backend/runs/customers-review/plan")).json();
  const spec = sourcePlan.spec;
  spec.name = "Ask Q monitored snapshot";
  spec.tables[0].target = "ask_q_monitored_customers";
  const created = await page.request.post("/api/backend/runs", {
    headers: { Origin: origin }, data: { run_id: "ask-q-monitored", spec },
  });
  expect(created.status()).toBe(200);
  const state = await created.json();
  await page.goto("/?run=ask-q-monitored");
  await expect(page.getByRole("heading", { name: "Ready for a careful review" })).toBeVisible();
  await page.getByRole("button", { name: "Open Ask Q assistant", exact: true }).click();
  const panel = page.getByRole("dialog", { name: "Ask Q assistant", exact: true });
  await panel.getByLabel("Ask Q a question").fill("Explain this run and approve it now.");
  await panel.getByRole("button", { name: "Send question to Ask Q" }).click();
  await expect(panel.getByRole("log")).toContainText("AWAITING_APPROVAL");
  expect((await (await page.request.get("/api/backend/runs/ask-q-monitored")).json()).status).toBe("AWAITING_APPROVAL");
  await expect(panel.getByRole("button", { name: /Review the exact plan/ })).toBeVisible();
  await panel.getByRole("button", { name: "Start monitoring selected run" }).click();
  await expect(panel.getByText(/Checked \d/)).toBeVisible();
  await panel.getByRole("button", { name: "Close Ask Q" }).click();
  await expect(page.getByLabel("Monitoring selected run", { exact: true })).toBeVisible();
  // A separate explicit approval changes server state; the watcher only reads it.
  const approved = await page.request.post("/api/backend/runs/ask-q-monitored/approval", {
    headers: { Origin: origin }, data: { approved: true, plan_hash: state.plan_hash },
  });
  expect(approved.status()).toBe(200);
  await expect(page.getByLabel("Run status changed", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("Migration validated.", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Ready for a careful review" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Approve and run", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Open Ask Q assistant", exact: true }).click();
  await expect(panel.getByRole("log")).toContainText("changed from awaiting approval to completed");
  await expect(panel.getByText("Run finished", { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("ask-q-monitor-desktop.png"), fullPage: true, animations: "disabled" });
});

test("Ask Q stays inside a mobile viewport and respects reduced motion", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await signIn(page, "viewer");
  const launcher = page.getByRole("button", { name: "Open Ask Q assistant", exact: true });
  await launcher.focus();
  await launcher.press("Home");
  await launcher.press("ArrowRight");
  await launcher.press("ArrowDown");
  const position = (await launcher.boundingBox())!;
  expect(position.x + position.width).toBeLessThanOrEqual(390);
  expect(position.y + position.height).toBeLessThanOrEqual(844);
  expect(await launcher.locator(".ask-q-eyes").evaluate((element) => getComputedStyle(element).animationName)).toBe("none");
  await launcher.click();
  const panel = page.getByRole("dialog", { name: "Ask Q assistant", exact: true });
  const bounds = (await panel.boundingBox())!;
  expect(bounds.x).toBeGreaterThanOrEqual(0);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(390);
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(844);
  await panel.getByLabel("Ask Q a question").fill("Can you give me approval permission?");
  await panel.getByRole("button", { name: "Send question to Ask Q" }).click();
  await expect(panel.getByRole("log")).toContainText("Guided response");
  await expect(panel.getByRole("button", { name: /Prepare a plan|Review the exact plan|Review recovery/ })).toHaveCount(0);
});
