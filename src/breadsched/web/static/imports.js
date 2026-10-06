// Imports: held GnuCash changes, CSV statements, file import, and write-back.

// GnuCash changes held back from transactions reconciled in BreadSched. Each row
// is decided on its own and the batch is applied atomically by the shared service;
// "Decide later" is the default, matching the desktop review.
async function openImportReviewDialog() {
  if (importReviewDialog?.isConnected) return;
  const data = await get("/api/import/review");
  if (!data.changes.length) return;
  const backdrop = el("div", { class:"detail-backdrop" });
  importReviewDialog = backdrop;
  const close = () => {
    backdrop.remove();
    if (importReviewDialog === backdrop) importReviewDialog = null;
  };
  const labels = { keep:"Keep BreadSched version", "use-source":"Use GnuCash version",
    later:"Decide later" };
  // A held deletion asks whether to delete the transaction here too.
  const deletionLabels = { keep:"Keep the transaction", "use-source":"Delete it here too",
    later:"Decide later" };
  const choosers = data.changes.map((item) => el("select", {
    "aria-label":`Decision for ${item.description}`,
  }, ...["keep", "use-source", "later"]
    .filter((value) => value !== "use-source" || item.can_use_gnucash)
    .map((value) => el("option", { value, selected:value === "later" ? "selected" : null },
      (item.deleted ? deletionLabels : labels)[value]))));
  const setAll = (value) => choosers.forEach((chooser) => {
    if ([...chooser.options].some((option) => option.value === value)) chooser.value = value;
  });
  const rows = data.changes.map((item, index) => el("tr", {},
    el("td", {}, item.date),
    el("td", {}, item.description),
    el("td", {}, el("ul", {}, ...item.changes.map((line) => el("li", {}, line)),
      ...(item.blocked_by.length
        ? [el("li", { class:"neg" },
          `${item.deleted ? "Still used by" : "Reopen first"}: ${item.blocked_by.join(", ")}`)]
        : []))),
    el("td", {}, choosers[index])));
  const apply = async () => {
    const decisions = data.changes
      .map((item, index) => ({ transaction:item.transaction, decision:choosers[index].value }))
      .filter((item) => item.decision !== "later");
    try {
      if (decisions.length) {
        const outcome = await post("/api/import/review", { decisions });
        say(`GnuCash changes: kept ${outcome.kept} BreadSched version(s), applied ${outcome.applied} GnuCash version(s).`);
      }
      close();
      render();
    } catch (error) { say(error.message, "error"); }
  };
  backdrop.append(el("section", { class:"detail-dialog wide" },
    el("div", { class:"detail-heading" },
      helpHeading("GnuCash changes to reconciled transactions", "held-import")),
    el("p", { class:"note" }, `GnuCash changed or deleted ${data.changes.length} reconciled transaction(s). They were left unchanged. Choose what to do with each.`),
    el("div", { class:"toolbar" },
      el("button", { class:"action", type:"button", onclick:()=>setAll("keep") },
        "Keep all BreadSched versions"),
      el("button", { class:"action", type:"button", onclick:()=>setAll("use-source") },
        "Use GnuCash where possible"),
      el("button", { class:"action", type:"button", onclick:()=>setAll("later") },
        "Decide all later")),
    table(["Date", "Transaction", "GnuCash change", "Action"], rows),
    el("div", { class:"toolbar" },
      el("button", { class:"action", type:"button", onclick:close }, "Decide later"),
      el("button", { class:"action primary", type:"button", onclick:apply }, "Apply"))));
  document.body.append(backdrop);
}

