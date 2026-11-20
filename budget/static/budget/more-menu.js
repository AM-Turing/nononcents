(() => {
  const menu = document.querySelector(".more-menu");
  if (!menu) return;
  const summary = menu.querySelector("summary");
  const sync = () => summary.setAttribute("aria-expanded", String(menu.open));
  sync();
  menu.addEventListener("toggle", sync);
  menu.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && menu.open) {
      menu.open = false;
      summary.focus();
    }
  });
  document.addEventListener("click", (event) => {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  });
})();
