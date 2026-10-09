// The Projection view, its chart, conservation bridges, and month detail.

async function showProjection() {
  let data = state.projectionData;
  if (!data) {
    const handle = state.projectionHandle ?? (state.plan ? state.plan.scenario : null);
    const suffix = handle ? `?scenario=${encodeURIComponent(handle)}` : "";
    data = await get(`/api/projection${suffix}`);
    state.projectionHandle = data.scenario.handle;
    state.projectionData = data;
  }
  const scenarioData = data.scenario;
  const s = data.summary;

  const scenarioPicker = el("select", {
    name: "scenario",
    onchange: (event) => {
      const handle = event.target.value || null;
      state.projectionHandle = handle;
      state.projectionData = null;
      state.projectionCompareHandle = null;
      state.projectionComparison = null;
      if (state.plan) state.plan.scenario = handle;
      render();
    },
  }, data.controls.scenarios.map((item) => el("option", {
    value: item.handle || "",
    selected: (item.handle || "") === (scenarioData.handle || "") ? "selected" : null,
  }, item.name)));

  const comparePicker = el("select", {
    name: "compare_handle",
    onchange: (event) => {
      state.projectionCompareHandle = event.target.value || null;
      state.projectionComparison = null;
    },
  },
    el("option", { value: "" }, "No comparison"),
    data.controls.scenarios
      .filter((item) => (item.handle || "") !== (scenarioData.handle || ""))
      .map((item) => {
        const value = item.handle || "__base__";
        return el("option", {
          value,
          selected: value === (state.projectionCompareHandle || "")
            ? "selected" : null,
        }, item.name);
      }));

  const form = el("form", { class: "panel scenario-panel", onsubmit: async (event) => {
    event.preventDefault();
    const values = new FormData(event.target);
    const payload = {
      handle: scenarioData.handle,
      years: Number(values.get("years")),
      assumptions: readAssumptions(event.target, "projection_"),
    };
    try {
      const compareToken = values.get("compare_handle") || null;
      state.projectionCompareHandle = compareToken;
      if (compareToken !== null) {
        const compareHandle = compareToken === "__base__" ? null : compareToken;
        const compared = await post("/api/projection/compare", {
          ...payload, compare_handle: compareHandle,
        });
        state.projectionData = compared.primary;
        state.projectionComparison = compared.comparison;
        say("Projection comparison recalculated. Changes are not saved yet.");
      } else {
        state.projectionData = await post("/api/projection/calculate", payload);
        state.projectionComparison = null;
        say("Projection recalculated. Changes are not saved yet.");
      }
      render();
    } catch (error) { say(error.message, "error"); }
  } },
    el("div", { class: "toolbar" },
      el("label", {}, "Scenario ", scenarioPicker),
      el("label", {}, "Compare with ", comparePicker),
      el("label", {}, "Years ", el("input", {
        type: "number", name: "years", min: "1", max: "100", step: "1",
        value: scenarioData.years,
      })),
      el("button", { class: "action primary", type: "submit" }, "Apply"),
      el("button", {
        class: "action", type: "button",
        title: "Keep this scenario's projection open in another browser tab",
        onclick: () => openInNewTab("Projection", { scenario: scenarioData.handle || "" }),
      }, "Open in new tab"),
      el("button", {
        class: "action", type: "button",
        onclick: async () => {
          const values = new FormData(form);
          try {
            state.projectionData = await post("/api/projection/save", {
              handle: scenarioData.handle,
              years: Number(values.get("years")),
              assumptions: readAssumptions(form, "projection_"),
            });
            say(scenarioData.handle
              ? "Saved scenario projection changes."
              : "Saved Base assumptions in this book.");
            render();
          } catch (error) { say(error.message, "error"); }
        },
      }, scenarioData.handle ? "Save scenario changes" : "Save Base assumptions")),
    el("h3", {}, "Annual assumptions"),
    assumptionInputs(scenarioData.assumptions, "projection_"),
    el("p", { class: "note" },
      "Apply recalculates this draft without saving it. Base saves only its annual assumptions; "
      + "saved scenarios also retain their projection horizon."));

  const cards = el("div", { class: "cards" },
    [["Ending net worth", s.ending_net_worth], ["Ending cash", s.ending_cash],
     ["Lowest cash", s.minimum_cash]].map(([label, value]) =>
      el("div", { class: "card" },
        el("div", { class: "label" }, label),
        el("div", { class: "value " + (Number(value) < 0 ? "neg" : "") }, money(value)),
        completenessFlag(data.completeness))),
    el("div", { class: "card" },
      el("div", { class: "label" }, "Cash runs out"),
      el("div", { class: "value " + (s.first_shortfall ? "neg" : "") },
        s.first_shortfall || "Never")));

  const projectionValue = (row, index, key, label) => el("button", {
    class: `plan-cell-button ${Number(row[key]) < 0 ? "neg" : ""}`,
    type: "button",
    title: `Explain ${row.label} projection`,
    "aria-label": `Explain ${row.label} ${label}: ${money(row[key])}`,
    onclick: () => openProjectionDetail(index),
  }, money(row[key]));
  const yearly = data.rows.map((row, i) => [row, i])
    .filter(([, i]) => (i + 1) % 12 === 0).map(([row, i]) => el("tr", {},
      el("td", {}, row.label, completenessFlag(row.completeness, " ")),
      el("td", { class: "plan-cell" }, projectionValue(row, i, "income", "income")),
      el("td", { class: "plan-cell" }, projectionValue(row, i, "expense", "expense")),
      el("td", { class: "plan-cell" }, projectionValue(row, i, "cash", "cash")),
      el("td", { class: "plan-cell" }, projectionValue(row, i, "holdings", "holdings")),
      el("td", { class: "plan-cell" }, projectionValue(row, i, "liabilities", "liabilities")),
      el("td", { class: "plan-cell" }, projectionValue(row, i, "net_worth", "net worth"))));

  const comparison = state.projectionComparison;
  let comparisonView = null;
  if (comparison) {
    const delta = comparison.summary_delta;
    const inherited = Object.values(comparison.scenario.assumption_sources || {})
      .filter((source) => source !== comparison.scenario.name).length;
    const compareYearly = comparison.rows.filter((_, i) => (i + 1) % 12 === 0)
      .map((row, i) => el("tr", {},
        el("td", {}, row.label, completenessFlag(row.completeness, " ")),
        el("td", { class: cls(data.rows[(i + 1) * 12 - 1].cash) },
          money(data.rows[(i + 1) * 12 - 1].cash)),
        el("td", { class: cls(row.cash) }, money(row.cash)),
        el("td", { class: cls(row.cash_delta) }, money(row.cash_delta)),
        el("td", { class: cls(data.rows[(i + 1) * 12 - 1].net_worth) },
          money(data.rows[(i + 1) * 12 - 1].net_worth)),
        el("td", { class: cls(row.net_worth) }, money(row.net_worth)),
        el("td", { class: cls(row.net_worth_delta) }, money(row.net_worth_delta))));
    comparisonView = el("section", { class: "panel scenario-panel" },
      el("h3", {}, `${scenarioData.name} vs ${comparison.scenario.name}`),
      el("div", { class: "cards" },
        [["Ending net worth difference", delta.ending_net_worth],
         ["Ending cash difference", delta.ending_cash],
         ["Lowest cash difference", delta.minimum_cash]].map(([label, value]) =>
          el("div", { class: "card" },
            el("div", { class: "label" }, label),
            el("div", { class: "value " + (Number(value) < 0 ? "neg" : "") },
              money(value)),
            completenessFlag(comparison.delta_completeness)))),
      el("p", { class: "note" },
        `Differences are ${scenarioData.name} minus ${comparison.scenario.name}.`),
      el("p", { class: "note runway-comparison" }, comparison.runway_comparison),
      inherited ? el("p", { class: "note" },
        `${comparison.scenario.name} inherits ${inherited} annual assumption(s) through its parent chain.`)
        : null,
      table(["Month", { label: `${scenarioData.name} cash`, num: true },
             { label: `${comparison.scenario.name} cash`, num: true },
             { label: "Cash difference", num: true },
             { label: `${scenarioData.name} net worth`, num: true },
             { label: `${comparison.scenario.name} net worth`, num: true },
             { label: "Net worth difference", num: true }], compareYearly));
  }

  const warnings = data.warnings && data.warnings.length
    ? el("p", { class: "note neg" }, data.warnings.join("  ")) : null;
  const runwayNotes = (data.runway_notes || []).length
    ? el("section", { class: "projection-runway" }, el("h3", {}, "Cash runway"),
      ...data.runway_notes.map((line) => el("p", { class: "note" }, line)))
    : null;
  const goalNotes = (data.goal_notes || []).length
    ? el("section", { class: "projection-goals" }, el("h3", {}, "Savings goals"),
      ...data.goal_notes.map((line) => el("p", { class: "note" }, line)))
    : null;
  const reimbursementNotes = (data.reimbursement_notes || []).length
    ? el("section", { class: "projection-reimbursements" },
      el("h3", {}, "Reimbursable expenses: gross and net cost"),
      ...data.reimbursement_notes.map((line) => el("p", { class: "note" }, line)))
    : null;
  return el("div", {},
    el("h2", {}, "Projection"),
    form, cards, completenessDetails(data.completeness, "Projection"),
    modelChart(data.chart, { collapseTable: true }),
    comparisonView, runwayNotes, goalNotes, reimbursementNotes, warnings,
    (data.bridges || []).length ? el("details", { class: "projection-bridge" },
      el("summary", {}, "How the projection reconciles"),
      el("p", { class: "note" }, "From the first month's opening to the last month's "
        + "closing: planned events plus cash interest, investment performance, and debt "
        + "interest explain every change. Choose a month for its own breakdown."),
      ...bridgeTables(data.bridges)) : null,
    el("p", { class: "note" }, `Scenario "${scenarioData.name}", by year.`),
    table(["Month", { label: "Income", num: true }, { label: "Expense", num: true },
           { label: "Cash", num: true }, { label: "Holdings", num: true },
           { label: "Liabilities", num: true }, { label: "Net worth", num: true }], yearly));
}