async function csvImportPanel() {
  const accounts = (await get("/api/accounts")).filter((item) =>
    !item.placeholder && (item.class === "asset" || item.class === "liability"));
  const file = el("input", { type:"file", accept:".csv,text/csv" });
  const path = el("input", { placeholder:"/path/to/statement.csv" });
  const account = el("select", {}, ...accounts.map((item) =>
    el("option", { value:item.handle }, item.full_name)));
  const columnSelect = () => el("select", {}, el("option", { value:"" }, "(none)"));
  const fields = {
    date: columnSelect(), amount: columnSelect(), debit: columnSelect(),
    credit: columnSelect(), description: columnSelect(), memo: columnSelect(),
    category: columnSelect(), payee: columnSelect(), currency: columnSelect(),
  };
  const dateFormat = el("select", {},
    ...[["auto","Detect date order"],["iso","Year first (YYYY-MM-DD)"],
      ["month-first","Month first (MM/DD/YYYY)"],["day-first","Day first (DD/MM/YYYY)"]]
      .map(([value, label]) => el("option", { value }, label)));
  const numberFormat = el("select", {},
    ...[["auto","Detect decimal separator"],["dot","Period decimal (1,234.56)"],
      ["comma","Comma decimal (1.234,56)"]]
      .map(([value, label]) => el("option", { value }, label)));
  const header = el("input", { type:"checkbox", checked:"checked" });
  const invert = el("input", { type:"checkbox" });
  const duplicates = el("input", { type:"checkbox" });
  const transfers = el("input", { type:"checkbox" });
  const layout = el("p", { class:"note" });
  const preview = el("div", {});
  let source = "";
  // Split columns: each pair of a category column and an amount column is one split.
  let columns = [];
  const fillColumns = (select) => select.replaceChildren(el("option", { value:"" }, "(none)"),
    ...columns.map((name) => el("option", { value:name }, name)));
  const splitPairs = [];
  const splitRows = el("div", { class:"csv-splits" });
  const addSplit = () => {
    const pair = { category: columnSelect(), amount: columnSelect() };
    fillColumns(pair.category);
    fillColumns(pair.amount);
    splitPairs.push(pair);
    const number = splitPairs.length;
    splitRows.append(el("div", { class:"toolbar" },
      el("label", {}, `Split ${number} category column`, pair.category),
      el("label", {}, `Split ${number} amount column`, pair.amount)));
  };
  const request = () => ({
    path: source,
    account: account.value,
    include_duplicates: duplicates.checked,
    link_transfers: transfers.checked,
    mapping: {
      ...Object.fromEntries(Object.entries(fields).map(([key, select]) => [key, select.value])),
      date_format: dateFormat.value, number_format: numberFormat.value,
      header: header.checked, invert: invert.checked,
      splits: splitPairs.map((pair) => ({ category:pair.category.value, amount:pair.amount.value })),
    },
  });
  const guess = (columns, patterns) => columns.find((name) =>
    patterns.some((pattern) => pattern.test(name))) || "";
  const inspect = async () => {
    if (file.files.length) {
      const query = new URLSearchParams({ filename:file.files[0].name });
      const upload = await fetch(`/api/import/upload?${query}`, {
        method:"POST", headers:{ ...apiHeaders(), "Content-Type":"application/octet-stream" },
        body:file.files[0],
      });
      const payload = await upload.json();
      if (!upload.ok) throw new Error(payload.error || upload.statusText);
      source = payload.path;
    } else {
      source = path.value.trim();
      if (!source) throw new Error("Choose a CSV file or enter a local path.");
    }
    const data = await post("/api/import/csv/inspect", { path:source, header:header.checked });
    columns = data.columns;
    for (const select of Object.values(fields)) fillColumns(select);
    for (const pair of splitPairs) { fillColumns(pair.category); fillColumns(pair.amount); }
    fields.date.value = guess(data.columns, [/date/i, /datum/i]);
    fields.amount.value = guess(data.columns, [/^amount$/i, /betrag/i, /amount/i]);
    fields.debit.value = fields.amount.value ? "" : guess(data.columns, [/debit/i, /withdraw/i]);
    fields.credit.value = fields.amount.value ? "" : guess(data.columns, [/credit/i, /deposit/i]);
    fields.description.value = guess(data.columns, [/desc/i, /payee/i, /name/i, /merchant/i]);
    fields.memo.value = guess(data.columns, [/memo/i, /note/i, /reference/i]);
    // Optional columns are suggested only on an exact header match.
    fields.category.value = guess(data.columns, [/^category$/i]);
    fields.payee.value = guess(data.columns, [/^payee$/i]);
    fields.currency.value = guess(data.columns, [/^currency$/i]);
    layout.textContent = `Read as ${data.encoding} with delimiter "${data.delimiter}". `
      + "Check the suggested columns, then preview.";
    preview.replaceChildren(table(data.columns, data.sample));
  };
  const showPreview = async () => {
    const data = await post("/api/import/csv/preview", request());
    const labels = { new:"New", imported:"Already imported",
      possible_duplicate:"Possible duplicate", possible_transfer:"Possible transfer",
      invalid:"Invalid" };
    layout.textContent = `${data.encoding}, delimiter "${data.delimiter}", `
      + `${data.date_format} dates, ${data.number_format} decimals. `
      + Object.entries(data.counts).map(([key, count]) => `${labels[key]}: ${count}`).join(" · ");
    preview.replaceChildren(table(["Line", "Date", {label:"Amount",num:true},
      "Description", "Category", "Payee", "Status", "Reason"], data.rows.map((row) => [
      String(row.line), row.date || "", row.amount === null ? "" : money(row.amount),
      row.description,
      row.splits.length
        ? row.splits.map((split) => `${split.category} ${money(split.amount)}`).join("; ")
        : row.category || "",
      row.payee || "", labels[row.status],
      row.reason || row.note || ""])));
  };
  const run = (action) => async () => {
    try { await action(); } catch (error) { say(error.message, "error"); }
  };
  return el("div", { class:"panel panel-pad-16" },
    helpHeading("CSV statement", "csv-import"),
    el("p", { class:"note" },
      "Choose the statement and its account, map the columns, and preview. Nothing is "
      + "written until you import. Re-importing the same rows adds nothing and keeps any "
      + "category you chose. A row matching a transaction already in the account on the same "
      + "date and amount is held back unless you include possible duplicates. A row that "
      + "looks like the other side of an uncategorized transfer already imported into another "
      + "account is imported as new unless you link transfers. An optional category column "
      + "must name an existing account (its full name, or a name no other account shares); "
      + "a payee column matches payees you already have; a currency column must match the "
      + "account's currency. Instead of one category column, a row can be split: map a "
      + "category column and an amount column for each split. A row's filled splits must "
      + "add up exactly to its amount, in the same sign as the amount, or it is not imported."),
    el("form", { class:"entry", onsubmit:(event) => event.preventDefault() },
      el("label", {}, "Choose a CSV file", file),
      el("label", {}, "Or enter a path visible to BreadSched", path),
      el("label", {}, el("span", {}, "First row is a header "), header),
      el("button", { class:"action", type:"button", onclick:run(inspect) }, "Read columns"),
      el("label", {}, "Account", account),
      ...Object.entries(fields).map(([key, select]) =>
        el("label", {}, `${key[0].toUpperCase()}${key.slice(1)} column`, select)),
      splitRows,
      el("button", { class:"action", type:"button", onclick:addSplit }, "Add split columns"),
      el("label", {}, "Date order", dateFormat),
      el("label", {}, "Number format", numberFormat),
      el("label", {}, el("span", {}, "Money out is shown positive "), invert),
      el("label", {}, el("span", {}, "Include possible duplicates "), duplicates),
      el("label", {}, el("span", {}, "Link possible transfers "), transfers),
      el("button", { class:"action", type:"button", onclick:run(showPreview) }, "Preview"),
      el("button", { class:"action primary", type:"button", onclick:run(async () => {
        const result = await post("/api/import/csv", request());
        say(`Imported ${result.new} new; ${result.already_imported} already imported; `
          + `${result.possible_duplicates} possible duplicate(s) `
          + `${result.duplicates_included ? "included" : "held back"}; `
          + `${result.transfers_linked} transfer(s) linked; ${result.skipped} skipped.`
          + (result.reimbursement_notice ? ` ${result.reimbursement_notice}` : "")
          + (result.claim_link_notice ? ` ${result.claim_link_notice}` : ""));
        await showPreview();
      }) }, "Import")),
    layout, preview);
}

