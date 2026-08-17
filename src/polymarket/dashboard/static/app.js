"use strict";

/* Dashboard over a read-only capture database.
 *
 * Charts are hand-drawn SVG rather than a charting library: the whole page is
 * served off localhost with no network, and the shapes needed here (a few line
 * charts with a shared crosshair) are smaller than the loader for a library.
 *
 * Series colours are set through CSS custom properties on the `style` property
 * rather than as presentation attributes, so a theme switch recolours every
 * chart already on screen without a re-render.
 */

const POLL_MS = 5000;
const SPARK_POINTS = 100;

const state = {
  overview: null,
  tab: null,
  match: null,       // condition_id of the open match, or null
  detail: null,
  series: null,
  range: "all",      // all | 15m | 1h | 3h
  depthOutcome: 0,
  showTable: false,
  tournament: "",
  search: "",
  sort: "natural",
  pulse: null,
  error: null,
};

const RANGES = { "15m": 900, "1h": 3600, "3h": 10800, all: null };

// How the score feed says a match stopped. Worth spelling out: a retirement or a
// walkover is a very different price history from a match played to the end.
const ENDINGS = {
  FT: "Finished",
  RET: "Retired",
  WO: "Walkover",
  CAN: "Cancelled",
  ABD: "Abandoned",
  POST: "Postponed",
};

/* ------------------------------------------------------------ formatting */

const nbsp = " ";

function fmtPrice(v, digits = 3) {
  return v === null || v === undefined ? "—" : v.toFixed(digits);
}

function fmtSize(v) {
  if (v === null || v === undefined) return "—";
  if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (Math.abs(v) >= 1000) return (v / 1000).toFixed(1) + "K";
  if (Number.isInteger(v)) return String(v);
  return Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1);
}

function fmtSigned(v, digits = 3) {
  if (v === null || v === undefined) return "—";
  return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(digits);
}

function fmtClock(ts, withSeconds = false) {
  if (!ts) return "—";
  const opts = { hour: "2-digit", minute: "2-digit" };
  if (withSeconds) opts.second = "2-digit";
  return new Date(ts * 1000).toLocaleTimeString([], opts);
}

function fmtDateTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return sameDay ? time : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + time;
}

