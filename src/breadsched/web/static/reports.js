// Reports: report breadlets, their options, and a document drawn from report_document_json.

// One block of a breadlet's document. A chart's exact values are the table block
// after it, so a chart is drawn without its own table.
function documentBlock(block) {
  if (block.type === "heading") return el("h2", {}, block.text);
  if (block.type === "paragraph") {
    if (block.bullet) return el("ul", {}, el("li", {}, block.text));
    return el("p", { class: block.warning ? "note neg" : "note" }, block.text);
  }
  if (block.type === "cards") {
    return el("div", { class: "cards" }, ...block.items.map((card) =>
      el("div", { class: "card" }, el("div", { class: "label" }, card.label),
        el("div", { class: card.alarm ? "value neg" : "value" }, card.value))));
  }
  if (block.type === "chart") return modelChart(block.model, { withTable: false });
  if (!block.rows.length) return el("p", { class: "note" }, block.empty);
  const columns = block.columns.map((column) => ({ label: column.label, num: column.numeric }));
  const styles = { heading: "total-row", section: "total-row", total: "total-row", grand: "total-row" };
  return table(columns, block.rows.map((row) => el("tr", { class: styles[row.style] || null },
    ...row.cells.map((cell, index) => {
      const numeric = cell.numeric || (columns[index] && columns[index].num);
      const kind = numeric ? (cell.negative ? "num neg" : "num") : null;
      const attrs = { class: kind, title: cell.hint || null,
        colspan: cell.span > 1 ? cell.span : null,
        style: cell.indent ? `padding-left: ${0.5 + cell.indent}em` : null };
      const note = cell.note
        ? [cell.below ? el("br") : " ", el("span", { class: "note" }, cell.note)] : [];
      return el("td", attrs, cell.text, ...note);
    }))));
}

function renderDocument(doc, { titled = true } = {}) {
  const parts = titled
    ? [el("h2", { class: "document-title" }, doc.title),
      doc.subtitle ? el("p", { class: "note" }, doc.subtitle) : null]
    : [];
  for (const section of doc.sections) {
    for (const block of section.blocks) parts.push(documentBlock(block));
  }
  return el("div", { class: "breadlet-document", "data-kind": doc.kind }, ...parts);
}

// The options form a breadlet declares (gen/plug/_breadlets.BreadletOption).
function breadletOptions(breadlet) {
  const inputs = {};
  const fields = breadlet.options.map((option) => {
    let input;
    if (option.kind === "choice") {
      input = el("select", {}, ...option.choices.map(([value, label]) =>
        el("option", { value, selected: value === option.default ? "selected" : null }, label)));
    } else if (option.kind === "flag") {
      input = el("input", { type: "checkbox" });
      input.checked = ["true", "yes", "1", "on"].includes(String(option.default).toLowerCase());
    } else {
      input = el("input", { type: option.kind === "date" ? "date" : "number",
        value: option.default, min: option.minimum, max: option.maximum,
        placeholder: option.help || null });
    }
    input.name = option.key;
    inputs[option.key] = input;
    return el("label", { title: option.help || null }, `${option.label} `, input);
  });
  const values = () => Object.fromEntries(Object.entries(inputs).map(([key, input]) =>
    [key, input.type === "checkbox" ? String(input.checked) : input.value]));
  return { fields, values };
}

async function runBreadlet(kind, id, values = {}) {
  const query = new URLSearchParams({ kind, id, ...values });
  return get(`/api/breadlet?${query}`);
}

// The interactive reports, listed before the report breadlets; a breadlet with the
// same key is the same report and is not listed twice. Each one either draws into
// the page (a section) or opens its own dialog over it.
const INTERACTIVE_REPORTS = [
  { id: "expense-explorer", name: "Expense Explorer",
    description: "Each category's plan and actual by period, its trend, and the merchants behind it",
    section: async (form) => expenseExplorerPanel(form) },
  { id: "budget-jars", name: "Budget Jars",
    description: "Each jar's fills, draws and level, by account and period",
    open: (form) => openBudgetJars(form.from, form.through, form.period) },
  { id: "net-worth", name: "Net Worth History",
    description: "What you own less what you owe at each period end, at market value",
    section: async () => netWorthHistory() },
  { id: "holdings", name: "Holdings and Cost Basis",
    description: "Each security holding's open lots and cost basis", open: () => openHoldings() },
  { id: "realized-gains", name: "Realized Gains",
    description: "Every sale with the lots it took, and yearly totals",
    open: () => openRealizedGains(null) },
  { id: "tax-year", name: "Tax Year",
    description: "One calendar year's gains, tax-relevant totals and income by source",
    open: () => openTaxYear(null) },
];