async function showImport() {
  const defaults = await get("/api/import");
  const file = el("input", { type:"file", accept:".qif,.ofx,.qfx,.gnucash,.xml,.sqlite,.db" });
  const path = el("input", { name:"path",
    value:defaults.path || "", placeholder:"/path/to/file.qif" });
  const numberFormat = el("select", { name:"number_format" },
    el("option", { value:"auto" }, "Auto-detect number format"),
    el("option", { value:"dot" }, "Period decimal (1,234.56)"),
    el("option", { value:"comma" }, "Comma decimal (1.234,56)"));
  const dateFormat = el("select", { name:"date_format" },
    el("option", { value:"auto" }, "Auto-detect QIF date order"),
    el("option", { value:"month-first" }, "Month first (MM/DD)"),
    el("option", { value:"day-first" }, "Day first (DD/MM)"));
  const result = el("pre", { class:"note" });
  const duplicates = el("input", { type:"checkbox" });
  const form = el("form", { class:"entry", onsubmit: async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target).entries());
    data.include_scheduled = true;
    data.include_duplicates = duplicates.checked;
    try {
      let response;
      if (file.files.length) {
        const query = new URLSearchParams({ filename:file.files[0].name,
          number_format:data.number_format, date_format:data.date_format,
          include_duplicates:duplicates.checked ? "1" : "0" });
        const upload = await fetch(`/api/import/upload?${query}`, {
          method:"POST", headers:{ ...apiHeaders(), "Content-Type":"application/octet-stream" },
          body:file.files[0],
        });
        const payload = await upload.json();
        if (!upload.ok) throw new Error(payload.error || upload.statusText);
        response = payload;
      } else {
        if (!data.path.trim()) throw new Error("Choose a file or enter a local path.");
        response = await post("/api/import", data);
      }
      if (response.format === "csv") {
        say("CSV statements need a column mapping; use the CSV statement section below.",
          "error");
        return;
      }
      result.textContent = `${response.format}\n\n${response.detail}`
        + (response.reimbursement_notice ? `\n\n${response.reimbursement_notice}` : "")
        + (response.claim_link_notice ? `\n\n${response.claim_link_notice}` : "");
      say("Import finished.");
      if (response.held) await openImportReviewDialog();
    } catch (error) { say(error.message, "error"); }
  } },
    el("label", {}, "Choose a file from this browser", file),
    el("label", {}, "Or enter a path visible to BreadSched", path),
    el("label", {}, "Number format", numberFormat),
    el("label", {}, "QIF date order", dateFormat),
    el("label", { title:"An OFX row matching a transaction already in the account on the "
      + "same date for the same amount is otherwise held back" },
      el("span", {}, "Include possible duplicates "), duplicates),
    el("button", { class:"action primary", type:"submit" }, "Import"));
  return el("div", {},
    el("p", { class:"note" }, "Choose a QIF, OFX, or GnuCash file (up to 32 MiB), or enter a path visible to the BreadSched process. Uploading the same filename again refreshes that source. Auto-detection is recommended; choose an explicit number or date format when the source is ambiguous."),
    el("p", { class:"note" },
      "Re-importing GnuCash updates source-owned data and removes transactions deleted from the source. Transactions still used by a BreadSched reconciliation or FSA claim are retained and reported for review. GnuCash changes to, or deletions of, a reconciled transaction are held for your decision."),
    el("div", { class:"toolbar" },
      el("button", { class:"action", type:"button",
        onclick:()=>openImportReviewDialog().catch((error)=>say(error.message, "error")) },
        "Review held GnuCash changes…")),
    el("div", { class:"panel panel-pad-16" }, form, result),
    await csvImportPanel(),
    await writebackPanel());
}

