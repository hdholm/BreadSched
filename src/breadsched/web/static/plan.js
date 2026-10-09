// The Plan view, its Expense Explorer, and Plan detail.

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

function spendingOverTime(data, periodIndex, kind = "spending") {
  // Total plan and actual per period; each period is a button that selects it.
  const points = data[kind] || [];
  const income = kind === "income";
  const values = points.flatMap((item) => [Number(item.planned), Number(item.actual)]);
  const low = Math.min(0, ...values);
  const high = Math.max(1, ...values);
  const width = 600, top = 10, bottom = 120;
  const step = 560 / Math.max(1, points.length - 1);
  const x = (index) => 20 + index * step;
  const y = (value) => bottom - (bottom - top) * (Number(value) - low) / (high - low);
  const svg = svgEl("svg", { viewBox: `0 0 ${width} 150`, role: "img",
    class: income ? "income-chart" : "spending-chart",
    "aria-label": `Total ${income ? "income" : "expense"} plan and actual by period` });
  const asOf = points.findIndex((item) => item.future);
  if (asOf > 0) {
    svg.append(svgEl("line", { x1: x(asOf) - step / 2, x2: x(asOf) - step / 2, y1: top,
      y2: bottom, stroke: "#888", "stroke-dasharray": "4 3" }));
  }
  for (const [key, color] of [["planned", "#2563a4"], ["actual", "#bd5824"]]) {
    svg.append(svgEl("path", { fill: "none", stroke: color, "stroke-width": "3",
      d: points.map((item, i) => `${i ? "L" : "M"}${x(i)},${y(item[key])}`).join(" ") }));
  }
  points.forEach((item, i) => {
    const hit = svgEl("rect", { x: x(i) - step / 2, y: 0, width: step, height: 150,
      fill: i === periodIndex ? "rgba(37,99,164,0.10)" : "transparent",
      class: "spending-period", tabindex: "0", role: "button",
      "aria-label": `${item.label}: plan ${item.planned}, actual ${item.actual}` });
    const choose = () => { state.expenseIndex = i; render(); };
    hit.addEventListener("click", choose);
    hit.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(); }
    });
    svg.append(hit);
  });
  const categories = points.length ? points[0].categories : [];
  const slot = el("div", { class: "net-worth-change-slot" });
  const note = (item) => [item.partial ? "to date" : "", item.future ? "future" : "",
    item.currency_incomplete ? (item.completeness?.label || "missing quote") : ""]
    .filter(Boolean).join(", ");
  return el("div", { class: income ? "income-over-time" : "spending-over-time" },
    el("h3", {}, income ? "Income over time" : "Spending over time"),
    el("p", { class: "note" }, `Blue: total plan · Orange: total actual. Actual is posted `
      + `through ${data.as_of}; the dashed line marks the first future period. `
      + (income ? "Income is split by top-level income category; selecting a period also "
        + "selects it for the expense comparison."
        : "Select a period to compare its categories and merchants.")),
    svg,
    table(["Period", {label:"Plan",num:true}, {label:"Actual",num:true},
      ...categories.map((item) => ({label:item.name, num:true})), "Note"],
      points.map((item, i) => el("tr", { class: i === periodIndex ? "selected" : null },
        el("td", {}, el("button", { class: "action", type: "button",
          onclick: () => { state.expenseIndex = i; render(); } }, item.label)),
        el("td", {class:"num"}, String(item.planned)),
        el("td", {class:"num"}, String(item.actual)),
        ...item.categories.map((part) => el("td", {class:"num"}, String(part.actual))),
        el("td", {class:"muted"}, note(item) || "—")))));
}

