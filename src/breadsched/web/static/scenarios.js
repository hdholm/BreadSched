// Scenarios: dated events, assumptions, and the scenario manager.

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
