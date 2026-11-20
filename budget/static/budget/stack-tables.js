// Data tables that do not fit their space are shown as stacked rows (mobile-layout.css,
// .table-stacked). Each cell is labelled with its column heading from the header row, and
// the choice is made again whenever the window size changes.
(() => {
  const wrappers = [...document.querySelectorAll(".table-wrap:not(.transaction-table), .table-scroll")]
    .filter((wrapper) => wrapper.querySelector("table"));
  for (const table of wrappers.map((wrapper) => wrapper.querySelector("table"))) {
    const head = table.tHead?.rows[table.tHead.rows.length - 1];
    if (!head) continue;
    const labels = [];
    for (const cell of head.cells) {
      const text = cell.textContent.trim();
      for (let i = 0; i < (cell.colSpan || 1); i += 1) labels.push(text);
    }
    for (const body of table.tBodies) {
      for (const row of body.rows) {
        let column = 0;
        for (const cell of row.cells) {
          if (!cell.hasAttribute("data-label") && labels[column]) cell.dataset.label = labels[column];
          column += cell.colSpan || 1;
        }
      }
    }
  }
  const fit = () => {
    for (const wrapper of wrappers) {
      wrapper.classList.remove("table-stacked");
      const table = wrapper.querySelector("table");
      if (table.scrollWidth > wrapper.clientWidth + 1) wrapper.classList.add("table-stacked");
    }
  };
  let pending = false;
  window.addEventListener("resize", () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; fit(); });
  });
  fit();
  document.fonts?.ready.then(fit);
})();