async function writebackPanel() {
  // #174: preview, then write only the ticked transactions back to GnuCash.
  const data = await get("/api/gnucash/writeback");
  const keep = el("input", { type:"number", min:"1", max:"1000", value:String(data.keep_backups),
    onchange: async (event) => {
      try {
        await post("/api/gnucash/writeback/settings",
          { keep_backups:Number(event.target.value) });
      } catch (error) { say(error.message, "error"); }
    } });
  const panel = el("div", { class:"panel panel-pad-16 writeback" },
    helpHeading("Write changes to GnuCash", "writeback"));
  if (!data.available) {
    panel.append(el("p", { class:"note" }, data.message),
      el("label", {}, "Backups to keep ", keep));
    return panel;
  }
  const boxes = data.changes.map((change) => el("input", { type:"checkbox",
    value:change.transaction }));
  panel.append(
    el("p", { class:"note" }, `GnuCash book: ${data.source}. Tick the transactions to write; `
      + "nothing is written until you press Write selected. Close the book in GnuCash first. "
      + "The book is backed up, written in one step, and read back; changes you do not tick "
      + "stay here and are offered again."),
    data.changes.length
      ? el("div", { class:"writeback-changes" }, data.changes.map((change, i) =>
        el("div", {}, el("label", {}, boxes[i],
          ` ${change.date} ${change.description} (${change.kinds.join(", ")})`),
        el("pre", { class:"note" }, change.details.join("\n")))))
      : el("p", { class:"note" }, "Nothing to write."),
    data.unsupported.length
      ? el("div", {}, el("h3", {}, "Not written"),
        el("ul", {}, data.unsupported.map((item) =>
          el("li", {}, `${item.date} ${item.description}: ${item.reason}`))))
      : el("span", {}),
    el("label", {}, "Backups to keep ", keep),
    el("div", { class:"toolbar" }, el("button", { class:"action primary", type:"button",
      disabled: data.changes.length ? null : "disabled",
      onclick: async () => {
        const chosen = boxes.filter((box) => box.checked).map((box) => box.value);
        if (!chosen.length) { say("Tick one or more transactions to write.", "error"); return; }
        try {
          const result = await post("/api/gnucash/writeback", { transactions:chosen });
          say(`Wrote ${result.written.length} transaction(s) to GnuCash. Backup: ${result.backup}`);
          await render();
        } catch (error) { say(error.message, "error"); }
      } }, "Write selected")));
  return panel;
}
