const { test, expect } = require("@playwright/test");
const { login, setAppearance } = require("./helpers");

// Accessibility checks. Tap targets are at least 44px tall, the desktop
// sidebar marks the current page in both themes, and focus moves to a new split row.
const PHONE = { width: 320, height: 568 };
const DESKTOP = { width: 1280, height: 800 };

async function expectTall(locator) {
  const box = await locator.first().boundingBox();
  expect(box, "element is visible").not.toBeNull();
  expect(Math.round(box.height)).toBeGreaterThanOrEqual(44);
}

test.describe("phone", () => {
  test.use({ viewport: PHONE, hasTouch: true });

  test("text links and summaries have a 44px tap height", async ({ page }) => {
    await login(page);
    await page.goto("/transactions/expense/");
    await expectTall(page.getByRole("link", { name: "← Back" }));
    await page.goto("/bills/");
    await expectTall(page.getByRole("link", { name: "Current month" }));
    await page.goto("/");
    await expectTall(page.getByText("How this is worked out").first());
    await expectTall(page.getByRole("link", { name: "View all" }));
    const reconcile = page.getByRole("link", { name: "Reconcile with bank" });
    for (let i = 0; i < (await reconcile.count()); i += 1) await expectTall(reconcile.nth(i));
  });

  test("Add another category moves focus to the new row", async ({ page }) => {
    await login(page);
    await page.goto("/transactions/expense/");
    await page.locator("details.more-options").getByText("More options").click();
    await page.getByLabel("Split this transaction across categories").check();
    await page.getByRole("button", { name: "Add another category" }).click();
    const added = page.locator(".split-row").last().locator("select").first();
    await expect(added).toBeFocused();
  });
});

for (const theme of ["light", "dark"]) {
  test(`desktop sidebar marks the current page (${theme})`, async ({ page }) => {
    await page.setViewportSize(DESKTOP);
    await login(page);
    await setAppearance(page, theme);
    for (const [url, name] of [
      ["/", "Home"],
      ["/transactions/", "History"],
      ["/bills/", "Bills"],
      ["/budgets/", "Budgets"],
      ["/savings/", "Savings"],
      ["/reports/", "Outlook"],
    ]) {
      await page.goto(url);
      const current = page.locator('#primary-navigation a[aria-current="page"]');
      await expect(current).toHaveCount(1);
      await expect(current).toHaveText(name);
      // Not colour only: the current entry is bold and carries a left bar.
      await expect(current).toHaveCSS("font-weight", "700");
      const shadow = await current.evaluate((el) => getComputedStyle(el).boxShadow);
      expect(shadow).not.toBe("none");
    }
  });
}
