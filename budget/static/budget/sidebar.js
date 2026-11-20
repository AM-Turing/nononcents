(() => {
  const toggle = document.querySelector(".sidebar-toggle");
  if (!toggle) return;

  const applyState = (collapsed) => {
    document.documentElement.classList.toggle("sidebar-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute("aria-label", collapsed ? "Expand menu" : "Collapse menu");
    toggle.title = collapsed ? "Expand menu" : "Collapse menu";
  };

  applyState(document.documentElement.classList.contains("sidebar-collapsed"));
  toggle.addEventListener("click", () => {
    const collapsed = !document.documentElement.classList.contains("sidebar-collapsed");
    try {
      localStorage.setItem("sidebar-collapsed", String(collapsed));
    } catch (e) {}
    applyState(collapsed);
  });
})();
