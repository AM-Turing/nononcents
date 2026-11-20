const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const { login, setAppearance } = require("./helpers");

const pages = ["/habits/", "/debts/", "/budgets/", "/statements/"];

for (const theme of ["light", "dark"]) {
  test(`habits, debts, budgets and statements fit phones and pass axe (${theme})`, async ({ page }) => {
    await login(page);
    await setAppearance(page, theme);
    for (const [width, height] of [[320, 568], [390, 844], [1366, 768]]) {
      await page.setViewportSize({ width, height });
      for (const path of pages) {
        await page.goto(path);
        await expect(page.locator("h1")).toBeVisible();
        const overflow = await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth
        );
        expect(overflow, `${path} at ${width}px scrolls sideways`).toBeLessThanOrEqual(0);
        if (width === 320) {
          const results = await new AxeBuilder({ page }).analyze();
          const serious = results.violations.filter((v) => ["serious", "critical"].includes(v.impact));
          expect(serious.map((v) => `${path}: ${v.id}`)).toEqual([]);
        }
      }
    }
  });
}

test("a debt can be added, given a balance and projected on a phone", async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto("/debts/add/");
  await page.getByLabel("Name").fill("Browser test loan");
  await page.getByLabel("APR (%)").fill("6");
  await page.getByLabel("Minimum payment").fill("193.33");
  await page.getByRole("button", { name: /save/i }).click();
  await expect(page).toHaveURL(/\/debts\//);
  await page.getByRole("link", { name: /balance/i }).first().click();
  await page.getByLabel("Balance owed:").fill("10000");
  await page.getByRole("button", { name: /save/i }).click();
  await expect(page.getByText(/Paid off in 60 months/)).toBeVisible();
});

test("habits page shows the cleanup queue and a saved view", async ({ page }) => {
  await login(page);
  await page.goto("/habits/views/add/");
  await page.getByLabel("Name").fill("Small stuff");
  await page.getByLabel(/threshold/i).fill("15");
  await page.getByRole("button", { name: /save/i }).click();
  await expect(page.getByRole("link", { name: "Small stuff" })).toBeVisible();
  await expect(page.locator("#cleanup")).toBeVisible();
});
