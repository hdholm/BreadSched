const VIEWS = ["Dashboard", "FSA Dashboard", "Accounts", "Register", "Scheduled", "Plan", "Review", "Projection", "Enter", "Import", "Verify"];
const launchParams = new URLSearchParams(window.location.search);
let current = VIEWS.includes(launchParams.get("view")) ? launchParams.get("view") : "Dashboard";
let state = {
  accounts: [], account: launchParams.get("account"), plan: null, review: null,
  planPrintDetail: false, expenseCategory: null, expenseIndex: 0, expenseSort: "actual",
  scenarioManager: null, scenarioPeriod: null, scenarioEvent: null,
  projectionData: null, projectionHandle: null, projectionCompareHandle: null,
  projectionComparison: null,
};
let historicalEstimateDialog = null;

const el = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
};

const svgEl = (tag, attrs = {}, text = null) => {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value));
  if (text !== null) node.append(document.createTextNode(String(text)));
  return node;
};

const depthClass = (kind, depth) => {
  const bounded = Math.min(12, Math.max(0, Number(depth) || 0));
  return `${kind} ${kind}-${bounded}`;
};

const money = (value) => {
  const n = Number(value);
  const text = Math.abs(n).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  return n < 0 ? `(${text})` : text;
};
const signedMoney = (value) => {
  if (value === null || value === undefined) return "—";
  const rendered = money(value);
  return Number(value) > 0 ? `+${rendered}` : rendered;
};
const cls = (value) => (Number(value) < 0 ? "num neg" : "num");

const params = new URLSearchParams(window.location.hash.slice(1));
const apiToken = params.get("token") || "";
if (params.has("token")) history.replaceState(null, "", window.location.pathname + window.location.search);
const apiHeaders = () => ({ "X-BreadSched-Token": apiToken });
const browserNumberFormat = (() => {
  const decimal = new Intl.NumberFormat().formatToParts(1.1)
    .find((part) => part.type === "decimal")?.value;
  return decimal === "," ? "comma" : "dot";
})();

