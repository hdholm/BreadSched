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
    el("h2", {}, "FSA claims"),
    el("p", { class:"note" }, "Every claim in a benefit year that can still be claimed, "
      + "settled or not, and any older claim still open."),
    claimRows.length ? table(["Service date", "Provider", "Status",
      {label:"Paid",num:true},{label:"Reimbursed",num:true},
      {label:"Remaining",num:true}, "Needs attention", "Action"], claimRows)
      : el("p", { class:"note" }, "No FSA claims in open benefit years."),
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

// Dashboard tables and their columns, as Configure Dashboard lists them (#381).
// Which columns show is each browser's own choice (localStorage), never the book's,
// as the desktop keeps it in each user's settings; the first column always shows.
const DASHBOARD_TABLES = {
  dashboard: { title:"Pending bills", headers:["Item", "Due", "Due in", "Frequency",
    { label:"Amount", num:true }, { label:"Monthly", num:true },
    { label:"In the jar", num:true }, { label:"Annual", num:true }, "Kind"] },
  "dashboard-income": { title:"Expected income", headers:["Item", "Due", "Due in", "Frequency",
    { label:"Amount", num:true }, { label:"Monthly", num:true },
    { label:"Annual", num:true }] },
  "dashboard-reimbursables": { title:"Pending reimbursables", headers:["Expense", "Payer",
    "Incurred", "Expected", { label:"Still owed", num:true }, "Status"] },
  "dashboard-goals": { title:"Savings goals", headers:["Goal", "Target date",
    { label:"Target", num:true }, { label:"In the jar", num:true },
    { label:"Remaining", num:true }, "Status"] },
};

function dashboardHiddenColumns(id) {
  try {
    const stored = JSON.parse(localStorage.getItem(`breadsched.dashboard.columns.${id}`) || "[]");
    return Array.isArray(stored) ? stored : [];
  } catch (_error) { return []; }
}

function setDashboardHiddenColumns(id, hidden) {
  try { localStorage.setItem(`breadsched.dashboard.columns.${id}`, JSON.stringify(hidden)); }
  catch (_error) { /* private browsing: the choice lasts this page only */ }
}

// A Dashboard table showing only its chosen columns.

