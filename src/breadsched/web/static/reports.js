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

async function showReports() {
  const listed = await get("/api/breadlets?kind=report");
  const output = el("div", { class: "breadlet-output" });
  const controls = el("div", { class: "toolbar breadlet-options" });
  const picker = el("select", { "aria-label": "Report" }, ...listed.breadlets.map((item) =>
    el("option", { value: item.id }, item.name)));
  const describe = el("p", { class: "note" });
  let form = null;
  const choose = () => {
    const breadlet = listed.breadlets.find((item) => item.id === picker.value);
    describe.textContent = breadlet.description;
    form = breadletOptions(breadlet);
    controls.replaceChildren(...form.fields,
      el("button", { class: "action primary", type: "button", onclick: run }, "Run"));
    run();
  };
  async function run() {
    output.replaceChildren(el("p", { class: "note" }, "Running…"));
    try {
      const found = await runBreadlet("report", picker.value, form.values());
      output.replaceChildren(renderDocument(found.document));
    } catch (error) {
      output.replaceChildren(el("p", { class: "note neg" }, error.message));
    }
  }
  picker.addEventListener("change", choose);
  const problems = listed.problems.map((item) =>
    el("p", { class: "note neg" }, `Add-on ${item.name} did not load: ${item.reason}`));
  const page = el("section", { class: "reports" },
    el("div", { class: "toolbar" }, el("label", {}, "Report ", picker)), describe, controls,
    ...problems, output);
  if (listed.breadlets.length) choose();
  else output.replaceChildren(el("p", { class: "note" }, "No report breadlets are installed."));
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
