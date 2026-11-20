const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const { login, addLargePurchase, removeLargePurchase, setAppearance } = require("./helpers");


const pages = [
  "/", "/transactions/", "/transactions/add/", "/transactions/expense/", "/bills/", "/budgets/",
  "/savings/", "/reports/", "/habits/", "/debts/", "/accounts/", "/categories/", "/statements/",
  "/manage/",
];

// Every text colour must meet WCAG AA (4.5:1 for small text) in both themes, on phone and
// desktop, with the phone More menu open so its contents are checked too.
test.beforeAll(async ({ browser }) => addLargePurchase(browser));
test.afterAll(async ({ browser }) => removeLargePurchase(browser));

for (const theme of ["light", "dark"]) {
  for (const [label, width] of [["phone", 390], ["desktop", 1440]]) {
    test(`text contrast meets WCAG AA (${theme}, ${label})`, async ({ page }) => {
      test.setTimeout(180000);
      await page.setViewportSize({ width, height: 900 });
      await login(page);
      await setAppearance(page, theme);
      const failures = [];
      for (const path of pages) {
        const response = await page.goto(path);
        expect(response.status(), path).toBe(200);
        if (label === "phone") await page.locator(".bottom summary").click();
        const results = await new AxeBuilder({ page }).withRules(["color-contrast"]).analyze();
        for (const violation of results.violations) {
          for (const node of violation.nodes) {
            failures.push(`${path}: ${node.target.join(" ")} — ${node.any[0]?.message ?? ""}`);
          }
        }
      }
      expect(failures).toEqual([]);
    });
  }
}
