const { test, expect } = require("@playwright/test");
const { login, addLargePurchase, removeLargePurchase, setAppearance } = require("./helpers");


const pages = [
  "/", "/reports/", "/savings/", "/debts/", "/budgets/", "/bills/", "/transactions/",
  "/accounts/", "/categories/", "/habits/", "/statements/",
];
const widths = [320, 390, 600, 768, 1024, 1280, 1440, 1920];


// Everything inside a card must stay inside it, unless it sits in its own scroll box.
function overflowing() {
  const problems = [];
  const scrollBox = (el, card) => {
    for (let node = el.parentElement; node && node !== card; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (["auto", "scroll"].includes(style.overflowX)) return true;
      if (node.getBoundingClientRect().width <= 1) return true; // inside a visually hidden part
    }
    return false;
  };
  for (const card of document.querySelectorAll(".card")) {
    if (!card.offsetParent) continue;
    const box = card.getBoundingClientRect();
    for (const el of card.querySelectorAll("*")) {
      if (!el.offsetParent || scrollBox(el, card)) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 1 || r.height <= 1) continue; // visually hidden (screen-reader only)
      if (r.right > box.right + 1 || r.left < box.left - 1) {
        problems.push(`${el.tagName.toLowerCase()}.${el.className} "${(el.textContent || "").trim().slice(0, 40)}"`);
      }
    }
  }
  for (const money of document.querySelectorAll(".money")) {
    if (money.offsetParent && money.scrollWidth > money.clientWidth + 1) {
      problems.push(`clipped .money "${money.textContent.trim()}"`);
    }
  }
  const sideways = document.documentElement.scrollWidth - document.documentElement.clientWidth;
  if (sideways > 0) problems.push(`page scrolls sideways by ${sideways}px`);
  return problems;
}

for (const theme of ["light", "dark"]) {
  test(`card contents fit at every width (${theme})`, async ({ page }) => {
    test.setTimeout(240000);
    await login(page);
    await setAppearance(page, theme);
    const failures = [];
    for (const width of widths) {
      await page.setViewportSize({ width, height: 900 });
      for (const path of pages) {
        await page.goto(path);
        await page.evaluate(() => document.fonts.ready);
        for (const problem of await page.evaluate(overflowing)) {
          failures.push(`${path} @${width}px: ${problem}`);
        }
      }
    }
    expect(failures).toEqual([]);
  });
}

// Tiny amounts in the demo data can hide cut-off figures on phones.
test.describe("with real-sized amounts", () => {
  test.beforeAll(async ({ browser }) => addLargePurchase(browser));
  test.afterAll(async ({ browser }) => removeLargePurchase(browser));

  for (const theme of ["light", "dark"]) {
    test(`phone cards fit with real-sized amounts (${theme})`, async ({ page }) => {
      test.setTimeout(120000);
      await login(page);
      await setAppearance(page, theme);
      const failures = [];
      for (const width of [320, 390]) {
        await page.setViewportSize({ width, height: 900 });
        for (const path of pages) {
          await page.goto(path);
          await page.evaluate(() => document.fonts.ready);
          for (const problem of await page.evaluate(overflowing)) {
            failures.push(`${path} @${width}px: ${problem}`);
          }
        }
        await page.goto("/reports/");
        await expect(page.locator(".cut-grid strong").first()).not.toHaveText("$0.00");
        const cutProblems = await page.evaluate(() => {
          const problems = [];
          for (const card of document.querySelectorAll(".cut-grid article")) {
            const box = card.getBoundingClientRect();
            for (const amount of card.querySelectorAll("strong")) {
              const r = amount.getBoundingClientRect();
              if (amount.scrollWidth > amount.clientWidth + 1 || r.right > box.right + 1) {
                problems.push(`amount cut off: ${amount.textContent}`);
              }
            }
            // Headings may wrap between words, never inside one ("Modera-te").
            const heading = card.querySelector("h3");
            const text = heading.firstChild;
            for (const match of text.textContent.matchAll(/\S+/g)) {
              const range = document.createRange();
              range.setStart(text, match.index);
              range.setEnd(text, match.index + match[0].length);
              const lines = new Set([...range.getClientRects()].map((rect) => Math.round(rect.top)));
              if (lines.size > 1) problems.push(`heading word split: ${match[0]}`);
            }
          }
          return problems;
        });
        failures.push(...cutProblems.map((problem) => `/reports/ @${width}px: ${problem}`));
      }
      expect(failures).toEqual([]);
    });
  }
});
