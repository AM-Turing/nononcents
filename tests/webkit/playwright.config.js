// WebKit (Safari's engine) with iPhone emulation. Run with `make browser-test-webkit`, which
// starts a seeded test server on the host and runs this in Playwright's Docker image (WebKit
// needs system libraries the host does not have).
const { defineConfig, devices } = require("@playwright/test");

module.exports = defineConfig({
  testDir: ".",
  workers: 1,
  reporter: "list",
  outputDir: "/tmp/webkit-test-results",
  use: {
    ...devices["iPhone 13"],
    baseURL: `http://127.0.0.1:${process.env.BROWSER_TEST_PORT || "8776"}`,
  },
});