async function get(path) {
  const response = await fetch(path, { headers: apiHeaders() });
  if (!response.ok) throw new Error((await response.json()).error || response.statusText);
  return response.json();
}
async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { ...apiHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({ number_format: browserNumberFormat, ...(body || {}) }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

function say(text, kind) {
  const box = document.getElementById("message");
  box.textContent = text;
  box.className = kind || "ok";
  setTimeout(() => box.classList.add("hidden"), 4000);
}


function timelineEditor(label, initial, withAmount, onChanged=()=>{}) {
  const rows = el("div", {});
  const box = el("div", { class:`timeline-editor${withAmount ? "" : " dates"}` },
    el("strong", {}, label), rows);
  const addRow = (item={}) => {
    const row = el("div", { class:"timeline-row" });
    const when = el("input", { type:"date", value:item.when || item.start || "" });
    const amount = withAmount ? el("input", { inputmode:"decimal", placeholder:"Amount", value:item.amount || "" }) : null;
    const remove = el("button", { class:"action", type:"button", onclick:()=>{ row.remove(); onChanged(); } }, "Remove");
    row.append(when);
    if (amount) row.append(amount);
    row.append(remove);
    rows.append(row);
    when.addEventListener("change", onChanged);
    if (amount) amount.addEventListener("change", onChanged);
  };
  (initial || []).forEach(addRow);
  box.append(el("button", { class:"action", type:"button", onclick:()=>{ addRow(); onChanged(); } },
    withAmount ? "Add date and amount" : "Add occurrence"));
  return {
    node:box,
    values:()=>Array.from(rows.children).map((row)=>{
      const inputs=row.querySelectorAll("input");
      const when=inputs[0].value;
      if (!when) throw new Error(`${label}: enter a date or remove the empty row.`);
      if (!withAmount) return when;
      const amount=inputs[1].value.trim();
      if (!amount) throw new Error(`${label}: enter an amount or remove the empty row.`);
      return {when, amount};
    }),
  };
}

function monthAmountEditor(initial, onChanged=()=>{}) {
  const names = ["January","February","March","April","May","June",
    "July","August","September","October","November","December"];
  const rows = el("div", {});
  const box = el("div", {class:"timeline-editor"},
    el("strong", {}, "Seasonal month amounts"), rows,
    el("p", {class:"note"}, "Override the ordinary estimate in selected calendar months."));
  const addRow = (item={}) => {
    const row = el("div", {class:"timeline-row"});
    const month = el("select", {}, names.map((name,index)=>el("option", {
      value:String(index+1), selected:Number(item.month || 1)===index+1 ? "selected" : null,
    }, name)));
    const amount = el("input", {inputmode:"decimal", placeholder:"Amount",
      value:item.amount ?? ""});
    const remove = el("button", {class:"action",type:"button",
      onclick:()=>{row.remove();onChanged();}}, "Remove");
    row.append(month, amount, remove); rows.append(row);
    [month, amount].forEach((control)=>control.addEventListener("change", onChanged));
  };
  (initial || []).forEach(addRow);
  box.append(el("button", {class:"action",type:"button",
    onclick:()=>{addRow();onChanged();}}, "Add seasonal month"));
  return {node:box, values:()=>{
    const values=Array.from(rows.children).map((row)=>({
      month:Number(row.querySelector("select").value),
      amount:row.querySelector("input").value.trim(),
    }));
    if (values.some((item)=>!item.amount)) throw new Error("Seasonal amounts: enter an amount or remove the row.");
    if (new Set(values.map((item)=>item.month)).size !== values.length) throw new Error("Seasonal amounts: each month may appear once.");
    return values;
  }};
}

function splitAmountTimelineEditor(accounts, initial, onChanged=()=>{}) {
  const rows = el("div", {});
  const box = el("div", { class:"timeline-editor" },
    el("strong", {}, "Per-leg future amounts"), rows,
    el("p", { class:"note" },
      "Use exact signed ledger amounts. Changes sharing a date must leave all legs balanced."));
  const addRow = (item={}) => {
    const row = el("div", { class:"timeline-row" });
    const account = el("select", {}, accounts.map((entry)=>el("option", {
      value:entry.handle, selected:entry.handle===item.account ? "selected" : null,
    }, entry.name)));
    const when = el("input", { type:"date", value:item.start || "" });
    const amount = el("input", { inputmode:"decimal", placeholder:"Signed ledger amount",
      value:item.amount ?? "" });
    const remove = el("button", { class:"action", type:"button",
      onclick:()=>{ row.remove(); onChanged(); } }, "Remove");
    row.append(account, when, amount, remove);
    rows.append(row);
    [account, when, amount].forEach((control)=>control.addEventListener("change", onChanged));
  };
  (initial || []).forEach(addRow);
  box.append(el("button", { class:"action", type:"button",
    onclick:()=>{ addRow(); onChanged(); } }, "Add leg amount change"));
  return {
    node:box,
    values:()=>Array.from(rows.children).map((row)=>{
      const account=row.querySelector("select").value;
      const inputs=row.querySelectorAll("input");
      if (!inputs[0].value || !inputs[1].value.trim()) {
        throw new Error("Per-leg future amounts: complete the account, date, and amount.");
      }
      return {account, start:inputs[0].value, amount:inputs[1].value.trim()};
    }),
  };
}

function occurrenceTimelineEditor(label, initial, withAmount, onChanged=()=>{}) {
  const rows = el("div", {});
  let options = [];
  const addButton = el("button", { class:"action", type:"button" },
    withAmount ? "Add occurrence amount" : "Add skipped occurrence");
  const box = el("div", { class:`timeline-editor${withAmount ? "" : " dates"}` },
    el("strong", {}, label), rows, addButton);

  const fillSelect = (select, current) => {
    const choices = [...options];
    if (current && !choices.includes(current)) choices.unshift(current);
    select.replaceChildren(...(choices.length
      ? choices.map((when) => el("option", { value:when, selected:when===current ? "selected" : null }, when))
      : [el("option", { value:"" }, "No occurrences available")]));
  };
  const addRow = (item={}) => {
    const row = el("div", { class:"timeline-row" });
    const when = el("select", {});
    fillSelect(when, item.when || item.start || options[0] || "");
    const amount = withAmount
      ? el("input", { inputmode:"decimal", placeholder:"Amount", value:item.amount || "" })
      : null;
    const remove = el("button", { class:"action", type:"button", onclick:()=>{ row.remove(); onChanged(); } }, "Remove");
    row.append(when);
    if (amount) row.append(amount);
    row.append(remove);
    rows.append(row);
    when.addEventListener("change", onChanged);
    if (amount) amount.addEventListener("change", onChanged);
  };
  addButton.onclick = () => { addRow(); onChanged(); };
  (initial || []).forEach(addRow);
  return {
    node:box,
    setOptions:(values)=>{
      options = values || [];
      Array.from(rows.children).forEach((row)=>{
        const select=row.querySelector("select");
        fillSelect(select, select.value);
      });
      addButton.disabled = options.length === 0;
    },
    values:()=>Array.from(rows.children).map((row)=>{
      const select=row.querySelector("select");
      const when=select.value;
      if (!when) throw new Error(`${label}: choose an occurrence or remove the row.`);
      if (!withAmount) return when;
      const amount=row.querySelector("input").value.trim();
      if (!amount) throw new Error(`${label}: enter an amount or remove the row.`);
      return {when, amount};
    }),
  };
}

const INVESTMENT_ACTIVITY_OPTIONS = [
  ["", "Ordinary investment activity"],
  ["contribution", "Contribution"],
  ["withdrawal", "Taxable withdrawal"],
  ["retirement_distribution", "Retirement distribution"],
  ["dividend", "Reinvested dividend"],
  ["interest", "Reinvested interest"],
  ["fee", "Investment fee"],
  ["rollover", "Retirement rollover"],
];

function planningSplitEditor(accounts, initial, onChanged=()=>{}) {
  const rows = el("div", {});
  const purposes = [
    ["", "Ordinary account flow"],
    ["retirement_saving", "Retirement saving"],
    ["benefit_funding", "Benefit / FSA funding"],
    ["debt_principal", "Debt principal"],
    ["retirement_income", "Retirement distribution"],
  ];
  const box = el("div", { class:"timeline-editor planning-splits" },
    el("strong", {}, "Additional splits"), rows);
  const addRow = (item={}) => {
    const row = el("div", { class:"timeline-row" });
    const account = el("select", {}, accounts.map((entry)=>
      el("option", { value:entry.handle, selected:entry.handle===item.account ? "selected" : null }, entry.name)));
    const amount = el("input", { inputmode:"decimal", placeholder:"Amount", value:item.amount || "" });
    const memo = el("input", { placeholder:"Memo", value:item.memo || "" });
    const purpose = el("select", {}, purposes.map(([value,label])=>
      el("option", { value, selected:value===(item.planning_flow || "") ? "selected" : null }, label)));
    const activity = el("select", {}, INVESTMENT_ACTIVITY_OPTIONS.map(([value,label])=>
      el("option", { value, selected:value===(item.investment_activity || "") ? "selected" : null }, label)));
    const direction = el("select", {}, [["normal","Normal direction"],["opposite","Opposite direction"]]
      .map(([value,label])=>el("option", {
        value, selected:value===(item.direction || "normal") ? "selected" : null,
      }, label)));
    const remove = el("button", { class:"action", type:"button", onclick:()=>{ row.remove(); onChanged(); } }, "Remove");
    row.append(account, amount, purpose, activity, direction, memo, remove);
    rows.append(row);
    [account, amount, purpose, activity, direction]
      .forEach((control)=>control.addEventListener("change", onChanged));
  };
  (initial || []).forEach(addRow);
  box.append(el("p", { class:"note" },
    "Use positive economic amounts. Debt principal reduces the liability; retirement distributions flow out of the investment account. Net paid-from/into cash is balanced automatically."));
  box.append(el("button", { class:"action", type:"button", onclick:()=>{ addRow(); onChanged(); } }, "Add split"));
  return {
    node:box,
    values:()=>Array.from(rows.children).map((row)=>{
      const selects=row.querySelectorAll("select");
      const inputs=row.querySelectorAll("input");
      const amount=inputs[0].value.trim();
      if (!amount) throw new Error("Additional splits: enter an amount or remove the empty row.");
      return {account:selects[0].value, amount, planning_flow:selects[1].value || null,
        investment_activity:selects[2].value || null, direction:selects[3].value,
        memo:inputs[1].value.trim()};
    }),
  };
}

function schedulePreviewNode() {
  const body = el("div", { class:"schedule-preview" });
  const node = el("div", { class:"panel compact" },
    el("strong", {}, "Upcoming occurrences"), body);
  return {
    node,
    show:(items)=>{
      body.replaceChildren(...((items || []).length
        ? items.map((item)=>el("div", { class:"preview-row" },
            el("span", {}, item.when),
            el("span", { class:"num" }, money(item.amount)),
            el("span", {}, item.status)))
        : [el("p", { class:"note" }, "No upcoming occurrences.")]));
    },
  };
}

function table(headers, rows) {
  return el("div", { class: "panel" },
    el("table", {},
      el("thead", {}, el("tr", {}, headers.map((h) =>
        el("th", { class: h.num ? "num" : null }, h.label ?? h)))),
      el("tbody", {}, rows)));
}


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
      el("input", { type:"date", value:year.runout_through || "", title:"Run-out through" }));
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


async function openFsaClaimsEditor(initialHandle=null) {
  const data = await get("/api/fsa/claims");
  const backdrop = el("div", {
    class:"detail-backdrop",
    onclick:(event)=>{ if (event.target === backdrop) backdrop.remove(); },
  });
  const list = el("div", { class:"stack" });
  let editingHandle = null;
  const service = el("input", { type:"date" });
  const provider = el("input", { placeholder:"Provider" });
  const description = el("input", { placeholder:"Description" });
  const eob = el("input", { placeholder:"EOB patient responsibility" });
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
      focusCandidateDate(reimburse);
    };
    accountSelect.onchange = ()=>{ initial = null; refresh(); };
    yearSelect.onchange = ()=>{ initial = null; refresh(); };
    const row = el("div", { class:"card" },
      el("div", { class:"row" }, accountSelect, yearSelect, target,
        el("button", { class:"action", type:"button", onclick:()=>row.remove() },
          "Remove allocation")),
      el("label", {}, "Reimbursement/payment splits"), reimburse,
      el("strong", {}, "Rejected/failed reimbursement attempts"), rejections,
      el("button", { class:"action", type:"button", onclick:()=>addRejection() },
        "Add rejected attempt"));
    row._claimFields = { accountSelect, yearSelect, target, reimburse, rejections };
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
    refreshClaimCandidates([], []);
    allocations.replaceChildren();
  };
  const loadClaim = (claim) => {
    clearForm(); editingHandle = claim.handle;
    service.value = claim.service_date; provider.value = claim.provider || "";
    description.value = claim.description || "";
    eob.value = claim.eob_responsibility || "";
    refreshClaimCandidates(claim.payments, claim.refunds);
    (claim.allocations || []).forEach((allocation)=>addAllocation(allocation));
  };
  const renderList = () => {
    const claimRows = data.claims.length ? data.claims.map((claim) =>
      el("div", { class:"card" },
        el("strong", {}, `${claim.service_date} ${claim.provider || "FSA claim"}`),
        el("span", {}, ` ${claim.status_label} — paid ${money(claim.net_paid)}, `
          + `reimbursed ${money(claim.reimbursed)}, rejected ${money(claim.rejected)}, `
          + `remaining ${money(claim.remaining)}`),
        el("div", { class:"row" },
          el("button", { class:"action", type:"button", onclick:()=>loadClaim(claim) }, "Edit"),
          el("button", { class:"action", type:"button", onclick:async()=>{
            await post("/api/fsa/claim/delete", { handle:claim.handle });
            data.claims = data.claims.filter((item)=>item.handle !== claim.handle);
            if (editingHandle === claim.handle) clearForm();
            renderList();
          }}, "Delete")))) : [el("p", { class:"note" }, "No FSA claims yet.")];
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
        rejections,
      };
    });
    await post("/api/fsa/claim/save", {
      handle:editingHandle,
      service_date:service.value,
      provider:provider.value.trim(),
      description:description.value.trim(),
      eob_responsibility:eob.value.trim(), payments, refunds, allocations:allocationPayload,
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
      ["FSA", "FSA / benefit"], ["ESCROW", "Escrow"],
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
    el("td", { class:"muted" }, account.missing_quote
      ? `No reporting-currency quote; ledger value${account.currency ? ` (${account.currency})` : ""}`
      : ["market", "currency"].includes(account.valuation_source)
        ? (account.price_source || "Unknown source") : "—"),
    el("td", {}, el("button", {class:"action", type:"button",
      onclick:()=>openAccountDetails(account)}, "Details…")),
    el("td", { class: cls(account.balance) },
      account.balance === null ? (account.rollup_missing_quotes?.length
        ? "Missing reporting-currency quote" : "Mixed currencies") : money(account.balance))));

  return el("div", {}, cards,
    el("div", { class:"toolbar" },
      el("button", { class:"action", type:"button",
        onclick:()=>openSecurityPriceEditor().catch((error)=>say(error.message,"error")) },
      "Security price…")),
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
    el("h2", {}, `Reconcile — ${data.account.name}`), content,
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
  const transferAccounts = usable.filter((item) =>
    !item.hidden && item.handle !== state.account);
  const picker = el("select", {
    onchange: (event) => { state.account = event.target.value; render(); },
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
  const rows = data.rows.map((row) => el("tr", {},
    el("td", {}, row.date),
    el("td", { class: "muted" }, row.num),
    el("td", {},
      el("div", {}, row.description),
      row.notes ? el("div", {class:"muted"}, `BreadSched: ${row.notes}`) : null,
      row.source_notes ? el("div", {class:"muted"}, `Imported: ${row.source_notes}`) : null),
    el("td", { class: "muted" }, row.transfer),
    el("td", { class: cls(row.amount) }, money(row.amount)),
    el("td", { class: cls(row.balance) }, money(row.balance)),
    el("td", {}, el("button", { class:"action", type:"button",
      onclick:()=>makeScheduled(row.handle) }, "Make scheduled…"))));

  const quickEntry = selectedAccount?.hidden ? el("p", {class:"note"},
    "Hidden accounts remain readable but cannot be used for a new transaction.")
    : el("form", {class:"toolbar", onsubmit:async(event)=>{
      event.preventDefault();
      const values = Object.fromEntries(new FormData(event.target).entries());
      const debit = event.submitter?.value === "debit";
      const transfer = transferAccounts.find((item)=>item.full_name === values.transfer);
      if (!selectedAccount || !transfer) {
        say("Choose two visible accounts.", "error");
        return;
      }
      try {
        await post("/api/transaction", {
          date:values.date, description:values.description, amount:values.amount,
          from:debit ? transfer.full_name : selectedAccount.full_name,
          to:debit ? selectedAccount.full_name : transfer.full_name,
        });
        say("Transaction posted.");
        render();
      } catch (error) { say(error.message, "error"); }
    }},
      el("strong", {}, "Quick entry"),
      el("input", {type:"date", name:"date", required:"required",
        value:new Date().toISOString().slice(0,10)}),
      el("input", {name:"description", placeholder:"Description", required:"required"}),
      el("select", {name:"transfer"}, transferAccounts.map((item)=>
        el("option", {value:item.full_name}, item.full_name))),
      el("input", {name:"amount", placeholder:"0.00", inputmode:"decimal",
        required:"required"}),
      el("button", {class:"action primary", type:"submit", name:"direction", value:"debit"},
        data.debit_label),
      el("button", {class:"action", type:"submit", name:"direction", value:"credit"},
        data.credit_label));

  return el("div", {},
    el("div", { class: "toolbar" }, picker,
      el("button", {class:"action", type:"button", onclick:()=>{
        const url = new URL(window.location.href);
        url.searchParams.set("view", "Register");
        url.searchParams.set("account", state.account);
        url.hash = new URLSearchParams({token:apiToken}).toString();
        window.open(url.toString(), "_blank", "noopener");
      }}, "Open in new window"),
      el("button", {class:"action", type:"button", onclick:()=>{
        const account = usable.find((item)=>item.handle === state.account);
        if (account) openReconciliation(account).catch((error)=>say(error.message,"error"));
      }}, "Reconcile…"),
      el("span", { class: "muted" }, `${data.rows.length} entries`)),
    quickEntry,
    table(["Date", "Num", "Description", "Transfer",
           { label: "Amount", num: true }, { label: "Balance", num: true }, ""], rows));
}

async function showScheduled() {
  if (!state.accounts.length) state.accounts = await get("/api/accounts");
  const data = await get("/api/scheduled?days=90");
  const definitionRow = (s) => el("tr", {},
    el("td", {}, el("div", {}, s.name),
      s.account_linked
        ? el("div", {class:"muted"}, "Account-linked current obligation")
        : null,
      s.unsupported_reason
        ? el("div", { class:"muted", title:String(s.source_recurrence || "") },
            `Read-only: ${s.unsupported_reason}`)
        : null),
    el("td", { class: "muted" }, s.frequency),
    el("td", { class: "num" }, money(s.amount)),
    el("td", { class: "muted" }, s.account_linked ? "account" :
      (s.placeholder ? "estimate" : (s.auto ? "automatic" : "manual"))),
    el("td", { class: "muted" }, s.enabled ? "enabled" : "disabled"),
    el("td", {}, el("div", { class:"row" },
      s.account_linked ? el("button", {class:"action", type:"button", onclick:()=>{
        const account = state.accounts.find((item)=>item.handle === s.linked_account);
        if (account) openCardPaymentEditor(account, state.accounts);
      }}, "Edit account…") : s.editor_mode === "fixed" && s.simple
        ? el("button", { class:"action", type:"button",
            onclick:()=>openScheduledEditor(data, s) }, "Edit…")
        : s.editor_mode === "formula" && s.editable
        ? el("button", { class:"action", type:"button",
            onclick:()=>openFormulaScheduleEditor(s) }, "Edit formulas…")
        : el("span", { class:"muted" }, "Read-only"),
      s.account_linked ? null : s.simple ? el("button", { class:"action", type:"button",
        onclick:()=>openScheduledEditor(data, {
          ...s, handle:null, name:`${s.name} copy`, skipped:[],
        }) }, "Duplicate…") : el("button", { class:"action", type:"button", onclick:async()=>{
          const name = window.prompt("Name for the exact protected copy", `${s.name} copy`);
          if (name == null) return;
          try {
            await post("/api/scheduled/duplicate", {handle:s.handle, name});
            say("Protected schedule copied exactly for independent review.");
            render();
          } catch (error) { say(error.message, "error"); }
        }}, "Duplicate…"),
      s.account_linked ? null : el("button", { class:"action", type:"button", onclick:async()=>{
        if (!window.confirm(`Delete scheduled transaction "${s.name}"? Already posted transactions remain.`)) return;
        try {
          await post("/api/scheduled/delete", {handle:s.handle});
          say("Scheduled transaction deleted. Undo from the desktop interface if needed.");
          render();
        } catch (error) { say(error.message, "error"); }
      }}, "Delete…"))));
  const commitments = data.definitions.filter((s) => !s.placeholder).map(definitionRow);
  const estimates = data.definitions.filter((s) => s.placeholder).map(definitionRow);

  const upcoming = data.upcoming.map((o) => el("tr", {
    ondblclick:()=>{
      const source = data.definitions.find((item)=>item.handle === o.schedule);
      if (source?.account_linked) {
        const account = state.accounts.find((item)=>item.handle === source.linked_account);
        if (account) openCardPaymentEditor(account, state.accounts);
      } else if (source?.editor_mode === "fixed" && source?.simple) {
        openScheduledEditor(data, source);
      } else if (source?.editor_mode === "formula" && source?.editable) {
        openFormulaScheduleEditor(source);
      }
      else switchTo("Scheduled");
    },
  },
    el("td", {}, o.date), el("td", {}, o.name),
    el("td", { class: "num" }, money(o.amount))));

  return el("div", {},
    el("div", { class: "toolbar" },
      el("button", { class: "action", type: "button",
        onclick: () => openScheduledEditor(data, null) }, "New scheduled…"),
      el("button", {class:"action", type:"button",
        onclick:()=>openLoanEditor().catch((error)=>say(error.message,"error"))},
        "New loan…"),
      el("button", { class: "action", type: "button",
        onclick: () => openHistoricalEstimateDialog() }, "Suggest from history…"),
      el("button", {
        class: "action primary",
        onclick: async () => {
          try {
            const result = await post("/api/post-scheduled");
            say(`Posted ${result.posted} transaction(s).`);
            render();
          } catch (error) { say(error.message, "error"); }
        },
      }, "Post due transactions")),
    el("p", { class: "note" },
      "Estimates shape Plan and Projection but are never posted. Account-linked card payments "
      + "show the current obligation and are edited on the account; they are not automatically posted. "
      + "Safe fixed and formula schedules use the same editability decision as the desktop."),
    el("h3", {}, "Upcoming"),
    table(["Due", "Schedule", { label: "Amount", num: true }], upcoming),
    el("h3", {}, "Commitments and account payments"),
    table(["Name", "Frequency", { label: "Amount", num: true }, "Posting", "State", ""],
      commitments),
    el("h3", {}, "Estimates"),
    table(["Name", "Frequency", { label: "Amount", num: true }, "Posting", "State", ""],
      estimates));
}

async function openHistoricalEstimateDialog() {
  if (historicalEstimateDialog?.isConnected) {
    historicalEstimateDialog.querySelector("button")?.focus();
    return;
  }
  const backdrop = el("div", { class: "detail-backdrop" });
  historicalEstimateDialog = backdrop;
  const close = () => {
    backdrop.remove();
    if (historicalEstimateDialog === backdrop) historicalEstimateDialog = null;
  };
  const body = el("div", {});
  const months = el("input", { type:"number", min:"3", max:"120", value:"12" });
  const target = el("select", {});
  const load = async () => {
    try {
      const selectedTarget = target.value;
      const data = await get(`/api/historical-estimates?months=${encodeURIComponent(months.value)}&scenario=${encodeURIComponent(selectedTarget)}`);
      target.replaceChildren(...data.targets.map((item) => el("option", {
        value:item.handle || "",
      }, item.name)));
      if ([...target.options].some((option) => option.value === selectedTarget)) {
        target.value = selectedTarget;
      }
      const evidenceView = (item) => {
        const evidence = item.evidence;
        const facts = [
          evidence.cadence.explanation,
          evidence.residual_explanation,
          evidence.trend?.explanation || "No material trend detected.",
          evidence.seasonality.explanation,
          evidence.funding.explanation,
          evidence.confidence.explanation,
        ];
        const history = evidence.history.map((month) => el("tr", {},
          el("td", {}, month.month.slice(0, 7)),
          el("td", {class:"num"}, money(month.gross)),
          el("td", {class:"num"}, money(month.planned)),
          el("td", {class:"num"}, money(month.residual)),
          el("td", {}, month.selected ? "Selected" : month.exclusion || "Not selected")));
        return el("details", {},
          el("summary", {}, `${evidence.selected_months}/${data.months} months selected; ${evidence.exclusions.length} exclusions`),
          el("ul", {}, ...facts.map((fact) => el("li", {}, fact))),
          table(["Month", {label:"Gross",num:true}, {label:"Plan",num:true},
            {label:"Residual",num:true}, "Decision"], history));
      };
      const rows = data.proposals.map((item) => el("tr", {},
        el("td", {}, item.purpose_name),
        el("td", {}, item.category_name),
        el("td", {}, `${item.source_name} → ${item.destination_name}`),
        el("td", {}, item.frequency),
        el("td", { class:"num" }, money(item.amount)),
        el("td", { class:"muted" }, evidenceView(item)),
        el("td", { class:"muted" }, `${Math.round(item.confidence * 100)}%`),
        el("td", {}, el("button", { class:"action", type:"button", onclick:async()=>{
          try {
            if (!target.value) {
              const schedules = await get("/api/scheduled?days=90");
              const draft = {...item.draft};
              close();
              openScheduledEditor(schedules, draft);
            } else {
              const plan = await get("/api/plan");
              state.plan = {
                from:plan.controls.from, through:plan.controls.through,
                period:plan.controls.period, scenario:target.value,
                compare:null, measure:plan.controls.measure,
              };
              state.scenarioEvent = {kind:"add", draft:{...item.draft}};
              close();
              switchTo("Plan");
            }
          } catch (error) { say(error.message, "error"); }
        }}, "Review…"))));
      body.replaceChildren(
        el("p", { class:"note" }, data.proposals.length
          ? "Proposals use completed activity after known schedules. Review and adjust one in the normal editor; it is not added until you choose Save."
          : "No categories have enough completed historical activity for a proposal."),
        table(["Purpose", "Account", "Direction", "Cadence", {label:"Estimate", num:true},
          "Evidence", "Confidence", ""], rows));
    } catch (error) { say(error.message, "error"); }
  };
  target.onchange = load;
  const controls = el("div", { class:"toolbar" },
    el("label", {}, "History months ", months),
    el("label", {}, "Add to ", target),
    el("button", { class:"action", type:"button", onclick:load }, "Analyze"),
    el("button", { class:"action", type:"button", onclick:close }, "Close"));
  backdrop.append(el("section", { class:"detail-dialog wide" },
    el("div", { class:"detail-heading" }, el("h2", {}, "Suggest estimates from history")),
    controls, body));
  document.body.append(backdrop);
  await load();
}

function openScheduledEditor(data, source) {
  const referenced = new Set(source?.account_handles || []);
  const accounts = data.accounts.filter((a) => !a.hidden || referenced.has(a.handle));
  const backdrop = el("div", { class: "detail-backdrop" });
  const form = el("form", { class: "entry" });
  const field = (label, control) => el("label", {}, label, control);
  const select = (name, items, value) => el("select", { name }, items.map(([key, label]) =>
    el("option", { value: key, selected: key === value ? "selected" : null }, label)));
  const today = new Date().toISOString().slice(0, 10);
  let refreshTimeline = () => {};
  const changed = () => refreshTimeline();
  const futureEditor = timelineEditor(
    "Future amounts", source?.amount_changes || [], true, changed);
  const seasonalEditor = monthAmountEditor(source?.seasonal_amounts || [], changed);
  const skipEditor = occurrenceTimelineEditor(
    "Skip occurrences", source?.skipped || [], false, changed);
  const oneTimeEditor = occurrenceTimelineEditor(
    "One-time amounts", source?.occurrence_adjustments || [], true, changed);
  const preview = schedulePreviewNode();
  const additionalSplits = planningSplitEditor(
    accounts, source?.additional_splits || [], changed);
  const splitAmountChanges = splitAmountTimelineEditor(
    accounts, source?.split_amount_changes || [], changed);
  const category = select("category", accounts.map((a) => [a.handle, a.name]),
    source?.category || accounts[0]?.handle);
  const funding = select("funding", accounts.map((a) => [a.handle, a.name]), source?.funding || accounts[0]?.handle);
  const growthPolicy = select("growth_policy", [
    ["auto", "Automatic from schedule contents"],
    ["none", "No growth - fixed nominal amount"],
    ["income", "Income growth"],
    ["inflation", "Expense inflation"],
  ], source?.growth_policy || "auto");
  const planningFlow = select("planning_flow", [
    ["", "Ordinary transfer"],
    ["retirement_saving", "Retirement saving"],
    ["benefit_funding", "Benefit / FSA funding"],
    ["debt_principal", "Debt principal"],
    ["retirement_income", "Retirement distribution"],
  ], source?.planning_flow || "");
  const categoryPlanningFlow = select("category_planning_flow", [
    ["", "Ordinary category activity"],
    ["retirement_saving", "Retirement saving"],
    ["benefit_funding", "Benefit / FSA funding"],
    ["debt_principal", "Debt principal"],
    ["retirement_income", "Retirement distribution"],
  ], source?.category_planning_flow || "");
  const investmentActivity = select(
    "investment_activity", INVESTMENT_ACTIVITY_OPTIONS, source?.investment_activity || "");
  const frequency = select("frequency", [["weekly","Weekly"],["biweekly","Fortnightly"],
    ["semimonthly","Twice a month"],["monthly","Monthly"],
    ["nth_weekday","Monthly — nth weekday"],["last_weekday","Monthly — last weekday"],["quarterly","Quarterly"],
    ["semiannual","Twice a year"],["annual","Yearly"],["once","Once"]], source?.frequency_key || "monthly");
  const weekend = select("weekend", [["none","Leave on the day"],["previous","Friday before"],["next","Monday after"]], source?.weekend || "none");
  form.append(
    field("Name", el("input", { name:"name", value:source?.name || "", required:"required" })),
    field("Status", select("enabled", [["true","Active"],["false","Inactive"]], source?.enabled === false ? "false" : "true")),
    field("Kind", select("kind", [["commitment","Commitment"],["estimate","Estimate"]], source?.placeholder ? "estimate" : "commitment")),
    field("Projection growth", growthPolicy),
    field("Category / investment account", category), field("Paid from / into", funding),
    field("Category planning purpose", categoryPlanningFlow),
    field("Planning purpose override", planningFlow),
    field("Investment activity", investmentActivity),
    field("Amount", el("input", { name:"amount", value:source?.amount || "", inputmode:"decimal", required:"required" })),
    field("Category memo", el("input", { name:"category_memo", value:source?.category_memo || "" })),
    field("Funding memo", el("input", { name:"funding_memo", value:source?.funding_memo || "" })),
    additionalSplits.node, splitAmountChanges.node,
    futureEditor.node, seasonalEditor.node, skipEditor.node, oneTimeEditor.node, preview.node,
    field("Frequency", frequency),
    field("First due", el("input", { name:"start", type:"date", value:source?.start || today, required:"required" })),
    field("End date", el("input", { name:"end", type:"date", value:source?.end || "" })),
    field("Occurrences", el("input", { name:"count", type:"number", min:"1", value:source?.count || "" })),
    field("Weekend", weekend),
    field("Posting", select("posting", [["manual","Manual"],["automatic","Automatic"]], source?.auto ? "automatic" : "manual")));
  form.append(el("div", { class:"toolbar" },
    el("button", { class:"action", type:"button", onclick:()=>backdrop.remove() }, "Cancel"),
    el("button", { class:"action primary", type:"submit" }, "Save")));
  const refreshOccurrenceOptions = async () => {
    const values = Object.fromEntries(new FormData(form).entries());
    if (!values.start) return;
    try {
      const payload = {
        frequency:values.frequency, start:values.start, end:values.end || null,
        count:values.count || null, weekend:values.weekend, amount:values.amount,
      };
      try {
        payload.amount_changes = futureEditor.values().map(
          (item)=>({start:item.when, amount:item.amount}));
        payload.skipped = skipEditor.values();
        payload.occurrence_adjustments = oneTimeEditor.values();
      } catch (_error) {}
      const result = await post("/api/scheduled/occurrences", payload);
      skipEditor.setOptions(result.occurrences);
      oneTimeEditor.setOptions(result.occurrences);
      preview.show(result.preview || []);
    } catch (_error) {
      skipEditor.setOptions([]);
      oneTimeEditor.setOptions([]);
    }
  };
  [frequency, form.elements.start, form.elements.end, form.elements.count, weekend]
    .forEach((control)=>control.addEventListener("change", refreshOccurrenceOptions));
  form.elements.start.addEventListener("input", refreshOccurrenceOptions);
  refreshTimeline = refreshOccurrenceOptions;
  form.elements.amount.addEventListener("change", refreshOccurrenceOptions);
  refreshOccurrenceOptions();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form).entries());
    try {
      await post("/api/scheduled/save", {
        handle: source?.handle || null, name: values.name,
        enabled: values.enabled === "true", placeholder: values.kind === "estimate", growth_policy: values.growth_policy,
        category: values.category, funding: values.funding,
        category_planning_flow: values.category_planning_flow || null,
        category_memo: values.category_memo, funding_memo: values.funding_memo,
        planning_flow: values.planning_flow || null, amount: values.amount,
        investment_activity: values.investment_activity || null,
        additional_splits: additionalSplits.values(),
        split_amount_changes: splitAmountChanges.values(),
        amount_changes: futureEditor.values().map((item)=>({start:item.when, amount:item.amount})),
        seasonal_amounts: seasonalEditor.values(),
        skipped: skipEditor.values(), occurrence_adjustments: oneTimeEditor.values(),
        frequency: values.frequency, start: values.start,
        end: values.end || null, count: values.count || null, weekend: values.weekend,
        auto: values.posting === "automatic" && values.kind !== "estimate",
        estimate_evidence: source?.estimate_evidence || null,
      });
      backdrop.remove(); say(source?.handle ? "Scheduled transaction updated." : "Scheduled transaction added."); render();
    } catch (error) { say(error.message, "error"); }
  });
  backdrop.append(el("section", { class:"detail-dialog" },
    el("div", { class:"detail-heading" }, el("h2", {}, source?.handle ? "Edit scheduled transaction" : "New scheduled transaction")),
    el("p", { class:"note" }, "Fixed-split schedules are editable when their complete structure has a shared lossless projection. Additional splits can represent payroll deductions, retirement/FSA funding, debt principal, and similar plan flows."), form));
  document.body.append(backdrop);
}