function fmtDuration(seconds) {
  if (!seconds || seconds < 0) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  if (h >= 1) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (seconds >= 60) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds)}s`;
}

function fmtRelative(ts) {
  if (!ts) return "—";
  const delta = ts - Date.now() / 1000;
  const ahead = delta > 0;
  const mag = fmtDuration(Math.abs(delta));
  return ahead ? `in ${mag}` : `${mag} ago`;
}

/* ------------------------------------------------------------ DOM helpers */

function h(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;   // untrusted data goes here
    else if (key === "style") Object.assign(node.style, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of [].concat(children)) {
    if (child) node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

const SVGNS = "http://www.w3.org/2000/svg";

function s(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "style") Object.assign(node.style, value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) if (child) node.appendChild(child);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/* ------------------------------------------------------------ scales */

function niceTicks(min, max, count) {
  if (!isFinite(min) || !isFinite(max)) return [];
  if (min === max) return [min];
  const raw = (max - min) / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  // Round the step to the NEAREST of 1/2/5/10, not up to it. Rounding up turns
  // a 0.023 ideal step into 0.05, which on a narrow price range leaves the
  // whole chart with a single gridline.
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const ticks = [];
  for (let t = Math.ceil(min / step) * step; t <= max + step * 1e-6; t += step) {
    ticks.push(Math.round(t / step) * step);
  }
  return ticks;
}

function extent(arrays) {
  let min = Infinity, max = -Infinity;
  for (const values of arrays) {
    for (const v of values) {
      if (v === null || v === undefined || !isFinite(v)) continue;
      if (v < min) min = v;
      if (v > max) max = v;
    }
  }
  return min <= max ? [min, max] : null;
}

/* ------------------------------------------------------------ line chart */

const PAD = { top: 16, right: 62, bottom: 26, left: 48 };

/* opts: { ts, series:[{name,values,color,width}], format, height, minSpan,
 *         clamp:[lo,hi], zeroBased, refLine, markers, scoreAt, unit } */
function lineChart(container, opts) {
  const render = () => drawLineChart(container, opts);
  render();
  if (container._observer) container._observer.disconnect();
  container._observer = new ResizeObserver(() => render());
  container._observer.observe(container);
}

function drawLineChart(container, opts) {
  clear(container);
  const ts = opts.ts || [];
  const series = (opts.series || []).filter((serie) => serie.values.some((v) => v !== null && v !== undefined));

  if (ts.length < 2 || series.length === 0) {
    container.appendChild(h("p", { class: "chart-empty", text: opts.emptyText || "No snapshots in this window yet." }));
    return;
  }

  const width = Math.max(320, container.clientWidth || 640);
  const height = opts.height || 220;
  const innerW = width - PAD.left - PAD.right;
  const innerH = height - PAD.top - PAD.bottom;
  const format = opts.format || ((v) => fmtPrice(v));

  let [lo, hi] = extent(series.map((serie) => serie.values)) || [0, 1];
  const dataLo = lo;
  if (opts.zeroBased) lo = Math.min(0, lo);
  const minSpan = opts.minSpan || 0;
  if (hi - lo < minSpan) {
    const mid = (hi + lo) / 2;
    lo = mid - minSpan / 2;
    hi = mid + minSpan / 2;
  }
  if (hi === lo) { hi = lo + 1; }
  const padY = (hi - lo) * 0.08;
  lo -= padY; hi += padY;
  if (opts.clamp) {
    lo = Math.max(opts.clamp[0], lo);
    hi = Math.min(opts.clamp[1], hi);
  }
  // Spread and depth cannot go below zero, so the padding must not invent a
  // negative axis tick under them.
  if (opts.zeroBased) lo = Math.min(0, dataLo);

  const t0 = ts[0], t1 = ts[ts.length - 1];
  const x = (t) => PAD.left + (t1 === t0 ? innerW / 2 : ((t - t0) / (t1 - t0)) * innerW);
  const y = (v) => PAD.top + innerH - ((v - lo) / (hi - lo)) * innerH;

  const svg = s("svg", {
    viewBox: `0 0 ${width} ${height}`,
    width: width,
    height: height,
    role: "img",
    "aria-label": opts.ariaLabel || "time series chart",
  });

  /* --- grid + y axis (hairline, solid, recessive) --- */
  for (const tick of niceTicks(lo, hi, 4)) {
    if (tick < lo || tick > hi) continue;
    svg.appendChild(s("line", {
      x1: PAD.left, x2: PAD.left + innerW, y1: y(tick), y2: y(tick),
      style: { stroke: "var(--gridline)", strokeWidth: 1 },
    }));
    svg.appendChild(s("text", {
      x: PAD.left - 8, y: y(tick) + 3.5, "text-anchor": "end", text: format(tick),
      style: { fill: "var(--text-muted)", fontSize: "10.5px", fontVariantNumeric: "tabular-nums" },
    }));
  }

  /* --- even-money reference, where it means something --- */
  if (opts.refLine !== undefined && opts.refLine > lo && opts.refLine < hi) {
    svg.appendChild(s("line", {
      x1: PAD.left, x2: PAD.left + innerW, y1: y(opts.refLine), y2: y(opts.refLine),
      style: { stroke: "var(--baseline)", strokeWidth: 1 },
    }));
    // Inside the plot, not out in the label gutter: on a market sitting near
    // even, the end labels are exactly where this would otherwise land. Drawn
    // with a surface-coloured halo, because a market near even is also the case
    // where the lines themselves run right through here.
    svg.appendChild(s("text", {
      x: PAD.left + innerW - 4, y: y(opts.refLine) - 5, "text-anchor": "end", text: opts.refLabel || "",
      style: {
        fill: "var(--text-muted)", fontSize: "10px",
        paintOrder: "stroke", stroke: "var(--surface-1)", strokeWidth: "3px", strokeLinejoin: "round",
      },
    }));
  }

  /* --- score markers: where the match moved on court --- */
  let lastLabelX = -Infinity;
  for (const marker of opts.markers || []) {
    if (marker.ts < t0 || marker.ts > t1) continue;
    const mx = x(marker.ts);
    svg.appendChild(s("line", {
      x1: mx, x2: mx, y1: PAD.top, y2: PAD.top + innerH,
      style: { stroke: "var(--baseline)", strokeWidth: 1, opacity: marker.major ? 0.85 : 0.4 },
    }));
    if (marker.major && marker.label && mx - lastLabelX > 52) {
      svg.appendChild(s("text", {
        x: mx + 3, y: PAD.top - 4, text: marker.label,
        style: { fill: "var(--text-muted)", fontSize: "10px" },
      }));
      lastLabelX = mx;
    }
  }

  /* --- x axis --- */
  svg.appendChild(s("line", {
    x1: PAD.left, x2: PAD.left + innerW, y1: PAD.top + innerH, y2: PAD.top + innerH,
    style: { stroke: "var(--baseline)", strokeWidth: 1 },
  }));
  const tickCount = Math.max(2, Math.min(6, Math.floor(innerW / 90)));
  for (let i = 0; i <= tickCount; i++) {
    const t = t0 + ((t1 - t0) * i) / tickCount;
    svg.appendChild(s("text", {
      x: x(t), y: PAD.top + innerH + 15,
      "text-anchor": i === 0 ? "start" : i === tickCount ? "end" : "middle",
      text: fmtClock(t),
      style: { fill: "var(--text-muted)", fontSize: "10.5px", fontVariantNumeric: "tabular-nums" },
    }));
  }

  /* --- the lines: a null is a real gap, so it breaks the path --- */
  for (const serie of series) {
    let run = [];
    const flush = () => {
      if (run.length === 1) {
        svg.appendChild(s("circle", {
          cx: run[0][0], cy: run[0][1], r: 1.6, style: { fill: serie.color },
        }));
      } else if (run.length > 1) {
        svg.appendChild(s("path", {
          d: "M" + run.map(([px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join("L"),
          fill: "none",
          style: {
            stroke: serie.color,
            strokeWidth: serie.width || 2,
            strokeLinejoin: "round",
            strokeLinecap: "round",
            strokeDasharray: serie.dash || "none",
            opacity: serie.opacity || 1,
          },
        }));
      }
      run = [];
    };
    serie.values.forEach((v, i) => {
      if (v === null || v === undefined || !isFinite(v)) flush();
      else run.push([x(ts[i]), y(Math.min(hi, Math.max(lo, v)))]);
    });
    flush();
  }

  /* --- direct end labels, nudged apart with leader lines when they converge --- */
  const ends = [];
  for (const serie of series) {
    let last = -1;
    for (let i = serie.values.length - 1; i >= 0; i--) {
      const v = serie.values[i];
      if (v !== null && v !== undefined && isFinite(v)) { last = i; break; }
    }
    if (last < 0) continue;
    ends.push({ serie, value: serie.values[last], y: y(Math.min(hi, Math.max(lo, serie.values[last]))), x: x(ts[last]) });
  }
  ends.sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) {
    if (ends[i].y - ends[i - 1].y < 13) ends[i].y = ends[i - 1].y + 13;
  }
  for (const end of ends) {
    const labelX = PAD.left + innerW + 8;
    if (Math.abs(end.y - y(end.value)) > 1.5) {
      svg.appendChild(s("line", {
        x1: end.x + 4, y1: y(end.value), x2: labelX - 2, y2: end.y,
        style: { stroke: "var(--baseline)", strokeWidth: 1 },
      }));
    }
    // ring in the surface colour, so overlapping end-dots stay legible
    svg.appendChild(s("circle", {
      cx: end.x, cy: y(end.value), r: 4,
      style: { fill: end.serie.color, stroke: "var(--surface-1)", strokeWidth: 2 },
    }));
    svg.appendChild(s("text", {
      x: labelX, y: end.y + 3.5, text: format(end.value),
      style: { fill: "var(--text-primary)", fontSize: "11px", fontWeight: 600, fontVariantNumeric: "tabular-nums" },
    }));
  }

  /* --- crosshair layer: readers aim at a time, not at a 2px line --- */
  const crosshair = s("line", {
    y1: PAD.top, y2: PAD.top + innerH,
    style: { stroke: "var(--text-muted)", strokeWidth: 1, opacity: 0 },
  });
  svg.appendChild(crosshair);
  const dots = series.map((serie) =>
    s("circle", { r: 4, style: { fill: serie.color, stroke: "var(--surface-1)", strokeWidth: 2, opacity: 0 } })
  );
  dots.forEach((dot) => svg.appendChild(dot));

  const hit = s("rect", {
    x: PAD.left, y: PAD.top, width: innerW, height: innerH,
    fill: "transparent", tabindex: "0",
    style: { cursor: "crosshair", outline: "none" },
  });
  svg.appendChild(hit);

  let index = -1;
  const moveTo = (i, clientX, clientY) => {
    if (i < 0 || i >= ts.length) return;
    index = i;
    crosshair.setAttribute("x1", x(ts[i]));
    crosshair.setAttribute("x2", x(ts[i]));
    crosshair.style.opacity = 1;
    series.forEach((serie, k) => {
      const v = serie.values[i];
      const known = v !== null && v !== undefined && isFinite(v);
      dots[k].style.opacity = known ? 1 : 0;
      if (known) {
        dots[k].setAttribute("cx", x(ts[i]));
        dots[k].setAttribute("cy", y(Math.min(hi, Math.max(lo, v))));
      }
    });
    showTooltip(clientX, clientY, ts[i], series, i, format, opts);
  };

  const fromPointer = (event) => {
    const box = svg.getBoundingClientRect();
    const px = ((event.clientX - box.left) / box.width) * width;
    const t = t0 + ((px - PAD.left) / innerW) * (t1 - t0);
    let best = 0, bestDist = Infinity;
    for (let i = 0; i < ts.length; i++) {
      const dist = Math.abs(ts[i] - t);
      if (dist < bestDist) { bestDist = dist; best = i; }
    }
    moveTo(best, event.clientX, event.clientY);
  };

  hit.addEventListener("pointermove", fromPointer);
  hit.addEventListener("pointerdown", fromPointer);
  hit.addEventListener("focus", () => {
    const box = hit.getBoundingClientRect();
    moveTo(ts.length - 1, box.right - 10, box.top + 10);
  });
  const leave = () => {
    crosshair.style.opacity = 0;
    dots.forEach((dot) => { dot.style.opacity = 0; });
    hideTooltip();
  };
  hit.addEventListener("pointerleave", leave);
  hit.addEventListener("blur", leave);
  hit.addEventListener("keydown", (event) => {
    const step = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
    if (!step) return;
    event.preventDefault();
    const box = hit.getBoundingClientRect();
    moveTo(Math.min(ts.length - 1, Math.max(0, (index < 0 ? ts.length - 1 : index) + step)), box.left + 20, box.top + 10);
  });

  container.appendChild(svg);
}

/* ------------------------------------------------------------ tooltip */

const tooltipEl = () => document.getElementById("tooltip");

function showTooltip(clientX, clientY, ts, series, index, format, opts) {
  const node = clear(tooltipEl());
  node.appendChild(h("div", { class: "tt-when", text: fmtClock(ts, true) }));

  for (const serie of series) {
    const v = serie.values[index];
    const row = h("div", { class: "tt-row" }, [
      h("span", { class: "tt-key", style: { background: serie.color } }),
      h("span", { class: "tt-name", text: serie.name }),
      h("span", { class: "tt-value", text: v === null || v === undefined ? "no quote" : format(v) }),
    ]);
    node.appendChild(row);
  }

  if (opts && opts.scoreAt) {
    const score = opts.scoreAt(ts);
    if (score) node.appendChild(h("div", { class: "tt-note", text: score }));
  }

  node.hidden = false;
  const box = node.getBoundingClientRect();
  const left = Math.min(window.innerWidth - box.width - 10, Math.max(8, clientX + 14));
  const top = Math.min(window.innerHeight - box.height - 10, Math.max(8, clientY - box.height - 12));
  node.style.left = `${left}px`;
  node.style.top = `${top}px`;
}

function hideTooltip() {
  tooltipEl().hidden = true;
}

/* ------------------------------------------------------------ sparkline */

function sparkline(values, { width = 92, height = 24 } = {}) {
  const known = values.map((v, i) => [i, v]).filter(([, v]) => v !== null && v !== undefined);
  const svg = s("svg", {
    class: "spark", viewBox: `0 0 ${width} ${height}`, width, height,
    "aria-hidden": "true", focusable: "false",
  });
  if (known.length < 2) return svg;

  const values_ = known.map(([, v]) => v);
  let lo = Math.min(...values_), hi = Math.max(...values_);
  if (hi - lo < 0.02) { const mid = (hi + lo) / 2; lo = mid - 0.01; hi = mid + 0.01; }
  const n = values.length - 1 || 1;
  const px = (i) => (i / n) * (width - 4) + 2;
  const py = (v) => height - 3 - ((v - lo) / (hi - lo)) * (height - 6);

  svg.appendChild(s("path", {
    d: "M" + known.map(([i, v]) => `${px(i).toFixed(1)},${py(v).toFixed(1)}`).join("L"),
    fill: "none",
    style: { stroke: "var(--series-1)", strokeWidth: 1.5, strokeLinejoin: "round", strokeLinecap: "round" },
  }));
  const [lastI, lastV] = known[known.length - 1];
  svg.appendChild(s("circle", {
    cx: px(lastI), cy: py(lastV), r: 2.4,
    style: { fill: "var(--series-1)", stroke: "var(--surface-1)", strokeWidth: 1.5 },
  }));
  return svg;
}

/* ------------------------------------------------------------ data */

async function getJSON(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const body = await response.json();
      detail = body.error || body.detail || detail;
    } catch (_) { /* keep the status */ }
    throw new Error(detail);
  }
  return response.json();
}

async function loadOverview() {
  try {
    state.overview = await getJSON("/api/overview");
    state.error = null;
  } catch (err) {
    state.error = err.message;
  }
  renderStatus();
  renderTournaments();
  renderTabs();
  if (!state.match) renderList();
}

async function loadDetail(cid) {
  const params = new URLSearchParams({ points: "900" });
  const window_ = RANGES[state.range];
  try {
    const detail = await getJSON(`/api/match/${cid}`);
    if (window_ && detail.last_ts) params.set("since", String(detail.last_ts - window_));
    const series = await getJSON(`/api/match/${cid}/series?${params}`);
    state.detail = detail;
    state.series = series;
    state.error = null;
  } catch (err) {
    state.error = err.message;
  }
  renderDetail();
}

/* ------------------------------------------------------------ list view */

function matchesFilter(match) {
  if (state.tournament && match.tournament !== state.tournament) return false;
  if (state.search) {
    const needle = state.search.toLowerCase();
    const hay = [match.question, ...(match.players || [])].join(" ").toLowerCase();
    if (!hay.includes(needle)) return false;
  }
  return true;
}

function sortMatches(matches) {
  const sorted = matches.slice();
  const by = {
    natural: (a, b) => (a.start_epoch || 0) - (b.start_epoch || 0),
    move: (a, b) => Math.abs(b.move || 0) - Math.abs(a.move || 0),
    snapshots: (a, b) => b.snapshots - a.snapshots,
    spread: (a, b) => (spreadOf(b) ?? -1) - (spreadOf(a) ?? -1),
  };
  // Past matches read best newest-first; everything else soonest-first.
  if (state.sort === "natural" && state.tab === "past") return sorted.sort((a, b) => (b.start_epoch || 0) - (a.start_epoch || 0));
  return sorted.sort(by[state.sort] || by.natural);
}

function spreadOf(match) {
  const values = (match.prices || []).map((p) => (p ? p.spread : null)).filter((v) => v !== null && v !== undefined);
  return values.length ? Math.max(...values) : null;
}

function renderList() {
  const container = document.getElementById("cards");
  const empty = document.getElementById("empty");
  const overview = state.overview;

  if (!overview) {
    clear(container);
    empty.hidden = false;
    empty.textContent = state.error ? `Cannot read the capture database — ${state.error}` : "Loading…";
    return;
  }

  const matches = sortMatches(overview.matches.filter((m) => m.state === state.tab && matchesFilter(m)));
  const total = overview.matches.filter((m) => m.state === state.tab).length;

  document.getElementById("filter-count").textContent =
    total === 0 ? "" : matches.length === total ? `${total} match${total === 1 ? "" : "es"}` : `${matches.length} of ${total}`;

  clear(container);
  empty.hidden = matches.length > 0;
  if (!matches.length) {
    empty.textContent = total
      ? "Nothing matches those filters."
      : {
          live: "No match is being played right now. Upcoming matches are picked up as they start.",
          upcoming: "No scheduled matches recorded yet — run the capture with --include-upcoming to poll them before they start.",
          past: "No finished matches recorded yet.",
        }[state.tab];
    return;
  }

  for (const match of matches) container.appendChild(matchCard(match));
}

function matchCard(match) {
  const overdue = match.state === "upcoming" && match.start_epoch && match.start_epoch < Date.now() / 1000;

  // The badge earns its place by saying something the rest of the card doesn't:
  // the set marker while live, the countdown while waiting, the reason it ended.
  const badge =
    match.state === "live"
      ? h("span", { class: "badge badge-live", text: match.period || "Live" })
      : overdue
        ? h("span", { class: "badge badge-overdue", text: "Overdue" })
        : match.state === "upcoming"
          ? h("span", { class: "badge", text: match.start_epoch ? `starts ${fmtRelative(match.start_epoch)}` : "Scheduled" })
          : h("span", { class: "badge", text: ENDINGS[match.period] || "Ended" });

  const when =
    match.state === "live"
      ? fmtClock(match.last_ts)
      : match.state === "upcoming"
        ? fmtDateTime(match.start_epoch)
        : match.last_ts ? fmtDateTime(match.last_ts) : "—";

  const players = h("div", { class: "players" },
    match.players.map((name, i) => {
      const price = match.prices[i];
      const mid = price ? price.mid : null;
      return h("div", { class: "player-row" }, [
        h("span", { class: `swatch swatch-${i}` }),
        h("span", { class: "player-name", text: name || `Outcome ${i}` }),
        h("span", {
          class: mid === null || mid === undefined ? "player-price is-null" : "player-price",
          text: mid === null || mid === undefined ? "no quote" : fmtPrice(mid, 3),
        }),
      ]);
    })
  );

  // A move of exactly zero is the common case on a market nobody is trading;
  // printing "0.000" on every card buries the ones that did move.
  const move = match.move;
  const moved = move !== null && move !== undefined && Math.abs(move) >= 0.0005;
  const moveClass = !moved ? "delta" : move > 0 ? "delta up" : "delta down";

  const card = h("button", { class: "card", type: "button", onclick: () => openMatch(match.condition_id) }, [
    h("div", { class: "card-top" }, [
      h("span", { class: "card-meta", text: match.tournament || "—" }),
      h("span", { class: "card-when", text: when }),
    ]),
    h("div", { class: "card-top" }, [badge, match.score ? h("span", { class: "card-score", text: match.score }) : null]),
    players,
    h("div", { class: "card-foot" }, [
      h("div", {}, [
        sparkline(match.spark || []),
        h("span", { class: moveClass, text: moved ? `${fmtSigned(move)} ${match.players[0].split(" ").pop()}` : "" }),
      ]),
      h("span", { class: "card-stats", text: `${match.snapshots.toLocaleString()} snaps` }),
    ]),
  ]);
  card.setAttribute("aria-label", `${match.question}. ${match.score || match.state}`);
  return card;
}

/* ------------------------------------------------------------ detail view */

function openMatch(cid) {
  state.match = cid;
  state.detail = null;
  state.series = null;
  location.hash = `match/${cid}`;
  document.getElementById("list-view").hidden = true;
  document.getElementById("detail-view").hidden = false;
  window.scrollTo(0, 0);
  renderDetail();
  loadDetail(cid);
}

function closeMatch() {
  state.match = null;
  state.detail = null;
  state.series = null;
  location.hash = `tab/${state.tab}`;
  document.getElementById("detail-view").hidden = true;
  document.getElementById("list-view").hidden = false;
  renderList();
}

function scoreMarkers(events) {
  const markers = [];
  let previous = null;
  for (const event of events || []) {
    if (!previous || event.period !== previous.period || event.score !== previous.score) {
      markers.push({
        ts: event.ts,
        major: !previous || event.period !== previous.period,
        label: [event.period, event.score].filter(Boolean).join(" "),
      });
    }
    previous = event;
  }
  return markers;
}

function scoreLookup(events) {
  const sorted = (events || []).slice().sort((a, b) => a.ts - b.ts);
  return (ts) => {
    let found = null;
    for (const event of sorted) {
      if (event.ts <= ts) found = event; else break;
    }
    if (!found) return null;
    const label = [found.period, found.score].filter(Boolean).join(" ");
    return label ? `Score: ${label}` : null;
  };
}

function renderDetail() {
  const view = clear(document.getElementById("detail-view"));
  const detail = state.detail;

  if (!detail) {
    view.appendChild(h("button", { class: "back-btn", type: "button", text: "← All matches", onclick: closeMatch }));
    view.appendChild(h("p", { class: "empty", text: state.error ? `Could not load this match — ${state.error}` : "Loading…" }));
    return;
  }

  const colors = ["var(--series-1)", "var(--series-2)"];
  const series = state.series;
  const markers = scoreMarkers(detail.score_events);
  const scoreAt = scoreLookup(detail.score_events);

  /* --- header --- */
  view.appendChild(
    h("div", { class: "detail-head" }, [
      h("button", { class: "back-btn", type: "button", text: "← All matches", onclick: closeMatch }),
      h("div", { class: "detail-title" }, [
        h("h2", { text: detail.players.join("  vs  ") }),
        h("div", { class: "sub", text: [
          detail.tournament,
          detail.state === "live" ? `live · ${[detail.period, detail.score].filter(Boolean).join(" ")}`
            : detail.state === "upcoming" ? `starts ${fmtDateTime(detail.start_epoch)} (${fmtRelative(detail.start_epoch)})`
            : `ended · ${[detail.period, detail.score].filter(Boolean).join(" ") || "no final score recorded"}`,
          detail.market_type === "moneyline" ? "match winner" : detail.market_type,
        ].filter(Boolean).join(" · ") }),
      ]),
    ])
  );

  /* --- stat tiles --- */
  const tiles = h("div", { class: "tiles" });
  detail.players.forEach((name, i) => {
    const book = detail.books[i];
    tiles.appendChild(
      h("div", { class: "tile" }, [
        h("div", { class: "tile-label" }, [h("span", { class: `swatch swatch-${i}` }), h("span", { text: name })]),
        h("div", {
          class: book && book.mid !== null && book.mid !== undefined ? "tile-value" : "tile-value small",
          text: book && book.mid !== null && book.mid !== undefined ? fmtPrice(book.mid, 3) : "no quote",
        }),
        h("div", { class: "tile-sub", text: book ? `buy ${fmtPrice(book.ask)} · sell ${fmtPrice(book.bid)}` : "—" }),
      ])
    );
  });
  const widest = Math.max(...detail.books.map((b) => (b && b.spread) || 0));
  tiles.appendChild(
    h("div", { class: "tile" }, [
      h("div", { class: "tile-label", text: "Widest spread" }),
      h("div", { class: "tile-value", text: widest ? fmtPrice(widest) : "—" }),
      h("div", { class: "tile-sub", text: "cost of crossing the book" }),
    ])
  );
  tiles.appendChild(
    h("div", { class: "tile" }, [
      h("div", { class: "tile-label", text: "Captured" }),
      h("div", { class: "tile-value", text: detail.snapshots.toLocaleString() }),
      h("div", {
        class: "tile-sub",
        text: detail.first_ts ? `over ${fmtDuration(detail.last_ts - detail.first_ts)} · updated ${fmtRelative(detail.last_ts)}` : "nothing yet",
      }),
    ])
  );
  view.appendChild(tiles);

  if (!series || !series.ts.length) {
    view.appendChild(h("div", { class: "panel" }, [h("p", { class: "chart-empty", text: "No order-book snapshots recorded for this match yet." })]));
    return;
  }

  /* --- range control, scoping every chart below it --- */
  const rangeRow = h("div", { class: "panel-head" }, [
    h("h3", { text: "History" }),
    h("div", { class: "seg", role: "group", "aria-label": "Time range" },
      Object.keys(RANGES).map((key) =>
        h("button", {
          type: "button",
          "aria-pressed": String(state.range === key),
          text: key === "all" ? "All" : key,
          onclick: () => { state.range = key; loadDetail(detail.condition_id); },
        })
      )
    ),
  ]);

  /* --- price history --- */
  const priceSeries = series.outcomes.map((outcome, i) => ({
    name: outcome.name || `Outcome ${i}`,
    values: outcome.mid,
    color: colors[i],
  }));
  const hasTrade = series.last_trade.some((v) => v !== null && v !== undefined);
  if (hasTrade) {
    priceSeries.push({
      name: `Last trade (as ${detail.players[0]})`,
      values: series.last_trade,
      color: "var(--series-3)",
      width: 1.5,
      dash: "3 3",
    });
  }

  const priceChart = h("div", { class: "chart-wrap" });
  view.appendChild(
    h("div", { class: "panel" }, [
      rangeRow,
      legend(priceSeries),
      priceChart,
      h("p", { class: "note", text:
        "Prices are probabilities: what one share of that player winning costs. The two sides sum to about 1." +
        (markers.length
          ? `  The ${markers.length} vertical rules are score changes, set changes labelled; hover anywhere to read the score at that moment.`
          : "") +
        (hasTrade
          ? "  Polymarket reports one last-traded price per match, oriented to whichever side traded last, and the record does not say which side that was — the dotted line is that price re-expressed as " +
            detail.players[0] + ", inferred by which of the two mids it sits nearer to. It is least reliable when the mids are close together."
          : "") +
        (series.decimated ? `  Chart shows ${series.points.toLocaleString()} of ${series.total_points.toLocaleString()} snapshots; the table below and the database hold every one.` : "") },
      ),
    ])
  );
  lineChart(priceChart, {
    ts: series.ts,
    series: priceSeries,
    format: (v) => fmtPrice(v, 3),
    height: 260,
    minSpan: 0.08,
    clamp: [0, 1],
    refLine: 0.5,
    refLabel: "even",
    markers,
    scoreAt,
    ariaLabel: `Price history for ${detail.question}`,
  });

  /* --- spread & liquidity --- */
  const spreadSeries = series.outcomes.map((outcome, i) => ({
    name: outcome.name || `Outcome ${i}`,
    values: outcome.spread,
    color: colors[i],
  }));
  const gaps = series.outcomes.map((outcome, i) => ({
    name: detail.players[i],
    count: outcome.mid.filter((v) => v === null || v === undefined).length,
  }));
  const spreadChart = h("div", { class: "chart-wrap" });
  view.appendChild(
    h("div", { class: "panel" }, [
      h("div", { class: "panel-head" }, [
        h("h3", { text: "Spread" }),
        h("span", { class: "hint", text: "how far apart the best bid and best ask sit" }),
      ]),
      legend(spreadSeries),
      spreadChart,
      h("p", { class: "note", text:
        gaps.some((g) => g.count)
          ? "Gaps in a line are ticks with no quote on one side — normal once a match is effectively decided. " +
            gaps.filter((g) => g.count).map((g) => `${g.name}: ${g.count} of ${series.points}`).join(", ") + "."
          : "Both sides were quoted on every tick in this window." }),
    ])
  );
  lineChart(spreadChart, {
    ts: series.ts,
    series: spreadSeries,
    format: (v) => fmtPrice(v, 3),
    height: 180,
    zeroBased: true,
    minSpan: 0.02,
    markers,
    scoreAt,
    ariaLabel: `Spread over time for ${detail.question}`,
  });

  /* --- depth over time, one player at a time --- */
  const depth = series.outcomes[state.depthOutcome];
  const depthSeries = [
    { name: "Bid depth", values: depth.bid_depth, color: "var(--bid)" },
    { name: "Ask depth", values: depth.ask_depth, color: "var(--ask)" },
  ];
  const depthChart = h("div", { class: "chart-wrap" });
  view.appendChild(
    h("div", { class: "panel" }, [
      h("div", { class: "panel-head" }, [
        h("h3", { text: "Order-book depth" }),
        h("div", { class: "seg", role: "group", "aria-label": "Player" },
          detail.players.map((name, i) =>
            h("button", {
              type: "button", "aria-pressed": String(state.depthOutcome === i), text: name,
              onclick: () => { state.depthOutcome = i; renderDetail(); },
            })
          )
        ),
      ]),
      legend(depthSeries),
      depthChart,
      h("p", { class: "note", text: `Shares resting within the top ${detail.depth} levels on each side of ${detail.players[state.depthOutcome]}'s book — every level the capture stores. Falling depth means liquidity is leaving.` }),
    ])
  );
  lineChart(depthChart, {
    ts: series.ts,
    series: depthSeries,
    format: fmtSize,
    height: 180,
    zeroBased: true,
    markers,
    scoreAt,
    ariaLabel: `Order book depth over time for ${detail.players[state.depthOutcome]}`,
  });

  /* --- live ladder --- */
  view.appendChild(
    h("div", { class: "panel" }, [
      h("div", { class: "panel-head" }, [
        h("h3", { text: "Order book" }),
        h("span", { class: "hint", text: detail.last_ts ? `as of ${fmtClock(detail.last_ts, true)}` : "" }),
      ]),
      h("div", { class: "ladders" }, detail.books.map((book, i) => ladder(book, detail.players[i], i))),
    ])
  );

  /* --- table view: every charted value, reachable without hovering --- */
  const tableBody = h("div");
  view.appendChild(
    h("div", { class: "panel" }, [
      h("div", { class: "panel-head" }, [
        h("h3", { text: "Table view" }),
        h("button", {
          class: "ghost-btn", type: "button",
          text: state.showTable ? "Hide" : "Show",
          onclick: () => { state.showTable = !state.showTable; renderDetail(); },
        }),
      ]),
      state.showTable ? seriesTable(series, detail) : h("p", { class: "note", text: "The same numbers the charts plot, as text." }),
    ])
  );
}

