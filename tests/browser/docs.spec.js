const { test, expect } = require("@playwright/test");
const { login } = require("./helpers");

// Help → guides: search (like the TestSecurity docs page), highlights and "On this page".

test("search finds a section in another guide, opens it and highlights the words", async ({ page }) => {
  await login(page);
  await page.goto("/docs/");
  await expect(page).toHaveURL("/docs/getting-started/");
  const box = page.getByRole("combobox", { name: "Search the guides" });
  await box.fill("safe to spend");
  const results = page.getByRole("listbox", { name: "Search results" });
  await expect(results).toBeVisible();
  await expect(page.locator(".docs-panel-status")).toHaveText(/\d+ results?|Showing the first/);
  const options = results.getByRole("option");
  expect(await options.count()).toBeGreaterThan(0);
  await expect(options.first().locator("mark.doc-hit").first()).toBeVisible();

  // Keyboard: arrow down moves the selection, Enter opens it.
  await box.press("ArrowDown");
  await expect(options.nth(1)).toHaveAttribute("aria-selected", "true");
  await box.press("ArrowUp");
  const chosen = options.first();
  const heading = (await chosen.locator(".docs-hit-heading").innerText()).split(" › ").pop();
  await box.press("Enter");
  await expect(page).toHaveURL(/q=safe(%20|\+)to(%20|\+)spend/);
  await expect(page.locator(".doc-body mark.doc-hit").first()).toBeVisible();
  await expect(page.getByRole("heading", { name: heading }).first()).toBeVisible();
  await expect(box).toHaveValue("safe to spend");

  // Clearing the search clears the highlight.
  await page.getByRole("button", { name: "Clear search" }).click();
  await expect(page.locator(".doc-body mark.doc-hit")).toHaveCount(0);
  await expect(page).not.toHaveURL(/q=/);
});

test("slash and Ctrl+K focus the search; Escape clears it; no match explains why", async ({ page }) => {
  await login(page);
  await page.goto("/docs/everyday-use/");
  const box = page.getByRole("combobox", { name: "Search the guides" });
  await page.locator("h1").first().click();
  await page.keyboard.press("/");
  await expect(box).toBeFocused();
  await box.fill("zzqqxx nothing");
  await expect(page.locator(".docs-empty")).toContainText("Nothing matches");
  await expect(page.locator(".docs-panel-status")).toHaveText("No results");
  await box.press("Escape");
  await expect(box).toHaveValue("");
  await box.blur();
  await page.keyboard.press("Control+k");
  await expect(box).toBeFocused();
});

test("exact phrases in quotes and every word must match", async ({ page }) => {
  await login(page);
  await page.goto("/docs/getting-started/");
  const box = page.getByRole("combobox", { name: "Search the guides" });
  await box.fill('"recurring bill"');
  const options = page.getByRole("listbox", { name: "Search results" }).getByRole("option");
  await expect(options.first()).toBeVisible();
  for (const text of await options.locator(".docs-hit-heading, .docs-hit-text").allInnerTexts()) {
    expect(text.length).toBeGreaterThan(0);
  }
  // Sections with the phrase in their heading rank first.
  await expect(options.first().locator(".docs-hit-heading")).toHaveText(/recurring bill/i);
  // "bill recurring" without quotes needs both words, in any order, so it finds at least as much.
  const phraseCount = await options.count();
  await box.fill("bill recurring");
  await expect(options.first()).toBeVisible();
  expect(await options.count()).toBeGreaterThanOrEqual(phraseCount);
});

test("administrators see the administrator chapters and On this page follows scrolling", async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/docs/administrator/");
  await expect(page.locator(".docs-chapter-list")).toContainText("For administrators");
  await expect(page.locator(".docs-chapter-list a[aria-current=page]")).toContainText("Administrator guide");
  const toc = page.locator(".docs-toc a[data-slug]");
  expect(await toc.count()).toBeGreaterThan(3);
  const third = toc.nth(2);
  await third.click();
  await expect(third).toHaveAttribute("aria-current", "location");
  // The Help link is in the sidebar.
  await expect(page.locator(".sidebar nav a", { hasText: "Help" })).toHaveAttribute("aria-current", "page");
});

test("phone: guide list and On this page are menus; Help is in More", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto("/");
  await page.locator(".more-menu > summary").click();
  await page.locator(".more-panel a", { hasText: "Help" }).click();
  await expect(page).toHaveURL("/docs/getting-started/");
  const menu = page.locator(".docs-chapter-menu");
  await menu.locator("summary").click();
  await menu.getByRole("link", { name: /Troubleshooting/ }).click();
  await expect(page).toHaveURL("/docs/troubleshooting/");
  const toc = page.locator(".docs-toc-menu");
  await toc.locator("summary").click();
  const link = toc.locator("a").nth(1);
  const target = (await link.getAttribute("href")).slice(1);
  await link.click();
  await expect(toc).not.toHaveAttribute("open", "");
  await expect(page.locator(`[id="${target}"]`)).toBeInViewport();
});