function openFormulaScheduleEditor(source) {
  const backdrop = el("div", { class:"detail-backdrop" });
  const form = el("form", { class:"entry" });
  const field = (label, control) => el("label", {}, label, control);
  const select = (name, items, value) => el("select", { name }, items.map(([key, label]) =>
    el("option", { value:key, selected:key === value ? "selected" : null }, label)));
  const frequency = select("frequency", [["weekly","Weekly"],["biweekly","Fortnightly"],
    ["semimonthly","Twice a month"],["monthly","Monthly"],
    ["nth_weekday","Monthly — nth weekday"],["last_weekday","Monthly — last weekday"],["quarterly","Quarterly"],
    ["semiannual","Twice a year"],["annual","Yearly"],["once","Once"]],
    source.frequency_key || "monthly");
  const weekend = select("weekend", [["none","Leave on the day"],
    ["previous","Friday before"],["next","Monday after"]], source.weekend || "none");
  const skipped = occurrenceTimelineEditor("Skip occurrences", source.skipped || [], false, ()=>{});
  const formulaInputs = (source.formula_splits || []).map((item) => ({
    item,
    input:el("input", {value:item.formula, required:"required", spellcheck:"false"}),
  }));
  const variableText = Object.entries(source.variables || {}).sort(([a],[b])=>a.localeCompare(b))
    .map(([name,value])=>`${name}=${value}`).join("; ");
  form.append(
    field("Name", el("input", {name:"name", value:source.name, required:"required"})),
    field("Status", select("enabled", [["true","Active"],["false","Inactive"]],
      source.enabled === false ? "false" : "true")),
    field("Kind", select("kind", [["commitment","Commitment"],["estimate","Estimate"]],
      source.placeholder ? "estimate" : "commitment")),
    field("Projection growth", select("growth_policy", [
      ["auto","Automatic from schedule contents"],["none","No growth - fixed nominal amount"],
      ["income","Income growth"],["inflation","Expense inflation"]], source.growth_policy || "auto")),
    ...formulaInputs.map(({item,input})=>field(`Formula — ${item.account_name}`, input)),
    field("Variables (name=value; …)", el("input", {name:"variables", value:variableText,
      placeholder:"principal=200000; rate=0.05", spellcheck:"false"})),
    skipped.node,
    field("Frequency", frequency),
    field("First due", el("input", {name:"start", type:"date", value:source.start,
      required:"required"})),
    field("End date", el("input", {name:"end", type:"date", value:source.end || ""})),
    field("Occurrences", el("input", {name:"count", type:"number", min:"1",
      value:source.count || ""})),
    field("Weekend", weekend),
    field("Posting", select("posting", [["manual","Manual"],["automatic","Automatic"]],
      source.auto ? "automatic" : "manual")));
  const refreshOccurrences = async () => {
    const values = Object.fromEntries(new FormData(form).entries());
    if (!values.start) return;
    try {
      const result = await post("/api/scheduled/occurrences", {
        frequency:values.frequency, start:values.start, end:values.end || null,
        count:values.count || null, weekend:values.weekend,
      });
      skipped.setOptions(result.occurrences);
    } catch (_error) { skipped.setOptions([]); }
  };
  [frequency, form.elements.start, form.elements.end, form.elements.count, weekend]
    .forEach((control)=>control.addEventListener("change", refreshOccurrences));
  form.elements.start.addEventListener("input", refreshOccurrences);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form).entries());
    const variables = {};
    try {
      for (const part of String(values.variables || "").split(";")) {
        if (!part.trim()) continue;
        const split = part.indexOf("=");
        if (split < 1) throw new Error("Formula variables must use name=value entries.");
        variables[part.slice(0, split).trim()] = part.slice(split + 1).trim();
      }
      await post("/api/scheduled/formula-save", {
        handle:source.handle, name:values.name, enabled:values.enabled === "true",
        placeholder:values.kind === "estimate", growth_policy:values.growth_policy,
        formulas:formulaInputs.map(({item,input})=>({index:item.index, formula:input.value})),
        variables, skipped:skipped.values(), frequency:values.frequency, start:values.start,
        end:values.end || null, count:values.count || null, weekend:values.weekend,
        auto:values.posting === "automatic" && values.kind !== "estimate",
      });
      backdrop.remove(); say("Formula schedule updated."); render();
    } catch (error) { say(error.message, "error"); }
  });
  form.append(el("div", {class:"toolbar"},
    el("button", {class:"action", type:"button", onclick:()=>backdrop.remove()}, "Cancel"),
    el("button", {class:"action primary", type:"submit"}, "Save")));
  backdrop.append(el("section", {class:"detail-dialog wide"},
    el("div", {class:"detail-heading"}, el("h2", {}, "Edit formula schedule")),
    el("p", {class:"note"}, "Formula expressions, variables, recurrence, and metadata are editable. Split accounts and formula-owned amount timelines remain protected."),
    form));
  document.body.append(backdrop);
  refreshOccurrences();
}

