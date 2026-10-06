// Accounts, their settings editors, transaction documents, FSA claims,
// reconciliation, and the register.

function openFsaYearsEditor(account) {
  const backdrop = el("div", {
    class:"detail-backdrop",
    onclick:(event)=>{ if (event.target === backdrop) backdrop.remove(); },
  });
  const rows = el("div", { class:"stack" });
  const addRow = (year={}) => {
    const row = el("div", { class:"row" },
      el("input", { type:"date", value:year.start || "", title:"Funding year start" }),
      el("input", { type:"date", value:year.through || "", title:"Funding year through" }),
      el("input", { value:year.election || "", placeholder:"Election" }),
      el("input", { type:"date", value:year.runout_through || "", title:"Run-out through" }),
      el("input", { value:year.carryover_limit || "", placeholder:"Carryover limit",
        title:"The most unused election the plan carries into the next plan year" }),
      el("input", { type:"date", value:year.grace_through || "",
        title:"Grace period through: services up to it may be claimed against this year" }));
    row.append(el("button", { class:"action", type:"button", onclick:()=>row.remove() }, "Remove"));
    rows.append(row);
  };
  (account.fsa_years || []).forEach(addRow);
  const save = async () => {
    const years = Array.from(rows.children).map((row) => {
      const inputs = row.querySelectorAll("input");
      return {
        start: inputs[0].value, through: inputs[1].value,
        election: inputs[2].value.trim(), runout_through: inputs[3].value,
        carryover_limit: inputs[4].value.trim() || null, grace_through: inputs[5].value || null,
      };
    });
    await post("/api/account/fsa-years", { handle:account.handle, years });
    backdrop.remove();
    say("FSA funding years saved.");
    render();
  };
  const body = el("section", { class:"detail-dialog wide" },
    el("h2", {}, `FSA funding years — ${account.full_name}`),
    el("p", { class:"note" },
      "Election availability is independent of the custodial ledger balance. "
      + "A run-out date permits explicitly assigned prior-year claims after year-end."),
    rows,
    el("div", { class:"toolbar" },
      el("button", { class:"action", type:"button", onclick:()=>addRow() }, "Add funding year"),
      el("span", { class:"spacer" }),
      el("button", { class:"action", type:"button", onclick:()=>backdrop.remove() }, "Cancel"),
      el("button", { class:"action primary", type:"button", onclick:()=>save().catch((error)=>say(error.message,"error")) }, "Save")));
  backdrop.append(body);
  document.body.append(backdrop);
}

function openCardPaymentEditor(account, accounts) {
  const backdrop = el("div", {
    class:"detail-backdrop",
    onclick:(event)=>{ if (event.target === backdrop) backdrop.remove(); },
  });
  const full = el("input", {
    type:"checkbox", checked:account.pays_in_full ? "checked" : null,
  });
  const usual = el("input", {
    inputmode:"decimal", value:account.usual_payment || "", placeholder:"100.00",
  });
  const day = el("input", {
    type:"number", min:"1", max:"28", value:account.payment_day || "",
    placeholder:"1–28",
  });
  const sources = accounts.filter((item)=>
    !item.placeholder && ["BANK", "CASH"].includes(item.type)
    && (!item.hidden || item.handle === account.card_payment_account));
  const payment = el("select", {},
    el("option", {value:""}, "Choose when recording payment"),
    sources.map((item)=>el("option", {
      value:item.handle,
      selected:item.handle === account.card_payment_account ? "selected" : null,
    }, item.full_name)));
  const sync = () => { usual.disabled = full.checked; };
  full.addEventListener("change", sync);
  sync();
  const save = async () => {
    await post("/api/account/card", {
      handle:account.handle,
      pays_in_full:full.checked,
      usual_payment:usual.value.trim(),
      payment_day:day.value,
      payment_account:payment.value,
    });
    backdrop.remove();
    say("Credit-card payment settings saved.");
    render();
  };
  const body = el("section", {class:"detail-dialog"},
    el("h2", {}, `Card payment — ${account.full_name}`),
    el("p", {class:"note"},
      "The account owns this monthly payment definition. An explicit scheduled "
      + "payment takes precedence, so the same card is never forecast twice."),
    el("label", {class:"row"}, full, " Pay the current balance in full"),
    el("label", {}, "Usual payment when carrying a balance", usual),
    el("label", {}, "Payment day", day),
    el("label", {}, "Paid from", payment),
    el("div", {class:"toolbar"},
      el("span", {class:"spacer"}),
      el("button", {class:"action", type:"button", onclick:()=>backdrop.remove()},
        "Cancel"),
      el("button", {class:"action primary", type:"button",
        onclick:()=>save().catch((error)=>say(error.message,"error"))}, "Save")));
  backdrop.append(body);
  document.body.append(backdrop);
}

