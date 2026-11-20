// Two small page-stability helpers.
// 1. After a list action (a form that posts to another URL, such as Restore, Dismiss or Remove)
//    the server redirects back to the same page. Put the page back where it was and re-open the
//    <details> sections that were open. Redirects that carry a #fragment are left alone.
// 2. data-autofocus: focus the field unless the main pointer is a finger, so a phone is not scrolled
//    to it and its keyboard is not opened unasked.
(() => {
  const KEY = "page-state";
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method !== "post") return;
    if (new URL(form.action, location.href).pathname === location.pathname) return;
    try {
      sessionStorage.setItem(KEY, JSON.stringify({
        path: location.pathname,
        y: window.scrollY,
        at: Date.now(),
        open: [...document.querySelectorAll("main details")].map((d) => d.open),
      }));
    } catch (e) {}
  });
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY) || "null");
    sessionStorage.removeItem(KEY);
    if (saved && saved.path === location.pathname && !location.hash && Date.now() - saved.at < 30000) {
      document.querySelectorAll("main details").forEach((d, i) => { if (saved.open[i]) d.open = true; });
      // After "load", so scripts that reshape tables on a phone have made the page its full height.
      const back = () => window.scrollTo(0, saved.y);
      if (document.readyState === "complete") back();
      else addEventListener("load", back);
    }
  } catch (e) {}
  if (!matchMedia("(pointer: coarse)").matches) {
    document.querySelector("[data-autofocus]")?.focus();
  }
})();