function expenseBars(items, periodIndex) {
  const max = Math.max(1, ...items.flatMap((item) => [
    Math.abs(Number(item.periods[periodIndex].planned)),
    Math.abs(Number(item.periods[periodIndex].actual)),
  ]));
  return el("div", { class: "expense-bars" }, items.map((item) => {
    const value = item.periods[periodIndex];
    const bars = svgEl("svg", { viewBox: "0 0 300 26", role: "img",
      "aria-label": `${item.full_name}: plan ${value.planned}, actual ${value.actual}` });
    bars.append(svgEl("rect", { x: 0, y: 1, height: 10,
      width: 300 * Math.abs(Number(value.planned)) / max, fill: "#2563a4" }));
    bars.append(svgEl("rect", { x: 0, y: 14, height: 10,
      width: 300 * Math.abs(Number(value.actual)) / max, fill: "#bd5824" }));
    return el("div", { class: "expense-bar-row" },
      el("span", { title: item.full_name }, item.full_name),
      bars,
      el("span", {}, `${value.planned} / ${value.actual}`));
  }));
}

function expenseTrend(category) {
  const points = category.periods;
  const values = points.flatMap((item) => [Number(item.planned), Number(item.actual)]);
  const low = Math.min(0, ...values);
  const high = Math.max(1, ...values);
  const svg = svgEl("svg", { viewBox: "0 0 600 140", role: "img",
    "aria-label": `Plan and actual expense trend for ${category.full_name}` });
  for (const [key, color] of [["planned", "#2563a4"], ["actual", "#bd5824"]]) {
    const path = points.map((item, index) => {
      const x = 20 + index * 560 / Math.max(1, points.length - 1);
      const y = 120 - 100 * (Number(item[key]) - low) / (high - low);
      return `${index ? "L" : "M"}${x},${y}`;
    }).join(" ");
    svg.append(svgEl("path", { d: path, fill: "none", stroke: color, "stroke-width": "3" }));
  }
  return svg;
}

async function expenseExplorerPanel(currentPlan) {
  const params = new URLSearchParams({
    from: currentPlan.from, through: currentPlan.through, period: currentPlan.period,
  });
  if (currentPlan.scenario) params.set("scenario", currentPlan.scenario);
  const data = await get(`/api/expense-explorer?${params}`);
  const choices = data.categories;
  const selected = choices.find((item) => item.account === state.expenseCategory) || choices[0];
  const index = Math.min(state.expenseIndex, Math.max(0, data.totals.length - 1));
  const panel = el("section", { class: "expense-explorer" },
    el("h2", {}, "Expense Explorer"),
    el("p", { class: "note" },
      "Blue: category plan · Orange: actual. Merchant groups use actuals only; the category plan is unallocated."));
  if (!selected) return panel;
  const periodSelect = el("select", { onchange: (event) => {
    state.expenseIndex = Number(event.target.value); render();
  } }, data.totals.map((item, i) => el("option", {
    value: String(i), selected: i === index ? "selected" : null,
  }, item.label)));
  const categorySelect = el("select", { onchange: (event) => {
    state.expenseCategory = event.target.value; render();
  } }, choices.map((item) => el("option", {
    value: item.account, selected: item.account === selected.account ? "selected" : null,
  }, item.full_name)));
  const sortSelect = el("select", { onchange: (event) => {
    state.expenseSort = event.target.value; render();
  } }, [["actual", "Actual"], ["planned", "Plan"], ["variance", "Variance"],
    ["name", "Category"]].map(([value, label]) => el("option", {
    value, selected: value === state.expenseSort ? "selected" : null,
  }, label)));
  panel.append(el("div", { class: "toolbar" },
    el("label", {}, "Period ", periodSelect),
    el("label", {}, "Category trend ", categorySelect),
    el("label", {}, "Sort by ", sortSelect)));
  const ordered = [...choices].sort((a, b) => state.expenseSort === "name"
    ? a.full_name.localeCompare(b.full_name)
    : Number(b.periods[index][state.expenseSort] || 0)
      - Number(a.periods[index][state.expenseSort] || 0));
  panel.append(expenseBars(ordered, index),
    table(["Category", {label:"Plan",num:true}, {label:"Actual",num:true},
      {label:"Variance",num:true}], ordered.map((item) => el("tr", {},
      el("td", {}, item.full_name),
      ...["planned", "actual", "variance"].map((key) => el("td", {class:"num"},
        item.periods[index][key] == null ? "—" : String(item.periods[index][key])))))));
  panel.append(el("h3", {}, `${selected.full_name} trend`), expenseTrend(selected),
    table(["Period", {label:"Plan",num:true}, {label:"Actual",num:true},
      {label:"Variance",num:true}], selected.periods.map((item) => el("tr", {},
      el("td", {}, item.label), ...["planned", "actual", "variance"].map((key) =>
        el("td", {class:"num"}, item[key] == null ? "—" : String(item[key])))))));
  const detailParams = new URLSearchParams(params);
  detailParams.set("account", selected.account);
  detailParams.set("index", String(index));
  const detail = (await get(`/api/expense-explorer?${detailParams}`)).drilldown;
  panel.append(el("h3", {}, `${selected.full_name} merchants — ${detail.period.label}`),
    table(["Merchant", {label:"Actual",num:true}, "Transactions"],
      detail.merchants.map((group) => el("tr", {},
        el("td", {}, group.name), el("td", {class:"num"}, String(group.amount)),
        el("td", {}, group.transactions.map((item) => el("div", {},
          `${item.date} · ${item.description || "Unknown merchant"} · ${item.amount}`)))))),
    el("p", {class:"note"}, `Category plan ${detail.period.planned}; actual ${detail.period.actual}; `
      + `variance ${detail.period.variance == null ? "—" : detail.period.variance}. `
      + "No merchant budgets are assigned."));
  return panel;
}

async function showPlan() {
  const applied = state.plan || {};
  const params = new URLSearchParams();
  if (applied.from) params.set("from", applied.from);
  if (applied.through) params.set("through", applied.through);
  if (applied.period) params.set("period", applied.period);
  if (applied.measure) params.set("measure", applied.measure);
  if (applied.scenario) params.set("scenario", applied.scenario);
  if (applied.compare) params.set("compare", applied.compare);
  const data = await get(`/api/plan${params.size ? `?${params}` : ""}`);
  if (!state.plan) {
    state.plan = {
      from: data.controls.from, through: data.controls.through,
      period: data.controls.period, scenario: data.controls.scenario,
      compare: data.controls.compare,
      measure: data.controls.measure,
    };
  }
  const currentPlan = state.plan;

  const scenario = el("select", { name: "scenario" },
    data.controls.scenarios.map((item) => el("option", {
      value: item.handle || "",
      selected: (item.handle || "") === (currentPlan.scenario || "") ? "selected" : null,
    }, item.name)));
  const compareChoices = [
    ["", "No comparison"],
    ...data.controls.scenarios
      .filter((item) => (item.handle || "") !== (currentPlan.scenario || ""))
      .map((item) => [item.handle || "__base__", item.name]),
  ];
  const compare = el("select", { name: "compare" },
    compareChoices.map(([value, label]) => el("option", {
      value, selected: value === (currentPlan.compare || "") ? "selected" : null,
    }, label)));

  const from = el("input", {
    name: "from", type: "month", value: currentPlan.from,
    min: data.controls.minimum, max: data.controls.maximum,
  });
  const through = el("input", {
    name: "through", type: "month", value: currentPlan.through,
    min: data.controls.minimum, max: data.controls.maximum,
  });
  const period = el("select", { name: "period" },
    [["month", "Month"], ["quarter", "Quarter"], ["year", "Year"]].map(([value, label]) =>
      el("option", { value, selected: value === currentPlan.period ? "selected" : null }, label)));
  const measure = el("select", { name: "measure" },
    [["planned", "Plan"], ["actual", "Actual"], ["variance", "Variance"]].map(([value, label]) =>
      el("option", { value, selected: value === currentPlan.measure ? "selected" : null }, label)));

  const controls = el("form", { class: "toolbar", onsubmit: async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.target).entries());
    if (values.through < values.from) {
      say("Through must be the same month as From or later.", "error");
      return;
    }
    const nextPlan = {
      from: values.from, through: values.through, period: values.period,
      scenario: values.scenario || null, compare: values.compare || null,
      measure: values.measure,
    };
    try {
      await post("/api/plan/settings", nextPlan);
      state.plan = nextPlan;
      render();
    } catch (error) {
      say(error.message, "error");
    }
  } },
    el("label", {}, "Scenario ", scenario),
    el("label", {}, "Compare with ", compare),
    el("label", {}, "From ", from),
    el("label", {}, "Through ", through),
    el("label", {}, "Group by ", period),
    el("label", {}, "Show ", measure),
    el("button", { class: "action primary", type: "submit" }, "Apply"),
    el("button", {
      class: "action", type: "button",
      onclick: () => { state.scenarioManager = currentPlan.scenario || ""; switchTo("Scenarios"); },
    }, "Manage scenarios…"),
    el("button", {
      class: "action", type: "button",
      disabled: data.summary.unresolved_actuals ? null : "disabled",
      title: "Inspect unmatched actual transactions",
      onclick: () => switchTo("Review"),
    }, "Resolve actuals…"));

  const summary = data.summary;
  const comparison = data.comparison;
  const summaryDelta = comparison ? comparison.summary : null;
  const cards = el("div", { class: "cards" },
    [["Opening spendable cash", summary.opening_cash, null, false],
     ["Ending spendable cash", summary.ending_cash, null, false],
     [`Lowest spendable cash (${summary.minimum_cash_date})`, summary.minimum_cash, null, false],
     ["Projected change in spendable cash", summary.planned_cash,
       summaryDelta?.planned_cash_delta, true],
     ["Actual change through as-of date", summary.actual_cash,
       summaryDelta?.actual_cash_delta, true],
     ["Variance through as-of date", summary.variance,
       summaryDelta?.variance_delta, true]].map(([label, value, delta, signed]) =>
      el("div", { class: "card" },
        el("div", { class: "label" }, label),
        el("div", { class: `value ${Number(value) < 0 ? "neg" : ""}` },
          value == null ? "Not applicable" : signed ? signedMoney(value) : money(value)),
        comparison && delta != null ? el("span", {
          class: `plan-delta ${Number(delta) < 0 ? "neg" : ""}`,
        }, `Δ vs ${comparison.name}: ${money(delta)}`) : null)),
    el("div", { class: "card" },
      el("div", { class: "label" }, "Expected occurrences pending"),
      el("div", { class: "value" }, summary.unresolved_expected)),
    el("div", { class: "card" },
      el("div", { class: "label" }, "Actuals to review"),
      el("div", { class: "value" }, summary.unresolved_actuals)));
  const selectedScenarioName = data.controls.scenarios
    .find((item) => (item.handle || null) === (data.controls.scenario || null))?.name
    || "Base scenario";
  const inheritedAssumptions = Object.values(data.controls.assumption_sources || {})
    .filter((source) => source !== selectedScenarioName).length;
  const comparedInheritedAssumptions = Object.values(comparison?.assumption_sources || {})
    .filter((source) => source !== comparison?.name).length;

  const headers = ["Category",
    ...data.periods.map((item) => ({ label: item.label, num: true })),
    { label: "Total", num: true }];
  const summaryRows = [];
  const detailRows = [];
  const totalRow = (label, totals, extraClass="", signed=false) => el("tr", {
    class: `total-row ${extraClass}`,
  },
    el("td", {}, label),
    ...totals.periods.map((value) => el("td", {
      class: value == null ? "num muted" : cls(value),
    }, value == null ? "—" : signed ? signedMoney(value) : money(value))),
    el("td", { class: totals.total == null ? "num muted" : cls(totals.total) },
      totals.total == null ? "—" : signed ? signedMoney(totals.total) : money(totals.total)));

  summaryRows.push(el("tr", { class: "section-row" },
    el("td", { colspan: String(headers.length) }, "Spendable cash bridge")));
  for (const bridge of data.cash_bridge) {
    const compared = comparison?.cash_bridge?.find((row) => row.kind === bridge.kind);
    const deltas = compared ? compared[`${currentPlan.measure}_delta`] : null;
    summaryRows.push(el("tr", {},
      el("td", {}, bridge.name),
      ...bridge[currentPlan.measure].map((value, index) => el("td", {
        class: value == null ? "num muted" : cls(value),
      }, value == null ? "—" : signedMoney(value),
      deltas && deltas[index] != null ? el("span", {
        class: `plan-delta ${Number(deltas[index]) < 0 ? "neg" : ""}`,
      }, `Δ ${signedMoney(deltas[index])}`) : null)),
      el("td", {
        class: bridge.totals[currentPlan.measure] == null
          ? "num muted" : cls(bridge.totals[currentPlan.measure]),
      }, bridge.totals[currentPlan.measure] == null
        ? "—" : signedMoney(bridge.totals[currentPlan.measure]))));
  }
  summaryRows.push(totalRow("Net change in spendable cash",
    data.column_totals.cash_bridge[currentPlan.measure], "grand-total", true));
  for (const [kind, heading] of [["income", "Income"], ["expense", "Expenses"]]) {
    detailRows.push(el("tr", { class: "section-row" },
      el("td", { colspan: String(headers.length) }, heading)));
    for (const category of data.categories.filter((row) => row.class === kind)) {
      const compared = comparison?.categories.find((row) => row.account === category.account);
      const deltas = compared ? compared[`${currentPlan.measure}_delta`] : null;
      detailRows.push(el("tr", {},
        el("td", {
          class: depthClass("indent", category.depth),
          title: category.full_name,
        }, category.name),
        category[currentPlan.measure].map((value, index) =>
          el("td", { class: `${value == null ? "num muted" : cls(value)} plan-cell` },
            el("button", {
              class: "plan-cell-button",
              type: "button",
              title: `Explain ${category.full_name} — ${data.periods[index].label}`,
              "aria-label": `Explain ${category.full_name} — ${data.periods[index].label}: ${value == null ? "not applicable" : money(value)}`,
              onclick: () => openPlanDetail(
                category, data.periods[index], currentPlan.scenario, data.periods[index].label),
            }, value == null ? "—" : money(value),
              deltas && deltas[index] != null ? el("span", {
                class: `plan-delta ${Number(deltas[index]) < 0 ? "neg" : ""}`,
                title: `Active scenario minus ${comparison.name}`,
              }, `Δ ${money(deltas[index])}`) : null))),
        el("td", {
          class: category.totals[currentPlan.measure] == null
            ? "num muted" : cls(category.totals[currentPlan.measure]),
        }, category.totals[currentPlan.measure] == null
          ? "—" : money(category.totals[currentPlan.measure]))));
    }
    detailRows.push(totalRow(`${heading} total`, data.column_totals[kind][currentPlan.measure]));
  }
  detailRows.push(totalRow("Income less expenses",
    data.column_totals.operating_net[currentPlan.measure], "grand-total", true));

  if (data.mortgage_payments?.length) {
    detailRows.push(el("tr", { class: "section-row" },
      el("td", { colspan: String(headers.length) }, "Cash requirements (informational)")));
    for (const payment of data.mortgage_payments) {
      const compared = comparison?.mortgage_payments?.find(
        (row) => row.account === payment.account);
      const deltas = compared ? compared[`${currentPlan.measure}_delta`] : null;
      const cells = payment[currentPlan.measure].map((value, index) =>
        el("td", { class: `${value == null ? "num muted" : cls(value)} plan-cell` },
          el("button", {
            class: "plan-cell-button", type: "button",
            title: `Explain ${payment.name} — ${data.periods[index].label}`,
            "aria-label": `Explain ${payment.name} — ${data.periods[index].label}: ${value == null ? "not applicable" : money(value)}`,
            onclick: () => openPlanDetail(
              payment, data.periods[index], currentPlan.scenario,
              data.periods[index].label, null, "mortgage"),
          }, value == null ? "—" : money(value),
            deltas && deltas[index] != null ? el("span", {
              class: `plan-delta ${Number(deltas[index]) < 0 ? "neg" : ""}`,
              title: `Active scenario minus ${comparison.name}`,
            }, `Δ ${money(deltas[index])}`) : null)));
      detailRows.push(el("tr", {},
        el("td", {
          title: "Whole mortgage payment; classified components below are non-additive.",
        }, payment.name),
        ...cells,
        el("td", {
          class: payment.totals[currentPlan.measure] == null
            ? "num muted" : cls(payment.totals[currentPlan.measure]),
        }, payment.totals[currentPlan.measure] == null
          ? "—" : money(payment.totals[currentPlan.measure]))));
    }
    detailRows.push(totalRow("Mortgage cash required",
      data.column_totals.mortgage_payments[currentPlan.measure]));
  }

  if (data.planning_flows?.length) {
    detailRows.push(el("tr", { class: "section-row" },
      el("td", { colspan: String(headers.length) },
        "Balance-sheet classifications (informational)")));
    for (const flow of data.planning_flows) {
      const compared = comparison?.planning_flows?.find(
        (row) => row.kind === flow.kind && row.account === flow.account);
      const deltas = compared ? compared[`${currentPlan.measure}_delta`] : null;
      const cells = flow[currentPlan.measure].map((value, index) =>
        el("td", { class: `${value == null ? "num muted" : cls(value)} plan-cell` },
          el("button", {
            class: "plan-cell-button", type: "button",
            title: `Explain ${flow.name} — ${data.periods[index].label}`,
            "aria-label": `Explain ${flow.name} — ${data.periods[index].label}: ${value == null ? "not applicable" : money(value)}`,
            onclick: () => openPlanDetail(
              flow, data.periods[index], currentPlan.scenario,
              data.periods[index].label, flow.kind),
          }, value == null ? "—" : money(value),
            deltas && deltas[index] != null ? el("span", {
              class: `plan-delta ${Number(deltas[index]) < 0 ? "neg" : ""}`,
              title: `Active scenario minus ${comparison.name}`,
            }, `Δ ${money(deltas[index])}`) : null)));
      detailRows.push(el("tr", {},
        el("td", { title: flow.full_name }, flow.name),
        ...cells,
        el("td", {
          class: flow.totals[currentPlan.measure] == null
            ? "num muted" : cls(flow.totals[currentPlan.measure]),
        }, flow.totals[currentPlan.measure] == null
          ? "—" : money(flow.totals[currentPlan.measure]))));
    }
  }

  const summaryHeaders = [{ ...headers[0], label: "Cash source / use" }, ...headers.slice(1)];
  const summaryTable = table(summaryHeaders, summaryRows);
  summaryTable.classList.add("plan-table", "plan-summary");
  const detailTable = table(headers, detailRows);
  detailTable.classList.add("plan-table");
  let eventPanel = null;
  if (currentPlan.scenario) {
    eventPanel = await scenarioEventPanel(currentPlan.scenario, currentPlan.from);
  }
  const detailSection = el("section", { class: "plan-detail" },
    el("h2", { class: "plan-detail-heading" }, "Budget and classifications"),
    eventPanel,
    detailTable);
  const detailOption = el("label", { class: "plan-print-option" },
    el("input", {
      type: "checkbox",
      onchange: (event) => {
        state.planPrintDetail = event.target.checked;
        document.body.classList.toggle("include-plan-detail", event.target.checked);
      },
    }), " Include category detail when printing");
  detailOption.querySelector("input").checked = state.planPrintDetail;
  document.body.classList.toggle("include-plan-detail", state.planPrintDetail);

  return el("div", {}, controls, cards,
    el("p", { class: "note plan-method-note" },
      `Showing ${data.controls.from} through ${data.controls.through}. `
      + "Changes to the controls take effect only when Apply is pressed."),
    el("p", { class: "note plan-method-note" },
      "Income and expense values are derived from exact-dated scheduled/estimated "
      + "and actual transaction splits; reporting periods do not store planning values. "
      + "Mortgage cash requirements are informational and are not added to their "
      + "classified components. Section totals count outermost category rollups once. "
      + "The signed spendable-cash bridge counts each cash dollar once. Income and "
      + "expense details remain positive budget magnitudes; Income less expenses exposes "
      + "their signed operating result. Balance-sheet classifications are informational "
      + "and have no mixed grand total. Variance totals include only applicable periods."),
    inheritedAssumptions ? el("p", { class: "note plan-method-note" },
      `${inheritedAssumptions} annual assumption(s) inherited through the parent chain.`) : null,
    comparedInheritedAssumptions ? el("p", { class: "note plan-method-note" },
      `${comparison.name} inherits ${comparedInheritedAssumptions} annual assumption(s) through its parent chain.`)
      : null,
    detailOption,
    el("h2", { class: "plan-summary-heading" }, "Cash outlook"),
    summaryTable, await expenseExplorerPanel(currentPlan), detailSection);
}

