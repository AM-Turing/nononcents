const { test, expect } = require("@playwright/test");
const { execFileSync } = require("child_process");
const path = require("path");
const { login, daysAgo } = require("./helpers");

// P4 item 3: every page works under the Content-Security-Policy. A violation, a console error or
// an uncaught page error fails the test.

const ROOT = path.resolve(__dirname, "../..");
const PYTHON = path.join(ROOT, ".venv/bin/python");
const SQLITE_PATH = process.env.BROWSER_TEST_SQLITE_PATH || "/tmp/nononcents-playwright.sqlite3";
const PAGES = [
  "/", "/transactions/", "/transactions/expense/", "/transactions/income/", "/bills/",
  "/bills/add/", "/budgets/", "/savings/", "/reports/", "/habits/", "/debts/", "/accounts/",
  "/categories/", "/statements/", "/manage/", "/docs/getting-started/",
  "/docs/administrator/",
];

function watch(page) {
  const problems = [];
  page.on("console", (message) => {
    if (message.type() === "error") problems.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
  page.on("response", (response) => {
    if (response.status() >= 400) problems.push(`${response.status()} ${response.url()}`);
  });
  return problems;
}

async function violations(page) {
  return page.evaluate(() => window.__cspViolations || []);
}

async function installViolationRecorder(context) {
  await context.addInitScript(() => {
    window.__cspViolations = [];
    document.addEventListener("securitypolicyviolation", (event) => {
      window.__cspViolations.push(`${event.violatedDirective} ${event.blockedURI} ${event.sample}`);
    });
  });
}

test("the policy header is sent", async ({ page }) => {
  const response = await page.goto("/login/");
  const policy = response.headers()["content-security-policy"];
  expect(policy).toContain("script-src 'self'");
  expect(policy).not.toMatch(/script-src[^;]*unsafe/);
});

for (const theme of ["light", "dark"]) {
  test(`every main page is clean under the policy, ${theme}`, async ({ page, context }) => {
    await installViolationRecorder(context);
    const problems = watch(page);
    await page.setViewportSize({ width: 1366, height: 900 });
    await login(page);
    await page.getByRole("button", { name: `Use ${theme} appearance` }).first().click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    for (const url of PAGES) {
      const response = await page.goto(url);
      expect(response.status(), url).toBeLessThan(400);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      expect(await page.locator("html").evaluate((el) => el.classList.contains("dark")), url).toBe(theme === "dark");
      expect(await violations(page), `violations on ${url}`).toEqual([]);
    }
    expect(problems).toEqual([]);
  });
}

test("theme is applied before first paint by a blocking head script", async ({ page }) => {
  await page.goto("/login/");
  const script = await page.evaluate(() => {
    const tag = document.head.querySelector("script[src*='theme-init']");
    return tag && { defer: tag.defer, async: tag.async };
  });
  expect(script).toEqual({ defer: false, async: false });
});

test("sidebar toggle and appearance picker still work and are remembered", async ({ page, context }) => {
  await installViolationRecorder(context);
  const problems = watch(page);
  await page.setViewportSize({ width: 1366, height: 900 });
  await login(page);
  const html = page.locator("html");
  const toggle = page.locator(".sidebar-toggle");
  await toggle.click();
  await expect(html).toHaveClass(/sidebar-collapsed/);
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(await page.evaluate(() => localStorage.getItem("sidebar-collapsed"))).toBe("true");
  await page.reload();
  await expect(html).toHaveClass(/sidebar-collapsed/);
  await toggle.click();
  await expect(html).not.toHaveClass(/sidebar-collapsed/);

  await page.getByRole("button", { name: "Use dark appearance" }).first().click();
  await expect(html).toHaveClass(/dark/);
  expect(await page.evaluate(() => localStorage.getItem("appearance"))).toBe("dark");
  await page.getByRole("button", { name: "Use light appearance" }).first().click();
  await expect(html).not.toHaveClass(/dark/);
  expect(await page.evaluate(() => localStorage.getItem("appearance"))).toBe("light");
  expect(await violations(page)).toEqual([]);
  expect(problems).toEqual([]);
});

test("sidebar and theme scripts survive blocked browser storage", async ({ page, context }) => {
  await context.addInitScript(() => {
    Object.defineProperty(window, "localStorage", {
      get() {
        throw new DOMException("denied", "SecurityError");
      },
    });
  });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/login/");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "system");
  expect(errors).toEqual([]);
});

test("Outlook charts, print button and analysis poll work under the policy", async ({ page, context }) => {
  await installViolationRecorder(context);
  const problems = watch(page);
  await page.setViewportSize({ width: 1366, height: 900 });
  await login(page);
  const start = daysAgo(30);
  const end = daysAgo(0);
  const code = [
    "from datetime import date",
    "from budget.models import Household, ReportAnalysisRun",
    "h = Household.objects.get(name='Demo Household')",
    `run = ReportAnalysisRun.objects.create(household=h, start=date.fromisoformat('${start}'), end=date.fromisoformat('${end}'), status='running')`,
    "print(run.pk)",
  ].join("\n");
  const env = { ...process.env, SQLITE_PATH, BROWSER_TEST_MODE: "true", DJANGO_DEBUG: "true" };
  const pk = execFileSync(PYTHON, ["manage.py", "shell", "-c", code], { cwd: ROOT, env, encoding: "utf8" })
    .trim().split("\n").pop();
  try {
    await page.addInitScript(() => {
      window.print = () => {
        window.__printed = true;
      };
    });
    await page.goto(`/reports/?start=${start}&end=${end}&analysis=${pk}`);
    // charts.js made the charts keyboard-reachable.
    await expect(page.locator("svg[role='application']").first()).toBeVisible();
    await page.getByRole("button", { name: "Print / PDF" }).click();
    expect(await page.evaluate(() => window.__printed)).toBe(true);
    // analysis-poll.js asks for the status after about 10 seconds.
    const request = await page.waitForRequest(/\/reports\/analysis\/\d+\/status\//, { timeout: 20000 });
    expect(request.url()).toContain(`/reports/analysis/${pk}/status/`);
  } finally {
    execFileSync(PYTHON, ["manage.py", "shell", "-c",
      `from budget.models import ReportAnalysisRun\nReportAnalysisRun.objects.filter(pk=${Number(pk)}).delete()`],
      { cwd: ROOT, env, encoding: "utf8" });
  }
  expect(await violations(page)).toEqual([]);
  expect(problems.filter((p) => !p.startsWith("404"))).toEqual([]);
});

test("admin page-name picker fills the name without inline script", async ({ page, context }) => {
  await installViolationRecorder(context);
  await login(page);
  await page.goto("/manage/");
  const select = page.locator("[data-page-names]");
  const input = page.locator("#id_page-name");
  const options = await select.locator("option").count();
  expect(options).toBeGreaterThan(1);
  await select.selectOption({ index: 1 });
  const current = await select.locator("option:checked").getAttribute("data-current");
  await expect(input).toHaveValue(current);
  expect(await violations(page)).toEqual([]);
});
