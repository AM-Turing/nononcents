const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const { login, setAppearance, daysAgo } = require("./helpers");

const viewports = [
  [320, 568], [360, 800], [390, 844], [430, 932], [768, 1024],
  [1024, 768], [1366, 768], [1440, 900], [1920, 1080],
];

async function assertTypography(page) {
  await expect.poll(() => page.evaluate(() => document.fonts.status)).toBe("loaded");
  expect(await page.locator("body").evaluate((node) => getComputedStyle(node).fontFamily)).toContain("Plus Jakarta Sans");
  expect(await page.locator("h1").evaluate((node) => getComputedStyle(node).fontFamily)).toContain("Space Grotesk");
  const button = page.locator("button, .button").first();
  if (await button.count()) expect(await button.evaluate((node) => getComputedStyle(node).fontFamily)).toContain("Plus Jakarta Sans");
  const input = page.locator("input:not([type=hidden])").first();
  if (await input.count()) expect(await input.evaluate((node) => getComputedStyle(node).fontFamily)).toContain("Plus Jakarta Sans");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBeTruthy();
}

test("login typography, local fonts, and accessibility", async ({ page }) => {
  const fontResponses = [];
  page.on("response", (response) => {
    if (response.url().includes("woff2")) fontResponses.push(response);
  });
  await page.goto("/login/");
  await assertTypography(page);
  await page.evaluate(() => document.fonts.ready);
  expect(fontResponses.length).toBeGreaterThanOrEqual(2);
  for (const response of fontResponses) {
    expect(response.ok()).toBeTruthy();
    expect(new URL(response.url()).origin).toBe(new URL(test.info().project.use.baseURL).origin);
  }
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test("login opens the selected authorized profile", async ({ page }) => {
  await page.goto("/login/");
  await page.getByLabel("Profile").selectOption({ label: "Second Household" });
  await page.getByLabel("Username").fill("browser-admin");
  await page.getByLabel("Password").fill("browser-test-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
  await expect(page.locator(".brand")).toHaveText("Second Household");
});

test("critical pages retain typography in light and dark mode", async ({ page }) => {
  await login(page);
  const pages = [
    ["/", "Demo’s Budget Overview"], ["/transactions/add/", "Add transaction"],
    ["/transactions/expense/", "Add expense"],
    ["/transactions/income/", "Add income"], ["/transactions/transfer/", "Add transfer"],
    ["/transactions/refund/", "Add refund"], ["/bills/", "Bills for"],
    ["/budgets/", "Budgets"], ["/savings/", "Savings"],
    ["/transactions/", "History"], ["/reports/", "Outlook"],
    ["/manage/", "Admin"],
  ];
  for (const theme of ["light", "dark"]) {
    await setAppearance(page, theme);
    for (const [url, title] of pages) {
      await page.goto(url);
      await expect(page.getByRole("heading", { level: 1 })).toContainText(title);
      await assertTypography(page);
      const results = await new AxeBuilder({ page }).analyze();
      expect(results.violations).toEqual([]);
    }
  }
});

test("mobile add transaction offers income", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 568 });
  await login(page);
  await page.locator(".bottom").getByRole("link", { name: "Add", exact: false }).click();
  await expect(page).toHaveURL("/transactions/expense/");
  await expect(page.getByRole("heading", { name: "Add expense" })).toBeVisible();
  await page.getByRole("navigation", { name: "Type of entry" }).getByRole("link", { name: "Income" }).click();
  await expect(page).toHaveURL("/transactions/income/");
  await expect(page.getByRole("heading", { name: "Add income" })).toBeVisible();
});