async function openPlanDetail(
  category, period, scenarioHandle, periodLabel, flowKind=null, requirementKind=null) {
  const params = new URLSearchParams({
    account: category.account, start: period.start, end: period.end,
  });
  if (scenarioHandle) params.set("scenario", scenarioHandle);
  if (flowKind) params.set("flow_kind", flowKind);
  if (requirementKind) params.set("requirement_kind", requirementKind);
  try {
    const data = await get(`/api/plan/detail?${params}`);
    const sourceName = (value) => ({
      scheduled: "Scheduled", one_off: "One-time estimate",
      scenario_schedule: "Scenario estimate",
    }[value] || value);
    const plannedRows = data.planned.map((item) => el("tr", {},
      el("td", {}, item.date),
      el("td", {}, item.description,
        ...(item.explanation || []).map((line) => el("div", { class: "note" }, line))),
      el("td", { class: "muted" }, sourceName(item.source)),
      el("td", { class: "muted" }, item.status),
      el("td", { class: cls(item.expected) }, money(item.expected)),
      el("td", { class: item.actual == null ? "num muted" : cls(item.actual) },
        item.actual == null ? "—" : money(item.actual)),
      el("td", { class: item.variance == null ? "num muted" : cls(item.variance) },
        item.variance == null ? "—" : money(item.variance))));
    const actualRows = data.actuals.map((item) => el("tr", {},
      el("td", {}, item.date),
      el("td", {}, item.description,
        ...(item.explanation || []).map((line) => el("div", { class: "note" }, line))),
      el("td", { class: "muted" }, item.resolution),
      el("td", { class: cls(item.amount) }, money(item.amount)),
      el("td", { class: item.expected == null ? "num muted" : cls(item.expected) },
        item.expected == null ? "—" : money(item.expected)),
      el("td", { class: item.variance == null ? "num muted" : cls(item.variance) },
        item.variance == null ? "—" : money(item.variance)),
      el("td", { class: "num muted" },
        item.date_variance_days == null ? "—" : String(item.date_variance_days))));
    const backdrop = el("div", {
      class: "detail-backdrop",
      onclick: (event) => { if (event.target === backdrop) backdrop.remove(); },
    });
    const summary = data.summary;
    backdrop.append(el("section", { class: "detail-dialog" },
      el("div", { class: "detail-heading" },
        el("div", {},
          el("h2", {}, `${data.category.full_name} — ${periodLabel}`),
          el("div", { class: "note" },
            `${data.scenario.name}; ${data.period.start} through ${data.period.end}`)),
        el("button", { class: "action", type: "button", onclick: () => backdrop.remove() },
          "Close")),
      el("div", { class: "cards detail-summary" },
        [["Plan", summary.planned], ["Actual", summary.actual], ["Variance", summary.variance]]
          .map(([label, value]) => el("div", { class: "card" },
            el("div", { class: "label" }, label),
            el("div", { class: `value ${value != null && Number(value) < 0 ? "neg" : ""}` },
              value == null ? "—" : money(value))))),
      el("h3", {}, "Planned occurrences"),
      plannedRows.length
        ? table(["Planned", "Description", "Source", "Status",
            {label:"Expected",num:true}, {label:"Matched actual",num:true},
            {label:"Variance",num:true}], plannedRows)
        : el("p", { class: "note" }, "No planned occurrences contribute to this cell."),
      el("h3", {}, "Actual transactions"),
      actualRows.length
        ? table(["Posted", "Description", "Resolution", {label:"Actual",num:true},
            {label:"Expected",num:true}, {label:"Variance",num:true},
            {label:"Date Δ days",num:true}], actualRows)
        : el("p", { class: "note" }, "No actual transactions contribute to this cell.")));
    document.body.append(backdrop);
  } catch (error) { say(error.message, "error"); }
}

