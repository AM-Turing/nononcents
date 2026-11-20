const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const { execFileSync } = require("child_process");
const path = require("path");
const { login, setAppearance } = require("./helpers");

// A made-up statement PDF from any bank: no balances, so it is staged "not verified".
// Covers the upload, account-choice and review screens (phone width, light and dark).

const ROOT = path.resolve(__dirname, "../..");
const PYTHON = path.join(ROOT, ".venv/bin/python");
const SQLITE_PATH = process.env.BROWSER_TEST_SQLITE_PATH || "/tmp/nononcents-playwright.sqlite3";

function shell(code) {
  return execFileSync(PYTHON, ["manage.py", "shell", "-c", code], {
    cwd: ROOT,
    env: { ...process.env, SQLITE_PATH, BROWSER_TEST_MODE: "true", DJANGO_DEBUG: "true" },
    encoding: "utf8",
  }).trim().split("\n").pop();
}

function statementPdf(lines) {
  const stream = ["BT", "/F1 9 Tf", "13 TL", "40 760 Td", ...lines.map((l) => `(${l}) Tj T*`), "ET"].join("\n");
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
    `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`,
  ];
  let out = "%PDF-1.4\n";
  const offsets = [];
  objects.forEach((body, i) => {
    offsets.push(out.length);
    out += `${i + 1} 0 obj\n${body}\nendobj\n`;
  });
  const xref = out.length;
  out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets) out += `${String(offset).padStart(10, "0")} 00000 n \n`;
  out += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(out, "latin1");
}

async function check(page, theme, label) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow, `${label} scrolls sideways (${theme})`).toBeLessThanOrEqual(0);
  const results = await new AxeBuilder({ page }).analyze();
  const serious = results.violations.filter((v) => ["serious", "critical"].includes(v.impact));
  expect(serious.map((v) => `${label}: ${v.id}`)).toEqual([]);
}

for (const theme of ["light", "dark"]) {
  test(`an unverified statement imports with plain warnings on a phone (${theme})`, async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 568 });
    await login(page);
    await setAppearance(page, theme);
    try {
      await page.goto("/statements/upload/");
      await expect(page.getByText("PDF statements from most banks work")).toBeVisible();
      await check(page, theme, "upload");
      await page.locator("#statement").setInputFiles({
        name: "made-up.pdf",
        mimeType: "application/pdf",
        buffer: statementPdf([
          "Statement period: 08/01/2026 - 08/31/2026",
          "Account ending in 6677",
          "Date Description Amount",
          "08/03 Hardware Store -60.00",
          "08/09 Paycheck +100.00",
        ]),
      });
      await page.getByRole("button", { name: "Read statement" }).click();
      await expect(page.getByRole("heading", { name: "Which account is which?" })).toBeVisible();
      await expect(page.getByText("not verified: check each line")).toBeVisible();
      await check(page, theme, "map");
      await page.getByLabel(/ending 6677/).selectOption({ label: "Spending" });
      await page.getByRole("button", { name: "Import statement" }).click();
      await expect(page.getByText(/Not verified for Spending/)).toBeVisible();
      await expect(page.getByText("not verified", { exact: true })).toBeVisible();
      await check(page, theme, "statements");
      await page.getByRole("link", { name: /Spending · Aug/ }).first().click();
      await expect(page.getByText("Not verified.")).toBeVisible();
      await check(page, theme, "review");
    } finally {
      shell(
        "from budget.models import Account, StatementImport\n" +
          "StatementImport.objects.filter(file_sha256__regex='.').delete()\n" +
          "Account.objects.update(statement_suffix='')"
      );
    }
  });
}
