// Budget Outlook charts.
// Pie: hover, tap or Tab to a slice to see its category, amount and share.
// Daily spending: hover or tap anywhere to see the nearest day; zoom with the + / − buttons,
// the scroll wheel over the chart, or two fingers; drag sideways to move when zoomed in;
// arrow keys step through the days. Without this script both charts still show.
(() => {
  const money = (value) =>
    `$${Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

  function placeTip(tip, box, x, y) {
    tip.hidden = false;
    const width = box.clientWidth;
    const tipWidth = tip.offsetWidth;
    tip.style.left = `${Math.min(Math.max(x - tipWidth / 2, 0), Math.max(width - tipWidth, 0))}px`;
    tip.style.top = `${Math.max(y - tip.offsetHeight - 12, 0)}px`;
  }

  // ------------------------------------------------------------------ pie chart
  for (const chart of document.querySelectorAll("[data-pie-chart]")) {
    const slices = [...chart.querySelectorAll("path[data-tip]")];
    const tip = chart.querySelector(".chart-tip");
    const readout = chart.querySelector(".chart-readout");
    const legend = chart.closest(".category-chart-layout, .chart-layout")?.querySelectorAll(".chart-legend li") || [];
    let active = null;

    const show = (slice, event) => {
      active = slice;
      for (const other of slices) other.classList.toggle("is-active", other === slice);
      for (const row of legend) row.classList.toggle("is-active", row.dataset.index === slice.dataset.index);
      readout.textContent = slice.dataset.tip;
      tip.textContent = slice.dataset.tip;
      const box = chart.getBoundingClientRect();
      let x;
      let y;
      if (event && event.clientX !== undefined && event.pointerType !== "touch") {
        x = event.clientX - box.left;
        y = event.clientY - box.top;
      } else {
        const r = slice.getBoundingClientRect();
        x = r.left + r.width / 2 - box.left;
        y = r.top + r.height / 2 - box.top;
      }
      placeTip(tip, chart, x, y);
    };
    const clear = () => {
      active = null;
      tip.hidden = true;
      for (const slice of slices) slice.classList.remove("is-active");
      for (const row of legend) row.classList.remove("is-active");
      readout.textContent = readout.dataset.hint;
    };
    readout.textContent = readout.dataset.hint;
    for (const slice of slices) {
      slice.addEventListener("pointermove", (event) => show(slice, event));
      slice.addEventListener("pointerdown", (event) => show(slice, event));
      slice.addEventListener("focus", () => show(slice));
      slice.addEventListener("blur", () => { if (active === slice) clear(); });
    }
    chart.querySelector("svg").addEventListener("pointerleave", (event) => {
      if (event.pointerType === "mouse") clear();
    });
    document.addEventListener("pointerdown", (event) => {
      if (active && !chart.contains(event.target)) clear();
    });
  }

  // ------------------------------------------------------------------ daily chart
  const SVG = "http://www.w3.org/2000/svg";
  const WIDTH = 760;
  const BASE = 220;
  const TOP = 16;
  const MIN_DAYS = 7;

  for (const chart of document.querySelectorAll("[data-daily-chart]")) {
    const source = chart.querySelector("#daily-series");
    const svg = chart.querySelector("svg");
    if (!source || !svg) continue;
    const data = JSON.parse(source.textContent).map((row) => ({
      date: new Date(`${row.date}T12:00:00`),
      amount: Number(row.amount),
    }));
    if (data.length < 2) continue;
    const tip = chart.querySelector(".chart-tip");
    const readout = chart.querySelector(".chart-readout");
    const zoomBar = chart.querySelector(".chart-zoom");
    const startLabel = chart.querySelector("[data-axis-start]");
    const endLabel = chart.querySelector("[data-axis-end]");
    const buttons = Object.fromEntries([...zoomBar.querySelectorAll("button")].map((b) => [b.dataset.zoom, b]));
    const dayText = (d) => d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
    const shortText = (d) => d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    const last = data.length - 1;
    const minDays = Math.min(MIN_DAYS, data.length);
    let lo = 0;
    let hi = last;
    let selected = null;

    zoomBar.hidden = false;
    readout.textContent = readout.dataset.hint;
    svg.setAttribute("tabindex", "0");
    svg.setAttribute("role", "application");
    svg.setAttribute("aria-label", "Daily net spending. Use the arrow keys to move between days, + and − to zoom.");
    svg.style.touchAction = "pan-y";

    const xOf = (index) => ((index - lo) / Math.max(hi - lo, 1)) * WIDTH;
    const peak = () => Math.max(...data.slice(lo, hi + 1).map((d) => d.amount), 1) * 1.08;
    const make = (name, attrs) => {
      const node = document.createElementNS(SVG, name);
      for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
      return node;
    };

    function render() {
      const top = peak();
      const yOf = (amount) => BASE - (amount / top) * (BASE - TOP);
      svg.replaceChildren();
      svg.append(make("line", { x1: 0, y1: BASE, x2: WIDTH, y2: BASE }));
      const points = [];
      for (let i = lo; i <= hi; i += 1) points.push(`${xOf(i).toFixed(1)},${yOf(data[i].amount).toFixed(1)}`);
      svg.append(make("polyline", { class: "daily-line", points: points.join(" ") }));
      const radius = hi - lo > 60 ? 3 : 5;
      for (let i = lo; i <= hi; i += 1) {
        svg.append(make("circle", { class: "daily-point", cx: xOf(i).toFixed(1), cy: yOf(data[i].amount).toFixed(1), r: radius }));
      }
      if (selected !== null && selected >= lo && selected <= hi) {
        const x = xOf(selected).toFixed(1);
        const y = yOf(data[selected].amount).toFixed(1);
        svg.append(make("line", { class: "daily-guide", x1: x, y1: TOP, x2: x, y2: BASE }));
        svg.append(make("circle", { class: "daily-point is-active", cx: x, cy: y, r: radius + 3 }));
      }
      startLabel.textContent = shortText(data[lo].date);
      endLabel.textContent = shortText(data[hi].date);
      buttons.in.disabled = hi - lo + 1 <= minDays;
      buttons.out.disabled = lo === 0 && hi === last;
      buttons.reset.disabled = lo === 0 && hi === last;
      placeSelected();
    }

    function placeSelected() {
      if (selected === null || selected < lo || selected > hi) {
        tip.hidden = true;
        return;
      }
      const text = `${dayText(data[selected].date)} · ${money(data[selected].amount)} spent`;
      readout.textContent = text;
      tip.textContent = text;
      const box = svg.getBoundingClientRect();
      const host = tip.parentElement.getBoundingClientRect();
      const scale = box.width / WIDTH;
      const yOf = (amount) => BASE - (amount / peak()) * (BASE - TOP);
      placeTip(tip, tip.parentElement, box.left - host.left + xOf(selected) * scale,
        box.top - host.top + yOf(data[selected].amount) * (box.height / 240));
    }

    function select(index) {
      selected = Math.min(Math.max(index, 0), last);
      if (selected < lo) { hi -= lo - selected; lo = selected; }
      if (selected > hi) { lo += selected - hi; hi = selected; }
      render();
    }

    function nearestIndex(clientX) {
      const box = svg.getBoundingClientRect();
      const fraction = Math.min(Math.max((clientX - box.left) / box.width, 0), 1);
      return Math.round(lo + fraction * (hi - lo));
    }

    // Show `days` days, keeping the day under `anchor` (an index) in the same place.
    function zoomTo(days, anchor) {
      const width = hi - lo;
      const target = Math.min(Math.max(Math.round(days), minDays), data.length) - 1;
      if (target === width) return false;
      const fraction = width ? (anchor - lo) / width : 0.5;
      let newLo = Math.round(anchor - fraction * target);
      newLo = Math.min(Math.max(newLo, 0), last - target);
      lo = newLo;
      hi = newLo + target;
      render();
      return true;
    }

    function panBy(days) {
      const width = hi - lo;
      const newLo = Math.min(Math.max(lo + days, 0), last - width);
      if (newLo === lo) return false;
      lo = newLo;
      hi = newLo + width;
      render();
      return true;
    }

    const centre = () => (selected !== null && selected >= lo && selected <= hi ? selected : Math.round((lo + hi) / 2));
    buttons.in.addEventListener("click", () => zoomTo((hi - lo + 1) / 1.5, centre()));
    buttons.out.addEventListener("click", () => zoomTo((hi - lo + 1) * 1.5, centre()));
    buttons.reset.addEventListener("click", () => { lo = 0; hi = last; render(); });

    svg.addEventListener("wheel", (event) => {
      const anchor = nearestIndex(event.clientX);
      let changed = false;
      if (Math.abs(event.deltaX) > Math.abs(event.deltaY) || event.shiftKey) {
        const delta = event.deltaX || event.deltaY;
        changed = panBy(Math.sign(delta) * Math.max(1, Math.round((hi - lo) / 10)));
      } else if (event.deltaY < 0) {
        changed = zoomTo((hi - lo + 1) / 1.25, anchor);
      } else if (event.deltaY > 0) {
        changed = zoomTo((hi - lo + 1) * 1.25, anchor);
      }
      // Only keep the page from scrolling when the chart actually changed.
      if (changed) event.preventDefault();
    }, { passive: false });

    const pointers = new Map();
    let pinch = null;
    let drag = null;
    svg.addEventListener("pointerdown", (event) => {
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinch = { distance: Math.hypot(a.x - b.x, a.y - b.y) || 1, days: hi - lo + 1, anchor: nearestIndex((a.x + b.x) / 2) };
        drag = null;
      } else if (pointers.size === 1) {
        drag = { x: event.clientX, lo, moved: false };
      }
    });
    svg.addEventListener("pointermove", (event) => {
      if (!pointers.has(event.pointerId)) {
        if (event.pointerType === "mouse") select(nearestIndex(event.clientX));
        return;
      }
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pinch && pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        const distance = Math.hypot(a.x - b.x, a.y - b.y) || 1;
        zoomTo(pinch.days / (distance / pinch.distance), pinch.anchor);
        event.preventDefault();
      } else if (drag && pointers.size === 1) {
        const box = svg.getBoundingClientRect();
        const daysPerPixel = (hi - lo) / box.width;
        const shift = Math.round((drag.x - event.clientX) * daysPerPixel);
        if (Math.abs(drag.x - event.clientX) > 6) drag.moved = true;
        if (drag.moved && (lo !== 0 || hi !== last)) {
          const width = hi - lo;
          lo = Math.min(Math.max(drag.lo + shift, 0), last - width);
          hi = lo + width;
          render();
        } else if (event.pointerType === "mouse") {
          select(nearestIndex(event.clientX));
        }
      }
    });
    const release = (event) => {
      if (!pointers.has(event.pointerId)) return;
      pointers.delete(event.pointerId);
      if (pointers.size < 2) pinch = null;
      if (event.type === "pointerup" && drag && !drag.moved && pointers.size === 0) {
        select(nearestIndex(event.clientX));
      }
      if (pointers.size === 0) drag = null;
    };
    svg.addEventListener("pointerup", release);
    svg.addEventListener("pointercancel", release);
    svg.addEventListener("pointerleave", (event) => {
      if (event.pointerType === "mouse" && !pointers.size) {
        selected = null;
        tip.hidden = true;
        readout.textContent = readout.dataset.hint;
        render();
      }
    });
    svg.addEventListener("keydown", (event) => {
      const keys = {
        ArrowLeft: () => select((selected ?? lo + 1) - 1),
        ArrowRight: () => select((selected ?? lo - 1) + 1),
        Home: () => select(lo),
        End: () => select(hi),
        "+": () => zoomTo((hi - lo + 1) / 1.5, centre()),
        "=": () => zoomTo((hi - lo + 1) / 1.5, centre()),
        "-": () => zoomTo((hi - lo + 1) * 1.5, centre()),
        Escape: () => { selected = null; render(); readout.textContent = readout.dataset.hint; },
      };
      if (keys[event.key]) {
        keys[event.key]();
        event.preventDefault();
      }
    });
    window.addEventListener("resize", placeSelected);
    render();
  }
})();