async function scenarioEventPanel(handle, fromMonth) {
  const data = await get(`/api/scenario/events?handle=${encodeURIComponent(handle)}`);
  const mode = state.scenarioEvent;
  const baselineOptions = data.baseline.map((item) => el("option", { value:item.handle }, item.name));
  const actions = el("div", { class:"toolbar compact" },
    el("button", { class:"action", type:"button", onclick:()=>{ state.scenarioEvent={kind:"add"}; render(); } }, "Add estimate…"),
    el("button", { class:"action", type:"button", onclick:()=>{ state.scenarioEvent={kind:"alter"}; render(); } }, "Alter baseline…"),
    el("button", { class:"action", type:"button", onclick:()=>{ state.scenarioEvent={kind:"suppress"}; render(); } }, "Suppress baseline…"));
  let editor = null;
  if (mode && mode.kind === "suppress") {
    const select = el("select", { name:"source" }, baselineOptions);
    editor = el("form", { class:"toolbar", onsubmit:async(e)=>{ e.preventDefault(); const v=Object.fromEntries(new FormData(e.target)); await post("/api/scenario/event/suppress", {handle, source_schedule:v.source}); state.scenarioEvent=null; say("Baseline schedule suppressed in this scenario."); render(); } },
      el("label", {}, "Baseline schedule ", select), el("button", {class:"action primary"}, "Suppress"), el("button", {class:"action", type:"button", onclick:()=>{state.scenarioEvent=null;render();}}, "Cancel"));
  } else if (mode && (mode.kind === "add" || mode.kind === "alter")) {
    const simpleBase = data.baseline.filter(x=>x.simple);
    let source = mode.kind === "alter" ? simpleBase.find(x=>x.handle===mode.source) || simpleBase[0] : null;
    const categories = data.accounts;
    const sourceSelect = mode.kind === "alter" ? el("select", {name:"source", onchange:e=>{state.scenarioEvent={kind:"alter",source:e.target.value};render();}}, simpleBase.map(x=>el("option",{value:x.handle,selected:x.handle===source?.handle?"selected":null},x.name))) : null;
    const existing = mode.kind === "alter" ? data.changes.find((x)=>x.source_schedule===source?.handle && x.enabled) : null;
    const editSource = mode.draft || existing || source;
    const category = editSource?.category || categories[0]?.handle || "";
    const funding = editSource?.funding || data.accounts.find(a=>a.handle!==category)?.handle || "";
    const planningFlow = editSource?.planning_flow || "";
    const investmentActivity = editSource?.investment_activity || "";
    const growthPolicy = editSource?.growth_policy || "auto";
    let refreshTimeline = () => {};
    const changed = () => refreshTimeline();
    const futureEditor = timelineEditor(
      "Future amounts", editSource?.amount_changes || [], true, changed);
    const seasonalEditor = monthAmountEditor(editSource?.seasonal_amounts || [], changed);
    const skipEditor = occurrenceTimelineEditor(
      "Skip occurrences", editSource?.skipped || [], false, changed);
    const oneTimeEditor = occurrenceTimelineEditor(
      "One-time amounts", editSource?.occurrence_adjustments || [], true, changed);
    const preview = schedulePreviewNode();
    const additionalSplits = planningSplitEditor(
      data.accounts, editSource?.additional_splits || [], changed);
    const form = el("form", { class:"scenario-fields", onsubmit:async(e)=>{e.preventDefault(); const v=Object.fromEntries(new FormData(e.target)); const amount_changes=futureEditor.values().map((item)=>({start:item.when, amount:item.amount})); const skipped=skipEditor.values(); const occurrence_adjustments=oneTimeEditor.values(); await post("/api/scenario/event/save", {handle, source_schedule:mode.kind==="alter"?v.source:null, name:v.name, growth_policy:v.growth_policy, category:v.category, funding:v.funding, category_planning_flow:v.category_planning_flow || null, planning_flow:v.planning_flow || null, investment_activity:v.investment_activity || null, amount:v.amount, additional_splits:additionalSplits.values(), amount_changes, seasonal_amounts:seasonalEditor.values(), skipped, occurrence_adjustments, frequency:v.frequency, start:v.start, end:v.end, count:v.count, weekend:v.weekend, estimate_evidence:editSource?.estimate_evidence || null}); state.scenarioEvent=null; say("Scenario event saved."); render(); } },
      sourceSelect ? el("label", {}, "Baseline schedule", sourceSelect) : null,
      el("label", {}, "Name", el("input", {name:"name", required:"required", value:editSource?.name || ""})),
      el("label", {}, "Projection growth", el("select", {name:"growth_policy"},
        [["auto","Automatic from schedule contents"],["none","No growth - fixed nominal amount"],
         ["income","Income growth"],["inflation","Expense inflation"]].map(([value,label])=>
          el("option",{value,selected:value===growthPolicy?"selected":null},label)))),
      el("label", {}, "Category / investment account", el("select", {name:"category"}, categories.map(a=>el("option",{value:a.handle,selected:a.handle===category?"selected":null},a.name)))),
      el("label", {}, "Paid from / into", el("select", {name:"funding"}, data.accounts.map(a=>el("option",{value:a.handle,selected:a.handle===funding?"selected":null},a.name)))),
      el("label", {}, "Category planning purpose", el("select", {name:"category_planning_flow"},
        [["","Ordinary category activity"],["retirement_saving","Retirement saving"],
         ["benefit_funding","Benefit / FSA funding"],["debt_principal","Debt principal"],
         ["retirement_income","Retirement distribution"]].map(([value,label])=>
          el("option",{value,selected:value===(editSource?.category_planning_flow || "")?"selected":null},label)))),
      el("label", {}, "Planning purpose override", el("select", {name:"planning_flow"},
        [["","Ordinary transfer"],["retirement_saving","Retirement saving"],
         ["benefit_funding","Benefit / FSA funding"],["debt_principal","Debt principal"],
         ["retirement_income","Retirement distribution"]].map(([value,label])=>
          el("option",{value,selected:value===planningFlow?"selected":null},label)))),
      el("label", {}, "Investment activity", el("select", {name:"investment_activity"},
        INVESTMENT_ACTIVITY_OPTIONS.map(([value,label])=>
          el("option",{value,selected:value===investmentActivity?"selected":null},label)))),
      el("label", {}, "Amount", el("input", {name:"amount", required:"required", inputmode:"decimal", value:editSource?.amount || ""})),
      additionalSplits.node,
      futureEditor.node, seasonalEditor.node, skipEditor.node, oneTimeEditor.node, preview.node,
      el("label", {}, "Frequency", el("select", {name:"frequency"}, [["weekly","Weekly"],["biweekly","Fortnightly"],["semimonthly","Twice a month"],["monthly","Monthly"],["nth_weekday","Monthly — nth weekday"],["last_weekday","Monthly — last weekday"],["quarterly","Quarterly"],["semiannual","Twice a year"],["annual","Yearly"],["once","Once"]].map(([v,l])=>el("option",{value:v,selected:v===(editSource?.frequency||"monthly")?"selected":null},l)))),
      el("label", {}, "First occurrence", el("input", {name:"start", type:"date", required:"required", value:editSource?.start || `${fromMonth}-01`})),
      el("label", {}, "End date (optional)", el("input", {name:"end", type:"date", value:editSource?.end || ""})),
      el("label", {}, "Occurrence count (optional)", el("input", {name:"count", type:"number", min:"1", step:"1", value:editSource?.count || ""})),
      el("p", {class:"note"}, "Use either an end date or an occurrence count, not both."),
      el("label", {}, "Weekend", el("select", {name:"weekend"}, [["none","Leave on the day"],["previous","Move to Friday before"],["next","Move to Monday after"]].map(([v,l])=>el("option",{value:v,selected:v===(editSource?.weekend||"none")?"selected":null},l)))),
      el("div", {class:"toolbar"}, el("button",{class:"action primary"},"Save scenario change"), el("button",{class:"action",type:"button",onclick:()=>{state.scenarioEvent=null;render();}},"Cancel")));
    const refreshOccurrenceOptions = async () => {
      const values = Object.fromEntries(new FormData(form).entries());
      if (!values.start) return;
      try {
        const payload = {
          frequency:values.frequency, start:values.start, end:values.end || null,
          count:values.count || null, weekend:values.weekend, amount:values.amount,
        };
        try {
          payload.amount_changes = futureEditor.values().map(
            (item)=>({start:item.when, amount:item.amount}));
          payload.skipped = skipEditor.values();
          payload.occurrence_adjustments = oneTimeEditor.values();
        } catch (_error) {}
        const result = await post("/api/scheduled/occurrences", payload);
        skipEditor.setOptions(result.occurrences);
        oneTimeEditor.setOptions(result.occurrences);
        preview.show(result.preview || []);
      } catch (_error) {
        skipEditor.setOptions([]);
        oneTimeEditor.setOptions([]);
      }
    };
    [form.elements.frequency, form.elements.start, form.elements.end,
      form.elements.count, form.elements.weekend]
      .forEach((control)=>control.addEventListener("change", refreshOccurrenceOptions));
    form.elements.start.addEventListener("input", refreshOccurrenceOptions);
    refreshTimeline = refreshOccurrenceOptions;
    form.elements.amount.addEventListener("change", refreshOccurrenceOptions);
    refreshOccurrenceOptions();
    editor = form;
  }
  const changes = data.changes.length ? table(["Change", "Baseline", "State"],
    data.changes.map((change) => el("tr", {},
      el("td", {}, change.name),
      el("td", {}, change.source_name || "Scenario-only estimate"),
      el("td", {}, change.enabled ? "Active" : "Suppressed"))))
    : el("p", { class:"note" }, "No scenario-specific recurring changes yet.");
  return el("div", {class:"panel scenario-events"}, el("h3",{},`Scenario events — ${data.scenario.name}`), actions, editor, changes);
}

const ASSUMPTION_FIELDS = [
  ["Income growth", "income_growth"],
  ["Expense inflation", "expense_inflation"],
  ["Investment return", "investment_return"],
  ["Cash interest", "cash_interest"],
  ["Liability interest", "liability_interest"],
];

function rateToPercent(value) {
  return value === null || value === undefined || value === "" ? "" : String(Number(value) * 100);
}

function percentToRate(value) {
  return String(value).trim() === "" ? "" : String(Number(value) / 100);
}

function assumptionInputs(assumptions, prefix = "") {
  return el("div", { class: "assumption-grid" }, ASSUMPTION_FIELDS.map(([label, field]) =>
    el("label", {}, label,
      el("span", { class: "rate-input" },
        el("input", {
          type: "number", min: "-100", max: "100", step: "0.01",
          name: `${prefix}${field}`, value: rateToPercent(assumptions[field]),
        }),
        el("span", { class: "muted" }, "%")))));
}

function scenarioAssumptionInputs(scenario) {
  const overrides = new Set(scenario.assumption_overrides || []);
  return el("div", { class: "assumption-grid" }, ASSUMPTION_FIELDS.map(([label, field]) =>
    el("label", {}, label,
      el("span", { class: "rate-input" },
        el("input", {
          type: "number", min: "-100", max: "100", step: "0.01",
          name: field, value: rateToPercent(scenario.assumptions[field]),
        }),
        el("span", { class: "muted" }, "%")),
      scenario.base ? null : el("span", { class: "note" },
        el("input", {
          type: "checkbox", name: `override_${field}`,
          checked: overrides.has(field) ? "checked" : null,
        }),
        overrides.has(field)
          ? ` Scenario override`
          : ` Inherited from ${scenario.assumption_sources?.[field] || "parent"}`))));
}

function readAssumptionOverrides(form) {
  const values = new FormData(form);
  return ASSUMPTION_FIELDS.filter(([, field]) => values.has(`override_${field}`))
    .map(([, field]) => field);
}

function readAssumptions(form, prefix = "") {
  const values = new FormData(form);
  return Object.fromEntries(ASSUMPTION_FIELDS.map(([, field]) =>
    [field, percentToRate(values.get(`${prefix}${field}`))]));
}

function accountAssumptionInputs(accounts, assumptions) {
  const overrides = assumptions.per_account || {};
  if (!accounts.length) return el("p", {class:"note"},
    "No investment or liability accounts are available for account-specific rates.");
  return el("div", {class:"assumption-grid"}, accounts.map((account) => {
    const inherited = account.class === "liability" ? "liability default" : "investment default";
    return el("label", {}, account.name,
      el("span", {class:"rate-input"},
        el("input", {
          type:"number", min:"-100", max:"100", step:"0.01",
          name:`account_rate_${account.handle}`,
          value: rateToPercent(overrides[account.handle] ?? ""),
          placeholder: inherited,
        }),
        el("span", {class:"muted"}, "%")));
  }));
}

function readAccountAssumptions(form, accounts) {
  const values = new FormData(form);
  return Object.fromEntries(accounts.map((account) => [
    account.handle, percentToRate(values.get(`account_rate_${account.handle}`)),
  ]).filter(([, value]) => value !== ""));
}

async function showScenarios() {
  const data = await get("/api/scenarios");
  const requested = state.scenarioManager || "";
  let selected = data.scenarios.find((item) => (item.handle || "") === requested);
  if (!selected) {
    selected = data.scenarios[0];
    state.scenarioManager = selected.handle || "";
  }

  const picker = el("select", {
    onchange: (event) => {
      state.scenarioManager = event.target.value;
      state.scenarioPeriod = null;
      render();
    },
  }, data.scenarios.map((scenario) => el("option", {
    value: scenario.handle || "",
    selected: (scenario.handle || "") === (selected.handle || "") ? "selected" : null,
  }, scenario.name)));

  const base = selected.base;
  const scenariosByHandle = new Map(data.scenarios
    .filter((scenario) => scenario.handle)
    .map((scenario) => [scenario.handle, scenario]));
  const wouldCreateParentCycle = (candidate) => {
    const seen = new Set();
    let ancestor = candidate;
    while (ancestor?.parent_handle && !seen.has(ancestor.handle)) {
      if (ancestor.parent_handle === selected.handle) return true;
      seen.add(ancestor.handle);
      ancestor = scenariosByHandle.get(ancestor.parent_handle);
    }
    return false;
  };
  const form = el("form", { class: "scenario-editor", onsubmit: async (event) => {
    event.preventDefault();
    const values = new FormData(event.target);
    try {
      const assumptions = readAssumptions(event.target);
      assumptions.per_account = readAccountAssumptions(
        event.target, data.projection_accounts || []);
      const saved = await post("/api/scenario/save", {
        handle: selected.handle,
        name: values.get("name"),
        description: values.get("description"),
        parent_handle: base ? null : values.get("parent_handle"),
        assumptions,
        assumption_overrides: base ? undefined : readAssumptionOverrides(event.target),
      });
      state.scenarioManager = saved.handle || "";
      say(base ? "Base scenario assumptions saved in this book." : "Scenario saved.");
      render();
    } catch (error) { say(error.message, "error"); }
  } },
    el("div", { class: "scenario-fields" },
      el("label", {}, "Name", el("input", {
        name: "name", value: selected.name, disabled: base ? "disabled" : null,
      })),
      el("label", {}, "Description", el("input", {
        name: "description", value: selected.description || "", disabled: base ? "disabled" : null,
      })),
      base ? null : el("label", {}, "Inherit assumptions from", el("select", {
        name: "parent_handle",
      }, [
        el("option", {
          value: "", selected: selected.parent_handle ? null : "selected",
        }, "Base scenario"),
        ...data.scenarios.filter((scenario) => !scenario.base
            && scenario.handle !== selected.handle && !wouldCreateParentCycle(scenario))
          .map((scenario) => el("option", {
            value: scenario.handle,
            selected: scenario.handle === selected.parent_handle ? "selected" : null,
          }, scenario.name)),
      ]))),
    el("h3", {}, base ? "Base annual assumptions" : "Annual assumptions"),
    scenarioAssumptionInputs(selected),
    el("details", {class:"panel"},
      el("summary", {}, "Account-specific projection rates"),
      el("p", {class:"note"},
        "Optional overrides take precedence over the account's own rate and the scenario default."),
      accountAssumptionInputs(data.projection_accounts || [], selected.assumptions)),
    el("div", { class: "toolbar" },
      el("button", { class: "action primary", type: "submit" }, "Save changes"),
      el("button", {
        class: "action", type: "button",
        onclick: async () => {
          try {
            const clone = await post("/api/scenario/duplicate", { handle: selected.handle });
            state.scenarioManager = clone.handle;
            state.scenarioPeriod = null;
            say(`Created "${clone.name}".`);
            render();
          } catch (error) { say(error.message, "error"); }
        },
      }, "Duplicate…"),
      base ? null : el("button", {
        class: "action destructive", type: "button",
        onclick: async () => {
          if (!confirm(`Delete scenario "${selected.name}"?`)) return;
          try {
            await post("/api/scenario/delete", { handle: selected.handle });
            state.scenarioManager = "";
            state.scenarioPeriod = null;
            if (state.plan && state.plan.scenario === selected.handle) state.plan.scenario = null;
            say("Scenario deleted. Base scenario was not changed.");
            render();
          } catch (error) { say(error.message, "error"); }
        },
      }, "Delete…")));

  const periodRows = base ? [] : [...selected.periods]
    .sort((a, b) => a.start.localeCompare(b.start) || (a.end || "9999").localeCompare(b.end || "9999"))
    .map((period) => {
      const changed = ASSUMPTION_FIELDS
        .filter(([, field]) => period[field] !== null)
        .map(([label]) => label)
        .join(", ") || "No rate overrides";
      return el("tr", {},
        el("td", {}, period.start),
        el("td", {}, period.end || "onward"),
        el("td", {}, period.description || changed),
        el("td", { class: "muted" }, changed),
        el("td", {},
          el("div", { class: "toolbar compact" },
            el("button", {
              class: "action", type: "button",
              onclick: () => { state.scenarioPeriod = period.index; render(); },
            }, "Edit"),
            el("button", {
              class: "action destructive", type: "button",
              onclick: async () => {
                try {
                  await post("/api/scenario/period/delete", {
                    handle: selected.handle, index: period.index,
                  });
                  state.scenarioPeriod = null;
                  say("Dated assumption period deleted.");
                  render();
                } catch (error) { say(error.message, "error"); }
              },
            }, "Delete"))));
    });

  let periodEditor = null;
  if (!base && state.scenarioPeriod !== null) {
    const editing = state.scenarioPeriod === "new" ? null
      : selected.periods.find((period) => period.index === state.scenarioPeriod);
    if (state.scenarioPeriod !== "new" && !editing) state.scenarioPeriod = null;
    else {
      const assumptions = Object.fromEntries(ASSUMPTION_FIELDS.map(([, field]) =>
        [field, editing ? editing[field] : null]));
      periodEditor = el("form", { class: "panel period-editor", onsubmit: async (event) => {
        event.preventDefault();
        const values = new FormData(event.target);
        const rates = readAssumptions(event.target, "period_");
        try {
          await post("/api/scenario/period/save", {
            handle: selected.handle,
            index: editing ? editing.index : null,
            start: values.get("start"), end: values.get("end"),
            description: values.get("description"), ...rates,
            per_account: readAccountAssumptions(
              event.target, data.projection_accounts || []),
          });
          state.scenarioPeriod = null;
          say("Dated assumptions saved.");
          render();
        } catch (error) { say(error.message, "error"); }
      } },
        el("h3", {}, editing ? "Edit dated assumptions" : "Add dated assumptions"),
        el("p", { class: "note" },
          "Blank rates inherit the value already in force. Later-starting overlapping periods win for values they override."),
        el("div", { class: "scenario-fields" },
          el("label", {}, "Start", el("input", {
            type: "date", name: "start", required: "required",
            value: editing ? editing.start : new Date().toISOString().slice(0, 10),
          })),
          el("label", {}, "Through (blank = onward)", el("input", {
            type: "date", name: "end", value: editing ? (editing.end || "") : "",
          })),
          el("label", {}, "Description", el("input", {
            name: "description", value: editing ? (editing.description || "") : "",
          }))),
        assumptionInputs(assumptions, "period_"),
        el("details", {class:"panel"},
          el("summary", {}, "Dated account-specific projection rates"),
          el("p", {class:"note"},
            "Blank values inherit the account rate already in force for this date."),
          accountAssumptionInputs(
            data.projection_accounts || [],
            {per_account: editing ? (editing.per_account || {}) : {}})),
        el("div", { class: "toolbar" },
          el("button", { class: "action primary", type: "submit" }, "Save dated assumptions"),
          el("button", {
            class: "action", type: "button",
            onclick: () => { state.scenarioPeriod = null; render(); },
          }, "Cancel")));
    }
  }

  return el("div", {},
    el("div", { class: "toolbar" },
      el("button", { class: "action", onclick: () => switchTo("Plan") }, "← Back to Plan"),
      el("label", {}, "Scenario ", picker)),
    el("h2", {}, "Manage scenarios"),
    el("p", { class: "note" },
      "Base scenario assumptions apply to the default plan. Saved scenarios can inherit assumptions from Base or another saved scenario, then keep deliberate local overrides. Dated assumptions and scenario events remain local."),
    el("div", { class: "panel scenario-panel" }, form),
    base
      ? el("p", { class: "note" },
          "Dated assumption periods belong to saved scenarios. Base uses the book's baseline scheduled and estimated activity.")
      : el("div", {},
          el("div", { class: "toolbar" },
            el("h3", { class: "push-right" }, "Dated assumptions"),
            el("span", { class: "muted" }, `${selected.schedule_changes} scenario event change(s)`),
            el("button", {
              class: "action", type: "button",
              onclick: () => { state.scenarioPeriod = "new"; render(); },
            }, "Add dated assumptions…")),
          periodRows.length
            ? table(["Start", "Through", "Description", "Overrides", "Action"], periodRows)
            : el("p", { class: "note" }, "No dated assumption periods."),
          periodEditor));
}

