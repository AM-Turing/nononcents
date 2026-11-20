const { test, expect } = require("@playwright/test");
const { execFileSync } = require("child_process");
const path = require("path");
const { login } = require("./helpers");

// Sweep for page-stability bugs: layout shift while a page
// loads, unasked autofocus, sticky bars covering focus, and scroll or open sections lost after
// an action. Each test describes correct behaviour. Phone = 390x844 with touch.

const ROOT = path.resolve(__dirname, "../..");
const PYTHON = path.join(ROOT, ".venv/bin/python");
const SQLITE_PATH = process.env.BROWSER_TEST_SQLITE_PATH || "/tmp/nononcents-playwright.sqlite3";
const PHONE = { width: 390, height: 844 };

function shell(code) {
  return execFileSync(PYTHON, ["manage.py", "shell", "-c", code], {
    cwd: ROOT,
    env: { ...process.env, SQLITE_PATH, BROWSER_TEST_MODE: "true", DJANGO_DEBUG: "true" },
    encoding: "utf8",
  }).trim().split("\n").pop();
}

test.describe("phone", () => {
  test.use({ viewport: PHONE, hasTouch: true });

  // charts.js un-hides the zoom buttons (+40px) and fills the chart hint (a short line becomes
  // three on a phone). On a slow phone the script arrives after the first paint, so the cards
  // below the charts visibly jump. Simulate that by delaying the script.
  test("Outlook charts do not shift the page when charts.js arrives late", async ({ page }) => {
    await login(page);
    await page.addInitScript(() => {
      window.__cls = 0;
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) if (!e.hadRecentInput) window.__cls += e.value;
      }).observe({ type: "layout-shift", buffered: true });
    });
    await page.route("**/charts.js", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      await route.continue();
    });
    await page.goto("/reports/", { waitUntil: "commit" });
    await page.locator(".chart-grid").waitFor({ state: "attached" });
    await page.evaluate(() => document.querySelector(".chart-grid").scrollIntoView());
    await page.waitForTimeout(2500);
    const cls = await page.evaluate(() => window.__cls);
    expect(cls, `layout shift ${cls}`).toBeLessThan(0.01);
  });

  test("money forms do not autofocus on a phone", async ({ page }) => {
    await login(page);
    for (const url of ["/transactions/expense/", "/transactions/income/", "/bills/1/edit-amount/"]) {
      await page.goto(url);
      await page.waitForTimeout(300);
      const active = await page.evaluate(() => document.activeElement && document.activeElement.tagName);
      expect(active, `${url} focused ${active}`).toBe("BODY");
    }
  });

  test("bottom bar does not cover the focused field", async ({ page }) => {
    await login(page);
    await page.goto("/transactions/expense/");
    const last = page.locator("main form :is(input, select, textarea, button):visible").last();
    await last.focus();
    await page.waitForTimeout(300);
    const box = await last.boundingBox();
    const bar = await page.locator(".bottom").boundingBox();
    expect(box.y + box.height, "field bottom vs bar top").toBeLessThanOrEqual(bar.y);
  });

  test("a bill fragment target is visible and not under the bottom bar", async ({ page }) => {
    await login(page);
    await page.goto("/bills/");
    const id = await page.locator("tbody tr[id^='bill-']").last().getAttribute("id");
    await page.goto(`/bills/#${id}`);
    await page.waitForTimeout(300);
    const box = await page.locator(`#${id}`).boundingBox();
    const bar = await page.locator(".bottom").boundingBox();
    expect(box.y).toBeGreaterThanOrEqual(0);
    expect(box.y).toBeLessThan(bar.y);
  });

  test("localStorage that throws does not break the page", async ({ page }) => {
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await login(page);
    await page.addInitScript(() => {
      Object.defineProperty(window, "localStorage", { get() { throw new Error("blocked"); } });
    });
    await page.goto("/");
    await page.goto("/manage/");
    await page.locator("button[name='appearance']").first().click({ timeout: 3000 }).catch(() => {});
    expect(errors).toEqual([]);
  });

  test("theme script is blocking in head", async ({ page }) => {
    await login(page);
    const attrs = await page.evaluate(() => {
      const s = document.querySelector("head script[src*='theme-init.js']");
      return s && { async: s.async, defer: s.defer, type: s.type };
    });
    expect(attrs).toEqual({ async: false, defer: false, type: "" });
  });

  // Statement review: matching or dismissing a line used to redirect to #line-N, but a reviewed
  // line moves to the "Reviewed" list at the bottom, so the page jumped away from the next line.
  test("dismissing a statement line keeps the next line in view", async ({ page }) => {
    const pk = shell([
      "from datetime import date",
      "from decimal import Decimal",
      "from budget.models import Account, Household, StatementImport, StatementLine",
      "h = Household.objects.get(name='Demo Household')",
      "a = Account.objects.filter(household=h).first()",
      "s = StatementImport.objects.create(household=h, account=a, period_start=date(2020,1,1), period_end=date(2020,1,31), file_sha256='stability-test')",
      "for i in range(12): StatementLine.objects.create(statement=s, account=a, date=date(2020,1,2), amount=Decimal('-1.%02d' % i), description='Stability line %d' % i, fingerprint='stab%d' % i)",
      "print(s.pk)",
    ].join("\n"));
    try {
      await login(page);
      await page.goto(`/statements/${pk}/`);
      const lines = page.locator("ol.statement-lines > li");
      await lines.nth(5).scrollIntoViewIfNeeded();
      const next = lines.nth(6);
      const nextText = await next.innerText();
      await lines.nth(5).getByRole("button", { name: "Dismiss" }).click();
      await page.waitForURL(new RegExp(`/statements/${pk}/`));
      await page.waitForTimeout(500);
      await expect(page.locator("ol.statement-lines > li").filter({ hasText: nextText.split("\n")[0] }).first()).toBeInViewport();
    } finally {
      shell(`from budget.models import StatementImport\nStatementImport.objects.filter(pk=${pk}).delete()`);
    }
  });

  // Restoring from an "Archived" list reloaded the page with the list collapsed and the page at
  // the top, so restoring several items meant reopening it every time.
  test("restoring an archived category keeps the list open and the place", async ({ page }) => {
    shell([
      "from django.utils import timezone",
      "from budget.models import Category, Household",
      "h = Household.objects.get(name='Demo Household')",
      "for i in range(3): Category.objects.create(household=h, name='Stability archived %d' % i, type='spending', archived_at=timezone.now())",
    ].join("\n"));
    try {
      await login(page);
      await page.goto("/categories/");
      const details = page.locator("details.manage-section", { hasText: "Archived" });
      await details.locator("summary").click();
      await details.getByRole("button", { name: /Restore/ }).first().scrollIntoViewIfNeeded();
      const before = await page.evaluate(() => window.scrollY);
      expect(before).toBeGreaterThan(0);
      await details.getByRole("button", { name: /Restore/ }).first().click();
      await page.waitForURL(/\/categories\/$/);
      await page.waitForTimeout(300);
      await expect(details).toHaveJSProperty("open", true);
      expect(Math.abs((await page.evaluate(() => window.scrollY)) - before)).toBeLessThan(50);
    } finally {
      shell("from budget.models import Category\nCategory.objects.filter(name__startswith='Stability archived').delete()");
    }
  });
});

test("money form still focuses the amount on a desktop pointer", async ({ page }) => {
  await login(page);
  await page.goto("/transactions/expense/");
  await expect(page.getByLabel("Amount")).toBeFocused();
});
