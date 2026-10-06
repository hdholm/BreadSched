// Editors several views share: dated amount timelines, occurrence
// adjustments, Plan split rows, and the schedule preview.

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