async function showReview() {
  let data = await get(state.review
    ? `/api/review?transaction=${encodeURIComponent(state.review)}`
    : "/api/review");
  if (!state.review && data.actuals.length) {
    state.review = data.actuals[0].handle;
    data = await get(`/api/review?transaction=${encodeURIComponent(state.review)}`);
  }

  const actualButtons = data.actuals.map((actual) => el("button", {
    type: "button",
    class: actual.handle === state.review ? "active" : null,
    onclick: () => { state.review = actual.handle; render(); },
  },
  el("strong", {}, `${actual.date}  ${actual.description}`),
  el("span", { class: "amount" }, money(actual.amount))));

  if (!data.actuals.length) {
    state.review = null;
    return el("div", {},
      el("h2", {}, "Resolve actuals"),
      el("p", { class: "note" },
        "No unresolved actual transactions. Imported historical transactions do not enter this queue."));
  }

  const selected = data.selected;
  const candidateRows = data.candidates.map((candidate) => {
    const variance = Number(candidate.amount_variance);
    const varianceText = `${variance > 0 ? "+" : ""}${money(candidate.amount_variance)}`;
    const dateText = `${candidate.date_variance_days >= 0 ? "+" : ""}${candidate.date_variance_days} day(s)`;
    return el("tr", {},
      el("td", {}, candidate.date),
      el("td", {}, candidate.description),
      el("td", { class: "num" }, money(candidate.expected_amount)),
      el("td", { class: cls(candidate.amount_variance) }, varianceText),
      el("td", { class: "num" }, dateText),
      el("td", {},
        el("div", { class: "review-actions" },
          el("button", {
            class: "action primary", type: "button",
            onclick: async () => {
              try {
                await post("/api/review/match", {
                  transaction: selected.handle, occurrence: candidate.key,
                });
                say("Actual matched to planned occurrence.");
                state.review = null;
                render();
              } catch (error) { say(error.message, "error"); }
            },
          }, "Match"),
          el("button", {
            class: "action", type: "button",
            onclick: async () => {
              try {
                await post("/api/review/reject", {
                  transaction: selected.handle, occurrence: candidate.key,
                });
                say("Candidate rejected.");
                render();
              } catch (error) { say(error.message, "error"); }
            },
          }, "Reject candidate"),
          el("button", {
            class: "action", type: "button",
            onclick: async () => {
              try {
                await post("/api/review/skip", {
                  transaction: selected.handle, occurrence: candidate.key,
                });
                say("Scheduled occurrence skipped.");
                render();
              } catch (error) { say(error.message, "error"); }
            },
          }, "Skip scheduled occurrence"))));
  });

  const fsaOptions = selected.fsa || { claims: [], roles: [] };
  let fsaAttach = null;
  if (fsaOptions.claims.length && fsaOptions.roles.length) {
    const claimSelect = el("select", {},
      fsaOptions.claims.map((claim, index) => el("option", { value: claim.handle },
        `${index === 0 ? "Suggested · " : ""}${claim.label} · remaining ${money(claim.remaining)} · ${claim.reason}`)));
    const roleSelect = el("select", {},
      fsaOptions.roles.map((role) => el("option", {
        value: `${role.role}|${role.split}|${(role.years || []).join(",")}`,
      }, `${role.role.replace("_", " ")} · ${role.account}`)));
    const yearSelect = el("select", {}, el("option", { value: "" }, "Auto funding year"));
    const refreshYears = () => {
      const parts = roleSelect.value.split("|");
      const years = (parts[2] || "").split(",").filter(Boolean);
      yearSelect.replaceChildren(el("option", { value: "" }, "Auto funding year"),
        ...years.map((year) => el("option", { value: year }, year)));
    };
    const applySuggestedRole = () => {
      const claim = fsaOptions.claims.find((item)=>item.handle === claimSelect.value);
      if (!claim) return;
      const option = Array.from(roleSelect.options).find((item)=> {
        const [role, split] = item.value.split("|");
        return role === claim.suggested_role && split === claim.suggested_split;
      });
      if (option) roleSelect.value = option.value;
      refreshYears();
    };
    roleSelect.onchange = refreshYears;
    claimSelect.onchange = applySuggestedRole;
    applySuggestedRole();
    fsaAttach = el("div", { class: "panel panel-pad-10-top" },
      el("strong", {}, "FSA claim"),
      el("div", { class: "review-actions" }, claimSelect, roleSelect, yearSelect,
        el("button", { class: "action", type: "button", onclick: async () => {
          const [role, split] = roleSelect.value.split("|");
          try {
            await post("/api/review/fsa-attach", {
              transaction: selected.handle, claim: claimSelect.value, role, split,
              funding_year: yearSelect.value,
            });
            say("Transaction attached to FSA claim."); render();
          } catch (error) { say(error.message, "error"); }
        }}, "Attach to claim")));
  }

  const detail = el("div", { class: "panel review-detail" },
    el("h2", {}, "Resolve actual"),
    el("p", {}, `${selected.date} · ${selected.description}`),
    el("p", { class: "note" }, `Actual amount: ${money(selected.amount)}`),
    fsaAttach,
    data.candidates.length
      ? table(["Planned", "Description", { label: "Expected", num: true },
               { label: "Amount variance", num: true }, { label: "Date variance", num: true },
               "Action"], candidateRows)
      : el("p", { class: "note" }, "No candidate within the matching window."),
    el("div", { class: "review-actions" },
      el("button", {
        class: "action", type: "button",
        onclick: async () => {
          try {
            await post("/api/review/unexpected", { transaction: selected.handle });
            say("Actual marked unexpected.");
            state.review = null;
            render();
          } catch (error) { say(error.message, "error"); }
        },
      }, "Mark unexpected")));

  return el("div", {},
    el("h2", {}, "Review"),
    el("p", { class: "note" },
      "Match entered or imported actual transactions to scheduled expectations, reject bad suggestions, "
      + "or mark transactions that are intentionally outside the plan."),
    el("div", { class: "review-grid" },
      el("div", { class: "panel review-list" }, actualButtons), detail));
}

function chart(rows) {
  const width = 900, height = 300, pad = { l: 70, r: 12, t: 12, b: 28 };
  const series = [
    { key: "cash", colour: "#2f6fd0" },
    { key: "holdings", colour: "#1c7a4a" },
    { key: "net_worth", colour: "#a04ec4" },
  ];
  const values = series.flatMap((s) => rows.map((r) => Number(r[s.key])));
  const low = Math.min(0, ...values), high = Math.max(...values, 1);
  const x = (i) => pad.l + (width - pad.l - pad.r) * i / Math.max(rows.length - 1, 1);
  const y = (v) => pad.t + (height - pad.t - pad.b) * (1 - (v - low) / (high - low));

  const parts = [];
  for (let step = 0; step <= 4; step++) {
    const value = low + (high - low) * step / 4;
    parts.push(svgEl("line", {
      x1: pad.l, y1: y(value), x2: width - pad.r, y2: y(value), stroke: "#e2e5ea",
    }));
    parts.push(svgEl("text", {
      x: pad.l - 8, y: y(value) + 4, "text-anchor": "end", "font-size": 11,
      fill: "#6b7280",
    }, Math.round(value).toLocaleString()));
  }
  if (low < 0) {
    parts.push(svgEl("line", {
      x1: pad.l, y1: y(0), x2: width - pad.r, y2: y(0), stroke: "#b3261e",
      "stroke-width": 1.4, opacity: 0.6,
    }));
  }
  for (const s of series) {
    const path = rows.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(Number(r[s.key])).toFixed(1)}`).join("");
    parts.push(svgEl("path", { d: path, fill: "none", stroke: s.colour, "stroke-width": 2 }));
  }
  const stride = Math.max(1, Math.floor(rows.length / 8));
  rows.forEach((row, i) => {
    if (i % stride) return;
    parts.push(svgEl("text", {
      x: x(i), y: height - 8, "text-anchor": "middle", "font-size": 11,
      fill: "#6b7280",
    }, row.label));
  });
  const legend = series.flatMap((s, i) => [
    svgEl("rect", { x: pad.l + i * 110, y: 4, width: 10, height: 3, fill: s.colour }),
    svgEl("text", {
      x: pad.l + i * 110 + 16, y: 8, "font-size": 11, fill: "#6b7280",
    }, s.key.replace("_", " ")),
  ]);

  const holder = el("div", { class: "panel panel-pad-12" });
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height + 14}`, width: "100%" });
  const plot = svgEl("g", { transform: "translate(0,14)" });
  plot.append(...parts);
  svg.append(plot, ...legend);
  holder.append(svg);
  return holder;
}

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
        el("div", { class: "value " + (Number(value) < 0 ? "neg" : "") }, money(value)))),
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
      el("td", {}, row.label),
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
        el("td", {}, row.label),
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
              money(value))))),
      el("p", { class: "note" },
        `Differences are ${scenarioData.name} minus ${comparison.scenario.name}. `
        + `Cash shortfall: ${s.first_shortfall || "never"} vs `
        + `${comparison.summary.first_shortfall || "never"}.`),
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
  return el("div", {},
    el("h2", {}, "Projection"),
    form, cards, chart(data.rows), comparisonView, warnings,
    el("p", { class: "note" }, `Scenario "${scenarioData.name}", by year.`),
    table(["Month", { label: "Income", num: true }, { label: "Expense", num: true },
           { label: "Cash", num: true }, { label: "Holdings", num: true },
           { label: "Liabilities", num: true }, { label: "Net worth", num: true }], yearly));
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