function openDashboardConfigEditor(config) {
  const backdrop = el("div", { class:"detail-backdrop" });
  const numberField = (value, [min, max]) => el("input", {
    type:"number", min:String(min), max:String(max), value:String(value) });
  const liquidity = numberField(config.liquidity_days, [1, 365]);
  const emergency = numberField(config.emergency_months, [1, 36]);
  const sectionChecks = Object.entries(config.section_titles).map(([name, title]) => {
    const check = el("input", { type:"checkbox", name:`section-${name}`,
      checked:config.sections[name] ? "checked" : null });
    check._section = name;
    return el("label", { class:"check" }, check, ` ${title}`);
  });

  const names = new Map(config.accounts.map((account) => [account.handle, account.name]));
  const byName = new Map(config.accounts.map((account) => [account.name, account.handle]));
  const listId = "dashboard-config-accounts";
  const datalist = el("datalist", { id:listId },
    config.accounts.map((account) => el("option", { value:account.name })));
  const status = el("p", { class:"note", role:"status" });
  const rows = el("div", { class:"stack" });
  const kinds = [
    ["liquid", "Liquid"], ["retirement", "Retirement"], ["asset", "Asset"],
    ["property", "Property"], ["liability", "Liability"],
  ];
  const groupRows = () => Array.from(rows.children);
  const removeAccount = (row, handle) => {
    row._accounts = row._accounts.filter((item) => item !== handle);
    row._members.querySelector(`[data-handle="${CSS.escape(handle)}"]`)?.remove();
  };
  const addAccount = (row, handle) => {
    if (!names.has(handle) || row._accounts.includes(handle)) return;
    status.textContent = "";
    for (const other of groupRows()) {
      if (other !== row && other._accounts.includes(handle)) {
        removeAccount(other, handle);
        status.textContent = `Moved ${names.get(handle)} from `
          + `${other._groupFields.name.value.trim() || "another group"}.`;
      }
    }
    row._accounts.push(handle);
    row._members.append(el("li", { "data-handle":handle }, names.get(handle), " ",
      el("button", { class:"action", type:"button", title:`Remove ${names.get(handle)}`,
        "aria-label":`Remove ${names.get(handle)}`, onclick:()=>removeAccount(row, handle) },
      "×")));
  };
  const addGroup = (source=null) => {
    const name = el("input", { placeholder:"Investments:Plan A", value:source?.name || "" });
    const kind = el("select", {}, kinds.map(([value, label]) => el("option", {
      value, selected:value === (source?.kind || "asset") ? "selected" : null,
    }, label)));
    // Type part of a name to search the accounts; the group lists only its own.
    const picker = el("input", { list:listId, placeholder:"Add an account…",
      "aria-label":"Add an account" });
    const members = el("ul", { class:"group-members" });
    const row = el("div", { class:"card" },
      el("div", { class:"row" }, name, kind,
        el("button", { class:"action", type:"button", onclick:()=>row.remove() }, "Remove group")),
      members,
      el("div", { class:"row" }, picker, el("button", { class:"action", type:"button",
        onclick:()=>{
          const handle = byName.get(picker.value);
          if (!handle) { status.textContent = "Choose an account from the list."; return; }
          addAccount(row, handle);
          picker.value = "";
        } }, "Add")));
    row._groupFields = { name, kind };
    row._members = members;
    row._accounts = [];
    rows.append(row);
    (source?.accounts || []).forEach((handle) => addAccount(row, handle));
  };
  (config.groups || []).forEach(addGroup);

  // Column choices apply at once and stay in this browser.
  const columnChoices = Object.entries(DASHBOARD_TABLES).map(([id, spec]) => {
    const hidden = new Set(dashboardHiddenColumns(id));
    return el("fieldset", {}, el("legend", {}, spec.title),
      ...spec.headers.map((header, index) => {
        const label = header.label ?? header;
        const check = el("input", { type:"checkbox", name:`column-${id}-${index}`,
          checked:hidden.has(label) ? null : "checked", disabled:index === 0 ? "disabled" : null,
          onchange:(event)=>{
            const now = new Set(dashboardHiddenColumns(id));
            if (event.target.checked) now.delete(label); else now.add(label);
            setDashboardHiddenColumns(id, Array.from(now));
          } });
        return el("label", { class:"check" }, check, ` ${label}`);
      }));
  });

  const save = async () => {
    const groups = groupRows().map((row) => ({
      name:row._groupFields.name.value.trim(),
      kind:row._groupFields.kind.value,
      accounts:row._accounts,
    }));
    if (groups.some((group) => !group.name)) {
      say("Every dashboard group needs a name.", "error");
      return;
    }
    const sections = {};
    for (const label of sectionChecks) {
      const check = label.querySelector("input");
      sections[check._section] = check.checked;
    }
    try {
      await post("/api/dashboard/config", {
        groups, sections,
        liquidity_days:Number(liquidity.value), emergency_months:Number(emergency.value),
      });
    } catch (error) { say(error.message, "error"); return; }
    backdrop.remove();
    render();
  };
  backdrop.append(el("section", { class:"detail-dialog wide dashboard-config" },
    el("div", { class:"detail-heading" }, helpHeading("Configure Dashboard", "dashboard")),
    el("h3", {}, "Horizons"),
    el("div", { class:"row" }, el("label", {}, "Liquid for ", liquidity, " days of bills")),
    el("div", { class:"row" },
      el("label", {}, "Emergency fund covers ", emergency, " months of outgoings")),
    el("h3", {}, "Sections"),
    el("div", { class:"stack" }, sectionChecks),
    el("p", { class:"note" }, "The summary cards are always shown. Pending reimbursables "
      + "lists each reimbursable expense still owed, as Pending bills lists bills."),
    el("h3", {}, "Groups"),
    el("p", { class:"note" },
      "Colon-separated names create totalled headings. Choosing a parent account includes its subaccounts once."),
    datalist,
    rows,
    el("div", { class:"toolbar" },
      el("button", { class:"action", type:"button", onclick:()=>addGroup() }, "Add group")),
    status,
    el("h3", {}, "Columns"),
    el("p", { class:"note" }, "Your own choice, kept in this browser rather than in the book; "
      + "it applies as soon as you tick a box."),
    ...columnChoices,
    el("div", { class:"toolbar" },
      el("button", { class:"action primary", type:"button", onclick:save }, "Save"),
      el("button", { class:"action", type:"button", onclick:()=>{ backdrop.remove(); render(); } },
        "Cancel"))));
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

// The dashboard: every item on it is a dashboard breadlet's document (the summary
// figures, the cash-flow chart, groups, bills, income, reimbursables, FSA claims,
// goals, and any other the household shows), sent together with /api/dashboard.
// Their rows and cards link to what they are; followDashboardLink opens it. The two
// horizons, the items shown, the groups, and the columns are set in Configure
// Dashboard (#381).
//
// The items sit on a grid (engine.dashboard_grid) as tiles: each has a place in the
// order, a width in columns and a height in rows, and scrolls inside its tile when
// it holds more. The CSS grid packs them (row dense), as the desktop does; a narrow
// window has fewer columns. Arrange lets the household move them (dragging a tile by
// its title, or with its arrows), resize them (dragging its corner, or with its
// width and height), hide them, and add hidden ones back; Done saves the
// arrangement with the rest of the Dashboard's settings.
async function showDashboard() {
  const data = await get("/api/dashboard");
  const config = data.config;
  const grid = config.grid;
  // Each item's drawn document (or failure), by key; a hidden one added while
  // arranging is fetched when it is added.
  const drawn = new Map();
  const byId = new Map(config.breadlets.map((item) => [item.id, item]));
  for (const item of data.documents || []) {
    const key = byId.get(item.id)?.key;
    if (!key) continue;
    if (item.error) {
      drawn.set(key, el("p", { class: "note neg" }, `${item.name}: ${item.error}`));
      continue;
    }
    const node = renderDocument(item.document, { titled: false,
      onLink: (link) => { if (!arranging) followDashboardLink(link); },
      hiddenColumns: dashboardHiddenColumns });
    // An item with nothing to show takes no room, until it is being arranged.
    node.dataset.empty = node.children.length ? "" : "true";
    node.classList.add("dashboard-breadlet", `dashboard-${item.id.replaceAll("_", "-")}`);
    node.dataset.breadlet = item.id;
    drawn.set(key, node);
  }
  let arranging = false;
  let items = config.breadlets.map((item) => ({ ...item }));
  const page = el("div", { class: "dashboard" });
  const board = el("div", { class: "dashboard-grid",
    style: `--row-height: ${grid.row_height}px` });
  const status = el("span", { class: "note neg", role: "status" });
  const indexOf = (key) => items.findIndex((item) => item.key === key);
  const clamp = (value, most) => Math.min(most, Math.max(1, Math.round(value)));

  const moveBefore = (key, target, after = false) => {
    if (key === target) return;
    const [item] = items.splice(indexOf(key), 1);
    items.splice(indexOf(target) + (after ? 1 : 0), 0, item);
    draw();
  };
  const move = (key, step) => {
    const shown = items.filter((item) => item.shown).map((item) => item.key);
    const position = shown.indexOf(key) + step;
    if (position >= 0 && position < shown.length) moveBefore(key, shown[position], step > 0);
  };
  const resize = (key, width, height) => {
    const item = items[indexOf(key)];
    const next = { width: clamp(width ?? item.width, grid.columns),
      height: clamp(height ?? item.height, grid.max_rows) };
    if (next.width === item.width && next.height === item.height) return;
    Object.assign(item, next);
    draw();
  };
  const showItem = async (key, shown) => {
    const index = indexOf(key);
    const item = { ...items[index], shown };
    if (shown) {
      items.splice(index, 1);
      items.push(item);
      if (!drawn.has(key)) {
        try {
          const found = await runBreadlet("dashboard", item.id);
          const node = renderDocument(found.document, { titled: false,
            hiddenColumns: dashboardHiddenColumns });
          node.classList.add("dashboard-breadlet");
          node.dataset.breadlet = item.id;
          drawn.set(key, node);
        } catch (error) {
          drawn.set(key, el("p", { class: "note neg" }, `${item.name}: ${error.message}`));
        }
      }
    } else items[index] = item;
    draw();
  };

  // Resizing by a tile's corner: the change in columns and rows the pointer has
  // moved, from where it started.
  const startResize = (event, key) => {
    event.preventDefault();
    const item = items[indexOf(key)];
    const start = { x: event.clientX, y: event.clientY, width: item.width, height: item.height };
    const columns = getComputedStyle(board).gridTemplateColumns.split(" ").length;
    const gap = parseFloat(getComputedStyle(board).columnGap) || 12;
    const column = (board.clientWidth - gap * (columns - 1)) / columns;
    const onMove = (moved) => resize(key,
      start.width + (moved.clientX - start.x) / (column + gap),
      start.height + (moved.clientY - start.y) / (grid.row_height + gap));
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const tileHeader = (item) => {
    const sizeInput = (side, most, label) => el("input", { type: "number", min: "1",
      max: String(most), value: String(item[side]), class: `tile-${side}`,
      "aria-label": `${label}: ${item.name}`,
      onchange: (event) => resize(item.key, side === "width" ? Number(event.target.value) : null,
        side === "height" ? Number(event.target.value) : null) });
    const button = (text, label, onclick) => el("button", { type: "button", class: "action",
      title: label, "aria-label": label, onclick }, text);
    return el("div", { class: "tile-header" },
      el("span", { class: "tile-handle", draggable: "true", title: `Drag to move ${item.name}`,
        ondragstart: (event) => {
          event.dataTransfer.setData("text/plain", item.key);
          event.dataTransfer.effectAllowed = "move";
        } }, el("span", { "aria-hidden": "true" }, "⠿ "), item.name),
      button("◀", `Move earlier: ${item.name}`, () => move(item.key, -1)),
      button("▶", `Move later: ${item.name}`, () => move(item.key, 1)),
      sizeInput("width", grid.columns, "Width in columns"), " × ",
      sizeInput("height", grid.max_rows, "Height in rows"),
      button("×", `Hide ${item.name}`, () => showItem(item.key, false)));
  };

  const tile = (item) => {
    const body = drawn.get(item.key);
    if (!body || (!arranging && body.dataset.empty === "true")) return null;
    const node = el("section", { class: `dashboard-tile w${item.width} h${item.height}`
      + (arranging ? " arranging" : ""), "data-key": item.key, "data-breadlet": item.id },
    arranging ? tileHeader(item) : null,
    el("div", { class: "tile-body" }, body,
      arranging && body.dataset.empty === "true"
        ? el("p", { class: "note" }, "Nothing to show right now.") : null),
    arranging ? el("span", { class: "resize-grip", title: `Drag to resize ${item.name}`,
      onpointerdown: (event) => startResize(event, item.key) }) : null);
    if (arranging) {
      node.addEventListener("dragover", (event) => event.preventDefault());
      node.addEventListener("drop", (event) => {
        event.preventDefault();
        const key = event.dataTransfer.getData("text/plain");
        if (indexOf(key) < 0) return;
        const box = node.getBoundingClientRect();
        moveBefore(key, item.key, event.clientX > box.left + box.width / 2);
      });
    }
    return node;
  };

  const arrangeBar = () => {
    const hidden = items.filter((item) => !item.shown);
    const adder = el("select", { "aria-label": "Add an item", disabled: hidden.length ? null : "disabled",
      onchange: (event) => { if (event.target.value) showItem(event.target.value, true); } },
    el("option", { value: "" }, hidden.length ? "Add an item…" : "Every item is shown"),
    ...hidden.map((item) => el("option", { value: item.key, title: item.description }, item.name)));
    return el("div", { class: "toolbar dashboard-arrange-bar" },
      el("span", { class: "note" }, "Drag an item by its title to move it, or its corner to resize it."),
      adder,
      el("button", { class: "action", type: "button", onclick: () => {
        const sizes = new Map(config.defaults.map((tile) => [tile.key, tile]));
        const known = new Map(items.map((item) => [item.key, item]));
        items = [...config.defaults.filter((tile) => known.has(tile.key)).map((tile) =>
          ({ ...known.get(tile.key), width: tile.width, height: tile.height })),
        ...items.filter((item) => !sizes.has(item.key))];
        draw();
      } }, "Reset layout"),
      el("button", { class: "action", type: "button", onclick: () => render() }, "Cancel"),
      el("button", { class: "action primary", type: "button", onclick: async () => {
        try {
          await post("/api/dashboard/config", {
            layout: items.map(({ key, width, height }) => ({ key, width, height })),
            sections: Object.fromEntries(items.map((item) => [item.key, item.shown])),
          });
        } catch (error) { status.textContent = error.message; return; }
        render();
      } }, "Done"),
      status);
  };

  // Done and Cancel finish arranging, so Arrange waits until then.
  const controls = () => el("div", { class: "toolbar" },
    el("button", { class:"action", type:"button", disabled: arranging ? "disabled" : null,
      onclick:()=>openDashboardConfigEditor(config) }, "Configure Dashboard…"),
    el("button", { class:"action", type:"button", disabled: arranging ? "disabled" : null,
      title: "Move, resize, hide and add Dashboard items",
      onclick: () => { arranging = true; draw(); } }, "Arrange"));

  // Each chart is drawn again as tall as its tile has room for, so a wide, short tile
  // keeps legible text rather than shrinking the whole chart (fitCharts).
  const fitting = new ResizeObserver((entries) => entries.forEach((entry) =>
    fitCharts(entry.target)));
  function draw() {
    fitting.disconnect();
    board.replaceChildren(...items.filter((item) => item.shown).map(tile).filter(Boolean));
    board.querySelectorAll(".tile-body").forEach((body) => fitting.observe(body));
    board.classList.toggle("arranging", arranging);
    page.replaceChildren(...[controls(), arranging ? arrangeBar() : null, board].filter(Boolean));
  }
  draw();
  return page;
}

// Draw each chart in a tile again at the size its tile gives it (the room left by the
// rest of the item, never less than reads well), so its text stays the size of the
// page's rather than shrinking with a narrow tile or growing with a wide one.
function fitCharts(body) {
  for (const figure of body.querySelectorAll("figure.model-chart")) {
    const svg = figure.querySelector("svg");
    if (!figure.chartModel || !svg || !svg.clientWidth) continue;
    const width = Math.round(svg.clientWidth), height = Math.round(Math.max(120, svg.clientHeight));
    const [, , drawnWidth, drawnHeight] = (svg.getAttribute("viewBox") || "").split(" ").map(Number);
    if (Math.abs(drawnWidth - width) < 3 && Math.abs(drawnHeight - height) < 3) continue;
    const next = modelChart(figure.chartModel, { withTable: false, width, height });
    if (!next) continue;
    next.chartModel = figure.chartModel;
    figure.replaceWith(next);
  }
}

// Open what a Dashboard row or card names (report_layout.LINK_KINDS).
async function followDashboardLink(link) {
  const [kind, target] = [link.split(":")[0], link.split(":").slice(1).join(":")];
  try {
    if (kind === "schedule") {
      const schedules = await get("/api/scheduled?days=90");
      const source = schedules.definitions.find((entry) => entry.handle === target);
      if (source?.simple) openScheduledEditor(schedules, source);
      else switchTo("Scheduled");
    } else if (kind === "register" || kind === "account") {
      state.account = target;
      switchTo("Register");
    } else if (kind === "due-review") await openDueReviewDialog();
    else if (kind === "goals") switchTo("Savings Goals");
    else if (kind === "receivables") switchTo("Reimbursable Expenses");
    else if (kind === "fsa-claims") switchTo("FSA Dashboard");
    else if (kind === "report") { state.report = target; switchTo("Reports"); }
  } catch (error) { say(error.message, "error"); }
}
