// The Dashboard, FSA Dashboard, Dashboard groups, and net worth history.

async function showFsaDashboard() {
  const by = state.fsaClaimsBy || "status";
  const data = await get(`/api/fsa/dashboard?by=${encodeURIComponent(by)}`);
  const yearRows = data.years.map((item) => [
    item.account, `${item.start} – ${item.through}`, item.phase, money(item.election),
    money(item.funded), money(item.used), item.usage_text || "—", money(item.remaining),
    money(item.carried_in), money(item.carried_over), money(item.forfeited),
  ]);
  const claimRows = data.claims.map((claim) => [
    claim.service_date, claim.provider, claim.status, money(claim.paid),
    money(claim.reimbursed), money(claim.remaining),
    claim.attention.length
      ? el("span", { class:"neg" }, claim.attention.join("; ")) : "—",
    el("button", { class:"action", type:"button", onclick:()=>
      openFsaClaimsEditor(claim.handle).catch((error)=>say(error.message,"error")) },
    "Review claim"),
  ]);
  const report = data.report;
  const grouping = el("select", { name:"claims_by", onchange:(event)=>{
    state.fsaClaimsBy = event.target.value;
    render();
  } }, [["status", "Status"], ["account", "FSA account"], ["year", "Funding year"],
        ["provider", "Provider"]].map(([value, label]) => el("option", {
    value, selected:value === report.by ? "selected" : null }, label)));
  const proposals = data.proposals || [];
  const proposalChecks = proposals.map((item) => el("input", {
    type:"checkbox", checked:"checked", "aria-label":`Link ${item.description}`,
  }));
  const proposalRows = proposals.map((item, index) => [
    proposalChecks[index], item.date, item.description, money(item.amount),
    item.claim_label, item.role_label, item.reason,
  ]);
  const linkSelected = async () => {
    const links = proposals.filter((_item, index) => proposalChecks[index].checked)
      .map((item) => [item.claim, item.transaction, item.split]);
    if (!links.length) return;
    try {
      const result = await post("/api/fsa/claim-links/accept", { links });
      say(`Linked ${result.linked} claim link${result.linked === 1 ? "" : "s"}.`
        + (result.unchanged ? ` ${result.unchanged} no longer applied.` : ""));
      render();
    } catch (error) { say(error.message, "error"); }
  };
  const groupRow = (group) => [group.label, String(group.claims), money(group.net_paid),
    money(group.reimbursed), money(group.rejected), money(group.remaining),
    group.attention ? String(group.attention) : ""];
  return el("div", {},
    el("div", { class:"toolbar" },
      el("button", { class:"action", type:"button", onclick:()=>
        openFsaClaimsEditor().catch((error)=>say(error.message,"error")) },
      "Manage FSA claims…")),
    data.attention
      ? el("p", { class:"note neg" },
        `${data.attention} claim${data.attention === 1 ? " needs" : "s need"} attention.`)
      : null,
    proposalRows.length ? el("h2", {}, "Proposed claim links") : null,
    proposalRows.length ? table(["", "Date", "Transaction", {label:"Amount",num:true},
      "Claim", "As", "Why"], proposalRows) : null,
    proposalRows.length ? el("div", { class:"toolbar" },
      el("button", { class:"action primary", type:"button", onclick:linkSelected },
        "Link selected")) : null,
    el("h2", {}, "FSA benefit years"),
    yearRows.length ? table(["Account", "Funding year", "Status",
      {label:"Election",num:true},{label:"Funded",num:true},{label:"Used",num:true},"How used",
      {label:"Remaining",num:true},{label:"Carried in",num:true},
      {label:"Carried over",num:true},{label:"Forfeited",num:true}], yearRows)
      : el("p", { class:"note" }, "No open or recently closed FSA benefit years."),
    el("h2", {}, "Open FSA claims"),
    claimRows.length ? table(["Service date", "Provider", "Status",
      {label:"Paid",num:true},{label:"Reimbursed",num:true},
      {label:"Remaining",num:true}, "Needs attention", "Action"], claimRows)
      : el("p", { class:"note" }, "No open FSA claims."),
    el("h2", {}, "Claims report"),
    el("div", { class:"toolbar" }, el("label", {}, "Group by ", grouping)),
    report.groups.length
      ? table([grouping.selectedOptions[0].textContent, {label:"Claims",num:true},
          {label:"Net paid",num:true}, {label:"Reimbursed",num:true},
          {label:"Rejected",num:true}, {label:"Remaining",num:true},
          {label:"Attention",num:true}],
        [...report.groups.map(groupRow), groupRow(report.totals)])
      : el("p", { class:"note" }, "No FSA claims yet."));
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

// Net worth at each period end from the shared service; a missing quote withholds
// that point's totals and names the account instead of guessing a conversion.
async function netWorthHistory() {
  const period = state.netWorthPeriod || "month";
  const data = await get(`/api/net-worth-history?period=${period}`);
  const points = data.points;
  const choose = el("select", { onchange: (event) => {
    state.netWorthPeriod = event.target.value; render();
  } }, [["month", "Month"], ["quarter", "Quarter"], ["year", "Year"]].map(([value, label]) =>
    el("option", { value, selected: value === period ? "selected" : null }, label)));
  const amount = (value) => value === null ? "Missing quote" : money(value);
  const slot = el("div", { class: "net-worth-change-slot" });
  const note = (item) => {
    const text = [item.partial ? "to date" : "",
      item.missing.length ? `missing quote: ${item.missing.join(", ")}` : ""]
      .filter(Boolean).join("; ") || "—";
    return item.completeness?.status === "complete" ? text
      : el("details", {}, el("summary", {}, text),
        el("ul", {}, ...(item.completeness?.detail || []).map((line) => el("li", {}, line))));
  };
  return el("section", { class: "net-worth-history" },
    helpHeading("Net worth history", "net-worth"),
    el("p", { class: "note" }, "Assets less debts, market-valued at each period end "
      + `(the last one on ${data.as_of}). A missing quote leaves that point out of the `
      + "charts rather than guessing a conversion. The second chart stacks each group "
      + "of accounts, assets above zero and debts below, so each column adds up to net worth."),
    el("div", { class: "toolbar" }, el("label", {}, "Group by ", choose)),
    ...(data.charts || []).map((chart) => modelChart(chart, { collapseTable: true })),
    el("div", { class: "net-worth-points" }, table(["Period", "Valued on", { label: "Assets", num: true }, { label: "Debts", num: true },
      { label: "Net worth", num: true }, { label: "Change", num: true }, "Note"],
      points.map((item) => [
        el("details", {}, el("summary", {}, item.label),
          el("ul", {}, ...item.groups.map((line) => el("li", {},
            `${line.name} (${line.kind}): ${amount(line.value)}`)))),
        item.valued_on, amount(item.assets), amount(item.debts), amount(item.net_worth),
        el("button", { class: "action net-worth-explain", type: "button",
          title: "Show the postings behind this change",
          onclick: () => netWorthChange(item, slot) },
          item.change === null ? "Explain" : money(item.change)), note(item)]))),
    slot);
}

// The postings behind one history point's change, with the market and exchange-rate
// movement that reconciles them to it. The CSV is the shared export, so its totals
// are the ones shown here and printed with the page.
async function netWorthChange(point, slot) {
  const data = await get(`/api/net-worth-change?from=${point.start}&through=${point.end}`);
  const amount = (value) => value === null ? "Missing quote" : money(value);
  const download = () => {
    const link = el("a", { href: URL.createObjectURL(new Blob([data.csv], { type: "text/csv" })),
      download: `net-worth-change-${data.start}-${data.closing_on}.csv` });
    document.body.append(link);
    link.click();
    link.remove();
  };
  const notes = ["Each posting is the net of its splits in asset and debt accounts, converted "
    + "with the quote applicable on its date. Market and exchange-rate changes are the rest."];
  if (data.transfers) {
    notes.push(`${data.transfers} transfer(s) between your own accounts left out: `
      + "they do not change net worth.");
  }
  if (data.missing.length) {
    notes.push(`Missing quote: ${data.missing.join(", ")}. Totals are withheld.`);
  }
  const total = (label, value) => el("tr", { class: "total" },
    el("th", { colspan: "4" }, label), el("td", { class: "num" }, amount(value)));
  slot.replaceChildren(el("section", { class: "net-worth-change" },
    el("h3", {}, `Net worth change ${data.start} through ${data.closing_on}`
      + (data.partial ? " (to date)" : "")),
    ...notes.map((text) => el("p", { class: "note" }, text)),
    completenessDetails(data.completeness, "Change"),
    el("div", { class: "toolbar" },
      el("button", { class: "action", type: "button", onclick: download }, "Download CSV"),
      el("button", { class: "action", type: "button",
        onclick: () => slot.replaceChildren() }, "Close")),
    table(["Date", "Description", "Accounts", "Currency", { label: "Net worth effect", num: true }],
      [...data.postings.map((item) => [item.posted, item.description, item.accounts.join("; "),
        item.currency, amount(item.effect)]),
      total(`Opening net worth (${data.opening_on})`, data.opening),
      total("Postings", data.posted),
      total("Market and exchange-rate changes", data.revaluation),
      total(`Closing net worth (${data.closing_on})`, data.closing),
      total("Change", data.change)])));
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

  const short = s.emergency_shortfall !== null && Number(s.emergency_shortfall) > 0;
  const dashboardMoney = (field) => s[field] === null
    ? data.unavailable_reasons[field] : money(s[field]);
  const tile = (label, value, alarm) => el("div", { class: "card" },
    el("div", { class: "label" }, label),
    el("div", { class: alarm ? "value neg" : "value" }, value));

  const cards = el("div", { class: "cards" },
    tile("Net worth", dashboardMoney("net_worth")),
    tile("Liquid", dashboardMoney("liquid")),
    tile(`Needed in ${data.config.liquidity_days} days`, money(s.required_liquid)),
    tile("Available", dashboardMoney("available"), s.available !== null && Number(s.available) < 0),
    tile(`Emergency fund (${data.config.emergency_months} mo)`, dashboardMoney("emergency_fund")),
    tile("Committed emergency outgoings / mo", money(s.emergency_monthly_outgoings)),
    tile("Including estimates / mo", money(s.emergency_monthly_outgoings_with_estimates)),
    tile("Months covered", s.months_covered === null
      ? data.unavailable_reasons.months_covered : s.months_covered,
         s.months_covered !== null && Number(s.months_covered) < Number(data.config.emergency_months)),
    short ? tile("Short of the fund", dashboardMoney("emergency_shortfall"), true) : null,
    // Owed back on reimbursable expenses: net worth, never liquidity (#170).
    Number(s.receivables_owed) > 0
      ? tile("Reimbursements due", money(s.receivables_owed)
          + (Number(s.receivables_attention) > 0
            ? ` (${money(s.receivables_attention)} disputed or overdue)` : ""),
        Number(s.receivables_attention) > 0)
      : null,
    // Claims with something left to do; details on the FSA Dashboard.
    Number(s.fsa_claims_attention) > 0
      ? tile("FSA claims needing attention", String(s.fsa_claims_attention), true)
      : null,
    // Savings-goal earmarks are held from Available like bill reserves.
    Number(s.goals_set_aside) > 0
      ? tile("Set aside for goals", money(s.goals_set_aside)
          + (Number(s.goals_held) !== Number(s.goals_set_aside)
            ? ` (${money(s.goals_held)} held from spendable cash)` : ""))
      : null);

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
      title:[g.path, ...(g.members || [])].join("\n"),
    }, el("h3",{},g.name), g.note ? el("p", {class:"note"}, g.note) : null,
    completenessFlag(g.completeness), el("dl",{},
      g.value === null ? null : [el("dt",{},"Value"),el("dd",{},money(g.value))],
      g.debt === null ? null : [el("dt",{},"Owed"),el("dd",{},money(g.debt))],
      el("dt",{},g.equity === null ? "Total" : "Equity"),
      el("dd",{},(g.equity === null ? g.total : g.equity) === null
        ? "Unavailable" : money(g.equity === null ? g.total : g.equity)),
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
  // Missed occurrences of one schedule arrive grouped: one row with the date
  // range, count, and total, and the individual dates available on demand.
  const itemCell = (item) => item.missed
    ? el("details", {},
        el("summary", { ondblclick:()=>activatePending(item), title:"Double-click to view" },
          item.name),
        el("ul", {}, ...item.occurrences.map((entry) =>
          el("li", {}, `${entry.date}: ${money(entry.amount)}`))))
    : el("span", { ondblclick:()=>activatePending(item), title:"Double-click to view" },
        item.name);
  const dueCell = (item) => item.missed
    ? `${item.next_due} to ${item.last_due}` : item.next_due;
  const dueIn = (item) => {
    const late = item.days_until < 0 ? `${-item.days_until} days overdue`
      : (item.days_until === 0 ? "today" : `${item.days_until} days`);
    return item.missed ? `${item.missed} missed, ${late}` : late;
  };
  const bills = data.display_bills || [];
  const income = data.display_income || [];
  const billRows = bills.map((item) => [
    itemCell(item), dueCell(item), dueIn(item), item.frequency,
    money(item.amount), item.generated ? "" : money(item.monthly),
    money(item.hold), item.generated ? "" : money(item.annual),
    item.generated ? "Account payment" : "Committed",
  ]);
  const incomeRows = income.map((item) => [
    itemCell(item), dueCell(item), dueIn(item), item.frequency,
    money(item.amount), money(item.monthly), money(item.annual),
  ]);

  return el("div", {}, cards,
    completenessDetails(data.completeness, "Net worth"),
    data.liquid_completeness?.status !== "complete"
      && JSON.stringify(data.liquid_completeness?.excluded)
        !== JSON.stringify(data.completeness?.excluded)
      ? completenessDetails(data.liquid_completeness, "Liquid cash") : null,
    ...(data.coverage_notes || []).map((note) => el("p", { class: "note" }, note)), controls,
    el("h2", {}, "Balances"),
    groupCards,
    el("h2", {}, `Pending bills (${bills.length})`),
    ...(bills.some((item) => item.days_until <= 0) || income.some((item) => item.days_until <= 0)
      ? [el("div", { class:"toolbar" }, el("button", { class:"action", type:"button",
          onclick:() => openDueReviewDialog().catch((error) => say(error.message, "error")) },
          "Review due transactions…"))]
      : []),
    table(["Item", "Due", "Due in", "Frequency",
           { label: "Amount", num: true }, { label: "Monthly", num: true },
           { label: "Hold now", num: true }, { label: "Annual", num: true }, "Kind"],
          billRows),
    el("h2", {}, `Expected income (${income.length})`),
    table(["Item", "Due", "Due in", "Frequency",
           { label: "Amount", num: true }, { label: "Monthly", num: true },
           { label: "Annual", num: true }], incomeRows),
    ...((data.goals || []).length
      ? [el("h2", {}, `Savings goals (${data.goals.length})`),
        el("div", { class: "dashboard-goals" },
          table(["Goal", "Target date", { label: "Target", num: true },
                 { label: "Set aside", num: true }, { label: "Remaining", num: true }, "Status"],
            data.goals.map((goal) => [
              el("button", { class: "action", type: "button", onclick: () => switchTo("Goals") },
                goal.name),
              goal.target_date, money(goal.target), money(goal.set_aside),
              money(goal.remaining), goal.status_text])))]
      : []),
    await netWorthHistory());
}