async function showEntry() {
  if (!state.accounts.length) state.accounts = await get("/api/accounts");
  const claimData = await get("/api/fsa/claims");
  const openClaims = (claimData.claims || []).filter((claim) => claim.status !== "fully_reimbursed");
  const usable = state.accounts.filter((a) => !a.placeholder && !a.hidden && a.depth > 0);
  const options = (name) => el("select", { name },
    usable.map((a) => el("option", { value: a.full_name }, a.full_name)));

  const form = el("form", { class: "entry", onsubmit: async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target).entries());
    try {
      await post("/api/transaction", data);
      say("Transaction posted.");
      event.target.reset();
    } catch (error) { say(error.message, "error"); }
  } },
    el("label", {}, "Date",
      el("input", { type: "date", name: "date", value: new Date().toISOString().slice(0, 10) })),
    el("label", {}, "Description", el("input", { name: "description", required: "required" })),
    el("label", {}, "BreadSched notes", el("textarea", { name: "notes", rows: "3" })),
    el("label", {}, "From (money leaves)", options("from")),
    el("label", {}, "To (money arrives)", options("to")),
    el("label", {}, "Amount", el("input", { name: "amount", required: "required", placeholder: "0.00" })),
    el("label", {}, "Investment activity (optional)",
      el("select", { name: "investment_activity" },
        el("option", { value: "" }, "Ordinary transaction"),
        el("option", { value: "contribution" }, "Contribution"),
        el("option", { value: "withdrawal" }, "Taxable withdrawal"),
        el("option", { value: "retirement_distribution" }, "Retirement distribution"),
        el("option", { value: "dividend" }, "Reinvested dividend"),
        el("option", { value: "interest" }, "Reinvested interest"),
        el("option", { value: "fee" }, "Investment fee"),
        el("option", { value: "rollover" }, "Retirement rollover"))),
    openClaims.length ? el("label", {}, "FSA claim (optional)",
      el("select", { name: "fsa_claim" },
        el("option", { value: "" }, "Do not attach"),
        openClaims.map((claim) => el("option", { value: claim.handle },
          `${claim.service_date} ${claim.provider || claim.description || "FSA claim"}`)))) : null,
    openClaims.length ? el("label", {}, "FSA claim role",
      el("select", { name: "fsa_role" },
        el("option", { value: "payment" }, "Healthcare payment"),
        el("option", { value: "refund" }, "Provider refund"),
        el("option", { value: "reimbursement" }, "FSA reimbursement"))) : null,
    el("button", { class: "action primary", type: "submit" }, "Post"));

  return el("div", {},
    el("p", { class: "note" },
      "Two-split entry. For anything more involved, use the desktop interface."),
    el("div", { class: "panel panel-pad-16" }, form));
}


async function showImport() {
  const defaults = await get("/api/import");
  const path = el("input", { name:"path", required:"required",
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
  const form = el("form", { class:"entry", onsubmit: async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target).entries());
    data.include_scheduled = true;
    try {
      const response = await post("/api/import", data);
      result.textContent = `${response.format}\n\n${response.detail}`;
      say("Import finished.");
    } catch (error) { say(error.message, "error"); }
  } },
    el("label", {}, "Local file path", path),
    el("label", {}, "Number format", numberFormat),
    el("label", {}, "QIF date order", dateFormat),
    el("button", { class:"action primary", type:"submit" }, "Import"));
  return el("div", {},
    el("p", { class:"note" }, "Import a file visible to the BreadSched process. Auto-detection is recommended; choose an explicit format when the source is ambiguous."),
    el("p", { class:"note" },
      "Re-importing GnuCash updates source-owned data and removes transactions deleted from the source. Transactions still used by a BreadSched reconciliation or FSA claim are retained and reported for review."),
    el("div", { class:"panel panel-pad-16" }, form, result));
}

async function showVerify() {
  const data = await get("/api/verify");
  const rerun = el("button", {class:"action", onclick:()=>showVerify()}, "Verify again");
  const findings = [];
  (data.sqlite || []).forEach((problem) => findings.push(
    el("li", {}, `SQLite: ${problem}`)));
  (data.issues || []).forEach((issue) => findings.push(
    el("li", {}, `${issue.code}: ${issue.message}`)));
  main.replaceChildren(
    el("div", {class:"toolbar"}, rerun),
    el("div", {class:"panel scenario-panel"},
      el("h2", {}, data.ok ? "No problems found" : "Book verification found problems"),
      data.ok
        ? el("p", {}, "SQLite integrity and BreadSched financial relationships are clean.")
        : el("ul", {}, findings)),
  );
}

const RENDERERS = {
  Dashboard: showDashboard, Accounts: showAccounts, Register: showRegister, Scheduled: showScheduled,
  "FSA Dashboard": showFsaDashboard, Plan: showPlan, Scenarios: showScenarios,
  Review: showReview, Projection: showProjection, Enter: showEntry, Import: showImport,
  Verify: showVerify,
};

async function showFsaDashboard() {
  const data = await get("/api/fsa/dashboard");
  const yearRows = data.years.map((item) => [
    item.account, `${item.start} – ${item.through}`, item.phase, money(item.election),
    money(item.funded), money(item.used), money(item.remaining), money(item.forfeited),
  ]);
  const claimRows = data.claims.map((claim) => [
    claim.service_date, claim.provider, claim.status, money(claim.paid),
    money(claim.reimbursed), money(claim.remaining),
    el("button", { class:"action", type:"button", onclick:()=>
      openFsaClaimsEditor(claim.handle).catch((error)=>say(error.message,"error")) },
    "Review claim"),
  ]);
  return el("div", {},
    el("div", { class:"toolbar" },
      el("button", { class:"action", type:"button", onclick:()=>
        openFsaClaimsEditor().catch((error)=>say(error.message,"error")) },
      "Manage FSA claims…")),
    el("h2", {}, "FSA benefit years"),
    yearRows.length ? table(["Account", "Funding year", "Status",
      {label:"Election",num:true},{label:"Funded",num:true},{label:"Used",num:true},
      {label:"Remaining",num:true},{label:"Forfeited",num:true}], yearRows)
      : el("p", { class:"note" }, "No open or recently closed FSA benefit years."),
    el("h2", {}, "Open FSA claims"),
    claimRows.length ? table(["Service date", "Provider", "Status",
      {label:"Paid",num:true},{label:"Reimbursed",num:true},
      {label:"Remaining",num:true}, "Action"], claimRows)
      : el("p", { class:"note" }, "No open FSA claims."));
}

function openDashboardGroupsEditor(config) {
  const backdrop = el("div", { class:"detail-backdrop" });
  const rows = el("div", { class:"stack" });
  const kinds = [
    ["liquid", "Liquid"], ["retirement", "Retirement"], ["asset", "Asset"],
    ["property", "Property"], ["liability", "Liability"],
  ];
  const addGroup = (source=null) => {
    const name = el("input", {
      placeholder:"Investments:Plan A", value:source?.name || "",
    });
    const kind = el("select", {}, kinds.map(([value, label]) => el("option", {
      value, selected:value === (source?.kind || "asset") ? "selected" : null,
    }, label)));
    const accounts = el("select", { multiple:"multiple", size:"7" },
      config.accounts.map((account) => el("option", {
        value:account.handle,
        selected:(source?.accounts || []).includes(account.handle) ? "selected" : null,
      }, account.name)));
    const row = el("div", { class:"card" },
      el("div", { class:"row" }, name, kind,
        el("button", { class:"action", type:"button", onclick:()=>row.remove() }, "Remove")),
      el("label", {}, "Accounts", accounts));
    row._groupFields = { name, kind, accounts };
    rows.append(row);
  };
  (config.groups || []).forEach(addGroup);
  const save = async () => {
    const groups = Array.from(rows.children).map((row) => ({
      name:row._groupFields.name.value.trim(),
      kind:row._groupFields.kind.value,
      accounts:Array.from(row._groupFields.accounts.selectedOptions).map(
        (option) => option.value),
    }));
    if (groups.some((group) => !group.name)) {
      say("Every dashboard group needs a name.", "error");
      return;
    }
    await post("/api/dashboard/config", {
      groups,
      liquidity_days:state.liquidityDays || config.liquidity_days,
      emergency_months:state.emergencyMonths || config.emergency_months,
    });
    backdrop.remove();
    render();
  };
  backdrop.append(el("section", { class:"detail-dialog wide" },
    el("div", { class:"detail-heading" }, el("h2", {}, "Dashboard groups")),
    el("p", { class:"note" },
      "Colon-separated names create totalled headings. Selecting a parent account includes its subaccounts once."),
    rows,
    el("div", { class:"toolbar" },
      el("button", { class:"action", type:"button", onclick:()=>addGroup() }, "Add group"),
      el("button", { class:"action primary", type:"button", onclick:save }, "Save"),
      el("button", { class:"action", type:"button", onclick:()=>backdrop.remove() }, "Cancel"))));
  document.body.append(backdrop);
}

// The dashboard: what is owned and owed, the liquidity verdict, and the bills.
// The two horizons are inputs rather than fixed constants, because how much must
// stay liquid and how long the fund should last are the household's judgement,
// not the program's.
async function showDashboard() {
  const query = new URLSearchParams();
  if (state.liquidityDays) query.set("liquidity_days", state.liquidityDays);
  if (state.emergencyMonths) query.set("emergency_months", state.emergencyMonths);
  const data = await get("/api/dashboard?" + query.toString());
  const s = data.summary;
  state.liquidityDays = data.config.liquidity_days;
  state.emergencyMonths = data.config.emergency_months;

  const short = Number(s.emergency_shortfall) > 0;
  const tile = (label, value, alarm) => el("div", { class: "card" },
    el("div", { class: "label" }, label),
    el("div", { class: alarm ? "value neg" : "value" }, value));

  const cards = el("div", { class: "cards" },
    tile("Net worth", money(s.net_worth)),
    tile("Liquid", money(s.liquid)),
    tile(`Needed in ${data.config.liquidity_days} days`, money(s.required_liquid)),
    tile("Available", money(s.available), Number(s.available) < 0),
    tile(`Emergency fund (${data.config.emergency_months} mo)`, money(s.emergency_fund)),
    tile("Committed emergency outgoings / mo", money(s.emergency_monthly_outgoings)),
    tile("Including estimates / mo", money(s.emergency_monthly_outgoings_with_estimates)),
    tile("Months covered", s.months_covered,
         Number(s.months_covered) < Number(data.config.emergency_months)),
    short ? tile("Short of the fund", money(s.emergency_shortfall), true) : null);

  const controls = el("div", { class: "row" },
    el("label", {}, "Liquid for "),
    el("input", {
      type: "number", min: "7", max: "365", value: data.config.liquidity_days,
      onchange: (e) => { state.liquidityDays = e.target.value; render(); },
    }),
    el("label", {}, " days   Emergency "),
    el("input", {
      type: "number", min: "1", max: "36", value: data.config.emergency_months,
      onchange: (e) => { state.emergencyMonths = e.target.value; render(); },
    }),
    el("label", {}, " months"),
    el("button", { class:"action", type:"button",
      onclick:()=>openDashboardGroupsEditor(data.config) }, "Configure groups…"));

  const groupCards = el("div", { class:"balance-groups" }, data.groups.map((g) =>
    el("div", {
      class:`balance-group ${depthClass("tree-depth", g.depth)} ${g.heading ? "group-heading" : ""}`,
      title:g.path,
    }, el("h3",{},g.name), g.note ? el("p", {class:"note"}, g.note) : null, el("dl",{},
      g.value === null ? null : [el("dt",{},"Value"),el("dd",{},money(g.value))],
      g.debt === null ? null : [el("dt",{},"Owed"),el("dd",{},money(g.debt))],
      el("dt",{},g.equity === null ? "Total" : "Equity"), el("dd",{},money(g.equity === null ? g.total : g.equity)),
      g.loan_to_value === null ? null : [el("dt",{},"LTV"),el("dd",{},(g.loan_to_value*100).toFixed(1)+"%")
      ], g.loan_end === null ? null : [el("dt",{},"Loan end"),el("dd",{},g.loan_end)
      ], ...(g.accounts || []).map((account) => [
        el("dt", {}, account.name),
        el("dd", { title:account.note || "" },
          account.balance === null ? "Unavailable" : money(account.balance)),
      ])))));

  const activatePending = async (item) => {
    if (item.schedule) {
      const schedules = await get("/api/scheduled?days=90");
      const source = schedules.definitions.find((entry)=>entry.handle === item.schedule);
      if (source?.simple) openScheduledEditor(schedules, source);
      else switchTo("Scheduled");
    } else if (item.account) {
      state.account = item.account;
      switchTo("Register");
    }
  };
  const billRows = data.bills.map((item) => [
    el("span", { ondblclick:()=>activatePending(item), title:"Double-click to view" },
      item.name),
    item.next_due,
    item.days_until < 0 ? `${-item.days_until} days overdue`
      : (item.days_until === 0 ? "today" : `${item.days_until} days`),
    item.cycle_months.toFixed(2).replace(/\.?0+$/, "") + " mo",
    money(item.amount), item.generated ? "" : money(item.monthly),
    money(item.hold), item.generated ? "" : money(item.annual),
    item.generated ? "Account payment" : "Committed",
  ]);
  const incomeRows = data.income.map((item) => [
    el("span", { ondblclick:()=>activatePending(item), title:"Double-click to view" },
      item.name),
    item.next_due,
    item.days_until < 0 ? `${-item.days_until} days overdue`
      : (item.days_until === 0 ? "today" : `${item.days_until} days`),
    item.cycle_months.toFixed(2).replace(/\.?0+$/, "") + " mo",
    money(item.amount), money(item.monthly), money(item.annual),
  ]);

  return el("div", {}, cards, controls,
    el("h2", {}, "Balances"),
    groupCards,
    el("h2", {}, `Pending bills (${data.bills.length})`),
    table(["Item", "Next due", "Due in", "Cycle",
           { label: "Amount", num: true }, { label: "Monthly", num: true },
           { label: "Hold now", num: true }, { label: "Annual", num: true }, "Kind"],
          billRows),
    el("h2", {}, `Expected income (${data.income.length})`),
    table(["Item", "Next due", "Due in", "Cycle",
           { label: "Amount", num: true }, { label: "Monthly", num: true },
           { label: "Annual", num: true }], incomeRows));
}

function switchTo(name) { current = name; render(); }

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
