const { test, expect } = require("@playwright/test");
const { login, daysAgo } = require("./helpers");

// Two purchases in different categories so the pie has slices and the daily chart has peaks.
// Runs in the empty Second Household so the demo data does not add slices.
const EMPTY_PROFILE = "Second Household";
const PURCHASES = [
  { merchant: "Chart test groceries", category: "Groceries", amount: "120.00", days: 3 },
  { merchant: "Chart test show", category: "Entertainment", amount: "60.00", days: 1 },
];

test.beforeAll(async ({ browser }) => {
  const page = await browser.newPage();
  await login(page, EMPTY_PROFILE);
  for (const p of PURCHASES) {
    await page.goto("/transactions/expense/");
    await page.getByLabel("Amount").fill(p.amount);
    await page.getByLabel("Date").fill(daysAgo(p.days));
    await page.getByLabel("Account").selectOption({ label: "Spending" });
    await page.getByLabel("Category").selectOption({ label: p.category });
    await page.getByLabel("Merchant").fill(p.merchant);
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page).toHaveURL("/");
  }
  await page.close();
});

test.afterAll(async ({ browser }) => {
  const page = await browser.newPage();
  await login(page, EMPTY_PROFILE);
  await page.goto("/transactions/");
  const rows = page
    .locator(".transaction-item, .transaction-table tbody tr")
    .filter({ hasText: "Chart test", visible: true });
  for (let left = await rows.count(); left > 0; left -= 1) {
    await rows.first().getByRole("button", { name: "Delete" }).click();
    // The URL already matches before the delete finishes; wait for the row to go.
    await expect(rows).toHaveCount(left - 1);
  }
  await page.close();
});

test("pie slices show category and amount on hover, tap and keyboard", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await login(page, EMPTY_PROFILE);
  await page.goto("/reports/");
  const chart = page.locator("[data-pie-chart]");
  const readout = chart.locator(".chart-readout");
  const slices = chart.locator("path[data-tip]");
  await expect(slices).toHaveCount(2);
  await expect(readout).toContainText("Tap or hover over a slice");

  await slices.first().hover();
  await expect(readout).toHaveText(/^Groceries · \$120\.00 · 66\.67%$/);
  await expect(chart.locator(".chart-tip")).toBeVisible();
  await expect(page.locator(".chart-legend li.is-active")).toContainText("Groceries");

  await slices.nth(1).focus();
  await expect(readout).toHaveText(/^Entertainment · \$60\.00 · 33\.33%$/);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await slices.nth(1).click();
  await expect(readout).toContainText("Entertainment");
  await page.locator("h1").click();
  await expect(readout).toContainText("Tap or hover over a slice");
});

async function axis(page) {
  return [
    await page.locator("[data-axis-start]").textContent(),
    await page.locator("[data-axis-end]").textContent(),
  ];
}

test("daily chart shows the nearest day and zooms with buttons, wheel, keys and pinch", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await login(page, EMPTY_PROFILE);
  await page.goto("/reports/");
  const chart = page.locator("[data-daily-chart]");
  const svg = chart.locator("svg");
  const readout = chart.locator(".chart-readout");
  const zoomIn = chart.getByRole("button", { name: "Zoom in" });
  const zoomOut = chart.getByRole("button", { name: "Zoom out" });
  const whole = chart.getByRole("button", { name: "Whole range" });
  await expect(zoomOut).toBeDisabled();
  await expect(whole).toBeDisabled();

  // Hovering anywhere picks the nearest day.
  await svg.scrollIntoViewIfNeeded();
  const box = await svg.boundingBox();
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
  await expect(readout).toHaveText(/^[A-Z][a-z]{2} \d{1,2}, \d{4} · \$[\d,]+\.\d{2} spent$/);

  const full = await axis(page);
  await zoomIn.click();
  const zoomed = await axis(page);
  expect(zoomed).not.toEqual(full);
  await expect(whole).toBeEnabled();
  await zoomIn.click();
  await zoomOut.click();
  expect(await axis(page)).not.toEqual(full); // one step back, still zoomed in
  await whole.click();
  expect(await axis(page)).toEqual(full);
  await expect(zoomOut).toBeDisabled();

  // Scroll wheel over the chart zooms in; scrolling out returns to the whole range.
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
  await page.mouse.wheel(0, -300);
  await expect.poll(() => axis(page)).not.toEqual(full);
  for (let i = 0; i < 12; i += 1) await page.mouse.wheel(0, 300);
  await expect.poll(() => axis(page)).toEqual(full);

  // Keyboard: arrow keys step through days.
  await svg.focus();
  await page.keyboard.press("Home");
  const first = await readout.textContent();
  await page.keyboard.press("ArrowRight");
  await expect(readout).not.toHaveText(first);
  await page.keyboard.press("+");
  await expect.poll(() => axis(page)).not.toEqual(full);
  await whole.click();

  // Two-finger pinch (pointer events, as a phone sends them) zooms in.
  await svg.evaluate((node) => {
    const r = node.getBoundingClientRect();
    const y = r.top + r.height / 2;
    const fire = (type, id, x) =>
      node.dispatchEvent(new PointerEvent(type, { pointerId: id, pointerType: "touch", clientX: x, clientY: y, bubbles: true }));
    fire("pointerdown", 1, r.left + r.width * 0.45);
    fire("pointerdown", 2, r.left + r.width * 0.55);
    fire("pointermove", 1, r.left + r.width * 0.25);
    fire("pointermove", 2, r.left + r.width * 0.75);
    fire("pointerup", 1, r.left + r.width * 0.25);
    fire("pointerup", 2, r.left + r.width * 0.75);
  });
  await expect.poll(() => axis(page)).not.toEqual(full);
});

test("homepage spending pie shows category and amount on hover, tap and keyboard", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await login(page, EMPTY_PROFILE);
  const chart = page.locator(".spending-habits [data-pie-chart]");
  const readout = chart.locator(".chart-readout");
  const slices = chart.locator("path[data-tip]");
  await expect(slices).toHaveCount(2);
  await slices.first().scrollIntoViewIfNeeded();
  await slices.first().hover();
  await expect(readout).toHaveText(/^Groceries · \$120\.00 · 67%$/);
  await expect(page.locator(".spending-habits .chart-legend li.is-active")).toContainText("Groceries");
  await slices.nth(1).focus();
  await expect(readout).toHaveText(/^Entertainment · \$60\.00 · 33%$/);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await slices.nth(1).scrollIntoViewIfNeeded();
  await slices.nth(1).click();
  await expect(readout).toContainText("Entertainment");
});