async function incomeDetail(data, params, index) {
  // The dated planned occurrences and actual receipts behind one income period.
  const choices = data.income_categories || [];
  if (!choices.length || !(data.income || []).length) return null;
  const selected = choices.find((item) => item.account === state.incomeCategory) || choices[0];
  const detailParams = new URLSearchParams(params);
  detailParams.set("account", selected.account);
  detailParams.set("index", String(index));
  const detail = (await get(`/api/expense-explorer?${detailParams}`)).drilldown;
  const pick = el("select", { onchange: (event) => {
    state.incomeCategory = event.target.value; render();
  } }, choices.map((item) => el("option", {
    value: item.account, selected: item.account === selected.account ? "selected" : null,
  }, item.full_name)));
  return el("div", { class: "income-detail" },
    el("h3", {}, `${selected.full_name} — ${detail.period.label}`),
    el("div", { class: "toolbar" }, el("label", {}, "Income detail ", pick)),
    table(["Planned date", "Scheduled", {label:"Expected",num:true}],
      detail.planned.map((item) => [item.date, item.description, String(item.expected)])),
    table(["Payer", {label:"Actual",num:true}, "Received"],
      detail.merchants.map((group) => el("tr", {},
        el("td", {}, group.name), el("td", {class:"num"}, String(group.amount)),
        el("td", {}, group.transactions.map((item) => el("div", {},
          `${item.date} · ${item.description || "Unknown payer"} · ${item.amount}`)))))),
    el("p", {class:"note"}, `Planned ${detail.period.planned}; actual ${detail.period.actual}.`));
}

