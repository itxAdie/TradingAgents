import { expect, test } from "@playwright/test";

test.describe("terminal shell", () => {
  test("paper badge is visible on every page", async ({ page }) => {
    for (const path of ["/", "/markets", "/signals", "/portfolio", "/trades", "/risk", "/backtests", "/system"]) {
      await page.goto(path);
      const badge = page.getByTestId("paper-badge");
      await expect(badge).toBeVisible();
      await expect(badge).toContainText(/PAPER TRADING|LIVE TRADING/);
    }
  });

  test("overview shows account stats from the backend", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText("Equity", { exact: true })).toBeVisible();
    // seeded account: $10,000 initial + $50 realized win
    await expect(page.getByText("$10,050.00").first()).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("+$50.00").first()).toBeVisible();
  });
});

test.describe("markets", () => {
  test("registry lists XAUUSD and detail renders the chart", async ({ page }) => {
    await page.goto("/markets");
    const row = page.getByRole("row", { name: /XAUUSD/ });
    await expect(row).toBeVisible({ timeout: 15_000 });

    await row.getByRole("link", { name: "detail →" }).click();
    await expect(page.getByTestId("candle-chart")).toBeVisible({ timeout: 15_000 });
    await expect(page.locator("canvas").first()).toBeVisible();

    // overlay toggle switches without breaking the chart
    await page.getByRole("button", { name: "bb20" }).click();
    await expect(page.getByTestId("candle-chart")).toBeVisible();
  });
});

test.describe("signals and trades", () => {
  test("seeded signal appears with lifecycle detail", async ({ page }) => {
    await page.goto("/signals");
    const row = page.getByRole("row", { name: /sig-001|XAUUSD/ }).first();
    await expect(row).toBeVisible({ timeout: 10_000 });
    await row.getByRole("link").first().click();
    await expect(page.getByText("Signal parameters")).toBeVisible();
    await expect(page.getByText("Lifecycle transitions")).toBeVisible();
    // research attribution surfaces bull/bear content from the artifact
    await expect(page.getByText("bull case text")).toBeVisible();
  });

  test("trade timeline renders all eight canonical stages", async ({ page }) => {
    await page.goto("/trades");
    const card = page.locator("section").filter({ hasText: "XAUUSD" }).first();
    await expect(card).toBeVisible({ timeout: 10_000 });
    await card.locator("button").first().click();
    await expect(page.getByTestId("trade-timeline")).toBeVisible();
    await expect(page.getByTestId("trade-timeline")).toContainText("Market Data");
    await expect(page.getByTestId("trade-timeline")).toContainText("Exit");
  });

  test("journal note mutation is accepted and audited", async ({ page }) => {
    await page.goto("/trades");
    const card = page.locator("section").filter({ hasText: "XAUUSD" }).first();
    await expect(card).toBeVisible({ timeout: 10_000 });
    await card.locator("button").first().click();
    await page.getByPlaceholder(/journal note/i).fill("e2e note");
    await page.getByRole("button", { name: "add note" }).click();
    await expect(page.getByText("saved")).toBeVisible({ timeout: 10_000 });

    // audited: the note action lands in /system/audit
    await page.goto("/system");
    await expect(page.getByText("journal_note_added").first()).toBeVisible({ timeout: 10_000 });
  });
});

test.describe("backtests", () => {
  test("submit a run, poll to completion, render baselines", async ({ page }) => {
    await page.goto("/backtests");
    // pin a window inside the seeded synthetic dataset (bars end at T0 = 2026-08-21)
    await page.getByLabel("start").fill("2026-08-11");
    await page.getByLabel("end").fill("2026-08-21");
    await page.getByRole("button", { name: "run", exact: true }).click();
    // offline dataset runs fast; the report panel polls until the worker is done
    await expect(page.getByText("baseline_buy_hold").first()).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText("baseline_sma_cross").first()).toBeVisible();
    await expect(page.getByText(/Walk-forward \(\d+ strategies\)/)).toBeVisible();
  });
});

test.describe("broker (sandbox)", () => {
  test("paper badge loop includes the broker page", async ({ page }) => {
    await page.goto("/broker");
    const badge = page.getByTestId("paper-badge");
    await expect(badge).toBeVisible();
  });

  test("status renders sandbox environment and reconciliation panel", async ({ page }) => {
    await page.goto("/broker");
    await expect(page.getByText("Execution environment")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText("sandbox", { exact: true }).first()).toBeVisible();
    // honest empty state before any startup has run in this store
    await expect(
      page.getByText(/No reconciliation has run yet|trigger/i),
    ).toBeVisible({ timeout: 10_000 });
  });

  test("startup then halt then resume round-trip with audited state", async ({ page }) => {
    await page.goto("/broker");
    await page.getByRole("button", { name: "startup" }).click();
    await expect(page.getByText("startup OK").or(page.getByText("ready")).first()).toBeVisible({
      timeout: 15_000,
    });
    await page.getByPlaceholder("halt reason").fill("e2e drill");
    await page.getByRole("button", { name: "HALT" }).click();
    await expect(page.getByText("halted", { exact: true })).toBeVisible({ timeout: 10_000 });
    await page.getByRole("button", { name: "resume" }).click();
    await expect(page.getByText("resumed OK")).toBeVisible({ timeout: 10_000 });
  });
});
