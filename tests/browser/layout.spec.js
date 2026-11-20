const { test, expect } = require("@playwright/test");
const { login, addLargePurchase, removeLargePurchase, setAppearance } = require("./helpers");
const { layoutProblems } = require("./layout-audit");

// Every main page, desktop to phone: no words split mid-word, no text cut off, no table
// that needs sideways scrolling (phones) or does not fit (desktop), nothing outside its
// card, headings clear of their buttons, and no sideways page scroll.
const pages = [
  "/", "/transactions/", "/transactions/expense/", "/bills/", "/bills/add/", "/budgets/",
  "/budgets/pay-periods/", "/savings/", "/reports/", "/habits/", "/debts/", "/accounts/",
  "/categories/", "/statements/", "/manage/",
  "/docs/getting-started/", "/docs/everyday-use/", "/docs/understanding-the-numbers/",
  "/docs/troubleshooting/", "/docs/administrator/", "/docs/deployment/",
  "/docs/backup-and-restore/", "/docs/calculation-rules/",
];
const widths = [320, 390, 768, 1024, 1280, 1440, 1920];

test.beforeAll(async ({ browser }) => addLargePurchase(browser));
test.afterAll(async ({ browser }) => removeLargePurchase(browser));

for (const theme of ["light", "dark"]) {
  test(`pages lay out cleanly from phone to desktop (${theme})`, async ({ page }) => {
    test.setTimeout(300000);
    await login(page);
    await setAppearance(page, theme);
    const failures = [];
    for (const width of widths) {
      await page.setViewportSize({ width, height: 900 });
      for (const path of pages) {
        const response = await page.goto(path);
        expect(response.status(), path).toBe(200);
        await page.evaluate(() => document.fonts.ready);
        for (const problem of await page.evaluate(layoutProblems, width <= 600)) {
          failures.push(`${path} @${width}px: ${problem}`);
        }
      }
    }
    expect(failures).toEqual([]);
  });
}
