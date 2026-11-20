// Guide screenshots from the made-up Demo Household profile.
// Run with `make guide-screenshots` (scripts/guide-screenshots.sh starts a scratch server first).
// Not a test: the normal browser run only picks up *.spec.js files.
const path = require("path");
const { chromium } = require("@playwright/test");

const base = `http://127.0.0.1:${process.env.BROWSER_TEST_PORT || "8775"}`;
const out = path.join(__dirname, "..", "..", "docs", "guide", "images");
// README-only shots (dark mode) go here; the guide keeps its own folder.
const readmeOut = path.join(__dirname, "..", "..", "docs", "images");
require("fs").mkdirSync(out, { recursive: true });
require("fs").mkdirSync(readmeOut, { recursive: true });
const PHONE = { width: 390, height: 844 };
const DESKTOP = { width: 1280, height: 900 };

// name, path (or a function finding it), viewport, element to scroll to the top (optional)
const SHOTS = [
  ["home", "/", PHONE],
  ["home-desktop", "/", DESKTOP],
  ["expense-form", "/transactions/expense/", PHONE],
  ["accounts", "/accounts/", PHONE],
  ["account-add", "/accounts/add/", PHONE],
  ["categories", "/categories/", PHONE],
  ["bills", "/bills/", PHONE],
  ["bill-add", "/bills/add/", PHONE],
  ["pay-periods", "/budgets/pay-periods/", PHONE],
  ["budgets", "/budgets/", PHONE],
  ["debts", "/debts/", PHONE],
  ["statement-upload", "/statements/upload/", PHONE],
  ["statement-review", (page) => firstLink(page, "/statements/", /\/statements\/\d+\/$/), PHONE],
  ["reconcile", (page) => firstLink(page, "/accounts/", /\/reconcile\/$/), PHONE],
  ["outlook", "/reports/", PHONE],
  ["history", "/transactions/", PHONE],
  ["savings", "/savings/", PHONE],
  ["admin", "/manage/", DESKTOP],
];

// Dark theme, phone size, for the README (docs/images/).
const DARK_SHOTS = [
  ["home-dark", "/", PHONE],
  ["outlook-dark", "/reports/", PHONE],
];

async function firstLink(page, listPath, pattern) {
  await page.goto(base + listPath);
  const hrefs = await page.$$eval("a[href]", (links) => links.map((a) => a.getAttribute("href")));
  const href = hrefs.find((h) => pattern.test(h));
  if (!href) throw new Error(`No link matching ${pattern} on ${listPath}`);
  return href;
}

async function signIn(page) {
  await page.goto(base + "/login/");
  await page.getByLabel("Profile").selectOption({ label: "Demo Household" });
  await page.getByLabel("Username").fill("browser-admin");
  await page.getByLabel("Password").fill("browser-test-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(base + "/");
}

// Environment noise that is not part of using the app (no backups run on the scratch server).
async function tidy(page) {
  await page.evaluate(() => {
    document.querySelectorAll(".backup-alert").forEach((el) => el.remove());
  });
}

(async () => {
  const browser = await chromium.launch();
  const failures = [];
  const runs = [
    { viewport: PHONE, scheme: "light", dir: out, shots: SHOTS },
    { viewport: DESKTOP, scheme: "light", dir: out, shots: SHOTS },
    { viewport: PHONE, scheme: "dark", dir: readmeOut, shots: DARK_SHOTS },
  ];
  for (const { viewport, scheme, dir, shots } of runs) {
    const context = await browser.newContext({
      viewport,
      deviceScaleFactor: viewport === PHONE ? 2 : 1,
      colorScheme: scheme,
      isMobile: viewport === PHONE,
      hasTouch: viewport === PHONE,
    });
    const page = await context.newPage();
    await signIn(page);
    for (const [name, target, size] of shots) {
      if (size !== viewport) continue;
      try {
        const url = typeof target === "function" ? await target(page) : target;
        const response = await page.goto(base + url);
        if (!response || response.status() !== 200) throw new Error(`HTTP ${response && response.status()} for ${url}`);
        await page.waitForLoadState("networkidle");
        await tidy(page);
        await page.screenshot({ path: path.join(dir, `${name}.png`) });
        console.log(`saved ${name}.png (${url})`);
      } catch (error) {
        failures.push(`${name}: ${error.message}`);
      }
    }
    await context.close();
  }
  await browser.close();
  if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
  }
})();
