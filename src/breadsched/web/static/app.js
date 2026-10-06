// Navigation and start-up. Loaded last: every view's renderer is defined by now.

const RENDERERS = {
  Dashboard: showDashboard, Accounts: showAccounts, Register: showRegister, Scheduled: showScheduled, Payroll: showPayroll,
  "FSA Dashboard": showFsaDashboard, Plan: showPlan, Scenarios: showScenarios,
  Review: showReview, Projection: showProjection, Enter: showEntry, Import: showImport,
  Payees: showPayees, Rules: showRules, Reimbursables: showReimbursables, Goals: showGoals, Verify: showVerify,
  Guide: showGuide,
};

// Views whose workflow has a section in the browser guide get a Help button.
const VIEW_HELP = {
  Scheduled: "scheduled", Payroll: "payroll", Plan: "plan", Projection: "plan",
  Import: "import", Payees: "payees", Rules: "rules", Reimbursables: "reimbursables",
  Goals: "goals",
};

function switchTo(name) { current = name; render(); }

// Open a view in another browser tab, which keeps its own place and scenario.
// The API token travels in the fragment, which the new page removes on load.
function openInNewTab(view, extra = {}) {
  const url = new URL(window.location.href);
  url.search = new URLSearchParams({ view, ...extra }).toString();
  url.hash = new URLSearchParams({ token: apiToken }).toString();
  window.open(url.toString(), "_blank", "noopener");
}

async function render() {
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
    const help = VIEW_HELP[current];
    view.replaceChildren(...(help
      ? [el("div", { class: "toolbar view-help" }, helpButton(help)), content] : [content]));
  } catch (error) {
    view.replaceChildren(el("p", { class: "note neg" }, `Could not load: ${error.message}`));
  }
}

get("/api/summary").then((s) => {
  document.getElementById("book").textContent = s.book || "";
}).catch(() => {});
document.getElementById("print-view").addEventListener("click", () => window.print());
render();
// Like the desktop, offer held GnuCash changes once when the book opens.
openImportReviewDialog().catch(() => {});
