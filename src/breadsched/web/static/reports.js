// Reports: report breadlets, their options, and a document drawn from report_document_json.

// One block of a breadlet's document. A chart's exact values are the table block
// after it, so a chart is drawn without its own table. With onLink, a card or row
// that links somewhere opens it, and an actions block is a row of buttons; a table
// with a key sorts by its column headers and shows only the columns the person kept
// (hiddenColumns(key) lists the hidden ones; the first column always shows).
function documentBlock(block, { onLink = null, hiddenColumns = null } = {}) {
  const follow = (link) => () => onLink && onLink(link);
  if (block.type === "heading") return el("h2", {}, block.text);
  if (block.type === "paragraph") {
    if (block.bullet) return el("ul", {}, el("li", {}, block.text));
    return el("p", { class: block.warning ? "note neg" : "note" }, block.text);
  }
  if (block.type === "cards") {
    return el("div", { class: "cards" }, ...block.items.map((card) => {
      const body = [el("div", { class: "label" }, card.label),
        el("div", { class: card.alarm ? "value neg" : "value" }, card.value)];
      return card.link && onLink
        ? el("button", { type: "button", class: "card card-link", onclick: follow(card.link),
          title: `Open ${card.label}` }, ...body)
        : el("div", { class: "card" }, ...body);
    }));
  }
  if (block.type === "actions") {
    return el("div", { class: "toolbar" }, ...block.items.map((item) =>
      el("button", { class: "action", type: "button", onclick: follow(item.link) }, item.label)));
  }
  if (block.type === "chart") return modelChart(block.model, { withTable: false });
  if (!block.rows.length) return el("p", { class: "note" }, block.empty);
  const hidden = new Set(block.key && hiddenColumns ? hiddenColumns(block.key) : []);
  const keep = block.columns.map((column, index) => index === 0 || !hidden.has(column.label));
  const styles = { heading: "total-row", section: "total-row", total: "total-row", grand: "total-row" };
  const cellNode = (cell, index, row) => {
    const numeric = cell.numeric || (block.columns[index] && block.columns[index].numeric);
    const kind = numeric ? (cell.negative ? "num neg" : "num") : null;
    const attrs = { class: kind, title: cell.hint || null,
      colspan: cell.span > 1 ? cell.span : null,
      style: cell.indent ? `padding-left: ${0.5 + cell.indent}em` : null };
    const note = cell.note
      ? [cell.below ? el("br") : " ", el("span", { class: "note" }, cell.note)] : [];
    // A row that links somewhere opens it from its first cell.
    const text = index === 0 && row.link && onLink
      ? el("button", { type: "button", class: "link-button", onclick: follow(row.link) }, cell.text)
      : cell.text;
    return el("td", attrs, text, ...note);
  };
  const rowNode = (row) => el("tr", { class: styles[row.style] || null },
    ...row.cells.map((cell, index) => keep[index] === false ? null : cellNode(cell, index, row)));
  const headers = block.columns.filter((_c, index) => keep[index])
    .map((column) => ({ label: column.label, num: column.numeric }));
  const drawn = table(headers, block.rows.map(rowNode));
  if (block.key) {
    // Sortable: a header click orders the rows by that column (again reverses it).
    const shown = block.columns.map((_c, index) => index).filter((index) => keep[index]);
    let sorted = { column: null, ascending: true };
    drawn.querySelectorAll("th").forEach((th, position) => {
      th.classList.add("sortable");
      th.tabIndex = 0;
      const sort = () => {
        const index = shown[position];
        sorted = { column: index, ascending: sorted.column === index ? !sorted.ascending : true };
        const value = (row) => {
          const cell = row.cells[index] || { text: "" };
          if (cell.sort !== null && cell.sort !== undefined) return cell.sort;
          const number = Number(String(cell.text).replaceAll(",", "").replace(/^\((.*)\)$/, "-$1"));
          return cell.numeric && !Number.isNaN(number) ? number : String(cell.text).toLowerCase();
        };
        const rows = [...block.rows].sort((a, b) => {
          const [x, y] = [value(a), value(b)];
          const order = typeof x === typeof y ? (x > y) - (x < y) : String(x).localeCompare(String(y));
          return sorted.ascending ? order : -order;
        });
        drawn.querySelector("tbody").replaceChildren(...rows.map(rowNode));
        drawn.querySelectorAll("th").forEach((other) => other.removeAttribute("aria-sort"));
        th.setAttribute("aria-sort", sorted.ascending ? "ascending" : "descending");
      };
      th.addEventListener("click", sort);
      th.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); sort(); }
      });
    });
    drawn.dataset.table = block.key;
  }
  return drawn;
}

function renderDocument(doc, { titled = true, onLink = null, hiddenColumns = null } = {}) {
  const parts = titled
    ? [el("h2", { class: "document-title" }, doc.title),
      doc.subtitle ? el("p", { class: "note" }, doc.subtitle) : null]
    : [];
  for (const section of doc.sections) {
    for (const block of section.blocks) parts.push(documentBlock(block, { onLink, hiddenColumns }));
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