test("dashboard moves quick actions above the pay-period card only on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await login(page);
  const mobileQuickActions = page.locator(".mobile-quick-actions");
  const spendingPlan = page.locator(".current-period");
  await expect(mobileQuickActions).toBeVisible();
  await expect(page.locator(".desktop-quick-actions")).toBeHidden();
  expect((await mobileQuickActions.boundingBox()).y).toBeLessThan((await spendingPlan.boundingBox()).y);

  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(mobileQuickActions).toBeHidden();
  const desktopQuickActions = page.locator(".desktop-quick-actions");
  await expect(desktopQuickActions).toBeVisible();
  expect((await desktopQuickActions.boundingBox()).y).toBeGreaterThan((await spendingPlan.boundingBox()).y);
});

test("mobile navigation and expense date stay within the viewport", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await login(page);
  const mobileNavigation = page.getByRole("navigation", { name: "Mobile primary" });
  await expect(mobileNavigation).toBeVisible();
  const labels = await mobileNavigation.locator("> a, > details > summary").allTextContents();
  expect(labels.map((label) => label.trim())).toEqual(["Home", "History", "Add＋", "Bills", "More"]);
  await mobileNavigation.locator("summary").click();
  const moreLabels = await mobileNavigation.locator(".more-panel a").allTextContents();
  expect(moreLabels.map((label) => label.trim())).toEqual([
    "Budgets", "Savings", "Outlook", "Habits", "Debts", "Accounts", "Statement check", "Categories", "Help", "Admin",
  ]);

  await page.goto("/transactions/expense/");
  const bounds = await page.getByLabel("Date").evaluate((input) => {
    const inputRect = input.getBoundingClientRect();
    const cardRect = input.closest(".card").getBoundingClientRect();
    return {
      inputLeft: inputRect.left,
      inputRight: inputRect.right,
      cardLeft: cardRect.left,
      cardRight: cardRect.right,
    };
  });
  expect(bounds.inputLeft).toBeGreaterThanOrEqual(bounds.cardLeft);
  expect(bounds.inputRight).toBeLessThanOrEqual(bounds.cardRight);
});

test("iPhone bottom navigation labels never overlap page content", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  const navigation = page.getByRole("navigation", { name: "Mobile primary" });
  await navigation.locator("summary").click();
  for (const name of ["Outlook", "Admin"]) {
    const link = navigation.getByRole("link", { name });
    await expect(link).toBeVisible();
    expect(await link.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBeTruthy();
  }

  for (const url of ["/reports/", "/manage/"]) {
    await page.goto(url);
    await page.evaluate(() => scrollTo(0, document.documentElement.scrollHeight));
    const clearance = await page.evaluate(() => {
      const main = document.querySelector("main");
      const bottom = document.querySelector(".bottom");
      const visibleChildren = [...main.children].filter((node) => {
        const style = getComputedStyle(node);
        return style.display !== "none" && node.getBoundingClientRect().height > 0;
      });
      return bottom.getBoundingClientRect().top - visibleChildren.at(-1).getBoundingClientRect().bottom;
    });
    expect(clearance).toBeGreaterThanOrEqual(8);
  }
});

test("system appearance preserves typography and follows dark preference", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await login(page);
  await setAppearance(page, "system");
  await page.reload();
  await assertTypography(page);
  expect(await page.locator("html").evaluate((node) => node.classList.contains("dark"))).toBeTruthy();
});

