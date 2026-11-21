const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const { login, setAppearance } = require("./helpers");

// Daily flows and first run. Phone = 320px wide, the narrowest
// supported screen.
const PHONE = { width: 320, height: 568 };

async function noSidewaysScroll(page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

test.describe("phone", () => {
  test.use({ viewport: PHONE, hasTouch: true });

  test("Add goes straight to the expense form, with a type switcher", async ({ page }) => {
    await login(page);
    await page.locator(".bottom").getByRole("link", { name: "Add", exact: false }).click();
    await expect(page).toHaveURL("/transactions/expense/");
    await expect(page.getByRole("heading", { name: "Add expense" })).toBeVisible();
    const switcher = page.getByRole("navigation", { name: "Type of entry" });
    await expect(switcher.getByRole("link", { name: "Expense" })).toHaveAttribute("aria-current", "page");
    for (const [name, url] of [
      ["Income", "/transactions/income/"],
      ["Refund", "/transactions/refund/"],
      ["Transfer", "/transactions/transfer/"],
    ]) {
      await switcher.getByRole("link", { name }).click();
      await expect(page).toHaveURL(url);
      await expect(page.getByRole("navigation", { name: "Type of entry" }).getByRole("link", { name }))
        .toHaveAttribute("aria-current", "page");
      await noSidewaysScroll(page);
    }
  });

  test("expense form keeps the rarely used fields in More options", async ({ page }) => {
    await login(page);
    await page.goto("/transactions/expense/");
    const more = page.locator("details.more-options");
    await expect(more).not.toHaveAttribute("open", "");
    await expect(page.getByLabel("Amount")).toBeVisible();
    await expect(page.getByLabel("Merchant")).toBeVisible();
    // Paying a bill is a main field, visible without opening More options.
    await expect(page.getByLabel("Apply payment to bill")).toBeVisible();
    await expect(page.getByLabel("Bill month (optional)")).toBeHidden();
    await more.getByText("More options").click();
    await expect(page.getByLabel("Bill month (optional)")).toBeVisible();
    await expect(page.getByLabel("Spending classification")).toBeVisible();
    await expect(page.getByLabel("Split this transaction across categories")).toBeVisible();
    await noSidewaysScroll(page);
  });

  test("More options opens by itself when an option has an error", async ({ page }) => {
    await login(page);
    await page.goto("/transactions/expense/");
    await page.getByLabel("Amount").fill("20.00");
    await page.getByLabel("Account").selectOption({ label: "Spending" });
    await page.getByLabel("Category").selectOption({ index: 1 });
    // A bill month without a bill is refused by the server and reported on the hidden field.
    await page.locator("details.more-options").getByText("More options").click();
    const month = page.getByLabel("Bill month (optional)");
    const options = await month.locator("option").count();
    test.skip(options < 2, "the demo data has no payable bill month");
    await month.selectOption({ index: 1 });
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByText("Choose a bill before choosing a bill month.")).toBeVisible();
    await expect(page.locator("details.more-options")).toHaveAttribute("open", "");
  });

  test("History filters are collapsed on a phone until one is used", async ({ page }) => {
    await login(page);
    await page.goto("/transactions/");
    const summary = page.getByText("Search and filters");
    await expect(summary).toBeVisible();
    await expect(page.getByLabel("Search transactions")).toBeHidden();
    await summary.click();
    await expect(page.getByLabel("Search transactions")).toBeVisible();
    await page.goto("/transactions/?q=zzz-no-match");
    await expect(page.getByLabel("Search transactions")).toBeVisible();
    await expect(page.getByText("0 transactions match your filters.")).toBeVisible();
    await noSidewaysScroll(page);
  });

  test("Home shows the setup checklist for a profile with no entries", async ({ page }) => {
    await login(page, "Second Household");
    const card = page.locator(".setup-checklist");
    await expect(card.getByRole("heading", { name: "Set up your budget" })).toBeVisible();
    await expect(card.getByRole("link", { name: /opening balance/ })).toHaveAttribute("href", "/accounts/");
    await noSidewaysScroll(page);
    await setAppearance(page, "dark");
    await page.reload();
    const results = await new AxeBuilder({ page }).include(".setup-checklist").analyze();
    expect(results.violations).toEqual([]);
  });
});

test.describe("split amounts", () => {
  test.use({ viewport: PHONE, hasTouch: true });

  test("shows what is left to assign and only removes rows above two", async ({ page }) => {
    await login(page);
    await page.goto("/transactions/expense/");
    await page.locator("details.more-options").getByText("More options").click();
    await page.getByLabel("Split this transaction across categories").check();
    await page.locator("#id_amount").fill("50.00");
    const left = page.locator("[data-split-left]");
    await expect(left).toHaveText("Left to assign: $50.00");
    await page.getByLabel("Cost of this part").first().fill("30.00");
    await expect(left).toHaveText("Left to assign: $20.00");
    await page.getByLabel("Cost of this part").nth(1).fill("20.00");
    await expect(left).toHaveText("Everything is assigned.");
    await page.getByLabel("Cost of this part").nth(1).fill("25.50");
    await expect(left).toHaveText("Over by $5.50");
    const remove = page.locator("[data-remove-split]");
    await expect(remove.first()).toBeDisabled();
    await page.getByRole("button", { name: "Add another category" }).click();
    await expect(remove.first()).toBeEnabled();
    await remove.last().click();
    await expect(page.locator(".split-row")).toHaveCount(2);
    await expect(remove.first()).toBeDisabled();
  });
});

test("History filters are always shown on a wide screen", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await login(page);
  await page.goto("/transactions/");
  await expect(page.getByLabel("Search transactions")).toBeVisible();
  await expect(page.getByText("Search and filters")).toBeHidden();
});
