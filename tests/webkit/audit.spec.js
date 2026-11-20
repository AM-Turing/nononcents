const { test, expect } = require("@playwright/test");
const { login, setAppearance } = require("../browser/helpers");
const { layoutProblems } = require("../browser/layout-audit");
const pages = ["/", "/transactions/", "/transactions/expense/", "/transactions/income/", "/transactions/transfer/",
  "/bills/", "/bills/add/", "/bills/1/edit-amount/", "/budgets/", "/budgets/pay-periods/", "/budgets/pay-periods/add/",
  "/savings/", "/savings/goals/add/", "/reports/", "/habits/", "/debts/", "/debts/add/", "/accounts/", "/accounts/add/",
  "/categories/", "/categories/add/?kind=spending", "/statements/", "/manage/",
  "/docs/getting-started/", "/docs/everyday-use/", "/docs/understanding-the-numbers/", "/docs/troubleshooting/",
  "/docs/administrator/", "/docs/deployment/", "/docs/backup-and-restore/", "/docs/calculation-rules/"];
// Main pages and forms at iPhone widths, light and dark, in WebKit.
test("WebKit phone layout", async ({ page }) => {
  test.setTimeout(600000);
  await login(page);
  const failures = [];
  for (const theme of ["light", "dark"]) {
    await setAppearance(page, theme);
    for (const width of [320, 375, 390, 430]) {
      await page.setViewportSize({ width, height: 844 });
      for (const path of pages) {
        const r = await page.goto(path);
        if (r.status() !== 200) { failures.push(`${path} status ${r.status()}`); continue; }
        for (const p of await page.evaluate(layoutProblems, true)) failures.push(`${theme} ${path} @${width}: ${p}`);
      }
    }
  }
  expect([...new Set(failures)]).toEqual([]);
});