function legend(series) {
  return h("div", { class: "legend" },
    series.map((serie) =>
      h("span", { class: "legend-item" }, [
        h("span", {
          class: "legend-key",
          style: serie.dash
            ? { background: "none", borderTop: `2px dotted ${serie.color}`, height: "0" }
            : { background: serie.color },
        }),
        h("span", { text: serie.name }),
      ])
    )
  );
}

function ladder(book, player, index) {
  const wrap = h("div", {}, [
    h("div", { class: "ladder-title" }, [h("span", { class: `swatch swatch-${index}` }), h("span", { text: player })]),
  ]);
  if (!book || (!book.bids.length && !book.asks.length)) {
    wrap.appendChild(h("p", { class: "ladder-empty", text: "No resting orders recorded." }));
    return wrap;
  }

  const maxSize = Math.max(...[...book.bids, ...book.asks].map((level) => level.size || 0), 1);
  const row = (level, side) =>
    h("tr", { class: side === "bid" ? "bid-row" : "ask-row" }, [
      h("td", { class: "px", text: fmtPrice(level.price) }),
      h("td", { class: "sz", text: fmtSize(level.size) }),
      h("td", { class: "bar-cell" }, [
        h("div", { class: `depth-bar ${side}`, style: { width: `${Math.max(2, ((level.size || 0) / maxSize) * 100)}%` } }),
      ]),
    ]);

  const table = h("table", { class: "ladder" }, [
    h("thead", {}, [h("tr", {}, [
      h("th", { text: "Price" }), h("th", { text: "Size" }), h("th", { text: "" }),
    ])]),
    h("tbody", {}, [
      ...book.asks.slice().reverse().map((level) => row(level, "ask")),
      h("tr", { class: "ladder-mid" }, [
        h("td", { text: "mid " + fmtPrice(book.mid) }),
        h("td", { class: "sz", text: "spread" }),
        h("td", { class: "sz", text: fmtPrice(book.spread) }),
      ]),
      ...book.bids.map((level) => row(level, "bid")),
    ]),
  ]);
  wrap.appendChild(table);
  wrap.appendChild(h("p", { class: "note", text: "Asks above, bids below — buying costs the lowest ask, selling earns the highest bid." }));
  return wrap;
}

