// Page state, DOM and number helpers, and the authenticated JSON API calls.

const VIEWS = ["Dashboard", "FSA Dashboard", "Accounts", "Register", "Scheduled", "Payroll", "Plan", "Review", "Projection", "Enter", "Import", "Payees", "Rules", "Reimbursables", "Goals", "Verify", "Guide"];
const launchParams = new URLSearchParams(window.location.search);
let current = VIEWS.includes(launchParams.get("view")) ? launchParams.get("view") : "Dashboard";
let state = {
  accounts: [], account: launchParams.get("account"), plan: null, review: null,
  planPrintDetail: false, expenseCategory: null, expenseIndex: 0, expenseSort: "actual",
  expenseRollover: false,
  scenarioManager: null, scenarioPeriod: null, scenarioEvent: null,
  // A page opened for one scenario ("Open in new tab") starts on it; "" is Base.
  projectionData: null, projectionHandle: launchParams.get("scenario"),
  projectionCompareHandle: null,
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