async function openLoanEditor() {
  const options = await get("/api/loan/options");
  if (!options.liabilities.length || !options.expenses.length
      || !options.payment_accounts.length) {
    throw new Error("A loan needs a visible liability, interest expense, and Bank or Cash account.");
  }
  const backdrop = el("div", {
    class:"detail-backdrop",
    onclick:(event)=>{ if (event.target === backdrop) backdrop.remove(); },
  });
  const field = (label, control) => el("label", {}, label, control);
  const select = (items) => el("select", {}, items.map((item)=>
    el("option", {value:item.handle}, item.name)));
  const name = el("input", {placeholder:"Mortgage"});
  const principal = el("input", {inputmode:"decimal", placeholder:"200000.00"});
  const rate = el("input", {inputmode:"decimal", placeholder:"6.0"});
  const years = el("input", {type:"number", min:"1", max:"100", value:"25"});
  const start = el("input", {type:"date", value:new Date().toISOString().slice(0, 10)});
  const liability = select(options.liabilities);
  const interest = select(options.expenses);
  const payment = select(options.payment_accounts);
  const opening = el("input", {type:"checkbox", checked:"checked"});
  const preview = el("div", {class:"stack"});
  const values = () => ({
    name:name.value.trim(), principal:principal.value.trim(), annual_rate:rate.value.trim(),
    years:years.value, start:start.value, liability:liability.value,
    interest_account:interest.value, payment_account:payment.value,
    opening_balance:opening.checked,
  });
  const showPreview = async () => {
    const result = await post("/api/loan/preview", values());
    preview.replaceChildren(
      el("p", {class:"note"},
        `Monthly payment ${money(result.payment)}; total interest ${money(result.total_interest)}.`),
      table(["Payment", {label:"Amount", num:true}, {label:"Interest", num:true},
        {label:"Principal", num:true}, {label:"Balance", num:true}],
      result.rows.map((row)=>el("tr", {},
        el("td", {}, row.period),
        el("td", {class:"num"}, money(row.payment)),
        el("td", {class:"num"}, money(row.interest)),
        el("td", {class:"num"}, money(row.principal)),
        el("td", {class:"num"}, money(row.balance))))));
  };
  const save = async () => {
    const result = await post("/api/loan/save", values());
    backdrop.remove();
    say(`${result.name} created with a monthly payment of ${money(result.payment)}.`);
    render();
  };
  const body = el("section", {class:"detail-dialog wide"},
    el("h2", {}, "Set up a loan"),
    el("p", {class:"note"},
      "Preview the calculated principal and interest split before creating the formula schedule."),
    field("Name", name), field("Amount borrowed", principal),
    field("Annual rate (%)", rate), field("Term (years)", years),
    field("First payment", start), field("Loan account", liability),
    field("Interest expense", interest), field("Paid from", payment),
    el("label", {class:"row"}, opening,
      " Record what is currently owed as an opening balance"),
    preview,
    el("div", {class:"toolbar"},
      el("button", {class:"action", type:"button",
        onclick:()=>showPreview().catch((error)=>say(error.message,"error"))}, "Preview"),
      el("span", {class:"spacer"}),
      el("button", {class:"action", type:"button", onclick:()=>backdrop.remove()}, "Cancel"),
      el("button", {class:"action primary", type:"button",
        onclick:()=>save().catch((error)=>say(error.message,"error"))}, "Create loan")));
  backdrop.append(body);
  document.body.append(backdrop);
}

async function openSecurityPriceEditor() {
  const data = await get("/api/commodities");
  const backdrop = el("div", {
    class:"detail-backdrop",
    onclick:(event)=>{ if (event.target === backdrop) backdrop.remove(); },
  });
  const security = el("select", {},
    el("option", { value:"" }, "New security…"),
    data.securities.map((item)=>el("option", { value:item.handle },
      `${item.mnemonic} — ${item.fullname}`)));
  const mnemonic = el("input", { placeholder:"INDEX" });
  const fullname = el("input", { placeholder:"Index fund" });
  const namespace = el("input", { value:"FUND", placeholder:"FUND" });
  const fraction = el("input", { value:"10000", inputmode:"numeric" });
  const currency = el("select", {}, data.currencies.length
    ? data.currencies.map((item)=>
        el("option", { value:item.handle }, `${item.mnemonic} — ${item.fullname}`))
    : [el("option", { value:"" }, "USD — US Dollar (create)")]);
  const when = el("input", { type:"date", value:new Date().toISOString().slice(0,10) });
  const price = el("input", { inputmode:"decimal", placeholder:"125.25" });
  const update = () => {
    const item = data.securities.find((candidate)=>candidate.handle === security.value);
    const creating = !item;
    [mnemonic, fullname, namespace, fraction].forEach((field)=>field.disabled = !creating);
    if (item) {
      mnemonic.value = item.mnemonic;
      fullname.value = item.fullname;
      namespace.value = item.namespace;
      fraction.value = item.fraction;
      if (item.price !== null) price.value = item.price;
      if (item.price_date) when.value = item.price_date;
      if (item.currency) currency.value = item.currency;
    } else {
      mnemonic.value = "";
      fullname.value = "";
      namespace.value = "FUND";
      fraction.value = "10000";
      price.value = "";
      when.value = new Date().toISOString().slice(0,10);
    }
  };
  security.addEventListener("change", update);
  const save = async () => {
    await post("/api/commodity/price", {
      commodity:security.value, mnemonic:mnemonic.value, fullname:fullname.value,
      namespace:namespace.value, fraction:fraction.value, currency:currency.value,
      date:when.value, price:price.value,
    });
    backdrop.remove();
    say("Security price saved.");
    render();
  };
  const form = el("section", { class:"detail-dialog" },
    el("h2", {}, "Security price"),
    el("p", { class:"note" },
      "Prices value the exact security quantities already stored in transactions. "
      + "They do not rewrite historical ledger amounts."),
    el("div", { class:"scenario-fields" },
      el("label", {}, "Security", security),
      el("label", {}, "Symbol", mnemonic),
      el("label", {}, "Full name", fullname),
      el("label", {}, "Namespace", namespace),
      el("label", {}, "Smallest-unit denominator", fraction),
      el("label", {}, "Quote currency", currency),
      el("label", {}, "As of", when),
      el("label", {}, "Price per unit", price)),
    el("div", { class:"toolbar" },
      el("span", { class:"spacer" }),
      el("button", { class:"action", type:"button", onclick:()=>backdrop.remove() }, "Cancel"),
      el("button", { class:"action primary", type:"button",
        onclick:()=>save().catch((error)=>say(error.message,"error")) }, "Save price")));
  backdrop.append(form);
  document.body.append(backdrop);
  update();
}