function seriesTable(series, detail) {
  const head = ["Time", ...detail.players.flatMap((p) => [`${p} mid`, `${p} spread`]), "Last trade"];
  const rows = [];
  for (let i = series.ts.length - 1; i >= 0; i--) {
    rows.push(
      h("tr", {}, [
        h("td", { text: fmtClock(series.ts[i], true) }),
        ...series.outcomes.flatMap((outcome) => [
          h("td", { text: fmtPrice(outcome.mid[i]) }),
          h("td", { text: fmtPrice(outcome.spread[i]) }),
        ]),
        h("td", { text: fmtPrice(series.last_trade[i]) }),
      ])
    );
  }
  return h("div", { class: "table-scroll" }, [
    h("table", { class: "data-table" }, [
      h("thead", {}, [h("tr", {}, head.map((label) => h("th", { text: label })))]),
      h("tbody", {}, rows),
    ]),
  ]);
}

/* ------------------------------------------------------------ chrome */

function renderStatus() {
  const node = document.getElementById("capture-status");
  const text = node.querySelector(".status-text");
  node.classList.remove("is-live", "is-stale", "is-error");

  if (state.error) {
    node.classList.add("is-error");
    text.textContent = state.error;
    return;
  }
  const overview = state.overview;
  if (!overview) { text.textContent = "connecting…"; return; }

  if (overview.capturing) {
    node.classList.add("is-live");
    text.textContent = `capturing · last tick ${fmtRelative(overview.last_tick)}`;
  } else {
    node.classList.add("is-stale");
    text.textContent = overview.last_tick ? `idle · last tick ${fmtRelative(overview.last_tick)}` : "no snapshots yet";
  }
}

