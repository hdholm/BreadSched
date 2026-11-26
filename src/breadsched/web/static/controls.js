// Editors several views share: schedule versions, skipped occurrences, Plan
// split rows, and the schedule preview.

// A schedule's versions (gen/lib/schedule_versions.py): each the whole transaction
// for some occurrences, from a date on, on one date only, or every year in one
// month. Legs are typed as a register's splits: an account, a memo, and an amount
// in the Increase or Decrease column. An amount may be a formula over base (the
// leg's amount below, as its column shows it) and base_total (the transaction
// below): base * 1.2 is 20% more. start(kind, when, month) asks the server for a
// new version filled in from what the schedule then is.
const VERSION_MONTHS = ["January","February","March","April","May","June",
  "July","August","September","October","November","December"];
const VERSION_KINDS = [["from","From a date on"],["once","On one date only"],
  ["month","Every year in a month"]];
const isVersionFormula = (text) => /[A-Za-z_]/.test(text);

function versionLabel(version) {
  if (version.kind === "month") return `Every ${VERSION_MONTHS[Number(version.month) - 1]}`;
  if (version.kind === "once") return `On ${version.start} only`;
  return `From ${version.start}`;
}

function versionsEditor(accounts, initial, start, onChanged=()=>{}) {
  const list = el("div", { class:"versions" });
  const message = el("p", { class:"note error", role:"status" });
  const kind = el("select", { "aria-label":"New version" },
    VERSION_KINDS.map(([value, label]) => el("option", { value }, label)));
  const when = el("input", { type:"date", "aria-label":"Version date" });
  const month = el("select", { "aria-label":"Version month" },
    VERSION_MONTHS.map((name, index) => el("option", { value:String(index + 1) }, name)));
  const showWhen = () => { when.hidden = kind.value === "month"; month.hidden = kind.value !== "month"; };
  kind.addEventListener("change", showWhen);
  const accountOptions = (handle) => {
    const choices = accounts.some((entry) => entry.handle === handle) || !handle
      ? accounts : [{ handle, name:handle }, ...accounts];
    return choices.map((entry) => el("option", {
      value:entry.handle, selected:entry.handle === handle ? "selected" : null }, entry.name));
  };
  const legRow = (rows, leg={}) => {
    const formula = leg.formula || "";
    const amount = leg.amount == null ? "" : String(leg.amount);
    const negative = formula ? Number(leg.direction) < 0 : amount.startsWith("-");
    const shown = formula || amount.replace(/^-/, "");
    const row = el("div", { class:"version-leg" });
    const account = el("select", { "aria-label":"Account" }, accountOptions(leg.account));
    const memo = el("input", { "aria-label":"Memo", placeholder:"Memo", value:leg.memo || "" });
    const increase = el("input", { "aria-label":"Increase", placeholder:"Increase",
      spellcheck:"false", value:negative ? "" : shown });
    const decrease = el("input", { "aria-label":"Decrease", placeholder:"Decrease",
      spellcheck:"false", value:negative ? shown : "" });
    // Typing in one column empties the other, as the register's split grid does.
    increase.addEventListener("input", () => { if (increase.value) decrease.value = ""; });
    decrease.addEventListener("input", () => { if (decrease.value) increase.value = ""; });
    const remove = el("button", { class:"action", type:"button",
      onclick:() => { row.remove(); onChanged(); } }, "Remove leg");
    row.dataset.planningFlow = leg.planning_flow || "";
    row.dataset.investmentActivity = leg.investment_activity || "";
    row.append(account, memo, increase, decrease, remove);
    [account, memo, increase, decrease].forEach((control) =>
      control.addEventListener("change", onChanged));
    rows.append(row);
  };
  const addVersion = (version) => {
    const rows = el("div", { class:"version-legs" });
    const box = el("fieldset", { class:"timeline-editor version" },
      el("legend", {}, versionLabel(version)), rows);
    box.dataset.kind = version.kind;
    box.dataset.start = version.start || "";
    box.dataset.month = version.month == null ? "" : String(version.month);
    (version.legs || []).forEach((leg) => legRow(rows, leg));
    box.append(el("div", { class:"toolbar compact" },
      el("button", { class:"action", type:"button",
        onclick:() => { legRow(rows); onChanged(); } }, "Add leg"),
      el("button", { class:"action", type:"button",
        onclick:() => { box.remove(); onChanged(); } }, "Remove version")));
    const key = (item) => [item.kind === "month" ? 1 : item.kind === "once" ? 2 : 0,
      item.kind === "month" ? String(item.month).padStart(2, "0") : item.start].join(":");
    const after = Array.from(list.children).find((other) => key(other.dataset) > key(version));
    list.insertBefore(box, after || null);
  };
  (initial || []).forEach(addVersion);
  const add = el("button", { class:"action", type:"button", onclick:async () => {
    message.textContent = "";
    const chosen = { kind:kind.value, start:kind.value === "month" ? null : when.value,
      month:kind.value === "month" ? Number(month.value) : null };
    if (chosen.kind !== "month" && !chosen.start) { message.textContent = "Enter the version's date."; return; }
    if (Array.from(list.children).some((box) => box.dataset.kind === chosen.kind
      && (chosen.kind === "month" ? Number(box.dataset.month) === chosen.month
        : box.dataset.start === chosen.start))) {
      message.textContent = "There is already a version for that date or month."; return;
    }
    try {
      addVersion(await start(chosen));
      onChanged();
    } catch (error) { message.textContent = error.message; }
  } }, "Add version");
  showWhen();
  const node = el("div", { class:"timeline-editor versions-editor" },
    el("strong", {}, "Versions"),
    el("p", { class:"note" }, "Each version is the whole transaction, typed as a register's splits: add a leg, remove one, or change any amount. An amount may be a formula: base is the leg's amount before this version (in its own column) and base_total the amount the transaction moved; base * 1.2 is 20% more. A month's version applies on top of the dated one in force, and a one-date version on top of both."),
    list, el("div", { class:"toolbar compact" }, kind, when, month, add), message);
  return {
    node,
    count:() => list.children.length,
    values:() => Array.from(list.children).map((box) => {
      const label = box.querySelector("legend").textContent;
      const legs = Array.from(box.querySelectorAll(".version-leg")).map((row) => {
        const [account] = row.querySelectorAll("select");
        const [memo, increase, decrease] = row.querySelectorAll("input");
        const up = increase.value.trim();
        const down = decrease.value.trim();
        if (up && down) throw new Error(`${label}: enter each leg in one column.`);
        const text = up || down;
        const leg = { account:account.value, memo:memo.value.trim(),
          planning_flow:row.dataset.planningFlow || null,
          investment_activity:row.dataset.investmentActivity || null };
        if (isVersionFormula(text)) return { ...leg, formula:text, direction:down ? -1 : 1 };
        return { ...leg, amount:text || "0", direction:down ? -1 : 1 };
      });
      if (legs.length < 2) throw new Error(`${label}: a version needs at least two legs.`);
      return { kind:box.dataset.kind, start:box.dataset.start || null,
        month:box.dataset.month ? Number(box.dataset.month) : null, legs };
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

// The schedule editors' "Every N <unit>" (#370): the units and keys of
// presentation/schedules.py. Twice a month and once take no number. The day
// comes from the first due date.
const FREQUENCY_UNITS = [
  ["days", "days"], ["weeks", "weeks"], ["months", "months"],
  ["nth_weekday", "months, on the same weekday"],
  ["last_weekday", "months, on the last weekday"],
  ["years", "years"], ["semimonthly", "twice a month"], ["once", "once"],
];
const FIXED_INTERVAL_UNITS = new Set(["semimonthly", "once"]);

function frequencyControl(source) {
  const unit = el("select", { name:"frequency", "aria-label":"Repeat unit" },
    FREQUENCY_UNITS.map(([key, label]) => el("option", {
      value:key, selected:key === (source?.frequency_key || "months") ? "selected" : null,
    }, label)));
  const every = el("input", { name:"interval", type:"number", min:"1", max:"999",
    step:"1", value:String(source?.interval || 1), "aria-label":"Repeat every",
    class:"interval" });
  const everyLabel = el("span", {}, "Every ");
  const box = el("span", { class:"frequency" }, everyLabel, every, " ", unit);
  const show = () => {
    const fixed = FIXED_INTERVAL_UNITS.has(unit.value);
    every.hidden = fixed;
    everyLabel.hidden = fixed;
  };
  unit.addEventListener("change", show);
  show();
  return box;
}