// Opening + planned events + assumption effects = closing, one table per stock.
function bridgeTables(bridges) {
  return (bridges || []).map((bridge) => el("div", { class: "bridge" },
    el("h4", {}, bridge.label),
    table(["", { label: "Amount", num: true }, "Basis"], [
      ...bridge.terms.map((term) => el("tr", { class: term.kind === "opening"
          || term.kind === "closing" ? "subtotal" : null },
        el("td", {}, term.label),
        el("td", { class: cls(term.amount) }, money(term.amount)),
        el("td", { class: "muted" }, term.note || (term.kind === "effect"
          ? "Assumption effect" : "")))),
      el("tr", {}, el("td", {}, "Unexplained"),
        el("td", { class: cls(bridge.unexplained) }, money(bridge.unexplained)),
        el("td", { class: bridge.reconciles ? "muted" : "neg" }, bridge.reconciles
          ? "Reconciles exactly" : "Does not reconcile; please report this")),
    ])));
}

async function openProjectionDetail(index) {
  const currentProjection = state.projectionData;
  if (!currentProjection) return;
  const scenario = currentProjection.scenario;
  try {
    const data = await post("/api/projection/explain", {
      handle: scenario.handle,
      years: scenario.years,
      assumptions: scenario.assumptions,
      month_index: index,
    });
    const activityText = (item) => Object.entries(item.activities || {})
      .filter(([_key,value]) => Number(value) !== 0)
      .map(([key,value]) => `${key.replaceAll("_", " ")}: ${money(value)}`).join("; ") || "—";
    const accountRows = (items, showActivity=false) => items.map((item) => el("tr", {},
      el("td", {}, item.name),
      el("td", { class: cls(item.opening) }, money(item.opening)),
      el("td", { class: cls(item.movement) }, money(item.movement)),
      showActivity ? el("td", {}, activityText(item)) : null,
      el("td", { class: cls(item.accrual) }, money(item.accrual)),
      el("td", { class: cls(item.closing) }, money(item.closing)),
      el("td", { class: "num muted" },
        `${(Number(item.annual_rate) * 100).toFixed(2)}% (${item.annual_rate_source})`)));
    const eventRows = data.events.map((item) => el("tr", {},
      el("td", {}, item.when),
      el("td", {}, item.description),
      el("td", { class: "muted" }, item.source),
      el("td", { class: "muted" }, item.status),
      el("td", { class: "muted" }, (item.amount_explanations || [])
        .map((detail) => `${detail.account}: ${detail.source}`).join("; ") || "—"),
      el("td", { class: cls(item.expected_amount) }, money(item.expected_amount)),
      el("td", { class: item.actual_amount == null ? "num muted" : cls(item.actual_amount) },
        item.actual_amount == null ? "—" : money(item.actual_amount)),
      el("td", { class: item.variance == null ? "num muted" : cls(item.variance) },
        item.variance == null ? "—" : money(item.variance))));
    const backdrop = el("div", {
      class: "detail-backdrop",
      onclick: (event) => { if (event.target === backdrop) backdrop.remove(); },
    });
    const rateCards = [["Income growth", "income_growth"],
      ["Expense inflation", "expense_inflation"],
      ["Investment return", "investment_return"],
      ["Cash interest", "cash_interest"],
      ["Liability interest", "liability_interest"]];
    backdrop.append(el("section", { class: "detail-dialog" },
      el("div", { class: "detail-heading" },
        el("div", {}, el("h2", {}, `${data.label} projection explanation`),
          el("div", { class: "note" },
            `${scenario.name}; exact events and accruals for this reporting month`)),
        el("button", { class: "action", type: "button", onclick: () => backdrop.remove() },
          "Close")),
      el("div", { class: "cards detail-summary" },
        [["Opening cash", data.cash.opening], ["Cash flow", data.cash.flow],
         ["Cash interest", data.cash.interest], ["Closing cash", data.cash.closing],
         ["Holdings", data.holdings.closing], ["Liabilities", data.liabilities.closing],
         ["Net worth", data.net_worth]].map(([label, value]) => el("div", { class: "card" },
          el("div", { class: "label" }, label),
          el("div", { class: `value ${Number(value) < 0 ? "neg" : ""}` }, money(value))))),
      el("h3", {}, "Active annual assumptions"),
      el("div", { class: "cards" }, rateCards.map(([label, field]) => el("div", { class: "card" },
        el("div", { class: "label" }, label),
        el("div", { class: "value" },
          `${(Number(data.assumptions[field]) * 100).toFixed(2)}%`),
        el("div", { class: "note" }, `From ${data.assumption_sources[field]}`)))),
      el("h3", {}, "How this month reconciles"),
      el("p", { class: "note" }, "Each closing balance is its opening balance plus the "
        + "planned events and the interest and performance the assumptions produce."),
      ...bridgeTables(data.bridges),
      el("h3", {}, "Exact planned events"),
      ...(data.escrow_explanations || []).map((line) =>
        el("p", { class: "note" }, line)),
      eventRows.length ? table(["Date", "Description", "Source", "Status", "Amount basis",
        {label:"Expected",num:true}, {label:"Actual",num:true}, {label:"Variance",num:true}], eventRows)
        : el("p", { class: "note" }, "No planned events occur in this month."),
      el("h3", {}, "Investment accounts"),
      data.holdings.accounts.length ? table(["Account", {label:"Opening",num:true},
        {label:"Movement",num:true}, "Activity", {label:"Growth",num:true},
        {label:"Closing",num:true}, {label:"Annual rate",num:true}],
        accountRows(data.holdings.accounts, true))
        : el("p", { class: "note" }, "No projected investment accounts."),
      el("h3", {}, "Liability accounts"),
      data.liabilities.accounts.length ? table(["Account", {label:"Opening",num:true},
        {label:"Principal movement",num:true}, {label:"Interest",num:true},
        {label:"Closing",num:true}, {label:"Annual rate",num:true}],
        accountRows(data.liabilities.accounts))
        : el("p", { class: "note" }, "No projected liabilities.")));
    document.body.append(backdrop);
  } catch (error) { say(error.message, "error"); }
}