function renderTournaments() {
  const select = document.getElementById("filter-tournament");
  const names = state.overview ? state.overview.tournaments : [];
  if (select.dataset.names === names.join("|")) return;
  select.dataset.names = names.join("|");
  const current = state.tournament;
  clear(select);
  select.appendChild(h("option", { value: "", text: "All" }));
  for (const name of names) select.appendChild(h("option", { value: name, text: name }));
  select.value = names.includes(current) ? current : "";
  state.tournament = select.value;
}

function renderTabs() {
  const counts = state.overview ? state.overview.counts : { live: 0, upcoming: 0, past: 0 };
  for (const [key, value] of Object.entries(counts)) {
    document.querySelector(`[data-count="${key}"]`).textContent = value;
  }
  if (!state.tab) {
    state.tab = counts.live ? "live" : counts.upcoming ? "upcoming" : "past";
  }
  for (const tab of document.querySelectorAll(".tab")) {
    tab.setAttribute("aria-selected", String(tab.dataset.tab === state.tab));
  }
}

function selectTab(name) {
  state.tab = name;
  if (state.match) closeMatch(); else location.hash = `tab/${name}`;
  renderTabs();
  renderList();
}

/* ------------------------------------------------------------ boot */

function applyStoredTheme() {
  const stored = localStorage.getItem("pm-theme");
  if (stored) document.documentElement.setAttribute("data-theme", stored);
}

