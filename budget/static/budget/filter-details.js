// On a phone the History filters sit in a collapsed <details> (open when a filter is active);
// from 640px wide they are always shown, so the summary is hidden.
(() => {
  const details = document.querySelector("[data-filter-details]");
  if (!details) return;
  const wide = matchMedia("(min-width: 640px)");
  const sync = () => {
    if (wide.matches) {
      details.open = true;
      details.dataset.wide = "1";
    } else {
      delete details.dataset.wide;
    }
  };
  sync();
  wide.addEventListener("change", sync);
})();
