// Help → guides: search across every chapter the reader may see, highlight matches in the
// open chapter, and follow the reader's place in "On this page".
// The search works like the TestSecurity docs page: each section (the text under an h2/h3,
// or a chapter's introduction) is one result; every word must appear in the section ("exact
// phrase" in quotes); sections whose heading holds more of the words rank first, then more
// matches, then chapter order. "/" or Ctrl+K (Cmd+K) focuses the box.
(() => {
  const root = document.querySelector(".docs-search");
  const article = document.getElementById("doc-article");
  if (!root || !article) return;
  const input = root.querySelector("#docs-q");
  const clearButton = root.querySelector(".docs-clear");
  const panel = root.querySelector("#docs-panel");
  const list = root.querySelector("#docs-results");
  const panelStatus = root.querySelector(".docs-panel-status");
  const empty = root.querySelector(".docs-empty");
  const emptyTitle = root.querySelector(".docs-empty-title");
  const liveStatus = root.querySelector("#docs-status");
  const MAX_RESULTS = 50;
  const SNIPPET_LENGTH = 160;
  const DEBOUNCE_MS = 120;

  // Lower-casing that never changes the length, so offsets in the copy match the original.
  const fold = (text) => {
    const lower = text.toLowerCase();
    if (lower.length === text.length) return lower;
    return Array.from(text, (c) => (c.toLowerCase().length === c.length ? c.toLowerCase() : c)).join("");
  };

  const parseQuery = (query) => {
    const terms = [];
    for (const m of query.matchAll(/"([^"]*)"|(\S+)/g)) {
      const raw = m[1] !== undefined ? m[1] : m[2].replace(/"/g, "");
      const term = fold(raw.replace(/\s+/g, " ").trim());
      if (term && !terms.includes(term)) terms.push(term);
    }
    return terms;
  };

  const count = (haystack, term) => {
    let n = 0;
    for (let at = haystack.indexOf(term); at >= 0; at = haystack.indexOf(term, at + term.length)) n += 1;
    return n;
  };

  const ranges = (lower, terms) => {
    const found = [];
    for (const term of terms) {
      for (let at = lower.indexOf(term); at >= 0; at = lower.indexOf(term, at + term.length)) {
        found.push([at, at + term.length]);
      }
    }
    found.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const merged = [];
    for (const r of found) {
      const last = merged[merged.length - 1];
      if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
      else merged.push([r[0], r[1]]);
    }
    return merged;
  };

  const snippet = (section, terms) => {
    const text = section.text.replace(/\s+/g, " ");
    const marks = ranges(fold(text), terms);
    let start = 0;
    if (marks.length) {
      const first = marks[0];
      start = Math.max(0, first[0] - Math.floor((SNIPPET_LENGTH - (first[1] - first[0])) / 3));
      const space = text.indexOf(" ", start);
      if (start > 0 && space >= 0 && space < first[0]) start = space + 1;
    }
    const end = Math.min(text.length, start + SNIPPET_LENGTH);
    const parts = [];
    if (start > 0) parts.push(["… ", false]);
    let pos = start;
    for (const [a, b] of marks) {
      if (b <= start || a >= end) continue;
      const from = Math.max(a, start);
      const to = Math.min(b, end);
      if (from > pos) parts.push([text.slice(pos, from), false]);
      parts.push([text.slice(from, to), true]);
      pos = to;
    }
    if (pos < end) parts.push([text.slice(pos, end), false]);
    if (end < text.length) parts.push([" …", false]);
    return parts;
  };

  let index = [];
  const loaded = fetch(root.dataset.indexUrl, { credentials: "same-origin" })
    .then((response) => (response.ok ? response.json() : { sections: [] }))
    .then((data) => {
      index = data.sections.map((s) => ({ ...s, haystack: fold(`${s.heading}\n${s.text}`), headingLower: fold(s.heading) }));
    })
    .catch(() => { index = []; });

  const search = (query) => {
    const terms = parseQuery(query);
    if (!terms.length) return { terms, total: 0, results: [] };
    const hits = [];
    for (const section of index) {
      let matches = 0;
      let headingTerms = 0;
      let all = true;
      for (const term of terms) {
        const n = count(section.haystack, term);
        if (!n) { all = false; break; }
        matches += n;
        if (section.headingLower.includes(term)) headingTerms += 1;
      }
      if (all) hits.push({ section, headingTerms, matches });
    }
    hits.sort((a, b) => b.headingTerms - a.headingTerms || b.matches - a.matches
      || a.section.chapterOrder - b.section.chapterOrder || a.section.order - b.section.order);
    return { terms, total: hits.length, results: hits.slice(0, MAX_RESULTS) };
  };

  // ---- Results panel ----------------------------------------------------------------------
  let outcome = { terms: [], total: 0, results: [] };
  let ordered = [];
  let selected = 0;
  let searched = "";
  let lastPointer = null;

  const statusText = () => {
    if (!searched.trim()) return "";
    if (outcome.total === 0) return "No results";
    if (outcome.total > outcome.results.length) return `Showing the first ${outcome.results.length} of ${outcome.total} results`;
    return `${outcome.total} ${outcome.total === 1 ? "result" : "results"}`;
  };

  const resultHref = (section, terms) => {
    const q = terms.length ? `?q=${encodeURIComponent(input.value.trim())}` : "";
    return `${section.url}${q}${section.slug ? `#${section.slug}` : ""}`;
  };

  const showPanel = (show) => {
    const visible = show && searched.trim() !== "";
    panel.hidden = !visible;
    input.setAttribute("aria-expanded", String(visible));
    if (!visible) input.removeAttribute("aria-activedescendant");
  };

  const select = (position) => {
    selected = position;
    for (const option of list.querySelectorAll("[role=option]")) {
      const active = Number(option.dataset.position) === selected;
      option.setAttribute("aria-selected", String(active));
      if (active) {
        input.setAttribute("aria-activedescendant", option.id);
        option.scrollIntoView({ block: "nearest" });
      }
    }
  };

  const draw = () => {
    list.replaceChildren();
    ordered = [];
    const groups = new Map();
    for (const result of outcome.results) {
      const key = result.section.chapter;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(result);
    }
    for (const results of groups.values()) {
      const group = document.createElement("li");
      group.setAttribute("role", "presentation");
      const name = document.createElement("p");
      name.className = "docs-group";
      name.setAttribute("aria-hidden", "true");
      name.textContent = results[0].section.chapterTitle;
      const inner = document.createElement("ul");
      inner.setAttribute("role", "group");
      inner.setAttribute("aria-label", results[0].section.chapterTitle);
      for (const result of results) {
        const position = ordered.push(result) - 1;
        const option = document.createElement("li");
        option.id = `docs-opt-${position}`;
        option.dataset.position = String(position);
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        const link = document.createElement("a");
        link.href = resultHref(result.section, outcome.terms);
        link.tabIndex = -1;
        const heading = document.createElement("span");
        heading.className = "docs-hit-heading";
        if (result.section.parent) {
          const parent = document.createElement("span");
          parent.className = "docs-hit-parent";
          parent.textContent = `${result.section.parent} › `;
          heading.append(parent);
        }
        heading.append(result.section.heading);
        const text = document.createElement("span");
        text.className = "docs-hit-text";
        for (const [part, hit] of snippet(result.section, outcome.terms)) {
          if (hit) {
            const mark = document.createElement("mark");
            mark.className = "doc-hit";
            mark.textContent = part;
            text.append(mark);
          } else {
            text.append(part);
          }
        }
        link.append(heading, text);
        option.append(link);
        option.addEventListener("mousemove", (event) => {
          // Moving the list under a still pointer (arrow keys) fires mousemove too.
          if (lastPointer && lastPointer.x === event.clientX && lastPointer.y === event.clientY) return;
          if (selected !== position) select(position);
        });
        option.addEventListener("click", (event) => {
          event.preventDefault();
          choose(result);
        });
        inner.append(option);
      }
      group.append(name, inner);
      list.append(group);
    }
    const status = statusText();
    panelStatus.textContent = status;
    liveStatus.textContent = status;
    empty.hidden = !(searched.trim() && outcome.total === 0);
    emptyTitle.textContent = `Nothing matches “${searched.trim()}”.`;
    list.hidden = outcome.total === 0;
    select(0);
  };

  const run = async () => {
    await loaded;
    searched = input.value;
    outcome = search(searched);
    draw();
    showPanel(document.activeElement === input);
  };

  let timer = 0;
  input.addEventListener("input", () => {
    clearButton.hidden = input.value === "";
    window.clearTimeout(timer);
    if (input.value.trim() === "") {
      unhighlight();
      setQueryInAddress("");
      run();
      return;
    }
    timer = window.setTimeout(run, DEBOUNCE_MS);
  });
  input.addEventListener("focus", () => showPanel(true));
  input.addEventListener("click", () => showPanel(true));
  input.addEventListener("blur", () => showPanel(false));
  // A click inside the panel must not blur the box first (that would close the panel).
  panel.addEventListener("mousedown", (event) => event.preventDefault());
  document.addEventListener("mousemove", (event) => { lastPointer = { x: event.clientX, y: event.clientY }; });

  input.addEventListener("keydown", (event) => {
    const n = ordered.length;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      showPanel(true);
      if (n) select((selected + (event.key === "ArrowDown" ? 1 : n - 1)) % n);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (input.value.trim() === "") return;
      if (searched !== input.value) { window.clearTimeout(timer); run(); return; }
      showPanel(true);
      if (ordered[selected]) choose(ordered[selected]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      if (input.value !== "") clear();
      else input.blur();
    }
  });

  const clear = () => {
    input.value = "";
    clearButton.hidden = true;
    unhighlight();
    setQueryInAddress("");
    run();
  };
  clearButton.addEventListener("mousedown", (event) => event.preventDefault());
  clearButton.addEventListener("click", () => { clear(); input.focus(); });

  const editable = (el) => el instanceof HTMLElement
    && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
  document.addEventListener("keydown", (event) => {
    const slash = event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey && !editable(event.target);
    const chord = event.key.toLowerCase() === "k" && (event.ctrlKey || event.metaKey) && !event.altKey;
    if (!slash && !chord) return;
    event.preventDefault();
    input.focus();
    input.select();
    showPanel(true);
  });

  // ---- Highlight in the open chapter ------------------------------------------------------
  const body = article.querySelector(".doc-body");

  function unhighlight() {
    for (const mark of body.querySelectorAll("mark.doc-hit")) {
      mark.replaceWith(document.createTextNode(mark.textContent));
    }
    body.normalize();
  }

  const highlight = (terms) => {
    unhighlight();
    if (!terms.length) return;
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    const nodes = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node);
    for (const node of nodes) {
      const marks = ranges(fold(node.nodeValue), terms);
      if (!marks.length) continue;
      const pieces = document.createDocumentFragment();
      let pos = 0;
      for (const [a, b] of marks) {
        if (a > pos) pieces.append(node.nodeValue.slice(pos, a));
        const mark = document.createElement("mark");
        mark.className = "doc-hit";
        mark.textContent = node.nodeValue.slice(a, b);
        pieces.append(mark);
        pos = b;
      }
      if (pos < node.nodeValue.length) pieces.append(node.nodeValue.slice(pos));
      node.replaceWith(pieces);
    }
  };

  function setQueryInAddress(query) {
    const url = new URL(window.location.href);
    if (query) url.searchParams.set("q", query);
    else url.searchParams.delete("q");
    window.history.replaceState(null, "", url);
  }

  const scrollToSlug = (slug) => {
    const target = slug ? document.getElementById(slug) : null;
    if (target) target.scrollIntoView({ block: "start" });
    else window.scrollTo({ top: 0 });
  };

  function choose(result) {
    showPanel(false);
    const query = input.value.trim();
    if (result.section.chapter !== article.dataset.chapter) {
      window.location.assign(resultHref(result.section, outcome.terms));
      return;
    }
    highlight(parseQuery(query));
    const url = new URL(window.location.href);
    url.searchParams.set("q", query);
    url.hash = result.section.slug || "";
    window.history.replaceState(null, "", url);
    scrollToSlug(result.section.slug);
    input.blur();
  }

  // Opened from a result on another chapter: show the query and its highlights.
  const initial = new URL(window.location.href).searchParams.get("q");
  if (initial) {
    input.value = initial;
    clearButton.hidden = false;
    highlight(parseQuery(initial));
    scrollToSlug(decodeURIComponent(window.location.hash.slice(1)));
    run();
  }

  // ---- "On this page" follows the reading position -----------------------------------------
  const tocLinks = [...document.querySelectorAll(".docs-toc a[data-slug]")];
  const headings = tocLinks.map((link) => document.getElementById(link.dataset.slug)).filter(Boolean);
  if (headings.length) {
    let ticking = false;
    const update = () => {
      ticking = false;
      let current = null;
      for (const heading of headings) {
        if (heading.getBoundingClientRect().top <= 96) current = heading.id;
        else break;
      }
      if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) {
        current = headings[headings.length - 1].id;
      }
      for (const link of tocLinks) {
        if (link.dataset.slug === current) link.setAttribute("aria-current", "location");
        else link.removeAttribute("aria-current");
      }
    };
    window.addEventListener("scroll", () => {
      if (!ticking) { ticking = true; requestAnimationFrame(update); }
    }, { passive: true });
    update();
  }

  // The phone "On this page" list closes after a choice.
  const tocMenu = article.querySelector(".docs-toc-menu");
  tocMenu?.addEventListener("click", (event) => {
    if (event.target.closest("a")) tocMenu.open = false;
  });
})();
