// Admin page names: choosing a page fills the "New name" box with that page's current name.
(() => {
  const select = document.querySelector("[data-page-names]");
  const input = document.getElementById("id_page-name");
  if (!select || !input) return;
  const fill = () => { input.value = select.selectedOptions[0]?.dataset.current || ""; };
  select.addEventListener("change", fill);
  if (!input.value) fill();
})();