test("typography remains usable at 200 percent page scale", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await login(page);
  const client = await page.context().newCDPSession(page);
  await client.send("Emulation.setPageScaleFactor", { pageScaleFactor: 2 });
  await expect(page.getByRole("heading", { name: "Demo’s Budget Overview" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Add transaction" }).first()).toBeVisible();
  await assertTypography(page);
});

test("desktop sidebar collapses persistently without changing mobile navigation", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await login(page);
  const sidebar = page.locator(".sidebar");
  const content = page.locator(".content");
  await expect(page.getByRole("button", { name: "Collapse menu" })).toBeVisible();
  expect((await sidebar.boundingBox()).width).toBe(230);

  await page.getByRole("button", { name: "Collapse menu" }).click();
  await expect(page.getByRole("button", { name: "Expand menu" })).toHaveAttribute("aria-expanded", "false");
  await expect.poll(async () => (await sidebar.boundingBox()).width).toBe(64);
  await expect.poll(async () => (await content.boundingBox()).x).toBe(64);
  await page.reload();
  await expect(page.getByRole("button", { name: "Expand menu" })).toBeVisible();
  expect((await sidebar.boundingBox()).width).toBe(64);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(sidebar).toBeHidden();
  await expect(page.getByRole("navigation", { name: "Mobile primary" })).toBeVisible();
});

for (const [width, height] of viewports) {
  test(`dashboard typography fits ${width}x${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await login(page);
    await assertTypography(page);
    await expect(page.getByRole("heading", { name: "Demo’s Budget Overview" })).toBeVisible();
  });
}

for (const theme of ["light", "dark"]) {
  for (const device of ["desktop", "mobile"]) {
    test(`dashboard visual ${device} ${theme}`, async ({ page }) => {
      await page.setViewportSize(device === "mobile" ? { width: 390, height: 844 } : { width: 1440, height: 900 });
      await login(page);
      await setAppearance(page, theme);
      await page.reload();
      await page.evaluate(() => document.fonts.ready);
      await expect(page).toHaveScreenshot(`dashboard-${device}-${theme}.png`, {
        fullPage: true,
        mask: [page.locator("[data-visual-dynamic]")],
      });
    });
  }
}

const pageVisuals = [
  ["add-transaction", "/transactions/expense/"],
  ["bills-review", "/bills/"],
  ["budget-management", "/budgets/"],
  ["savings-goals", "/savings/"],
  ["transaction-history", "/transactions/"],
  ["monthly-report", "/reports/"],
  ["application-admin", "/manage/"],
];

for (const [name, url] of pageVisuals) {
  test(`${name} typography visual`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await login(page);
    await page.goto(url);
    await page.evaluate(() => document.fonts.ready);
    const screenshotOptions = { fullPage: true };
    if (["monthly-report", "application-admin"].includes(name)) {
      screenshotOptions.mask = [page.locator("[data-visual-dynamic], input[type=date]")];
    }
    if (name === "application-admin") {
      // Activity log rows depend on what earlier test files created; keep the layout only.
      await page.evaluate(() => document.querySelectorAll(".admin-logs tbody tr").forEach((row) => row.remove()));
      // Last login text changes width with the time of day; use fixed text so columns stay put.
      await page.evaluate(() =>
        document.querySelectorAll("td[data-visual-dynamic]").forEach((cell) => (cell.textContent = "Jan 1, 2026, 12:00 pm"))
      );
    }
    await expect(page).toHaveScreenshot(`${name}-desktop-light.png`, screenshotOptions);
  });
}

test("form validation typography visual", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto("/transactions/expense/");
  await page.locator("main form").evaluate((form) => { form.noValidate = true; });
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".errorlist").first()).toBeVisible();
  await expect(page).toHaveScreenshot("form-validation-mobile-light.png", {
    fullPage: true,
    mask: [page.locator("input[type=date]")],
  });
});

test("empty transaction state typography visual", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto("/transactions/?q=no-results-for-this-visual-test");
  await expect(page.locator(".empty:visible")).toContainText("No transactions match");
  await expect(page).toHaveScreenshot("empty-transactions-mobile-light.png", { fullPage: true });
});

test("bills keep paid and actions visible on phone", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await login(page);
  await page.goto("/bills/");
  const firstBill = page.locator("tbody tr").first();
  await expect(firstBill.locator("td").nth(4)).toBeVisible();
  await expect(firstBill.locator("td").nth(5)).toBeVisible();
  await firstBill.locator(".bill-more > summary").click();
  await expect(firstBill.getByRole("link", { name: "Edit bill" })).toBeVisible();
  await assertTypography(page);
  await expect(page).toHaveScreenshot("bills-review-mobile-light.png", { fullPage: true });
});

test("bill edit offers future due-date updates", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto("/bills/");
  await page.locator(".bill-more > summary").first().click();
  await page.getByRole("link", { name: "Edit bill" }).first().click();

  const billName = page.getByLabel("Bill name");
  const dueDate = page.getByLabel("Due date for this month");
  await expect(billName).toBeVisible();
  await expect(dueDate).toBeVisible();
  expect((await billName.boundingBox()).y).toBeLessThan((await dueDate.boundingBox()).y);
  await expect(page.getByLabel("Use this due day for future months too")).toBeVisible();
  await assertTypography(page);
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test("confirming a bill returns to that bill on phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto("/bills/");
  const row = page.locator("tbody tr").filter({ has: page.getByRole("button", { name: "Confirm" }) }).last();
  const rowId = await row.getAttribute("id");
  await row.getByRole("button", { name: "Confirm" }).click();

  await expect(page).toHaveURL(new RegExp(`#${rowId}$`));
  const box = await page.locator(`#${rowId}`).boundingBox();
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.y).toBeLessThan(844);
});