function toggleTheme() {
  const root = document.documentElement;
  const dark = root.getAttribute("data-theme")
    ? root.getAttribute("data-theme") === "dark"
    : window.matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  root.setAttribute("data-theme", next);
  localStorage.setItem("pm-theme", next);
  if (state.match) renderDetail();  // re-measure end labels against the new surface
}

function readHash() {
  const hash = location.hash.replace(/^#/, "");
  if (hash.startsWith("match/")) return { match: hash.slice(6) };
  if (hash.startsWith("tab/")) return { tab: hash.slice(4) };
  return {};
}

async function poll() {
  try {
    const pulse = await getJSON("/api/pulse");
    const key = `${pulse.last_ts}:${pulse.rows}`;
    if (key === state.pulse) return;   // nothing new; leave the render alone
    state.pulse = key;

    document.getElementById("cards").classList.add("is-refetching");
    await loadOverview();
    if (state.match) await loadDetail(state.match);
  } catch (err) {
    state.error = err.message;
    renderStatus();
  } finally {
    document.getElementById("cards").classList.remove("is-refetching");
  }
}

function init() {
  applyStoredTheme();
  document.getElementById("theme-toggle").addEventListener("click", toggleTheme);

  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => selectTab(tab.dataset.tab));
  }

  document.getElementById("filter-tournament").addEventListener("change", (event) => {
    state.tournament = event.target.value;
    renderList();
  });
  let searchTimer;
  document.getElementById("filter-search").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    const value = event.target.value.trim();
    searchTimer = setTimeout(() => { state.search = value; renderList(); }, 140);
  });
  document.getElementById("filter-sort").addEventListener("change", (event) => {
    state.sort = event.target.value;
    renderList();
  });

  // Back and forward move between the list and a match, so the hash has to be
  // read on every change, not only at boot. Each branch compares against the
  // state it would set, so routing our own hash writes back in is a no-op.
  window.addEventListener("hashchange", () => {
    const target = readHash();
    if (target.match) {
      if (target.match !== state.match) openMatch(target.match);
    } else if (state.match) {
      closeMatch();
    } else if (target.tab && target.tab !== state.tab) {
      selectTab(target.tab);
    }
  });

  const route = readHash();
  if (route.tab) state.tab = route.tab;

  loadOverview().then(() => {
    if (route.match) openMatch(route.match);
  });

  setInterval(poll, POLL_MS);
  // Timestamps on screen are relative ("4m ago"), so they go stale without new data.
  setInterval(() => { if (state.overview) renderStatus(); }, 15000);
}

document.addEventListener("DOMContentLoaded", init);