async function expenseExplorerPanel(currentPlan) {
  const params = new URLSearchParams({
    from: currentPlan.from, through: currentPlan.through, period: currentPlan.period,
  });
  if (currentPlan.scenario) params.set("scenario", currentPlan.scenario);
  if (state.expenseRollover) params.set("rollover", "1");
  const data = await get(`/api/expense-explorer?${params}`);
  const choices = data.categories;
  const selected = choices.find((item) => item.account === state.expenseCategory) || choices[0];
  const index = Math.min(state.expenseIndex, Math.max(0, data.totals.length - 1));
  const panel = el("section", { class: "expense-explorer" },
    helpHeading("Expense Explorer", "expenses"),
    el("p", { class: "note" },
      "Blue: category plan · Orange: full-period actual. Remaining uses actual through today. Merchant groups use actuals only."));
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
  } }, [["actual", "Period actual"], ["planned", "Plan"], ["variance", "Period variance"],
    ["name", "Category"]].map(([value, label]) => el("option", {
    value, selected: value === state.expenseSort ? "selected" : null,
  }, label)));
  panel.append(el("div", { class: "toolbar" },
    el("label", {}, "Period ", periodSelect),
    el("label", {}, "Category trend ", categorySelect),
    el("label", {}, "Sort by ", sortSelect),
    el("label", {}, el("input", { type:"checkbox", checked:state.expenseRollover,
      onchange:(event)=>{ state.expenseRollover = event.target.checked; render(); } }),
      " Carry prior periods")));
  panel.append(spendingOverTime(data, index));
  if ((data.income || []).length) {
    panel.append(spendingOverTime(data, index, "income"));
    const detail = await incomeDetail(data, params, index);
    if (detail) panel.append(detail);
  }
  const ordered = [...choices].sort((a, b) => state.expenseSort === "name"
    ? a.full_name.localeCompare(b.full_name)
    : Number(b.periods[index][state.expenseSort] || 0)
      - Number(a.periods[index][state.expenseSort] || 0));
  panel.append(expenseBars(ordered, index),
    table(["Category", {label:"Plan",num:true}, {label:"Period actual",num:true},
      {label:"Period variance",num:true}, {label:"Carry in",num:true},
      {label:"Remaining",num:true}], ordered.map((item) => el("tr", {},
      el("td", {}, item.full_name),
      ...["planned", "actual", "variance"].map((key) => el("td", {class:"num"},
        item.periods[index][key] == null ? "—" : String(item.periods[index][key]))),
      el("td", {class:"num"}, item.periods[index].carry_in == null
        ? "—" : String(item.periods[index].carry_in)),
      el("td", {class:"num"}, item.periods[index].remaining == null
        ? item.periods[index].remaining_reason || "—" : String(item.periods[index].remaining))))));
  panel.append(el("h3", {}, `${selected.full_name} trend`), expenseTrend(selected),
    table(["Period", {label:"Plan",num:true}, {label:"Period actual",num:true},
      {label:"Period variance",num:true}, {label:"Carry in",num:true},
      {label:"Remaining",num:true}], selected.periods.map((item) => el("tr", {},
      el("td", {}, item.label), ...["planned", "actual", "variance"].map((key) =>
        el("td", {class:"num"}, item[key] == null ? "—" : String(item[key]))),
      el("td", {class:"num"}, item.carry_in == null ? "—" : String(item.carry_in)),
      el("td", {class:"num"}, item.remaining == null
        ? item.remaining_reason || "—" : String(item.remaining))))));
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
    el("p", {class:"note"}, `Category plan ${detail.period.planned}; full-period actual ${detail.period.actual}; `
      + `actual through today ${detail.period.actual_to_date == null ? "—" : detail.period.actual_to_date}; `
      + `carry in ${detail.period.carry_in == null ? "—" : detail.period.carry_in}; `
      + `variance ${detail.period.variance == null ? "—" : detail.period.variance}. `
      + `Remaining ${detail.period.remaining == null ? detail.period.remaining_reason : detail.period.remaining}. `
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
    [["planned", "Plan"], ["actual", "Period actual"], ["variance", "Period variance"]].map(([value, label]) =>
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
  const horizon = data.completeness?.horizon;
  const throughAsOf = data.completeness?.through_as_of;
  const cards = el("div", { class: "cards" },
    [["Opening spendable cash", summary.opening_cash, null, false, horizon],
     ["Ending spendable cash", summary.ending_cash, null, false, horizon],
     [`Lowest spendable cash (${summary.minimum_cash_date})`, summary.minimum_cash, null, false,
       horizon],
     ["Projected change in spendable cash", summary.planned_cash,
       summaryDelta?.planned_cash_delta, true, horizon],
     ["Planned change through as-of date", summary.planned_cash_through_as_of, null, true,
       throughAsOf],
     ["Actual change through as-of date", summary.actual_cash,
       summaryDelta?.actual_cash_delta, true, throughAsOf],
     ["Variance through as-of date", summary.variance,
       summaryDelta?.variance_delta, true, throughAsOf]].map(
      ([label, value, delta, signed, coverage]) =>
      el("div", { class: "card" },
        el("div", { class: "label" }, label),
        el("div", { class: `value ${Number(value) < 0 ? "neg" : ""}` },
          value == null ? "Not applicable" : signed ? signedMoney(value) : money(value)),
        value == null ? null : completenessFlag(coverage),
        comparison && delta != null ? el("span", {
          class: `plan-delta ${Number(delta) < 0 ? "neg" : ""}`,
        }, `Δ vs ${comparison.name}: ${money(delta)}`,
          completenessFlag(comparison.completeness, " — ")) : null)),
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
    ...data.periods.map((item) => ({
      label: item.completeness?.status === "partial" ? `${item.label} (partial)` : item.label,
      num: true,
    })),
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
              value != null && category.complete && !category.complete[index]
                ? el("span", {
                  class: "completeness-flag",
                  title: (data.periods[index].completeness?.detail || []).join("\n"),
                }, " partial") : null,
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
    completenessDetails(horizon, "Plan totals"),
    ...((data.goal_milestones || []).length
      ? [el("section", { class: "plan-goals" },
        el("h2", {}, "Savings goals reaching their target"),
        ...data.goal_milestones.map((item) => el("p", { class: "note" }, item.text)))]
      : []),
    ...((data.reimbursable?.categories || []).length
      ? [el("section", { class: "plan-reimbursable" },
        el("h2", {}, data.reimbursable.heading),
        ...data.reimbursable.categories.map((item) => el("p", { class: "note" }, item.text)))]
      : []),
    ...(data.currency?.notes || []).map((line) => el("p", {
      class: `note plan-currency-note${line.startsWith("Not included") ? " neg" : ""}`,
    }, line)),
    ...(comparison?.currency_notes || [])
      .filter((line) => !(data.currency?.notes || []).includes(line))
      .map((line) => el("p", { class: "note plan-currency-note" }, `${comparison.name}: ${line}`)),
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
      + "and have no mixed grand total. Period variance totals include only periods that "
      + "have started; the through-as-of cards stop both plan and actual at the as-of date."),
    inheritedAssumptions ? el("p", { class: "note plan-method-note" },
      `${inheritedAssumptions} annual assumption(s) inherited through the parent chain.`) : null,
    comparedInheritedAssumptions ? el("p", { class: "note plan-method-note" },
      `${comparison.name} inherits ${comparedInheritedAssumptions} annual assumption(s) through its parent chain.`)
      : null,
    detailOption,
    el("div", { class: "toolbar screen-only" },
      el("button", { class: "action", type: "button",
        onclick: () => openBudgetJars(data.controls.from, data.controls.through,
          data.controls.period).catch((error) => say(error.message, "error")) },
      "Budget jars…")),
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
      ...(summary.cost_text ? [el("p", { class: "note plan-detail-cost" }, summary.cost_text)] : []),
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

// Budget jars: each schedule and goal filled from income and drawn by actuals,
// bundled by account; dated events grouped by period; prints on its own.
async function openBudgetJars(from, through, period) {
  const query = new URLSearchParams({ from, through, period });
  const data = await get(`/api/budget-jars?${query}`);
  document.querySelector(".jars-backdrop")?.remove();
  const backdrop = el("div", {
    class: "detail-backdrop jars-backdrop printable-dialog",
    onclick: (event) => { if (event.target === backdrop) backdrop.remove(); },
  });
  const choice = el("select", { "aria-label": "Group by",
    onchange: () => openBudgetJars(data.from, data.through, choice.value)
      .catch((error) => say(error.message, "error")) },
    ...["month", "quarter", "year"].map((value) => el("option", { value }, value)));
  choice.value = data.period;
  const num = (label) => ({ label, num: true });
  const periodTable = (periods) => table(
    ["Period", num("Filled"), num("Planned"), num("Actual"), num("Variance"), num("Level")],
    periods.map((item) => el("tr", {},
      el("td", {}, item.label),
      el("td", { class: "num" }, money(item.filled)),
      el("td", { class: "num" }, money(item.planned)),
      el("td", { class: "num" }, money(item.actual)),
      el("td", { class: cls(item.variance) }, money(item.variance)),
      el("td", { class: cls(item.level) }, money(item.level)))));
  const printDialog = () => {
    document.body.classList.add("printing-dialog");
    const done = () => {
      document.body.classList.remove("printing-dialog");
      window.removeEventListener("afterprint", done);
    };
    window.addEventListener("afterprint", done);
    window.print();
  };
  const sections = data.accounts.map((bundle) => el("section", { class: "jar-account" },
    el("h3", {}, bundle.currency ? `${bundle.name} (${bundle.currency})` : bundle.name),
    el("p", { class: "note" }, "Jars: " + bundle.jars.map((jar) =>
      `${jar.name} (${jar.kind_label.toLowerCase()})`).join("; ")),
    periodTable(bundle.periods),
    ...(bundle.charts || []).map(modelChart),
    el("details", { class: "screen-only" }, el("summary", {}, "Each jar"),
      ...bundle.jars.map((jar) => el("div", {},
        el("h4", {}, `${jar.name} — ${jar.kind_label}`), periodTable(jar.periods))))));
  backdrop.append(el("section", { class: "detail-dialog jars-dialog" },
    helpHeading("Budget jars", "jars"),
    el("p", { class: "note" }, "Each scheduled payment and estimate, and each savings goal, "
      + "is a jar. It fills from each income in its cycle by that income's share, and is "
      + "drawn by the actual transaction matched to it. Periods only group dated events; "
      + "a level carries in every earlier fill and draw."),
    el("div", { class: "toolbar screen-only" },
      el("span", {}, `${data.from} through ${data.through}`),
      el("label", {}, " Group by ", choice)),
    ...data.totals.map((total) => el("section", {},
      el("h3", {}, total.currency ? `All jars (${total.currency})` : "All jars"),
      periodTable(total.periods))),
    ...(sections.length ? sections : [el("p", { class: "note" },
      "No scheduled payments, estimates, or goals in this range.")]),
    ...data.problems.map((problem) => el("p", { class: "note neg" }, problem)),
    el("div", { class: "toolbar screen-only" }, el("span", { class: "spacer" }),
      el("button", { class: "action", type: "button", onclick: printDialog }, "Print"),
      el("button", { class: "action", type: "button", onclick: () => backdrop.remove() },
        "Close"))));
  document.body.append(backdrop);
}
