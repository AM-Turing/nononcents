const { test, expect } = require("@playwright/test");
const { execFileSync } = require("child_process");
const path = require("path");
const { login, daysAgo } = require("./helpers");

// Reproductions for two Outlook bugs: running analysis must not reload the page, and opening
// assumptions must keep the reader's position.
// These tests describe correct behaviour and are expected to FAIL on the current code.

const ROOT = path.resolve(__dirname, "../..");
const PYTHON = path.join(ROOT, ".venv/bin/python");
const SQLITE_PATH = process.env.BROWSER_TEST_SQLITE_PATH || "/tmp/nononcents-playwright.sqlite3";
const DESKTOP = { width: 1366, height: 900 };
const PHONE = { width: 390, height: 844 };

// Creates a RUNNING ReportAnalysisRun directly in the test database. No model is called and no
// worker thread is spawned (analysis_jobs.start_run is not used), so the run stays "running"
// until it is deleted. It is created now, so it is inside the STALE_AFTER window (10 minutes).
function createRunningAnalysis() {
  const start = daysAgo(30);
  const end = daysAgo(0);
  const code = [
    "from datetime import date",
    "from budget.models import Household, ReportAnalysisRun",
    "h = Household.objects.get(name='Demo Household')",
    `run = ReportAnalysisRun.objects.create(household=h, start=date.fromisoformat('${start}'), end=date.fromisoformat('${end}'), status='running')`,
    "print(run.pk)",
  ].join("\n");
  const output = execFileSync(PYTHON, ["manage.py", "shell", "-c", code], {
    cwd: ROOT,
    env: { ...process.env, SQLITE_PATH, BROWSER_TEST_MODE: "true", DJANGO_DEBUG: "true" },
    encoding: "utf8",
  });
  const pk = output.trim().split("\n").pop();
  if (!/^\d+$/.test(pk)) throw new Error(`Running analysis not created: ${output}`);
  return { pk, start, end };
}

function deleteRun(pk) {
  const code = `from budget.models import ReportAnalysisRun\nReportAnalysisRun.objects.filter(pk=${Number(pk)}).delete()`;
  execFileSync(PYTHON, ["manage.py", "shell", "-c", code], {
    cwd: ROOT,
    env: { ...process.env, SQLITE_PATH, BROWSER_TEST_MODE: "true", DJANGO_DEBUG: "true" },
    encoding: "utf8",
  });
}

const createdRunPks = [];

test.afterAll(() => {
  for (const pk of createdRunPks) deleteRun(pk);
});

function outlookUrl(start, end, extra = "") {
  return `/reports/?start=${start}&end=${end}${extra}`;
}

async function openRunningAnalysis(page, viewport) {
  const run = createRunningAnalysis();
  createdRunPks.push(run.pk);
  await page.setViewportSize(viewport);
  await login(page);
  await page.goto(outlookUrl(run.start, run.end, `&analysis=${run.pk}`));
  await expect(page.getByText("Analyzing…")).toBeVisible();
  return run;
}

async function openPlain(page, viewport, fragment = "") {
  await page.setViewportSize(viewport);
  await login(page);
  const start = daysAgo(30);
  const end = daysAgo(0);
  await page.goto(outlookUrl(start, end, "") + fragment);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Outlook");
}

const summary = (page) => page.locator("details.outlook-settings > summary");

// Scroll the summary into the viewport, record scrollY, open the details, and check that the
// page did not jump. Used by every variant of bug 2.
async function assertSummaryStaysPut(page) {
  await summary(page).scrollIntoViewIfNeeded();
  await expect(summary(page)).toBeInViewport();
  const before = await page.evaluate(() => window.scrollY);
  await summary(page).click();
  await page.waitForTimeout(1000);
  await expect(summary(page)).toBeInViewport();
  const after = await page.evaluate(() => window.scrollY);
  expect(Math.abs(after - before), `scrollY moved from ${before} to ${after}`).toBeLessThan(50);
}

// ---- Bug 1: the whole page reloads every 10 seconds while the analysis runs. ----

test("bug 1: running analysis does not reload the page, desktop", async ({ page }) => {
  await openRunningAnalysis(page, DESKTOP);
  const card = page.locator("#deep-analysis-title");
  await card.scrollIntoViewIfNeeded();
  const before = await page.evaluate(() => window.scrollY);
  await page.evaluate(() => {
    window.__stable = 1;
  });
  await page.waitForTimeout(12000);
  expect(await page.evaluate(() => window.__stable), "page reloaded").toBe(1);
  expect(await page.evaluate(() => window.scrollY)).toBe(before);
  await expect(page.getByText("Analyzing…")).toBeVisible();
});

test("bug 1: running analysis does not reload the page, phone", async ({ page }) => {
  await openRunningAnalysis(page, PHONE);
  const card = page.locator("#deep-analysis-title");
  await card.scrollIntoViewIfNeeded();
  const before = await page.evaluate(() => window.scrollY);
  await page.evaluate(() => {
    window.__stable = 1;
  });
  await page.waitForTimeout(12000);
  expect(await page.evaluate(() => window.__stable), "page reloaded").toBe(1);
  expect(await page.evaluate(() => window.scrollY)).toBe(before);
  await expect(page.getByText("Analyzing…")).toBeVisible();
});

// ---- Bug 2: "Adjust forecast assumptions" jumps the page to the bottom. ----

for (const [label, viewport] of [["desktop", DESKTOP], ["390px", PHONE]]) {
  test(`bug 2: opening assumptions keeps position, plain /reports/, ${label}`, async ({ page }) => {
    await openPlain(page, viewport);
    await assertSummaryStaysPut(page);
  });

  test(`bug 2: opening assumptions keeps position, #deep-analysis-title, ${label}`, async ({ page }) => {
    await openPlain(page, viewport, "#deep-analysis-title");
    await assertSummaryStaysPut(page);
  });

  test(`bug 2: opening assumptions keeps position, analysis running, ${label}`, async ({ page }) => {
    await openRunningAnalysis(page, viewport);
    await page.waitForTimeout(12000);
    await expect(page.getByText("Analyzing…")).toBeVisible();
    await assertSummaryStaysPut(page);
  });

  test(`bug 2: saving assumptions leaves the confirmation in view, ${label}`, async ({ page }) => {
    await openPlain(page, viewport);
    await summary(page).scrollIntoViewIfNeeded();
    await summary(page).click();
    await page.getByRole("button", { name: "Save assumptions" }).click();
    await expect(page).toHaveURL(/\/reports\/\?start=/);
    const notice = page.getByText("Outlook assumptions updated.");
    await expect(notice).toBeVisible();
    await expect(notice).toBeInViewport();
  });
}
