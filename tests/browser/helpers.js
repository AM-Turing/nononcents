const { expect } = require("@playwright/test");

// The browser-test database holds the full "Demo Household" and an empty "Second Household"
// (spending categories Groceries and Entertainment only), for tests that need a blank profile.
async function login(page, profile = "Demo Household") {
  await page.goto("/login/");
  await page.getByLabel("Profile").selectOption({ label: profile });
  await page.getByLabel("Username").fill("browser-admin");
  await page.getByLabel("Password").fill("browser-test-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
  // Display Mode is saved to the account, so start every test from System (a fresh browser).
  await setAppearance(page, "system");
  await page.reload();
}

// The server runs on a fixed "today" (BROWSER_TEST_TODAY, set by playwright.config.js), so tests
// must compute dates from it, never from the real clock.
function daysAgo(days) {
  const [y, m, d] = process.env.BROWSER_TEST_TODAY.split("-").map(Number);
  const date = new Date(Date.UTC(y, m - 1, d - days));
  return date.toISOString().slice(0, 10);
}

// The demo data has small amounts and little non-essential spending, which hides cut-off figures
// and the Outlook cut tables. These add one real-sized non-essential purchase and remove it.
const LARGE_PURCHASE = "Browser test large purchase";

async function addLargePurchase(browser) {
  const page = await browser.newPage();
  await login(page);
  await page.goto("/transactions/expense/");
  await page.getByLabel("Amount").fill("18765.43");
  await page.getByLabel("Date").fill(daysAgo(1));
  await page.getByLabel("Account").selectOption({ label: "Spending" });
  await page.getByLabel("Category").selectOption({ label: "Entertainment" });
  await page.getByLabel("Merchant").fill(LARGE_PURCHASE);
  await page.locator("details.more-options").getByText("More options").click();
  await page.getByLabel("Spending classification").selectOption({ label: "Non-essential" });
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page).toHaveURL("/");
  await page.close();
}

async function removeLargePurchase(browser) {
  const page = await browser.newPage();
  await login(page);
  await page.goto("/transactions/");
  // History shows a table on wide screens and a card list otherwise; use whichever is visible.
  const row = page
    .locator(".transaction-item, .transaction-table tbody tr")
    .filter({ hasText: LARGE_PURCHASE, visible: true });
  for (let left = await row.count(); left > 0; left -= 1) {
    await row.first().getByRole("button", { name: "Delete" }).click();
    // The URL already matches before the delete finishes; wait for the row to go.
    await expect(row).toHaveCount(left - 1);
  }
  await page.close();
}

// Display Mode is saved to the account (and copied into the browser), so tests choose it the way a
// person does: through the Display Mode form on the current page. Call on a signed-in page, then
// navigate or reload.
async function setAppearance(page, value) {
  // The URL can match before the new page has rendered; wait for the form to exist.
  await page.locator("form.appearance-picker").first().waitFor({ state: "attached" });
  await page.evaluate(async (choice) => {
    const form = document.querySelector("form.appearance-picker");
    const data = new FormData(form);
    data.set("appearance", choice);
    // The redirect target shows (and so uses up) the "Appearance updated." notice.
    data.set("next", "/accounts/");
    const response = await fetch(form.action, { method: "POST", body: data, credentials: "same-origin" });
    if (!response.ok) throw new Error(`Display Mode not saved: ${response.status}`);
    localStorage.setItem("appearance", choice);
  }, value);
}

module.exports = { login, daysAgo, addLargePurchase, removeLargePurchase, setAppearance };
