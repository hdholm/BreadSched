// Scheduled transactions, historical estimates, due review, the schedule
// editors, and paychecks.

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
        onclick: () => openDueReviewDialog().catch((error) => say(error.message, "error")),
      }, "Review due transactions…")),
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

// Due and missed scheduled occurrences, grouped by schedule. Each date has its own
// decision (defer by default); a schedule with several dates also has a chooser
// that sets them all. The shared service rechecks every date before writing, so a
// date posted elsewhere meanwhile is refused rather than posted twice.
async function openDueReviewDialog() {
  if (dueReviewDialog?.isConnected) return;
  const data = await get("/api/due-review");
  if (!data.schedules.length) { say("Nothing is due."); return; }
  const backdrop = el("div", { class:"detail-backdrop" });
  dueReviewDialog = backdrop;
  const close = () => {
    backdrop.remove();
    if (dueReviewDialog === backdrop) dueReviewDialog = null;
  };
  const labels = { post:"Post now", defer:"Remind me later", skip:"Never (mark as done)" };
  const chooser = (label) => el("select", { "aria-label":label },
    ...["post", "defer", "skip"].map((value) =>
      el("option", { value, selected:value === "defer" ? "selected" : null }, labels[value])));
  const rows = [];
  const choices = [];
  for (const review of data.schedules) {
    const own = review.items.map((item) => {
      const select = chooser(`${review.name} ${item.date}`);
      choices.push({ schedule:review.schedule, date:item.date, select });
      return { item, select };
    });
    const heading = [
      el("strong", {}, review.name),
      `${review.items.length} due`, review.frequency, money(review.total), "",
    ];
    if (review.items.length > 1) {
      const all = el("select", { "aria-label":`All dates for ${review.name}`,
        onchange:(event) => {
          if (event.target.value) own.forEach(({ select }) => { select.value = event.target.value; });
        } },
        el("option", { value:"" }, "Choose each date"),
        el("option", { value:"post" }, "Post all"),
        el("option", { value:"defer" }, "Remind me later for all"),
        el("option", { value:"skip" }, "Never, all"));
      heading[4] = all;
    }
    rows.push(el("tr", { class:"heading" }, ...heading.map((cell) => el("td", {}, cell))));
    for (const { item, select } of own) {
      rows.push(el("tr", {},
        el("td", {}, ""),
        el("td", { class:item.overdue ? "neg" : null },
          item.overdue ? `${item.date} (overdue)` : item.date),
        el("td", {}, ""),
        el("td", { class:"num" }, money(item.amount)),
        el("td", {}, select)));
    }
  }
  const apply = async () => {
    const decisions = choices
      .map(({ schedule, date, select }) => ({ schedule, date, decision:select.value }))
      .filter((item) => item.decision !== "defer");
    try {
      if (decisions.length) {
        const outcome = await post("/api/due-review", { decisions });
        say(`Posted ${outcome.posted}, marked ${outcome.skipped} as done.`);
      }
      close();
      render();
    } catch (error) { say(error.message, "error"); }
  };
  backdrop.append(el("section", { class:"detail-dialog wide" },
    el("div", { class:"detail-heading" },
      helpHeading("Scheduled transactions due", "due-review")),
    el("p", { class:"note" }, "Choose what to do with each date. Nothing is posted until you apply, and the batch is one undo step."),
    el("table", {}, el("tbody", {}, ...rows)),
    el("div", { class:"toolbar" },
      el("button", { class:"action", type:"button", onclick:close }, "Decide later"),
      el("button", { class:"action primary", type:"button", onclick:apply }, "Apply"))));
  document.body.append(backdrop);
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

const PAY_PERIOD_LABELS = {
  biweekly:"Every 2 weeks", weekly:"Weekly",
  semimonthly:"Twice a month (15th and last day)", monthly:"Monthly",
};

async function showPayroll() {
  const today = new Date().toISOString().slice(0, 10);
  const asOf = state.payrollAsOf || today;
  const data = await get(`/api/payroll?as_of=${encodeURIComponent(asOf)}`);
  const refresh = async () => { current = "Payroll"; await render(); };
  const run = (action) => async () => {
    try { await action(); } catch (error) { say(error.message, "error"); }
  };
  const options = (items, chosen) => items.map((item) => el("option",
    { value:item.handle, selected:item.handle === chosen ? "selected" : null }, item.name));
  const paycheck = data.paychecks.find((item) => item.schedule === state.payrollSchedule)
    || data.paychecks[0] || null;

  // Paychecks and the selected one's lines.
  const asOfInput = el("input", { type:"date", value:asOf, "aria-label":"As of" });
  asOfInput.addEventListener("change", run(async () => {
    state.payrollAsOf = asOfInput.value || null;
    await refresh();
  }));
  const summaryRows = data.paychecks.map((item) => el("tr", {},
    el("td", {}, el("button", { class:"action", type:"button", onclick:run(async () => {
      state.payrollSchedule = item.schedule;
      await refresh();
    }) }, item.name)),
    el("td", { class:"num" }, money(item.gross)),
    el("td", { class:"num" }, money(item.withheld)),
    el("td", { class:"num" }, money(item.net)),
    el("td", { class:"num" }, `${item.take_home_percent}%`)));
  const legRows = paycheck ? paycheck.legs.map((leg) => [leg.kind_label, leg.name, money(leg.amount)]) : [];
  const paycheckPanel = el("div", { class:"panel panel-pad-16" },
    el("h2", {}, "Paychecks"),
    el("label", {}, "As of ", asOfInput),
    data.paychecks.length
      ? table(["Paycheck", { label:"Gross", num:true }, { label:"Withheld", num:true },
        { label:"Net", num:true }, { label:"Take-home", num:true }], summaryRows)
      : el("p", { class:"note" }, "No schedule reads as a paycheck. Create one from a "
        + "template below, or add a schedule with gross pay into an income account and the "
        + "net into a bank account."),
    paycheck ? el("h3", {}, paycheck.name) : null,
    paycheck ? table(["Line", "Account", { label:"Amount", num:true }], legRows) : null);

  // Pay change for the selected paycheck.
  let changePanel = null;
  if (paycheck) {
    const start = el("input", { type:"date", value:today, "aria-label":"Pay change from" });
    const gross = el("input", { inputmode:"decimal", value:paycheck.gross,
      "aria-label":"New gross pay" });
    const choices = {};
    const rows = paycheck.legs.map((leg) => {
      if (leg.kind === "gross" || leg.kind === "net") {
        return el("tr", {}, el("td", {}, leg.kind_label), el("td", {}, leg.name),
          el("td", { class:"num" }, money(leg.amount)), el("td", {}, "—"), el("td", {}, ""),
          el("td", { class:"num", "data-after":leg.account }, ""));
      }
      const how = el("select", { "aria-label":`Change ${leg.name}` },
        el("option", { value:"keep" }, "Keep"),
        el("option", { value:"scale", selected:leg.kind === "tax" ? "selected" : null },
          "Scale with gross"),
        el("option", { value:"set" }, "Set to"));
      const amount = el("input", { inputmode:"decimal", "aria-label":`New amount for ${leg.name}`,
        disabled:"disabled" });
      how.addEventListener("change", () => { amount.disabled = how.value !== "set"; });
      choices[leg.account] = { how, amount };
      return el("tr", {}, el("td", {}, leg.kind_label), el("td", {}, leg.name),
        el("td", { class:"num" }, money(leg.amount)), el("td", {}, how), el("td", {}, amount),
        el("td", { class:"num", "data-after":leg.account }, ""));
    });
    const changeTable = table(["Line", "Account", { label:"Now", num:true }, "Change",
      "Amount", { label:"After", num:true }], rows);
    const body = () => {
      const scaled = [];
      const amounts = {};
      for (const [account, choice] of Object.entries(choices)) {
        if (choice.how.value === "scale") scaled.push(account);
        if (choice.how.value === "set") amounts[account] = choice.amount.value;
      }
      return { schedule:paycheck.schedule, start:start.value, gross:gross.value, scaled, amounts };
    };
    const showPlan = (plan) => {
      for (const line of plan.lines) {
        const cell = changeTable.querySelector(`[data-after="${line.account}"]`);
        if (cell) cell.textContent = money(line.after);
      }
    };
    changePanel = el("div", { class:"panel panel-pad-16" },
      el("h2", {}, `Pay change: ${paycheck.name}`),
      el("p", { class:"note" }, "A pay change applies from its date onward; earlier paychecks "
        + "keep their amounts. Taxes scale with gross by default, other lines stay the same, "
        + "and the net deposit takes the difference."),
      el("div", { class:"entry" }, el("label", {}, "From", start),
        el("label", {}, "New gross", gross)),
      changeTable,
      el("div", { class:"toolbar" },
        el("button", { class:"action", type:"button", onclick:run(async () => {
          const plan = await post("/api/payroll/change/preview", body());
          showPlan(plan);
          say(`From ${plan.start}: take-home ${money(plan.net_after)}. Nothing is saved until `
            + "you choose Save pay change.");
        }) }, "Preview"),
        el("button", { class:"action primary", type:"button", onclick:run(async () => {
          const plan = await post("/api/payroll/change", body());
          await refresh();
          say(`Saved the pay change from ${plan.start}: take-home ${money(plan.net_after)}.`);
        }) }, "Save pay change")));
  }

  // Templates: list, editor, and new paycheck.
  const editing = state.payrollTemplate || null;
  const name = el("input", { "aria-label":"Template name", value:editing?.name || "" });
  const income = el("select", { "aria-label":"Gross pay into" },
    ...options(data.income_accounts, editing?.income_account));
  const deposit = el("select", { "aria-label":"Net deposit to" },
    ...options(data.deposit_accounts, editing?.deposit_account));
  const usual = el("input", { inputmode:"decimal", "aria-label":"Usual gross",
    value:editing?.gross || "" });
  const lines = el("div", { class:"stack" });
  const addLine = (line) => {
    const account = el("select", { "aria-label":"Line account" },
      ...options(data.line_accounts, line?.account));
    const amount = el("input", { "aria-label":"Line amount or percentage",
      placeholder:"85.50 or 6.2%",
      value:line ? (line.percent != null ? `${line.percent}%` : line.amount) : "" });
    const row = el("div", { class:"row" }, account, amount,
      el("button", { class:"action", type:"button", onclick:() => row.remove() }, "Remove"));
    row.lineFields = { account, amount };
    lines.append(row);
  };
  (editing?.lines || []).forEach(addLine);
  const templateBody = () => ({
    name:name.value, existing_name:editing?.existing_name || null,
    income_account:income.value, deposit_account:deposit.value, gross:usual.value,
    lines:[...lines.children].map((row) => {
      const text = row.lineFields.amount.value.trim();
      return text.endsWith("%")
        ? { account:row.lineFields.account.value, percent:text }
        : { account:row.lineFields.account.value, amount:text };
    }),
  });
  const templateRows = data.templates.map((item) => el("tr", {},
    el("td", {}, item.name),
    el("td", {}, item.deposit_name),
    el("td", { class:"num" }, money(item.gross)),
    el("td", {}, item.lines.map((line) =>
      `${line.name}: ${line.percent != null ? `${line.percent}%` : money(line.amount)}`).join("; ")
      || "—"),
    el("td", {},
      el("button", { class:"action", type:"button", onclick:run(async () => {
        state.payrollTemplate = { ...item, existing_name:item.name };
        await refresh();
      }) }, "Edit"),
      el("button", { class:"action", type:"button", onclick:run(async () => {
        await post("/api/payroll/template/delete", { name:item.name });
        if (state.payrollTemplate?.existing_name === item.name) state.payrollTemplate = null;
        await refresh();
        say(`Deleted payroll template ${item.name}.`);
      }) }, "Delete"))));
  const editor = el("div", { class:"stack" },
    el("h3", {}, editing?.existing_name ? `Edit ${editing.existing_name}` : "New template"),
    el("div", { class:"entry" }, el("label", {}, "Name", name),
      el("label", {}, "Gross pay into", income), el("label", {}, "Net deposit to", deposit),
      el("label", {}, "Usual gross", usual)),
    el("p", { class:"note" }, "Lines out of gross: an amount such as 85.50, or a percentage "
      + "such as 6.2%."),
    lines,
    el("div", { class:"toolbar" },
      el("button", { class:"action", type:"button", onclick:() => addLine(null) }, "Add line"),
      paycheck ? el("button", { class:"action", type:"button", onclick:run(async () => {
        const filled = await post("/api/payroll/template/from-schedule",
          { schedule:paycheck.schedule, name:paycheck.name });
        state.payrollTemplate = filled;
        await refresh();
        say("Filled the template from the paycheck. Save template to keep it.");
      }) }, "Fill from paycheck") : null,
      el("button", { class:"action primary", type:"button", onclick:run(async () => {
        const saved = await post("/api/payroll/template/save", templateBody());
        state.payrollTemplate = null;
        await refresh();
        say(`Saved payroll template ${saved.name}.`);
      }) }, "Save template")));
  const createTemplate = el("select", { "aria-label":"Template for the new paycheck" },
    ...data.templates.map((item) => el("option", { value:item.name }, item.name)));
  const createName = el("input", { "aria-label":"Paycheck schedule name",
    placeholder:"Schedule name" });
  const createStart = el("input", { type:"date", value:today, "aria-label":"First payday" });
  const createPeriod = el("select", { "aria-label":"Pay period" },
    ...data.periods.map((key) => el("option", { value:key }, PAY_PERIOD_LABELS[key] || key)));
  const createGross = el("input", { inputmode:"decimal", "aria-label":"Gross (optional)",
    placeholder:"Gross (optional)" });
  const createForm = data.templates.length ? el("div", { class:"stack" },
    el("h3", {}, "New paycheck from a template"),
    el("div", { class:"entry" }, el("label", {}, "Template", createTemplate),
      el("label", {}, "Name", createName), el("label", {}, "First payday", createStart),
      el("label", {}, "Pay period", createPeriod), el("label", {}, "Gross", createGross),
      el("button", { class:"action primary", type:"button", onclick:run(async () => {
        const created = await post("/api/payroll/create", {
          template:createTemplate.value, name:createName.value || createTemplate.value,
          start:createStart.value, period:createPeriod.value, gross:createGross.value || null,
        });
        state.payrollSchedule = created.handle;
        await refresh();
        say(`Added paycheck ${created.name}.`);
      }) }, "Create paycheck"))) : null;
  const templatePanel = el("div", { class:"panel panel-pad-16" },
    el("h2", {}, "Payroll templates"),
    data.templates.length
      ? table(["Template", "Deposit", { label:"Gross", num:true }, "Lines", ""], templateRows)
      : el("p", { class:"note" }, "No payroll templates yet."),
    editor, createForm);

  return el("div", {}, paycheckPanel, changePanel, templatePanel);
}