test("phone More menu switches display mode by keyboard and stays on the page", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await login(page);
  await page.goto("/bills/");
  await setAppearance(page, "light");
  await page.reload();
  const navigation = page.getByRole("navigation", { name: "Mobile primary" });
  const summary = navigation.locator("summary");
  await summary.focus();
  await page.keyboard.press("Enter");
  const dark = navigation.getByRole("button", { name: "Use dark appearance" });
  await expect(dark).toBeVisible();
  await dark.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL("/bills/");
  await expect(page.locator("html")).toHaveClass(/dark/);
  await summary.click();
  await navigation.getByRole("button", { name: "Use light appearance" }).click();
  await expect(page).toHaveURL("/bills/");
  await expect(page.locator("html")).not.toHaveClass(/dark/);
});

test("staff can reach admin and password reset on phone", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await login(page);

  await page.locator(".bottom summary").click();
  const mobileAdmin = page.locator(".bottom").getByRole("link", { name: "Admin" });
  await expect(mobileAdmin).toBeVisible();
  await mobileAdmin.click();
  await expect(page).toHaveURL("/manage/");
  await expect(page.getByRole("heading", { level: 1, name: "Admin" })).toBeVisible();

  await page.getByRole("link", { name: "Reset password" }).first().click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Reset password for");
  await expect(page.locator("#id_new_password1")).toBeVisible();
  await expect(page.locator("#id_new_password2")).toBeVisible();
  await assertTypography(page);
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test("staff can open profile access management on phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto("/manage/");
  const profileRow = page.locator(".admin-profiles tbody tr").filter({ hasText: "Second Household" });
  await profileRow.getByRole("link", { name: "Manage access" }).click();

  await expect(page.getByRole("heading", { name: "Manage Second Household" })).toBeVisible();
  await expect(page.getByRole("group", { name: "Users with access" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save profile access" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBeTruthy();
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test("phone outlook shows the compact report and keeps dates in bounds", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await login(page);
  await page.goto("/reports/");

  const card = page.locator(".report-filters");
  const cardBox = await card.boundingBox();
  expect(cardBox).not.toBeNull();
  expect(
    await card.evaluate((node) => node.scrollWidth <= node.clientWidth)
  ).toBeTruthy();
  for (const shell of await card.locator(".date-input-shell").all()) {
    const shellBox = await shell.boundingBox();
    expect(shellBox).not.toBeNull();
    expect(shellBox.x).toBeGreaterThanOrEqual(cardBox.x);
    expect(shellBox.x + shellBox.width).toBeLessThanOrEqual(cardBox.x + cardBox.width);
    expect(await shell.evaluate((node) => getComputedStyle(node).overflow)).toBe("hidden");
  }
  await expect(page.getByRole("heading", { name: "Outlook" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Forecast for this range" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Spending by category" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Category breakdown" })).toBeVisible();
  await expect(page.getByText("Deep Pattern Analysis", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Export CSV" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Print / PDF" })).toBeVisible();
  await expect(page.locator(".mobile-outlook-detail").first()).toBeVisible();
  await expect(page.locator(".savings-health")).toBeVisible();
  await page.getByRole("button", { name: "Generate AI analysis" }).click();
  await expect(page).toHaveURL(/analysis=\d+/);
  // The analysis runs in the background; with no local model configured it fails quickly.
  await expect(async () => {
    await page.reload();
    await expect(page.getByText("Local analysis unavailable.")).toBeVisible({ timeout: 1000 });
  }).toPass({ timeout: 20000 });
  await assertTypography(page);
});

test.describe("without JavaScript", () => {
  test.use({ javaScriptEnabled: false });

  test("outlook charts still show labelled values", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await login(page);
    await page.goto("/reports/");
    const point = page.locator(".daily-point").first();
    await expect(point).toHaveAttribute("aria-label", /\$[0-9,.]+ spent/);
    await expect(point.locator("title")).toContainText("spent");
    await expect(page.locator(".chart-zoom")).toBeHidden();
  });
});

test("move savings defaults to one goal and reveals an intentional split", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto("/savings/move/");

  const rows = page.locator("[data-savings-row]");
  await expect(rows.first()).toBeVisible();
  await expect(rows.nth(1)).toBeHidden();
  const split = page.getByLabel("Split withdrawal across multiple goals");
  const size = await split.boundingBox();
  expect(size.width).toBeLessThanOrEqual(20);
  await split.check();
  await expect(rows.nth(1)).toBeVisible();
  await assertTypography(page);
});

test("history transactions can be corrected and deleted", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto("/transactions/expense/");
  await page.getByLabel("Amount").fill("12.34");
  await page.getByLabel("Date").fill(daysAgo(1));
  await page.getByLabel("Account").selectOption({ label: "Spending" });
  await page.getByLabel("Category").selectOption({ label: "Groceries" });
  await page.getByLabel("Merchant").fill("Original description");
  await page.getByRole("button", { name: "Save", exact: true }).click();

  await page.goto("/transactions/");
  const row = page.locator(".transaction-item").filter({ hasText: "Original description" });
  await row.getByRole("link", { name: "Edit" }).click();
  await expect(page.locator("#id_transaction_type")).toBeDisabled();
  await page.getByLabel("Description").fill("Corrected description");
  await page.getByLabel("Amount").fill("21.43");
  await page.getByRole("button", { name: "Save changes" }).click();

  const correctedRow = page.locator(".transaction-item").filter({ hasText: "Corrected description" });
  await expect(correctedRow).toBeVisible();
  await expect(correctedRow).toContainText("−$21.43");
  await correctedRow.getByRole("button", { name: "Delete" }).click();

  await expect(page).toHaveURL("/transactions/");
  await expect(page.locator(".transaction-list")).not.toContainText("Corrected description");
  const undo = page.locator(".undo-notice");
  await expect(undo).toContainText("Corrected description");
  await expect(undo.getByRole("button", { name: "Undo" })).toBeVisible();
});

test("amounts can be typed with commas and are shown with commas", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto("/transactions/expense/");
  await page.getByLabel("Amount").fill("$1,234.56");
  await page.getByLabel("Date").fill(daysAgo(1));
  await page.getByLabel("Account").selectOption({ label: "Spending" });
  await page.getByLabel("Category").selectOption({ label: "Groceries" });
  await page.getByLabel("Merchant").fill("Comma merchant");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page).toHaveURL("/");

  await page.goto("/transactions/");
  const item = page.locator(".transaction-item").filter({ hasText: "Comma merchant" });
  await expect(item).toContainText("−$1,234.56");
  await item.getByRole("link", { name: "Edit" }).click();
  await expect(page.getByLabel("Amount")).toHaveValue("1234.56");
  await page.goto("/transactions/");
  await page.locator(".transaction-item").filter({ hasText: "Comma merchant" }).getByRole("button", { name: "Delete" }).click();
});

test("transaction history uses readable cards on phone", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await login(page);
  await page.goto("/transactions/expense/");
  await page.getByLabel("Amount").fill("12.34");
  await page.getByLabel("Date").fill(daysAgo(1));
  await page.getByLabel("Account").selectOption({ label: "Spending" });
  await page.getByLabel("Category").selectOption({ label: "Groceries" });
  await page.getByLabel("Merchant").fill("Mobile merchant");
  await page.getByLabel("Notes").fill("Mobile transaction note");
  await page.getByRole("button", { name: "Save", exact: true }).click();

  await page.goto("/transactions/");
  await expect(page.locator(".transaction-table")).toBeHidden();
  const item = page.locator(".transaction-item").filter({ hasText: "Mobile merchant" });
  await expect(item).toContainText("Spending");
  await expect(item).toContainText("Mobile transaction note");
  await expect(item.getByRole("link", { name: "Edit" })).toBeVisible();
  await expect(item.getByRole("button", { name: "Delete" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBeTruthy();
  for (const shell of await page.locator(".transaction-date-shell").all()) {
    const inputBox = await shell.boundingBox();
    const cardBox = await page.locator(".transaction-history").boundingBox();
    expect(inputBox.x).toBeGreaterThanOrEqual(cardBox.x);
    expect(inputBox.x + inputBox.width).toBeLessThanOrEqual(cardBox.x + cardBox.width);
    expect(await shell.evaluate((node) => getComputedStyle(node).overflow)).toBe("hidden");
  }
});

test("transaction history desktop columns keep dates and actions readable", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await login(page);
  await page.goto("/transactions/expense/");
  await page.getByLabel("Amount").fill("12.34");
  await page.getByLabel("Date").fill(daysAgo(1));
  await page.getByLabel("Account").selectOption({ label: "Spending" });
  await page.getByLabel("Category").selectOption({ label: "Groceries" });
  await page.getByLabel("Merchant").fill("Desktop merchant");
  await page.getByLabel("Notes").fill("A useful note");
  await page.getByRole("button", { name: "Save", exact: true }).click();

  await page.goto("/transactions/");
  const row = page.locator(".transaction-table tbody tr").filter({ hasText: "Desktop merchant" });
  await expect(row).toBeVisible();
  expect(await row.locator("td").nth(0).evaluate((cell) => getComputedStyle(cell).whiteSpace)).toBe("nowrap");
  const amount = row.locator("td.num").first();
  expect(await amount.evaluate((cell) => getComputedStyle(cell).whiteSpace)).toBe("nowrap");
  expect(await row.locator(".form-actions").evaluate((actions) => getComputedStyle(actions).flexWrap)).toBe("nowrap");
  expect(await amount.evaluate((cell) => parseFloat(getComputedStyle(cell).paddingRight))).toBeGreaterThanOrEqual(20);
  // Type badge and notes now sit in the Description cell; nothing runs out of its cell.
  await expect(row.locator("td").nth(1)).toContainText("Expense");
  await expect(row.locator("td").nth(1)).toContainText("A useful note");
  for (const cell of await row.locator("td").all()) {
    expect(await cell.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBeTruthy();
  }
  expect(await row.locator("td").last().evaluate((cell) => parseFloat(getComputedStyle(cell).paddingLeft))).toBeGreaterThanOrEqual(20);
  for (const button of await row.locator("td").last().getByRole("link").all()) {
    expect(await button.evaluate((link) => link.scrollWidth <= link.clientWidth)).toBeTruthy();
    expect((await button.boundingBox()).height).toBeLessThanOrEqual(40);
  }
});

test("transaction history adapts across narrow desktop widths", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await login(page);
  await page.goto("/transactions/expense/");
  await page.getByLabel("Amount").fill("12.34");
  await page.getByLabel("Date").fill(daysAgo(1));
  await page.getByLabel("Account").selectOption({ label: "Spending" });
  await page.getByLabel("Category").selectOption({ label: "Groceries" });
  await page.getByLabel("Merchant").fill("Responsive merchant");
  await page.getByLabel("Notes").fill("Visible at every width");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await page.goto("/transactions/");

  for (const width of [1199, 1024, 800, 640]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.locator(".transaction-table")).toBeHidden();
    const item = page.locator(".transaction-item").filter({ hasText: "Responsive merchant" });
    await expect(item).toBeVisible();
    await expect(item).toContainText("Spending");
    await expect(item).toContainText("Visible at every width");
    expect(await item.locator(".status").evaluate((status) => getComputedStyle(status).whiteSpace)).toBe("nowrap");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBeTruthy();
  }
});

test("a bill can be safely removed on phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto("/bills/");
  const row = page.locator("tbody tr").filter({ has: page.locator(".bill-more-items a:text-is('Remove')") }).first();
  const billName = (await row.locator("strong").first().textContent()).trim();
  await row.locator(".bill-more > summary").click();
  await row.getByRole("link", { name: "Remove" }).click();

  await expect(page.getByRole("heading", { name: `Remove ${billName}` })).toBeVisible();
  await expect(page.getByText("Existing bill and payment history will be preserved")).toBeVisible();
  await page.getByLabel(`Type \"${billName}\" to confirm`).fill(billName);
  await page.getByRole("button", { name: "Remove bill" }).click();

  await expect(page).toHaveURL(/\/bills\/\?month=/);
  await expect(page.locator("tbody")).not.toContainText(billName);
});

