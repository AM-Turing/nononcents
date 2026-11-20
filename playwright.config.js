const { defineConfig } = require("@playwright/test");

// Override the port with BROWSER_TEST_PORT if 8775 is taken on your machine.
const fs = require("fs");
const path = require("path");

// The server and seed commands run on a fixed "today" (see budget/testclock.py) so screenshots
// never depend on the real date. The date lives in tests/browser/FIXED_TODAY.
process.env.BROWSER_TEST_TODAY =
  process.env.BROWSER_TEST_TODAY || fs.readFileSync(path.join(__dirname, "tests/browser/FIXED_TODAY"), "utf8").trim();

const port = process.env.BROWSER_TEST_PORT || "8775";
const sqlitePath = process.env.BROWSER_TEST_SQLITE_PATH || "/tmp/nononcents-playwright.sqlite3";

module.exports = defineConfig({
  testDir: "./tests/browser",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "line",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
  },
  webServer: {
    command: `.venv/bin/python manage.py migrate --noinput && .venv/bin/python manage.py flush --noinput && .venv/bin/python manage.py seed_demo_profile --yes && .venv/bin/python manage.py create_browser_user && .venv/bin/python manage.py runserver 127.0.0.1:${port} --noreload`,
    env: {
      DJANGO_DEBUG: "true",
      BROWSER_TEST_MODE: "true",
      BROWSER_TEST_TODAY: process.env.BROWSER_TEST_TODAY,
      SQLITE_PATH: sqlitePath,
    },
    url: `http://127.0.0.1:${port}/login/`,
    reuseExistingServer: false,
    timeout: 120000,
  },
});
