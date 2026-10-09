// Page state, DOM and number helpers, and the authenticated JSON API calls.

const VIEWS = ["Dashboard", "FSA Dashboard", "Accounts", "Register", "Scheduled", "Payroll", "Plan", "Review", "Projection", "Enter", "Import", "Rules", "Reimbursables", "Goals", "Verify", "Guide"];
const launchParams = new URLSearchParams(window.location.search);
let current = VIEWS.includes(launchParams.get("view")) ? launchParams.get("view") : "Dashboard";
let state = {
  accounts: [], account: launchParams.get("account"), plan: null, review: null,
  planPrintDetail: false, expenseCategory: null, expenseIndex: 0, expenseSort: "actual",
  expenseRollover: false,
  scenarioManager: null, scenarioPeriod: null, scenarioEvent: null, scenarioDrawdown: null,
  // A page opened for one scenario ("Open in new tab") starts on it; "" is Base.
  projectionData: null, projectionHandle: launchParams.get("scenario"),
  projectionCompareHandle: null,
  // A Help button opens a page on the Guide at its workflow's heading.
  guideTopic: launchParams.get("help"),
  projectionComparison: null,
};
let historicalEstimateDialog = null;
let importReviewDialog = null;
let dueReviewDialog = null;

const el = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
};

const svgEl = (tag, attrs = {}, text = null) => {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value));
  if (text !== null) node.append(document.createTextNode(String(text)));
  return node;
};

const depthClass = (kind, depth) => {
  const bounded = Math.min(12, Math.max(0, Number(depth) || 0));
  return `${kind} ${kind}-${bounded}`;
};

const money = (value) => {
  const n = Number(value);
  const text = Math.abs(n).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  return n < 0 ? `(${text})` : text;
};
const signedMoney = (value) => {
  if (value === null || value === undefined) return "—";
  const rendered = money(value);
  return Number(value) > 0 ? `+${rendered}` : rendered;
};
const cls = (value) => (Number(value) < 0 ? "num neg" : "num");
// Valuation completeness (#236): a textual label beside a partial or withheld
// value, and an expandable list of what was left out and how to include it.
const completenessFlag = (coverage, prefix = "") => {
  if (!coverage || coverage.status === "complete") return null;
  return el("span", {
    class: `completeness-flag ${coverage.status}`,
    title: (coverage.detail || []).join("\n"),
  }, `${prefix}${coverage.label}`);
};
const completenessDetails = (coverage, what) => {
  if (!coverage || coverage.status === "complete") return null;
  return el("details", { class: `completeness-details ${coverage.status}` },
    el("summary", {}, `${what}: ${coverage.label}`),
    el("ul", {}, ...(coverage.detail || []).map((line) => el("li", {}, line))));
};

const params = new URLSearchParams(window.location.hash.slice(1));
const apiToken = params.get("token") || "";
if (params.has("token")) history.replaceState(null, "", window.location.pathname + window.location.search);
const apiHeaders = () => ({ "X-BreadSched-Token": apiToken });
const browserNumberFormat = (() => {
  const decimal = new Intl.NumberFormat().formatToParts(1.1)
    .find((part) => part.type === "decimal")?.value;
  return decimal === "," ? "comma" : "dot";
})();

