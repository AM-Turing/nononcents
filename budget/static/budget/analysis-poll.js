(() => {
  const notice = document.querySelector("[data-analysis-status-url]");
  if (!notice) return;
  const statusUrl = notice.dataset.analysisStatusUrl;
  const live = document.querySelector("[data-analysis-live]");
  const INTERVAL = 10000;
  const LIMIT = 10 * 60 * 1000;
  const started = Date.now();

  const announce = (text) => {
    if (live) live.textContent = text;
  };

  async function swapCard() {
    const url = location.pathname + location.search;
    const page = await fetch(url, { credentials: "same-origin", headers: { Accept: "text/html" } });
    if (!page.ok) return false;
    const doc = new DOMParser().parseFromString(await page.text(), "text/html");
    const fresh = doc.querySelector(".deep-analysis-card");
    const current = document.querySelector(".deep-analysis-card");
    if (!fresh || !current) return false;
    current.replaceWith(document.importNode(fresh, true));
    return true;
  }

  async function check() {
    try {
      const response = await fetch(statusUrl, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (response.ok) {
        const data = await response.json();
        if (!data.running) {
          if (await swapCard()) {
            announce(
              data.status === "done"
                ? "Deep Pattern Analysis is ready."
                : "Deep Pattern Analysis did not finish."
            );
          }
          return;
        }
      }
    } catch (error) {
      // Network hiccup: try again on the next tick.
    }
    if (Date.now() - started < LIMIT) setTimeout(check, INTERVAL);
  }

  setTimeout(check, INTERVAL);
})();