test("saved expense can add another or go Home", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto("/transactions/expense/");
  await page.getByLabel("Amount").fill("12.57");
  await page.getByLabel("Date").fill(daysAgo(1));
  await page.getByLabel("Account").selectOption({ label: "Spending" });
  await page.getByLabel("Category").selectOption({ label: "Entertainment" });
  await page.getByLabel("Merchant").fill("Game store");
  await page.getByRole("button", { name: "Save and add another" }).click();
  await expect(page).toHaveURL("/transactions/expense/");

  await page.getByLabel("Amount").fill("3.21");
  await page.getByLabel("Date").fill(daysAgo(1));
  await page.getByLabel("Account").selectOption({ label: "Spending" });
  await page.getByLabel("Category").selectOption({ label: "Entertainment" });
  await page.getByLabel("Merchant").fill("Game store two");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page).toHaveURL("/");
});

test("refund original expense choices describe the transaction", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto("/transactions/expense/");
  await page.getByLabel("Amount").fill("12.57");
  await page.getByLabel("Date").fill(daysAgo(2));
  await page.getByLabel("Account").selectOption({ label: "Spending" });
  await page.getByLabel("Category").selectOption({ label: "Entertainment" });
  await page.getByLabel("Merchant").fill("Apple Games");
  await page.getByRole("button", { name: "Save", exact: true }).click();

  await page.goto("/transactions/refund/");
  const choices = await page.getByLabel("Original expense (optional)").locator("option").allTextContents();
  expect(choices).toContain(`${daysAgo(2)} — Apple Games — $12.57`);
  expect(choices.some((choice) => choice.includes("Expense object"))).toBeFalsy();
});