async function get(path) {
  const response = await fetch(path, { headers: apiHeaders() });
  if (!response.ok) throw new Error((await response.json()).error || response.statusText);
  return response.json();
}
async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { ...apiHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({ number_format: browserNumberFormat, ...(body || {}) }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

// Contextual help: open this workflow's section of the browser guide in another
// tab, so a form or dialog in progress here is kept.
const helpButton = (topic) => el("button", {
  class: "action help", type: "button", "data-help": topic,
  title: "Open the guide section for this page in a new tab",
  onclick: () => openInNewTab("Guide", { help: topic }),
}, "Help");
const helpHeading = (text, topic, attrs = {}) => el("div", { class: "help-heading" },
  el("h2", attrs, text), helpButton(topic));

function say(text, kind) {
  const box = document.getElementById("message");
  box.textContent = text;
  box.className = kind || "ok";
  setTimeout(() => box.classList.add("hidden"), 4000);
}

function table(headers, rows) {
  // Rows are <tr> elements or arrays of cell values; an array becomes one row whose
  // cells follow the header's numeric alignment.
  const body = rows.map((row) => Array.isArray(row)
    ? el("tr", {}, ...row.map((cell, index) =>
      el("td", { class: headers[index]?.num ? "num" : null }, cell)))
    : row);
  return el("div", { class: "panel" },
    el("table", {},
      el("thead", {}, el("tr", {}, headers.map((h) =>
        el("th", { class: h.num ? "num" : null }, h.label ?? h)))),
      el("tbody", {}, body)));
}

// An engine chart (gen/engine/chart_model) as grouped columns with a legend, each
// column's exact amount on hover, and the table of its values beside it. The scale
// and marks follow presentation.charts: round ticks including zero, columns at most
// 24 wide with a 2-unit gap, a rounded data end and a square foot on the baseline.
function chartTicks(low, high, count = 4) {
  low = Math.min(low, 0); high = Math.max(high, 0);
  if (low === high) high = low + 1;
  const raw = (high - low) / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw);
  const ticks = [];
  for (let value = Math.floor(low / step) * step; value < high + step * 0.999; value += step) {
    ticks.push(Math.round(value * 1e10) / 1e10);
  }
  return ticks;
}

function columnPath(x, y, width, height, negative) {
  const r = Math.min(4, width / 2, height);
  if (negative) {
    return `M${x},${y}h${width}v${height - r}a${r},${r} 0 0 1 ${-r},${r}`
      + `h${-(width - 2 * r)}a${r},${r} 0 0 1 ${-r},${-r}z`;
  }
  return `M${x},${y + height}v${-(height - r)}a${r},${r} 0 0 1 ${r},${-r}`
    + `h${width - 2 * r}a${r},${r} 0 0 1 ${r},${r}v${height - r}z`;
}

function chartLabelIndices(count, most = 8) {
  if (count <= most) return [...Array(count).keys()];
  const stride = Math.ceil(count / most);
  const chosen = [];
  for (let index = 0; index < count; index += stride) chosen.push(index);
  if (chosen[chosen.length - 1] !== count - 1) {
    if (count - 1 - chosen[chosen.length - 1] < stride) chosen[chosen.length - 1] = count - 1;
    else chosen.push(count - 1);
  }
  return chosen;
}

function modelChart(model, { collapseTable = false } = {}) {
  if (!model) return null;
  const values = model.series.flatMap((series) => series.values)
    .filter((value) => value !== null && (model.kind !== "bars" || Number(value) !== 0))
    .map(Number);
  if (!model.categories.length || !values.length) return null;
  const width = 900, height = 280, left = 78, top = 34, right = 12, bottom = 30;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const ticks = chartTicks(Math.min(...values), Math.max(...values));
  const low = ticks[0], high = ticks[ticks.length - 1];
  const y = (value) => top + plotHeight * (high - value) / (high - low);
  const count = model.categories.length;
  const currency = model.currency ? ` ${model.currency}` : "";
  const label = model.currency ? `${model.title} (${model.currency})` : model.title;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img",
    "aria-label": label });
  const lines = model.kind !== "bars";
  const xs = lines
    ? model.categories.map((_c, index) => left + (count > 1 ? plotWidth * index / (count - 1) : plotWidth / 2))
    : model.categories.map((_c, index) => left + plotWidth / count * (index + 0.5));
  if (lines && model.partial_from !== null && model.partial_from !== undefined) {
    svg.append(svgEl("rect", { x: xs[model.partial_from], y: top, class: "chart-partial",
      width: Math.max(width - right - xs[model.partial_from], 2), height: plotHeight }));
  }
  for (const value of ticks) {
    svg.append(svgEl("line", { x1: left, x2: width - right, y1: y(value), y2: y(value),
      class: value === 0 ? "chart-axis" : "chart-grid", "stroke-width": 1 }));
    svg.append(svgEl("text", { x: left - 8, y: y(value) + 4, "text-anchor": "end" },
      value.toLocaleString(undefined, { maximumFractionDigits: 0 })));
  }
  if (lines) {
    for (const marker of model.markers || []) {
      const x = xs[marker.index];
      if (x === undefined) continue;
      svg.append(svgEl("line", { x1: x, x2: x, y1: top, y2: top + plotHeight,
        class: "chart-marker", "stroke-width": 1 }));
      svg.append(svgEl("text", { x: x + 4, y: top + 12, class: "chart-marker-label" },
        marker.label));
    }
    for (const series of model.series) {
      let path = "", drawing = false;
      series.values.forEach((value, index) => {
        if (value === null) { drawing = false; return; }
        path += `${drawing ? "L" : "M"}${xs[index].toFixed(1)},${y(Number(value)).toFixed(1)}`;
        drawing = true;
      });
      svg.append(svgEl("path", { d: path, fill: "none", stroke: `var(--series-${series.slot})`,
        "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    }
    const step = plotWidth / Math.max(count - 1, 1);
    model.categories.forEach((category, index) => {
      const hit = svgEl("rect", { x: xs[index] - step / 2, y: top, width: step,
        height: plotHeight, fill: "transparent", class: "chart-hit" });
      hit.append(svgEl("title", {}, [category, ...model.series.map((series) =>
        `${series.name}: ${series.values[index] === null ? "—" : money(series.values[index])}${currency}`)]
        .join("\n")));
      svg.append(hit);
    });
  } else {
    const baseline = y(0);
    const band = plotWidth / count;
    const columns = model.series.length;
    const bar = Math.min(24, Math.max(1, (band * 0.7 - 2 * (columns - 1)) / columns));
    const group = bar * columns + 2 * (columns - 1);
    model.categories.forEach((category, index) => {
      model.series.forEach((series, position) => {
        const value = series.values[index];
        if (value === null || Number(value) === 0) return;
        const number = Number(value);
        const [topY, bottomY] = [y(number), baseline].sort((a, b) => a - b);
        const path = svgEl("path", {
          d: columnPath(xs[index] - group / 2 + position * (bar + 2), topY, bar,
            Math.max(bottomY - topY, 1), number < 0),
          fill: `var(--series-${series.slot})`,
        });
        path.append(svgEl("title", {}, `${category}, ${series.name}: ${money(value)}${currency}`));
        svg.append(path);
      });
    });
  }
  for (const index of chartLabelIndices(count)) {
    // A line's end points sit on the plot's edges: keep their labels inside.
    const anchor = !lines || (index > 0 && index < count - 1) ? "middle"
      : index === 0 ? "start" : "end";
    svg.append(svgEl("text", { x: xs[index], y: height - bottom + 16, "text-anchor": anchor },
      model.categories[index]));
  }
  const legend = svgEl("g", { class: "chart-legend" });
  let x = left;
  for (const series of model.series) {
    legend.append(lines
      ? svgEl("line", { x1: x, y1: 16, x2: x + 14, y2: 16, stroke: `var(--series-${series.slot})`,
        "stroke-width": 2, "stroke-linecap": "round" })
      : svgEl("rect", { x, y: 10, width: 12, height: 12, rx: 2,
        fill: `var(--series-${series.slot})` }));
    legend.append(svgEl("text", { x: x + 18, y: 20 }, series.name));
    x += 36 + 7 * series.name.length;
  }
  svg.append(legend);
  const valuesTable = table(["", ...model.series.map((series) => ({ label: series.name, num: true }))],
    model.categories.map((category, index) => el("tr", {},
      el("td", {}, category),
      ...model.series.map((series) => el("td", { class: cls(series.values[index]) },
        series.values[index] === null ? "—" : money(series.values[index]))))));
  return el("figure", { class: "model-chart" },
    el("figcaption", { class: "note" }, label), svg,
    model.partial_note ? el("p", { class: "note neg chart-partial-note" }, model.partial_note) : null,
    collapseTable
      ? el("details", { class: "chart-values" }, el("summary", {}, "Chart values"), valuesTable)
      : valuesTable);
}