// The months a range may cover, from the book's first month (Plan bounds).
async function reportRange() {
  const bounds = await get("/api/v1/plan/bounds");
  const today = new Date();
  const first = `${today.getFullYear()}-01-01`;
  const from = first < bounds.minimum ? bounds.minimum : first;
  return { from, through: `${today.getFullYear()}-12-31`, period: "month" };
}

async function showReports() {
  const listed = await get("/api/breadlets?kind=report");
  const known = new Set(INTERACTIVE_REPORTS.map((item) => item.id));
  const reports = [...INTERACTIVE_REPORTS,
    ...listed.breadlets.filter((item) => !known.has(item.id))];
  const output = el("div", { class: "breadlet-output" });
  const controls = el("div", { class: "toolbar breadlet-options" });
  const picker = el("select", { "aria-label": "Report" }, ...reports.map((item) =>
    el("option", { value: item.id, selected: item.id === state.report ? "selected" : null },
      item.name)));
  const describe = el("p", { class: "note" });
  let run = async () => {};
  const runButton = () => el("button", { class: "action primary", type: "button",
    onclick: () => run() }, "Run");
  const fail = (error) => output.replaceChildren(el("p", { class: "note neg" }, error.message));
  const choose = async () => {
    state.report = picker.value;
    const report = reports.find((item) => item.id === picker.value);
    describe.textContent = report.description;
    if (report.section || report.open) {
      // The interactive reports take a range where they have one, and keep their own controls.
      const range = state.reportRange || await reportRange();
      state.reportRange = range;
      const ranged = report.id === "expense-explorer" || report.id === "budget-jars";
      const fields = ranged ? [
        el("label", {}, "From ", el("input", { type: "date", name: "from", value: range.from })),
        el("label", {}, "Through ", el("input", { type: "date", name: "through", value: range.through })),
        el("label", {}, "Group by ", el("select", { name: "period" },
          ...[["month", "Month"], ["quarter", "Quarter"], ["year", "Year"]].map(([value, label]) =>
            el("option", { value, selected: value === range.period ? "selected" : null }, label)))),
      ] : [];
      const form = () => {
        if (!ranged) return {};
        const value = (name) => controls.querySelector(`[name=${name}]`).value;
        state.reportRange = { from: value("from"), through: value("through"),
          period: value("period"), scenario: state.reportRange?.scenario || null };
        return state.reportRange;
      };
      run = async () => {
        output.replaceChildren(el("p", { class: "note" }, "Running…"));
        try {
          if (report.section) output.replaceChildren(await report.section(form()));
          else { output.replaceChildren(); await report.open(form()); }
        } catch (error) { fail(error); }
      };
      controls.replaceChildren(...fields, runButton());
      await run();
      return;
    }
    const form = breadletOptions(report);
    run = async () => {
      output.replaceChildren(el("p", { class: "note" }, "Running…"));
      try {
        const found = await runBreadlet("report", report.id, form.values());
        output.replaceChildren(renderDocument(found.document));
      } catch (error) { fail(error); }
    };
    controls.replaceChildren(...form.fields, runButton());
    await run();
  };
  picker.addEventListener("change", () => choose().catch(fail));
  const problems = listed.problems.map((item) =>
    el("p", { class: "note neg" }, `Add-on ${item.name} did not load: ${item.reason}`));
  const page = el("section", { class: "reports" },
    el("div", { class: "toolbar" }, el("label", {}, "Report ", picker)), describe, controls,
    ...problems, output);
  choose().catch(fail);
  return page;
}

// The Dashboard's breadlets: each shown one drawn below the Dashboard's own sections.
async function dashboardBreadlets(shown) {
  const parts = await Promise.all(shown.map(async (item) => {
    try {
      const found = await runBreadlet("dashboard", item.id);
      return el("section", { class: "dashboard-breadlet", "data-breadlet": item.id },
        renderDocument(found.document, { titled: false }));
    } catch (error) {
      return el("p", { class: "note neg" }, `${item.name}: ${error.message}`);
    }
  }));
  return parts;
}