async function openCurrencyRateEditor() {
  const data = await get("/api/commodities");
  const backdrop = el("div", {
    class:"detail-backdrop",
    onclick:(event)=>{ if (event.target === backdrop) backdrop.remove(); },
  });
  const choices = () => data.currencies.map((item)=>
    el("option", { value:item.handle }, `${item.mnemonic} — ${item.fullname}`));
  const source = el("select", {}, choices());
  const target = el("select", {}, choices());
  if (data.currencies.length > 1) target.value = data.currencies[1].handle;
  const when = el("input", { type:"date", value:new Date().toISOString().slice(0,10) });
  const rate = el("input", { inputmode:"decimal", placeholder:"1.25" });
  const save = async () => {
    const quote = await post("/api/currency/quote", {
      from:source.value, to:target.value, date:when.value, rate:rate.value,
    });
    backdrop.remove();
    say(`Manual rate saved for ${quote.date} (${quote.source}).`);
    render();
  };
  backdrop.append(el("section", { class:"detail-dialog" },
    el("h2", {}, "Exchange rate"),
    el("p", { class:"note" },
      "Enter target-currency units per one source-currency unit. "
      + "The dated manual quote leaves imported quotes and ledger amounts intact. "
      + "Accounts show the selected quote date and source or a missing-quote warning."),
    el("div", { class:"scenario-fields" },
      el("label", {}, "From currency", source),
      el("label", {}, "To currency", target),
      el("label", {}, "As of", when),
      el("label", {}, "Target units per source unit", rate)),
    el("div", { class:"toolbar" },
      el("span", { class:"spacer" }),
      el("button", { class:"action", type:"button", onclick:()=>backdrop.remove() }, "Cancel"),
      el("button", { class:"action primary", type:"button",
        onclick:()=>save().catch((error)=>say(error.message,"error")) }, "Save rate"))));
  document.body.append(backdrop);
}


// Tags and linked documents for one transaction. Documents stay outside the book,
// in the attachment folder beside it; a missing file is marked, never dropped.
// A file opens only if the page can show it safely; anything else is saved.
const SAFE_DOCUMENT_TYPES = ["application/pdf", "image/png", "image/jpeg", "image/gif",
  "image/webp", "text/plain"];

