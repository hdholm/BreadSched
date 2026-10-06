// Navigation and start-up. Loaded last: every view's renderer is defined by now.

const RENDERERS = {
  Dashboard: showDashboard, Accounts: showAccounts, Register: showRegister, Scheduled: showScheduled, Payroll: showPayroll,
  "FSA Dashboard": showFsaDashboard, Plan: showPlan, Scenarios: showScenarios,
  Review: showReview, Projection: showProjection, Enter: showEntry, Import: showImport,
  Payees: showPayees, Rules: showRules, Reimbursables: showReimbursables, Goals: showGoals, Verify: showVerify,
  Guide: showGuide,
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
    view.replaceChildren(await RENDERERS[current]());
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
