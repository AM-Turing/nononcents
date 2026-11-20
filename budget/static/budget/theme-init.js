// Runs in <head> before first paint (a blocking script, not deferred): sets the saved
// appearance and the sidebar state so the page does not flash. Loaded as a file because the
// Content-Security-Policy forbids inline scripts.
(function () {
  const root = document.documentElement;
  const saved = root.dataset.savedAppearance;
  let theme = saved;
  try {
    if (saved) localStorage.setItem("appearance", saved);
    else theme = localStorage.getItem("appearance");
  } catch (e) {}
  theme = theme || "system";
  root.dataset.theme = theme;
  if (theme === "dark" || (theme === "system" && matchMedia("(prefers-color-scheme:dark)").matches)) {
    root.classList.add("dark");
  }
  try {
    if (localStorage.getItem("sidebar-collapsed") === "true") root.classList.add("sidebar-collapsed");
  } catch (e) {}
})();