async function openDocument(transaction, item) {
  if (item.kind === "web") {
    window.open(item.location, "_blank", "noopener");
    return;
  }
  const query = new URLSearchParams({ transaction, location:item.location });
  const response = await fetch(`/api/attachment/content?${query}`, { headers:apiHeaders() });
  if (!response.ok) throw new Error((await response.json()).error || response.statusText);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const type = (response.headers.get("Content-Type") || "").split(";")[0];
  if (SAFE_DOCUMENT_TYPES.includes(type)) {
    window.open(url, "_blank", "noopener");
  } else {
    const link = el("a", { href:url, download:item.location.split(/[\\/]/).pop() });
    document.body.append(link);
    link.click();
    link.remove();
  }
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

function openTransactionDocuments(row) {
  const backdrop = el("div", {
    class:"detail-backdrop",
    onclick:(event)=>{ if (event.target === backdrop) close(); },
  });
  let changed = false;
  const close = () => { backdrop.remove(); if (changed) render(); };
  const tags = el("input", { value:row.tags.join(", "), "aria-label":"Tags",
    placeholder:"Comma-separated, e.g. Tax, Home repair" });
  const list = el("div", { class:"stack" });
  const address = el("input", { placeholder:"https://… or a file in the attachment folder",
    "aria-label":"Document address" });
  const file = el("input", { type:"file", "aria-label":"Document file" });
  const act = async (work) => {
    try {
      const result = await work();
      changed = true;
      row.tags = result.tags;
      row.documents = result.documents;
      show();
      return result;
    } catch (error) { say(error.message, "error"); return null; }
  };
  const show = () => {
    list.replaceChildren(...(row.documents.length ? row.documents.map((item) => el("div",
      { class:"row" },
      el("span", { class:item.missing ? "negative" : (item.owner === "source" ? "muted" : ""),
        title:item.missing ? `Not found at ${item.path || item.location}` : (item.path || "") },
        item.location, item.missing ? " — missing" : "",
        item.owner === "source" ? " (linked in GnuCash)" : ""),
      el("button", { class:"action", type:"button", disabled:item.missing ? "disabled" : null,
        onclick:()=>openDocument(row.handle, item).catch((error)=>say(error.message,"error")) },
        "Open"),
      item.owner === "breadsched" ? el("button", { class:"action", type:"button",
        onclick:()=>{
          const to = window.prompt(
            `Where is ${item.location} now? Give its place in the attachment folder.`,
            item.location);
          if (to) act(()=>post("/api/transaction/attachment/relink",
            { transaction:row.handle, location:item.location, to }));
        } }, "Relink…") : null,
      item.owner === "breadsched" ? el("button", { class:"action", type:"button",
        title:"Unlink; the file itself is kept",
        onclick:()=>act(()=>post("/api/transaction/attachment/remove",
          { transaction:row.handle, location:item.location })) }, "Remove") : null))
      : [el("p", { class:"muted" }, "No linked documents.")]));
  };
  show();
  const upload = async () => {
    if (!file.files.length) throw new Error("Choose a file to attach.");
    const query = new URLSearchParams({ transaction:row.handle, filename:file.files[0].name });
    const response = await fetch(`/api/attachment/upload?${query}`, {
      method:"POST", headers:{ ...apiHeaders(), "Content-Type":"application/octet-stream" },
      body:file.files[0],
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || response.statusText);
    file.value = "";
    return payload;
  };
  backdrop.append(el("section", { class:"detail-dialog" },
    el("h2", {}, `Tags & documents: ${row.description}`),
    el("div", { class:"scenario-fields" },
      el("label", {}, "Tags", tags),
      el("button", { class:"action", type:"button",
        onclick:()=>act(()=>post("/api/transaction/tags",
          { transaction:row.handle, tags:tags.value.split(",") })).then((result)=>{
          if (result) { tags.value = result.tags.join(", "); say("Tags saved."); }
        }) }, "Save tags")),
    el("h3", {}, "Documents"),
    list,
    el("div", { class:"toolbar" }, file,
      el("button", { class:"action", type:"button",
        title:"Copy the file into the attachment folder beside the book and link it",
        onclick:()=>act(upload) }, "Attach file")),
    el("div", { class:"toolbar" }, address,
      el("button", { class:"action", type:"button",
        onclick:()=>act(()=>post("/api/transaction/attachment/link",
          { transaction:row.handle, location:address.value })).then((result)=>{
          if (result) address.value = "";
        }) }, "Link")),
    el("div", { class:"toolbar" },
      el("span", { class:"spacer" }),
      el("button", { class:"action primary", type:"button", onclick:close }, "Done"))));
  document.body.append(backdrop);
}


async function openFsaClaimsEditor(initialHandle=null) {
  const data = await get("/api/fsa/claims");
  const backdrop = el("div", {
    class:"detail-backdrop",
    onclick:(event)=>{ if (event.target === backdrop) backdrop.remove(); },
  });
  const list = el("div", { class:"stack" });
  const act = (work) => work().catch((error)=>say(error.message, "error"));
  let editingHandle = null;
  const service = el("input", { type:"date" });
  const provider = el("input", { placeholder:"Provider" });
  const description = el("input", { placeholder:"Description" });
  const eob = el("input", { placeholder:"EOB patient responsibility" });
  const eobNote = el("input", { placeholder:"Why the EOB changed (kept in the history)",
    "aria-label":"EOB change note" });
  // A payer covering part of this expense leaves the FSA the rest (#192).
  const payer = el("select", { "aria-label":"Payer covers part",
    title:"A reimbursable expense whose payer covers part of this bill" },
    el("option", { value:"" }, "No payer covers part"),
    ...(data.candidates.receivables || []).map((item) =>
      el("option", { value:item.handle }, item.label)));
  const paymentSelect = el("select", { multiple:"multiple", size:"5" });
  const refundSelect = el("select", { multiple:"multiple", size:"4" });
  const allocations = el("div", { class:"stack" });

  const linkValue = (item) => JSON.stringify({
    transaction:item.transaction, split:item.split,
  });
  const selectLinks = (select, links) => {
    const wanted = new Set((links || []).map((link)=>JSON.stringify(link)));
    Array.from(select.options).forEach((option)=>{ option.selected = wanted.has(option.value); });
  };
  const claimWindow = () => {
    if (!service.value) return null;
    const years = data.candidates.fsa_accounts.flatMap((account) =>
      (account.years || []).filter((year) =>
        year.start <= service.value && service.value <= year.through));
    if (!years.length) return null;
    return { start:years.map((year)=>year.start).sort()[0] };
  };
  const focusCandidateDate = (select) => {
    if (!service.value) return;
    requestAnimationFrame(() => {
      const option = Array.from(select.options).find(
        (item)=>item.dataset.date >= service.value);
      if (option) option.scrollIntoView({ block:"start" });
    });
  };
  const candidateOptions = (rows) => rows.map((item) => el("option", {
    value:linkValue(item), "data-date":item.date,
  }, `${item.date} ${item.description} — ${item.account_name} ${money(item.amount)}`));
  const refreshClaimCandidates = (paymentLinks=null, refundLinks=null) => {
    const window = claimWindow();
    const within = (item) => !window || window.start <= item.date;
    const currentPayments = paymentLinks || Array.from(paymentSelect.selectedOptions).map(
      (option)=>JSON.parse(option.value));
    const currentRefunds = refundLinks || Array.from(refundSelect.selectedOptions).map(
      (option)=>JSON.parse(option.value));
    paymentSelect.replaceChildren(...candidateOptions(data.candidates.payments.filter(within)));
    refundSelect.replaceChildren(...candidateOptions(data.candidates.refunds.filter(within)));
    selectLinks(paymentSelect, currentPayments);
    selectLinks(refundSelect, currentRefunds);
    focusCandidateDate(paymentSelect);
    focusCandidateDate(refundSelect);
  };
  service.addEventListener("change", ()=>refreshClaimCandidates());
  const addAllocation = (initial=null) => {
    const accountSelect = el("select", {}, data.candidates.fsa_accounts.map((account) =>
      el("option", { value:account.handle }, account.name)));
    const yearSelect = el("select");
    const reimburse = el("select", { multiple:"multiple", size:"4" });
    // Money paid back into the FSA, such as an over-reimbursement.
    const repay = el("select", { multiple:"multiple", size:"3", "aria-label":"Repaid to the FSA" });
    const target = el("input", { placeholder:"Target amount (optional)" });
    const rejections = el("div", { class:"stack" });
    const addRejection = (value=null) => {
      const attempted = el("input", { type:"date", value:value?.attempted_on || "" });
      const amount = el("input", { placeholder:"Rejected amount",
        value:value?.amount ? money(value.amount[0] / value.amount[1]) : "" });
      const reason = el("input", { placeholder:"Reason", value:value?.reason || "" });
      const row = el("div", { class:"row" }, attempted, amount, reason,
        el("button", { class:"action", type:"button", onclick:()=>row.remove() }, "Remove"));
      row._rejectionFields = { attempted, amount, reason };
      rejections.append(row);
    };
    const refresh = () => {
      const account = data.candidates.fsa_accounts.find(
        (item)=>item.handle === accountSelect.value);
      const wantedYear = initial?.funding_year_start || yearSelect.value;
      yearSelect.replaceChildren(...((account?.years || []).map((year) =>
        el("option", { value:year.start }, `${year.start} – ${year.through}`))));
      if (wantedYear) yearSelect.value = wantedYear;
      const selectedYear = (account?.years || []).find(
        (year)=>year.start === yearSelect.value);
      reimburse.replaceChildren(...data.candidates.reimbursements
        .filter((item)=>item.account === accountSelect.value)
        .filter((item)=>!selectedYear || (
          selectedYear.start <= item.date
          && item.date <= (selectedYear.runout_through || selectedYear.through)))
        .map((item)=>el("option", {
          value:linkValue(item), "data-date":item.date,
        }, `${item.date} ${item.description} — ${money(item.amount)}`)));
      if (initial) selectLinks(reimburse, initial.reimbursements);
      const wantedRepayments = initial ? initial.repayments
        : Array.from(repay.selectedOptions).map((option)=>JSON.parse(option.value));
      repay.replaceChildren(...(data.candidates.repayments || [])
        .filter((item)=>item.account === accountSelect.value)
        .map((item)=>el("option", { value:linkValue(item), "data-date":item.date },
          `${item.date} ${item.description} — ${money(item.amount)}`)));
      selectLinks(repay, wantedRepayments);
      focusCandidateDate(reimburse);
    };
    accountSelect.onchange = ()=>{ initial = null; refresh(); };
    yearSelect.onchange = ()=>{ initial = null; refresh(); };
    const row = el("div", { class:"card" },
      el("div", { class:"row" }, accountSelect, yearSelect, target,
        el("button", { class:"action", type:"button", onclick:()=>row.remove() },
          "Remove allocation")),
      el("label", {}, "Reimbursement/payment splits"), reimburse,
      el("label", {}, "Repaid to the FSA"), repay,
      el("strong", {}, "Rejected/failed reimbursement attempts"), rejections,
      el("button", { class:"action", type:"button", onclick:()=>addRejection() },
        "Add rejected attempt"));
    row._claimFields = { accountSelect, yearSelect, target, reimburse, repay, rejections };
    allocations.append(row);
    if (initial) {
      accountSelect.value = initial.account;
      if (initial.target) target.value = String(initial.target[0] / initial.target[1]);
      (initial.rejections || []).forEach(addRejection);
    }
    refresh();
  };
  const clearForm = () => {
    editingHandle = null;
    service.value = ""; provider.value = ""; description.value = ""; eob.value = "";
    eobNote.value = "";
    payer.value = "";
    refreshClaimCandidates([], []);
    allocations.replaceChildren();
  };
  const loadClaim = (claim) => {
    clearForm(); editingHandle = claim.handle;
    service.value = claim.service_date; provider.value = claim.provider || "";
    description.value = claim.description || "";
    eob.value = claim.eob_responsibility || "";
    payer.value = claim.receivable || "";
    refreshClaimCandidates(claim.payments, claim.refunds);
    (claim.allocations || []).forEach((allocation)=>addAllocation(allocation));
  };
  const eventText = (event) => {
    const amount = (value) => value === null ? "none" : money(value);
    const what = event.kind === "eob_changed"
      ? `EOB changed from ${amount(event.previous)} to ${amount(event.current)}`
      : (event.kind === "closed" ? "Closed" : "Reopened");
    return `${event.on} ${what}${event.note ? `: ${event.note}` : ""}`;
  };
  const reload = async () => {
    const fresh = await get("/api/fsa/claims");
    data.claims = fresh.claims;
    renderList();
  };
  const renderList = () => {
    const claimRows = data.claims.length ? data.claims.map((claim) => {
      const why = el("input", { placeholder:claim.closed_on ? "Why reopen" : "Why close",
        "aria-label":claim.closed_on ? "Why reopen" : "Why close" });
      const closing = claim.closed_on
        ? el("button", { class:"action", type:"button", onclick:()=>act(async()=>{
          await post("/api/fsa/claim/reopen", { handle:claim.handle, note:why.value.trim() });
          say("FSA claim reopened."); await reload();
        }) }, "Reopen")
        : el("button", { class:"action", type:"button",
          title:"Stop pursuing what is left to reimburse; it is recorded as given up",
          onclick:()=>act(async()=>{
            await post("/api/fsa/claim/close", { handle:claim.handle, reason:why.value.trim() });
            say("FSA claim closed."); await reload();
          }) }, "Close claim");
      const owed = [
        Number(claim.repaid) ? `repaid to the FSA ${money(claim.repaid)}` : null,
        Number(claim.over_reimbursed) ? `to repay ${money(claim.over_reimbursed)}` : null,
        claim.closed_on ? `closed ${claim.closed_on}`
          + (claim.close_reason ? ` (${claim.close_reason})` : "")
          + `, gave up ${money(claim.forgone)}` : null,
      ].filter(Boolean);
      return el("div", { class:"card" },
        el("strong", {}, `${claim.service_date} ${claim.provider || "FSA claim"}`),
        el("span", {}, ` ${claim.status_label} — paid ${money(claim.net_paid)}, `
          + `reimbursed ${money(claim.reimbursed)}, rejected ${money(claim.rejected)}, `
          + `remaining ${money(claim.remaining)}`
          + (owed.length ? `; ${owed.join("; ")}` : "")),
        claim.shared ? el("p", { class:claim.shared.needs_review ? "note negative" : "note" },
          claim.shared.text) : null,
        (claim.events || []).length ? el("ul", { class:"note claim-history" },
          ...claim.events.map((event)=>el("li", {}, eventText(event)))) : null,
        el("div", { class:"row" },
          why, closing,
          el("button", { class:"action", type:"button", onclick:()=>loadClaim(claim) }, "Edit"),
          el("button", { class:"action", type:"button", onclick:async()=>{
            await post("/api/fsa/claim/delete", { handle:claim.handle });
            data.claims = data.claims.filter((item)=>item.handle !== claim.handle);
            if (editingHandle === claim.handle) clearForm();
            renderList();
          }}, "Delete")));
    }) : [el("p", { class:"note" }, "No FSA claims yet.")];
    list.replaceChildren(...claimRows);
  };
  const save = async () => {
    const payments = Array.from(paymentSelect.selectedOptions).map(
      (option)=>JSON.parse(option.value));
    const refunds = Array.from(refundSelect.selectedOptions).map(
      (option)=>JSON.parse(option.value));
    const allocationPayload = Array.from(allocations.children).map((row) => {
      const fields = row._claimFields;
      const targetValue = fields.target.value.trim();
      const rejections = Array.from(fields.rejections.children).map((rejectionRow) => {
        const r = rejectionRow._rejectionFields;
        return { attempted_on:r.attempted.value, amount:r.amount.value.trim(), reason:r.reason.value.trim() };
      });
      return {
        account:fields.accountSelect.value,
        funding_year_start:fields.yearSelect.value,
        target:targetValue || null,
        reimbursements:Array.from(fields.reimburse.selectedOptions).map(
          (option)=>JSON.parse(option.value)),
        repayments:Array.from(fields.repay.selectedOptions).map(
          (option)=>JSON.parse(option.value)),
        rejections,
      };
    });
    await post("/api/fsa/claim/save", {
      handle:editingHandle,
      service_date:service.value,
      provider:provider.value.trim(),
      description:description.value.trim(),
      eob_responsibility:eob.value.trim(), payments, refunds, allocations:allocationPayload,
      receivable:payer.value || null,
      eob_note:eobNote.value.trim(),
    });
    backdrop.remove(); say(editingHandle ? "FSA claim updated." : "FSA claim saved."); render();
  };
  refreshClaimCandidates([], []);
  renderList();
  if (initialHandle) {
    const initial = data.claims.find((claim)=>claim.handle === initialHandle);
    if (initial) loadClaim(initial);
  }
  const form = el("section", { class:"detail-dialog wide" },
    el("h2", {}, "FSA claims"), list,
    el("h3", {}, "Claim / service episode"),
    el("p", { class:"note" },
      "Service and EOB information is separate from ledger dates. Provider refunds reduce "
      + "the net amount paid; rejected reimbursement attempts are tracked without creating ledger activity."),
    el("div", { class:"row" }, service, provider, description, eob),
    el("label", {}, "EOB change note", eobNote),
    el("label", {}, "Payer covers part", payer),
    el("label", {}, "Healthcare payments"), paymentSelect,
    el("label", {}, "Provider refunds / credits"), refundSelect,
    el("h3", {}, "FSA allocations"), allocations,
    el("div", { class:"toolbar" },
      el("button", { class:"action", type:"button", onclick:()=>addAllocation() },
        "Add FSA allocation"),
      el("button", { class:"action", type:"button", onclick:clearForm }, "New claim"),
      el("span", { class:"spacer" }),
      el("button", { class:"action", type:"button", onclick:()=>backdrop.remove() }, "Close"),
      el("button", { class:"action primary", type:"button",
        onclick:()=>save().catch((error)=>say(error.message,"error")) }, "Save claim")));
  backdrop.append(form); document.body.append(backdrop);
}


function openAccountDetails(account) {
  const backdrop = el("div", {
    class:"detail-backdrop",
    onclick:(event)=>{ if (event.target === backdrop) backdrop.remove(); },
  });
  const value = (item) => item === null || item === undefined || item === "" ? "—" : String(item);
  const metadata = [
    ["Full name", account.full_name], ["Code", account.code],
    ["Description", account.description], ["Local notes", account.notes],
    ["Commodity", account.commodity], ["Commodity SCU", account.commodity_scu],
    ["Placeholder", account.placeholder ? "Yes" : "No"],
    ["Hidden", account.hidden ? "Yes" : "No"],
  ].map(([name, item]) => el("tr", {}, el("td", {}, name), el("td", {}, value(item))));
  const source = account.source_guid || account.source_type
    ? el("div", {},
        el("h3", {}, "Read-only GnuCash provenance"),
        table(["Field", "Type", "Value"], [
          el("tr", {}, el("td", {}, "Source GUID"), el("td", {}, "guid"),
            el("td", {}, value(account.source_guid))),
          el("tr", {}, el("td", {}, "Source account type"), el("td", {}, "type"),
            el("td", {}, value(account.source_type))),
          el("tr", {}, el("td", {}, "Source notes"), el("td", {}, "string"),
            el("td", {}, value(account.source_notes))),
          ...(account.source_fields || []).map((field) => el("tr", {},
            el("td", {}, field.name), el("td", {}, field.value_type),
            el("td", {}, value(field.value)))),
        ]))
    : el("p", {class:"note"}, "This is a native BreadSched account.");
  const content = el("section", {class:"detail-dialog wide"},
    el("h2", {}, account.name),
    table(["Account field", "Value"], metadata), source,
    el("div", {class:"toolbar"}, el("span", {class:"spacer"}),
      el("button", {class:"action", type:"button", onclick:()=>backdrop.remove()}, "Close")));
  backdrop.append(content); document.body.append(backdrop);
}

async function showAccounts() {
  const [summary, accounts] = await Promise.all([
    get("/api/summary"), get("/api/accounts"),
  ]);
  state.accounts = accounts;
  const cards = el("div", { class: "cards" },
    [["Cash on hand", summary.cash], ["Net worth", summary.net_worth],
     ["Accounts", summary.accounts], ["Transactions", summary.transactions]]
      .map(([label, value]) => el("div", { class: "card" },
        el("div", { class: "label" }, label),
        el("div", { class: "value" },
          value === null ? "Missing reporting-currency quote"
            : typeof value === "string" ? money(value) : value))));

  const rows = accounts.map((account) => el("tr", {},
    el("td", { class: depthClass("indent", account.depth) },
      account.placeholder
        ? el("span", { class: "muted" }, account.name)
        : el("span", {
            class: "clickable",
            onclick: () => { state.account = account.handle; switchTo("Register"); },
          }, account.name)),
    el("td", {}, el("select", {
      onchange: async (event) => {
        await post("/api/account/type", {
          handle: account.handle, type: event.target.value,
        });
        render();
      },
    }, ([
      ["BANK", "Bank"], ["CASH", "Cash"], ["ASSET", "Asset"],
      ["INVESTMENT", "Investment"], ["RETIREMENT", "Retirement"],
      ["FSA", "FSA / benefit"], ["ESCROW", "Escrow"], ["RECEIVABLE", "Receivable"],
      ["CREDIT CARD", "Credit card"], ["LOAN", "Loan"],
      ["LIABILITY", "Liability"], ["INCOME", "Income"],
      ["EXPENSE", "Expense"], ["EQUITY", "Equity"],
    ]).map(([value, label]) => el("option", {
      value, selected: account.type === value ? "selected" : null,
    }, label)))),
    el("td", {}, account.emergency_fund_eligible
      ? el("input", {
          type:"checkbox", checked:account.emergency_fund_included ? "checked" : null,
          title:"Carry recurring activity in the no-income emergency fund",
          onchange: async (event) => {
            await post("/api/account/emergency-fund", {
              handle:account.handle, included:event.target.checked,
            });
            account.emergency_fund_included = event.target.checked;
          },
        })
      : el("span", { class:"muted", title:"This type is always excluded" }, "—")),
    el("td", {}, account.type === "FSA"
      ? el("button", { class:"action", type:"button", onclick:()=>openFsaYearsEditor(account) },
          `${(account.fsa_years || []).length} funding year${(account.fsa_years || []).length === 1 ? "" : "s"}…`)
      : el("span", { class:"muted" }, "—")),
    el("td", {}, account.type === "CREDIT CARD"
      ? el("button", {class:"action", type:"button",
          onclick:()=>openCardPaymentEditor(account, accounts)},
          account.payment_day ? `Day ${account.payment_day}…` : "Configure…")
      : el("span", {class:"muted"}, "—")),
    el("td", { class:"num" }, account.valuation_source === "market"
      ? `${account.quantity} ${account.commodity}` : "—"),
    el("td", { class:"num" }, account.valuation_source === "market"
      ? `${money(account.price)} ${account.currency}` : "—"),
    el("td", { class:"muted" }, account.price_date || "—"),
    el("td", { class:"muted" }, account.quote_evidence || "—"),
    el("td", {}, el("button", {class:"action", type:"button",
      onclick:()=>openAccountDetails(account)}, "Details…")),
    el("td", { class: cls(account.balance) },
      account.balance === null ? (account.rollup_missing_quotes?.length
        ? "Missing reporting-currency quote" : "Mixed currencies") : money(account.balance))));

  return el("div", {}, cards,
    el("div", { class:"toolbar" },
      el("button", { class:"action", type:"button",
        onclick:()=>openSecurityPriceEditor().catch((error)=>say(error.message,"error")) },
      "Security price…"),
      el("button", { class:"action", type:"button",
        onclick:()=>openCurrencyRateEditor().catch((error)=>say(error.message,"error")) },
      "Exchange rate…")),
    el("p", { class: "note" },
      "Click an account to open its register. Parent rows show the total of "
      + "everything beneath them. Investment values use the latest dated price "
      + "when one is available; otherwise they retain their ledger value."),
    table(["Account", "Type", "Emergency", "FSA years", "Card payment",
           { label:"Units", num:true },
           { label:"Price", num:true }, "As of", "Quote source", "Metadata",
           { label: "Value", num: true }], rows));
}

async function openReconciliation(account) {
  const data = await get(`/api/reconciliation?account=${encodeURIComponent(account.handle)}`);
  const backdrop = el("div", {
    class:"detail-backdrop",
    onclick:(event)=>{ if (event.target === backdrop) backdrop.remove(); },
  });
  const reload = async () => {
    backdrop.remove();
    await openReconciliation(account);
  };
  let content;
  if (!data.open) {
    const statementDate = el("input", {
      type:"date", value:new Date().toISOString().slice(0, 10),
    });
    const ending = el("input", {inputmode:"decimal", placeholder:"0.00"});
    const actions = [
      el("button", {class:"action primary", type:"button", onclick:async()=>{
        try {
          await post("/api/reconciliation/start", {
            account:account.handle,
            statement_date:statementDate.value,
            ending_balance:ending.value.trim(),
          });
          await reload();
        } catch (error) { say(error.message, "error"); }
      }}, "Start reconciliation"),
    ];
    const completed = data.history.find((item)=>item.status === "completed");
    if (completed) actions.push(
      el("button", {class:"action", type:"button", onclick:async()=>{
        try {
          await post("/api/reconciliation/reopen", {handle:completed.handle});
          await reload();
        } catch (error) { say(error.message, "error"); }
      }}, `Reopen ${completed.statement_date}`));
    content = el("div", {class:"stack"},
      el("p", {class:"note"},
        "Already-cleared entries start checked. Ledger states change only when the "
        + "statement difference is zero and you finish."),
      el("label", {}, "Statement date", statementDate),
      el("label", {}, "Ending balance", ending),
      el("div", {class:"toolbar"}, actions));
  } else {
    const session = data.open;
    const ending = el("input", {
      inputmode:"decimal", value:String(session.ending_balance),
    });
    const checks = session.candidates.map((item)=>{
      const check = el("input", {
        type:"checkbox", checked:item.selected ? "checked" : null,
        value:item.split,
      });
      return {item, check, row:el("tr", {},
        el("td", {}, check),
        el("td", {}, item.date),
        el("td", {}, item.description || "(no description)"),
        el("td", {class:`num ${cls(item.amount)}`}, money(item.amount)),
        el("td", {class:"muted"}, item.state === "c" ? "cleared" : "not cleared"))};
    });
    const save = async () => {
      await post("/api/reconciliation/update", {
        handle:session.handle,
        ending_balance:ending.value.trim(),
        selected_splits:checks.filter(({check})=>check.checked).map(({item})=>item.split),
      });
      await reload();
    };
    content = el("div", {class:"stack"},
      el("p", {class:"note"}, `Statement through ${session.statement_date}`),
      el("label", {}, "Ending balance", ending),
      el("p", {class:session.balanced ? "note" : "note negative"},
        `Prior reconciled ${money(session.opening_balance)} · Checked `
        + `${money(session.selected_balance)} · Difference ${money(session.difference)}`),
      table(["✓", "Date", "Description", {label:"Amount", num:true}, "State"],
        checks.map(({row})=>row)),
      el("div", {class:"toolbar"},
        el("button", {class:"action", type:"button", onclick:()=>save().catch(
          (error)=>say(error.message, "error"))}, "Save checks"),
        el("button", {class:"action", type:"button", onclick:async()=>{
          await post("/api/reconciliation/cancel", {handle:session.handle});
          await reload();
        }}, "Cancel reconciliation"),
        el("span", {class:"spacer"}),
        el("button", {class:"action primary", type:"button", disabled:!session.balanced,
          onclick:async()=>{
            try {
              await post("/api/reconciliation/complete", {handle:session.handle});
              backdrop.remove();
              say("Statement reconciled.");
              render();
            } catch (error) { say(error.message, "error"); }
          }}, "Finish")));
  }
  const body = el("section", {class:"detail-dialog wide"},
    el("h2", {}, `Reconcile — ${data.account.name}`),
    data.reimbursement_notice ? el("p", {class:"note"}, data.reimbursement_notice) : null,
    data.claim_link_notice ? el("p", {class:"note"}, data.claim_link_notice) : null,
    content,
    el("div", {class:"toolbar"}, el("span", {class:"spacer"}),
      el("button", {class:"action", type:"button", onclick:()=>backdrop.remove()}, "Close")));
  backdrop.append(body);
  document.body.append(backdrop);
}

async function showRegister() {
  if (!state.accounts.length) state.accounts = await get("/api/accounts");
  const usable = state.accounts.filter((a) => !a.placeholder && a.depth > 0);
  if (!state.account && usable.length) state.account = usable[0].handle;
  if (!state.account) return el("p", { class: "note" }, "No accounts yet.");

  const data = await get(`/api/register?account=${encodeURIComponent(state.account)}`);
  const selectedAccount = usable.find((item) => item.handle === state.account);
  const picker = el("select", {
    onchange: (event) => {
      const next = event.target.value;
      if (entryDirty() && !window.confirm(
        "Discard the transaction you were entering or editing?")) {
        event.target.value = state.account;
        return;
      }
      state.entryDrafts = {};
      state.editing = null;
      state.account = next;
      render();
    },
  }, usable.map((a) => el("option", {
    value: a.handle, selected: a.handle === state.account ? "selected" : null,
  }, a.full_name)));

  const makeScheduled = async (transaction) => {
    try {
      const schedules = await get("/api/scheduled?days=90");
      const draft = await post("/api/scheduled/draft", {transaction});
      openScheduledEditor(schedules, draft);
    } catch (error) { say(error.message, "error"); }
  };
  const payeePicker = (row) => {
    // The payee is BreadSched's own reference; the description is never rewritten.
    const select = el("select", { "aria-label":`Payee for ${row.description}`,
      onchange: async (event) => {
        try {
          await post("/api/transaction/payee",
            { transaction:row.handle, payee:event.target.value || null });
          say("Payee saved.");
        } catch (error) { say(error.message, "error"); render(); }
      } },
      el("option", { value:"" }, "(no payee)"),
      ...data.payees.map((payee) => el("option", { value:payee.handle,
        selected: payee.handle === row.payee ? "selected" : null }, payee.name)));
    return select;
  };
  const rows = [];
  for (const row of data.rows) {
    if (state.editing && state.editing.split === row.split) {
      rows.push(...registerEntry(data, usable, row));
      continue;
    }
    const amount = Number(row.amount);
    rows.push(el("tr", {},
      el("td", {}, row.date),
      el("td", { class: "muted" }, row.num),
      el("td", {},
        el("div", {}, row.description),
        row.notes ? el("div", {class:"muted"}, `BreadSched: ${row.notes}`) : null,
        row.source_notes ? el("div", {class:"muted"}, `Imported: ${row.source_notes}`) : null,
        row.tags.length ? el("div", {class:"muted"}, `Tags: ${row.tags.join(", ")}`) : null,
        row.documents.length ? el("div", {class:
          row.documents.some((item)=>item.missing) ? "negative" : "muted"},
          `Documents: ${row.documents.length}`
          + (row.documents.some((item)=>item.missing)
            ? ` (${row.documents.filter((item)=>item.missing).length} missing)` : "")) : null),
      el("td", {}, payeePicker(row)),
      el("td", { class: "muted" }, row.transfer),
      el("td", { class: "num" }, amount > 0 ? money(row.amount) : ""),
      el("td", { class: "num" }, amount < 0 ? money(String(row.amount).slice(1)) : ""),
      el("td", { class: cls(row.balance) }, money(row.balance)),
      el("td", {}, el("div", { class:"row" },
        el("button", { class:"action", type:"button", title:"Edit this transaction in its row",
          onclick:()=>leaveEntry(()=>{ state.editing = {split:row.split, focus:true}; render(); }) },
          "Edit"),
        el("button", { class:"action", type:"button",
          title:"Tag this transaction and link receipts, statements, or web pages",
          onclick:()=>openTransactionDocuments(row) }, "Tags & documents…"),
        el("button", { class:"action", type:"button",
          onclick:()=>makeScheduled(row.handle) }, "Make scheduled…"),
        el("button", { class:"action", type:"button",
          title:"Track this expense as owed back by an insurer, employer, or other payer",
          onclick:()=>{ state.receivableFrom = row.handle; switchTo("Reimbursables"); } },
          "Reimbursable…")))));
  }
  rows.push(...registerEntry(data, usable, null));

  return el("div", {},
    el("div", { class: "toolbar" }, picker,
      el("button", {class:"action", type:"button",
        onclick:()=>openInNewTab("Register", { account: state.account }) },
        "Open in new tab"),
      el("button", {class:"action", type:"button", onclick:()=>{
        const account = usable.find((item)=>item.handle === state.account);
        if (account) openReconciliation(account).catch((error)=>say(error.message,"error"));
      }}, "Reconcile…"),
      el("span", { class: "muted" }, `${data.rows.length} entries`)),
    table(["Date", "Num", "Description", "Payee", "Transfer",
           { label: data.debit_label, num: true }, { label: data.credit_label, num: true },
           { label: "Balance", num: true }, ""], rows));
}
