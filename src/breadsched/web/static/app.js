// Navigation and start-up. Loaded last: every view's renderer is defined by now.

const RENDERERS = {
  Dashboard: showDashboard, Accounts: showAccounts, Register: showRegister, Find: showFind, Scheduled: showScheduled, Payroll: showPayroll,
  "FSA Dashboard": showFsaDashboard, Plan: showPlan, Scenarios: showScenarios,
  Review: showReview, Projection: showProjection, Reports: showReports, Enter: showEntry, Import: showImport,
  Rules: showRules, "Reimbursable Expenses": showReimbursables, "Savings Goals": showGoals,
  Verify: showVerify,
  Guide: showGuide,
  Account: showAccount,
};

// Views whose workflow has a section in the browser guide get a Help button.
const VIEW_HELP = {
  Scheduled: "scheduled", Payroll: "payroll", Plan: "plan", Projection: "plan",
  Import: "import", Rules: "rules", "Reimbursable Expenses": "reimbursables",
  "Savings Goals": "goals", Find: "find", Reports: "reports",
};

function switchTo(name) { current = name; render(); }

// The navigation bar: each group is a button that opens its menu of views; a menu
// closes when a view is chosen, on Escape, or on a click elsewhere.
function closeNavMenus(except = null) {
  for (const menu of document.querySelectorAll("#nav .nav-menu")) {
    if (menu === except) continue;
    menu.hidden = true;
    menu.previousElementSibling.setAttribute("aria-expanded", "false");
  }
}

function renderNav() {
  const nav = document.getElementById("nav");
  const item = (name) => el("button", {
    type: "button", "data-view": name, class: name === current ? "active" : null,
    "aria-current": name === current ? "page" : null,
    onclick: () => { closeNavMenus(); switchTo(name); },
  }, name);
  nav.replaceChildren(...NAV_GROUPS.map(([name, views]) => {
    if (!views) return item(name);
    const menu = el("div", { class: "nav-menu", role: "menu", hidden: "" }, ...views.map(item));
    const toggle = el("button", {
      type: "button", class: views.includes(current) ? "nav-group active" : "nav-group",
      "data-group": name, "aria-haspopup": "true", "aria-expanded": "false",
      onclick: () => {
        const opening = menu.hidden;
        closeNavMenus(menu);
        menu.hidden = !opening;
        toggle.setAttribute("aria-expanded", String(opening));
        if (opening) menu.querySelector("button").focus();
      },
    }, views.includes(current) ? `${name}: ${current}` : name);
    return el("div", { class: "nav-group-wrap" }, toggle, menu);
  }));
}

document.addEventListener("click", (event) => {
  if (!event.target.closest("#nav .nav-group-wrap")) closeNavMenus();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeNavMenus();
});

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
  renderNav();
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
