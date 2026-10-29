// Navigation and start-up. Loaded last: every view's renderer is defined by now.

const RENDERERS = {
  Dashboard: showDashboard, Accounts: showAccounts, Register: showRegister, Find: showFind, Scheduled: showScheduled, Payroll: showPayroll,
  "FSA Dashboard": showFsaDashboard, Plan: showPlan, Scenarios: showScenarios,
  Review: showReview, Projection: showProjection, Enter: showEntry, Import: showImport,
  Rules: showRules, "Reimbursable Expenses": showReimbursables, "Savings Goals": showGoals,
  Verify: showVerify,
  Guide: showGuide,
  Account: showAccount,
};

// Views whose workflow has a section in the browser guide get a Help button.
const VIEW_HELP = {
  Scheduled: "scheduled", Payroll: "payroll", Plan: "plan", Projection: "plan",
  Import: "import", Rules: "rules", "Reimbursable Expenses": "reimbursables",
  "Savings Goals": "goals", Find: "find",
};

function switchTo(name) { current = name; render(); }

// Open a view in another browser tab, which keeps its own place and scenario.
// The API token travels in the fragment, which the new page removes on load.
function openInNewTab(view, extra = {}) {
  const url = new URL(window.location.href);
  url.search = new URLSearchParams({ view, ...extra }).toString();
  url.hash = tokenFragment();
  window.open(url.toString(), "_blank", "noopener");
}

// Each render is numbered: a view chosen while another is still loading wins, and the
// slower, older one is dropped rather than drawn over it.
let renderGeneration = 0;

async function render() {
  const generation = ++renderGeneration;
  const nav = document.getElementById("nav");
  nav.replaceChildren(...VIEWS.map((name) => el("button", {
    class: name === current ? "active" : null, onclick: () => switchTo(name),
  }, name)));
  const view = document.getElementById("view");
  document.body.dataset.view = current;
  document.getElementById("print-title").textContent = current;
  view.replaceChildren(el("p", { class: "note" }, "Loading…"));
  try {
    const content = await RENDERERS[current]();
    if (generation !== renderGeneration) return;
    const help = VIEW_HELP[current];
    view.replaceChildren(...(help
      ? [el("div", { class: "toolbar view-help" }, helpButton(help)), content] : [content]));
  } catch (error) {
    if (generation !== renderGeneration) return;
    view.replaceChildren(el("p", { class: "note neg" }, `Could not load: ${error.message}`));
  }
}

document.getElementById("print-view").addEventListener("click", () => window.print());
ready().then(({ choices }) => {
  // Quick entry is its own page; it needs the token, and the book, too.
  document.getElementById("quick-entry").href = `/quick#${tokenFragment()}`;
  const switcher = document.getElementById("other-books");
  if (choices > 1) {
    switcher.hidden = false;
    switcher.addEventListener("click", otherBooks);
  }
  get("/api/summary").then((s) => {
    document.getElementById("book").textContent = s.book || "";
  }).catch(() => {});
  render();
  // Like the desktop, offer held GnuCash changes once when the book opens.
  openImportReviewDialog().catch(() => {});
});