test("add bill payment account offers Bills and Spending", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto("/bills/add/");

  const choices = await page.getByLabel("Payment account").locator("option").allTextContents();
  expect(choices).toContain("Bills");
  expect(choices).toContain("Spending");
  expect(choices).not.toContain("Savings");
});

test("pay-period budget has a spending target and expected paycheck, and no Bills target", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto("/budgets/");

  await expect(page.getByLabel("Spending target for this pay period")).toBeVisible();
  await expect(page.getByLabel("Expected paycheck for this pay period")).toBeEditable();
  await expect(page.getByLabel("Bills target")).toHaveCount(0);
});

test("next-period carry-over links to editable paycheck", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);

  await expect(page.locator(".money-card").nth(1).getByRole("heading", { name: "Bills account" })).toBeVisible();
  await page.getByRole("link", { name: "Edit paycheck or spending target" }).click();

  await expect(page).toHaveURL(/\/budgets\/\?period=\d+/);
  const paycheck = page.getByLabel("Expected paycheck for this pay period");
  await expect(paycheck).toBeEditable();
  await paycheck.fill("4500.25");
  await page.getByRole("button", { name: "Save budget" }).click();
  await expect(page).toHaveURL(/\/budgets\/\?period=\d+/);
  await expect(paycheck).toHaveValue("4500.25");
});

