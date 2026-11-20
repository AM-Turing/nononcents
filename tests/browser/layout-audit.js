// Layout checks run inside the page (page.evaluate(layoutProblems, phone)).
// Each problem is a short string naming the element and what is wrong.
function layoutProblems(phone) {
  const problems = [];
  const describe = (el) => {
    const cls = typeof el.className === "string" && el.className.trim()
      ? "." + el.className.trim().split(/\s+/).join(".")
      : "";
    return `${el.tagName.toLowerCase()}${cls}`;
  };
  const visible = (el) => {
    if (!el.offsetParent && getComputedStyle(el).position !== "fixed") return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) return false;
    for (let node = el; node; node = node.parentElement) {
      if (node.classList?.contains("visually-hidden")) return false;
      if (node !== el && getComputedStyle(node).clipPath.startsWith("inset(50%")) return false;
      if (node.tagName === "DETAILS" && !node.open && node !== el && !node.querySelector("summary")?.contains(el)) return false;
    }
    return true;
  };

  // 1. Words split across two lines when they would fit on one line.
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let text = walker.nextNode(); text; text = walker.nextNode()) {
    const parent = text.parentElement;
    if (!parent || !text.textContent.trim()) continue;
    // .admin-logs code holds raw change details, allowed to break anywhere. Code in the guides
    // (.doc-body code: file paths, commands, setting names) wraps at "/", "-" and "_" like any
    // code listing; the guides' prose is still checked, and every other check covers the code.
    if (parent.closest("script, style, noscript, option, textarea, .bottom, .admin-logs code, .doc-body code")) continue;
    if (!visible(parent)) continue;
    const block = parent.closest("p, h1, h2, h3, h4, li, td, th, dt, dd, label, button, a, span, div, summary, strong, small") || parent;
    // Compare with the card (or page) width, so a word split because its box was squeezed
    // by neighbours (a title beside buttons, a narrow table column) is caught too.
    const area = parent.closest(".card, main") || document.body;
    const available = area.getBoundingClientRect().width - 32;
    for (const match of text.textContent.matchAll(/\S{2,}/g)) {
      const range = document.createRange();
      range.setStart(text, match.index);
      range.setEnd(text, match.index + match[0].length);
      const rects = [...range.getClientRects()].filter((r) => r.width > 0);
      const lines = new Set(rects.map((r) => Math.round(r.top)));
      if (lines.size < 2) continue;
      // A hyphenated word or name ("non-essential", "7-Eleven") may wrap at its hyphen, like
      // printed text; dates and amounts (no letters) may not.
      if (/[A-Za-z]/.test(match[0]) && /^[A-Za-z0-9]+(-[A-Za-z0-9]+)+[.,;:)]?$/.test(match[0])) continue;
      const width = rects.reduce((sum, r) => sum + r.width, 0);
      if (width <= available + 1) {
        problems.push(`word split: "${match[0]}" in ${describe(block)}`);
      }
    }
  }

  // 2. Text cut off by its own box.
  for (const el of document.querySelectorAll("body *")) {
    if (!visible(el) || el.closest(".bottom")) continue;
    const style = getComputedStyle(el);
    if (!["hidden", "clip"].includes(style.overflowX)) continue;
    if (el.matches("input, select, textarea, img, svg, .date-input-shell, details, progress")) continue;
    if (!el.textContent.trim()) continue;
    if (el.scrollWidth > el.clientWidth + 1) {
      problems.push(`text cut off: ${describe(el)} "${el.textContent.trim().slice(0, 40)}"`);
    }
  }

  // 3. On phones, nothing should need sideways scrolling (tables become stacked rows).
  if (phone) {
    for (const el of document.querySelectorAll("body *")) {
      if (!visible(el) || el.closest(".bottom")) continue;
      const style = getComputedStyle(el);
      if (!["auto", "scroll"].includes(style.overflowX)) continue;
      if (el.scrollWidth > el.clientWidth + 1) {
        problems.push(`scrolls sideways on phone: ${describe(el)} (${el.scrollWidth}px in ${el.clientWidth}px)`);
      }
    }
  }

  // 3b. Table cells whose content runs into the next cell (fixed-width columns).
  for (const cell of document.querySelectorAll("td, th")) {
    if (!visible(cell) || !cell.textContent.trim()) continue;
    if (getComputedStyle(cell).display !== "table-cell") continue;
    if (cell.scrollWidth > cell.clientWidth + 1) {
      problems.push(`text runs out of its cell: ${describe(cell)} "${cell.textContent.trim().slice(0, 30)}"`);
    }
  }

  // 3c. Desktop: tables must fit without sideways scrolling.
  if (!phone && window.innerWidth >= 1024) {
    for (const el of document.querySelectorAll(".table-wrap, .table-scroll")) {
      if (!visible(el)) continue;
      if (el.scrollWidth > el.clientWidth + 1) {
        problems.push(`scrolls sideways on desktop: ${describe(el)} (${el.scrollWidth}px in ${el.clientWidth}px)`);
      }
    }
  }

  // 4. Content spilling out of its card (outside a scroll box).
  for (const card of document.querySelectorAll(".card")) {
    if (!visible(card)) continue;
    const box = card.getBoundingClientRect();
    for (const el of card.querySelectorAll("*")) {
      if (!visible(el)) continue;
      let scroller = false;
      for (let node = el.parentElement; node && node !== card; node = node.parentElement) {
        if (["auto", "scroll"].includes(getComputedStyle(node).overflowX)) scroller = true;
      }
      if (scroller) continue;
      const r = el.getBoundingClientRect();
      if (r.right > box.right + 1 || r.left < box.left - 1) {
        problems.push(`outside card: ${describe(el)} "${(el.textContent || "").trim().slice(0, 30)}"`);
      }
    }
  }

  // 5. Page headings must not overlap their buttons.
  for (const head of document.querySelectorAll(".pagehead, .section-heading, .sectionhead")) {
    if (!visible(head)) continue;
    const title = head.querySelector("h1, h2");
    if (!title) continue;
    const t = title.getBoundingClientRect();
    for (const control of head.querySelectorAll("a, button, input, select")) {
      if (!visible(control)) continue;
      const c = control.getBoundingClientRect();
      if (c.left < t.right - 1 && c.right > t.left + 1 && c.top < t.bottom - 1 && c.bottom > t.top + 1) {
        problems.push(`overlaps heading: ${describe(control)} "${control.textContent.trim().slice(0, 30)}"`);
      }
    }
  }

  // 6. The page itself must never scroll sideways.
  const sideways = document.documentElement.scrollWidth - document.documentElement.clientWidth;
  if (sideways > 0) problems.push(`page scrolls sideways by ${sideways}px`);

  return [...new Set(problems)];
}

module.exports = { layoutProblems };