test("expense can be split across categories as one transaction", async ({ page }) => {
  await login(page);
  await page.goto("/transactions/expense/");
  await page.getByLabel("Amount").fill("100.00");
  await page.getByLabel("Date").fill(daysAgo(1));
  await page.getByLabel("Account").selectOption({ label: "Spending" });
  const categoryChoices = await page.getByLabel("Category").locator("option").allTextContents();
  expect(categoryChoices).toContain("Groceries");
  await page.locator("details.more-options").getByText("More options").click();
  await page.getByLabel("Split this transaction across categories").check();
  const allocations = page.getByLabel("What it was for");
  const dollars = page.getByLabel("Cost of this part");
  await allocations.nth(0).selectOption({ label: "Groceries" });
  await dollars.nth(0).fill("60.00");
  await allocations.nth(1).selectOption({ label: "Utilities" });
  await dollars.nth(1).fill("40.00");
  await page.getByLabel("Merchant").fill("Split purchase");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page).toHaveURL("/");
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/transactions/");
  await expect(
    page.getByRole("cell", { name: "Split — Groceries: $60.00; Utilities: $40.00" }),
  ).toBeVisible();
});

test("bill actions are grouped on phone and More opens by keyboard", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await login(page);
  await page.goto("/bills/");
  const row = page.locator("tbody tr").filter({ has: page.getByRole("button", { name: "Confirm" }) }).first();
  const paid = row.locator("button.primary[aria-label^='Mark']");
  const confirm = row.getByRole("button", { name: "Confirm", exact: true });
  const skip = row.getByRole("link", { name: "Skip", exact: true });
  const cell = row.locator("td[data-label='Actions']");
  const [cellBox, paidBox, confirmBox, skipBox] = await Promise.all([cell, paid, confirm, skip].map((l) => l.boundingBox()));
  expect(paidBox.width).toBeGreaterThan(cellBox.width * 0.85);
  expect(Math.abs(confirmBox.y - skipBox.y)).toBeLessThan(3);
  expect(Math.abs(confirmBox.width - skipBox.width)).toBeLessThan(3);
  expect(paidBox.width).toBeGreaterThan(confirmBox.width * 1.8);
  for (const box of [paidBox, confirmBox, skipBox]) expect(box.height).toBeGreaterThanOrEqual(43.5);

  await expect(row.getByRole("link", { name: "Edit bill" })).toBeHidden();
  const more = row.locator(".bill-more > summary");
  await expect(more).toHaveText(/More for /);
  await more.focus();
  await page.keyboard.press("Enter");
  await expect(row.getByRole("link", { name: "Edit bill" })).toBeVisible();
  await expect(row.getByRole("link", { name: "Remove" })).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(row.getByRole("link", { name: "Edit bill" })).toBeHidden();
  await page.keyboard.press("Space");
  await expect(row.getByRole("link", { name: "Remove" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBeTruthy();
});
