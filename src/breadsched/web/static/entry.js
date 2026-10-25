// Typing transactions: the register's blank row and edited rows, and the Enter view.

// ---------------------------------------------------------------- register entry
//
// The register's last row is a blank transaction (#158), as in the desktop
// register: type a new entry where it will appear, press Enter to save it, or
// choose Split for one line per split. "Edit" turns an existing row into the same
// fields. Amounts are exact: each is kept as a scaled BigInt, never a float, and
// sent as a [numerator, denominator] pair.

const ENTRY_SCALE = 6;
const ENTRY_DENOMINATOR = (10n ** BigInt(ENTRY_SCALE)).toString();

function entryUnits(text, format = browserNumberFormat) {
  // Undefined for unreadable text, null for blank. Typed text follows the
  // browser's convention; the server always sends dot decimals ("dot").
  let clean = String(text ?? "").trim().replace(/\s/g, "");
  if (!clean) return null;
  if (format === "comma") clean = clean.replace(/\./g, "").replace(",", ".");
  else clean = clean.replace(/,/g, "");
  const match = /^(-)?(\d*)(?:\.(\d*))?$/.exec(clean);
  if (!match || (!match[2] && !match[3])) return undefined;
  const fraction = match[3] || "";
  if (fraction.length > ENTRY_SCALE) return undefined;
  const units = BigInt((match[2] || "0") + fraction.padEnd(ENTRY_SCALE, "0"));
  return match[1] ? -units : units;
}

function entryText(units) {
  const negative = units < 0n;
  const digits = (negative ? -units : units).toString().padStart(ENTRY_SCALE + 1, "0");
  let fraction = digits.slice(-ENTRY_SCALE).replace(/0+$/, "");
  if (fraction.length < 2) fraction = fraction.padEnd(2, "0");
  const whole = digits.slice(0, -ENTRY_SCALE);
  const decimal = browserNumberFormat === "comma" ? "," : ".";
  return `${negative ? "-" : ""}${whole}${decimal}${fraction}`;
}

function entryValue(units) {
  return [units.toString(), ENTRY_DENOMINATOR];
}

// Shares keep every digit the security records, so they travel as decimal text
// rather than scaled units (#323). These convert between the browser's typed
// convention and the server's dot decimals.
function entryDecimal(text, format = browserNumberFormat) {
  // Undefined for unreadable text, null for blank; otherwise dot-decimal text.
  let clean = String(text ?? "").trim().replace(/\s/g, "");
  if (!clean) return null;
  if (format === "comma") clean = clean.replace(/\./g, "").replace(",", ".");
  else clean = clean.replace(/,/g, "");
  return /^-?(\d+\.?\d*|\.\d+)$/.test(clean) ? clean : undefined;
}

function shownDecimal(text) {
  // Dot-decimal text from the server, in the browser's convention.
  if (text === null || text === undefined) return "";
  return browserNumberFormat === "comma" ? String(text).replace(".", ",") : String(text);
}

function sizeText(text) {
  return String(text ?? "").replace(/^-/, "");
}

// What typed text means comes from the server's shared entry rules
// (gen/services/entry_input), the same ones the desktop register uses.
const ENTRY_DATE_KEYS = new Set("+-=_[]tTmMhHyYrR");
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function plainKey(event) {
  return !event.ctrlKey && !event.metaKey && !event.altKey && event.key.length === 1;
}

async function entryDate(text, base) {
  const query = new URLSearchParams({ text });
  if (ISO_DATE.test(base || "")) query.set("base", base);
  return (await get(`/api/entry/date?${query}`)).date;
}

function isArithmetic(text) {
  // A calculation rather than one number: an operator after the first character.
  const clean = String(text ?? "").trim().replace(/^[-+]/, "").replace(/^\((.*)\)$/, "$1");
  return /[-+*/()]/.test(clean);
}

async function entryAmount(text) {
  const query = new URLSearchParams({ text, number_format:browserNumberFormat });
  const { amount } = await get(`/api/entry/amount?${query}`);
  return amount === null ? null : entryUnits(amount, "dot");
}

// Typed-but-unsaved state survives the register repainting, keyed by what is
// being entered: "blank" for the new row, or the edited split's handle.
function entryDraft(key) {
  state.entryDrafts ??= {};
  return state.entryDrafts[key];
}

function entryDirty() {
  return Object.values(state.entryDrafts || {}).some((draft) => draft && draft.dirty);
}

function leaveEntry(proceed) {
  // Switching accounts or editing another row asks before losing typed input.
  if (entryDirty() && !window.confirm(
    "Discard the transaction you were entering or editing?")) return;
  state.entryDrafts = {};
  state.editing = null;
  proceed();
}

const RECONCILE_TIPS = {
  n: "Not cleared: click to mark it cleared",
  c: "Cleared: click to mark it not cleared",
  y: "Reconciled: reopen its statement to change it",
};

function reconcileButton(row) {
  // The R column: n and c toggle through the shared reconciliation service.
  return el("button", { class:"action reconcile", type:"button",
    title:RECONCILE_TIPS[row.reconcile] || "",
    "aria-label":`Reconcile state ${row.reconcile} for ${row.description}`,
    onclick:async (event) => {
      event.stopPropagation();
      try {
        const { reconcile } = await post("/api/register/cleared",
          { transaction:row.handle, split:row.split });
        say(`Marked ${row.description} ${reconcile === "c" ? "cleared" : "not cleared"}.`);
        render();
      } catch (error) { say(error.message, "error"); }
    } }, row.reconcile);
}

async function editRow(split) {
  // Clicking a row, or Up/Down, leaves the row being edited by saving it when it
  // changed; the blank row's typing is kept as it is.
  const current = state.editing?.split;
  if (current === split) return true;
  if (current) {
    const draft = entryDraft(current);
    if (draft?.dirty) {
      const commit = state.entryCommits?.[current];
      if (!commit || !(await commit())) return false;
    }
    delete state.entryDrafts?.[current];
  }
  state.editing = split ? { split, focus:true } : null;
  if (!split) state.entryFocus = { key:"blank", field:"date" };
  render();
  return true;
}

function blankLine() {
  return { handle:null, account:"", memo:"", increase:"", decrease:"", shares:"", price:"",
    settled:{} };
}

function tradeCells(holder) {
  return { shares:(holder.shares || "").trim(), price:(holder.price || "").trim(),
    increase:(holder.increase || "").trim(), decrease:(holder.decrease || "").trim() };
}

function registerEntry(data, usable, row) {
  const key = row ? row.split : "blank";
  const editing = Boolean(row);
  const selected = usable.find((item) => item.handle === state.account);
  const referenced = new Set(editing ? row.splits.map((split) => split.account) : []);
  const accounts = usable.filter((item) => !item.hidden || referenced.has(item.handle));
  const transfers = accounts.filter((item) => item.handle !== state.account);
  // An account holding a security takes shares and a price as well (#323).
  const security = Boolean(data.security);
  const holdsShares = new Set(data.security_accounts || []);
  const span = security ? "11" : "9";
  if (!editing && (!selected || selected.hidden)) {
    return [el("tr", { class:"entry-row" }, el("td", { colspan:span, class:"note" },
      "Hidden accounts remain readable but take no new transactions."))];
  }
  if (!editing && !transfers.length) {
    return [el("tr", { class:"entry-row" }, el("td", { colspan:span, class:"note" },
      "No other visible account to transfer to."))];
  }

  let draft = entryDraft(key);
  if (!draft) {
    const mine = editing ? row.splits.find((split) => split.handle === row.split) : null;
    const others = editing ? row.splits.filter((split) => split !== mine) : [];
    const simple = !editing || (others.length === 1 && others[0].account !== mine.account);
    const mineUnits = mine ? entryUnits(mine.value, "dot") : null;
    draft = {
      dirty:false,
      date: editing ? row.date : (state.entryLastDate || new Date().toISOString().slice(0, 10)),
      num: editing ? row.num : "",
      description: editing ? row.description : "",
      transfer: editing && simple ? others[0].account : "",
      transferTouched: editing,
      increase: editing && simple && mineUnits > 0n ? entryText(mineUnits) : "",
      decrease: editing && simple && mineUnits < 0n ? entryText(-mineUnits) : "",
      handles: editing && simple ? [mine.handle, others[0].handle] : [null, null],
      // Shares as a size beside an amount; signed only without one (a share split).
      shares: editing && simple && mine.quantity
        ? shownDecimal(mineUnits ? sizeText(mine.quantity) : mine.quantity) : "",
      price: editing && simple ? shownDecimal(row.price || "") : "",
      split: editing && !simple,
      lines: editing && !simple ? [mine, ...others].filter(Boolean).map((split) => {
        const units = entryUnits(split.value, "dot");
        return { handle:split.handle, account:split.account, memo:split.memo || "",
          increase: units > 0n ? entryText(units) : "",
          decrease: units < 0n ? entryText(-units) : "",
          shares: split.quantity ? shownDecimal(units ? sizeText(split.quantity) : split.quantity)
            : "",
          price: "" };
      }) : [],
    };
    if (draft.split) draft.lines.push(blankLine());
    draft.settled = tradeCells(draft);
    for (const line of draft.lines) line.settled = tradeCells(line);
    state.entryDrafts[key] = draft;
  }
  const touch = () => { draft.dirty = true; };
  const noun = editing ? "Edited transaction" : "New transaction";

  const input = (field, attrs = {}) => {
    const node = el("input", { value:draft[field], "aria-label":`${noun} ${attrs.label || field}`,
      ...attrs, label:null });
    node.addEventListener("input", () => { draft[field] = node.value; touch(); });
    return node;
  };
  const settleAmount = async (holder, up, down) => {
    // Show a calculation's result; a negative one moves to the other column.
    for (const [field, other, node, otherNode] of [
      ["increase", "decrease", up, down], ["decrease", "increase", down, up]]) {
      if (!isArithmetic(holder[field])) continue;
      const units = await entryAmount(holder[field]);
      if (units === null || units === undefined) continue;
      holder[field] = units < 0n ? "" : entryText(units);
      holder[other] = units < 0n ? entryText(-units) : holder[other];
      if (node) node.value = holder[field];
      if (otherNode) otherNode.value = holder[other];
      touch();
    }
  };
  const amountPair = (holder, label) => {
    // Typing in one amount clears the other, so the direction is never ambiguous.
    const up = el("input", { value:holder.increase, inputmode:"decimal", class:"num",
      placeholder:"0.00", "aria-label":`${label} ${data.debit_label.toLowerCase()}` });
    const down = el("input", { value:holder.decrease, inputmode:"decimal", class:"num",
      placeholder:"0.00", "aria-label":`${label} ${data.credit_label.toLowerCase()}` });
    up.addEventListener("input", () => {
      holder.increase = up.value; touch();
      if (up.value) { holder.decrease = ""; down.value = ""; }
      updateImbalance();
    });
    down.addEventListener("input", () => {
      holder.decrease = down.value; touch();
      if (down.value) { holder.increase = ""; up.value = ""; }
      updateImbalance();
    });
    const settle = async () => {
      try { await settleAmount(holder, up, down); updateImbalance(); }
      catch (error) { say(error.message, "error"); }
    };
    up.addEventListener("change", settle);
    down.addEventListener("change", settle);
    return [up, down];
  };
  const fillTrade = async (holder, changed, nodes, account) => {
    // Any two of shares, price, and amount give the third, by the server's
    // shared rule (gen/services/entry_input.read_entry_trade).
    const field = changed === "amount"
      ? (holder.increase.trim() ? "increase" : "decrease") : changed;
    if ((holder[field] || "").trim() === (holder.settled?.[field] ?? "")) return;
    const amountText = holder.increase.trim() || holder.decrease.trim();
    const selling = holder.shares.trim().startsWith("-");
    // What this fill reads; a cell typed while it runs is read by its own fill.
    const sent = tradeCells(holder);
    const query = new URLSearchParams({ account, shares:holder.shares, price:holder.price,
      amount:amountText, changed, number_format:browserNumberFormat });
    const filled = await get(`/api/entry/trade?${query}`);
    const written = {};
    if (filled.shares !== null) {
      written.shares = (selling && !amountText && !filled.amount ? "-" : "")
        + shownDecimal(filled.shares);
    }
    if (filled.price !== null) written.price = shownDecimal(filled.price);
    if (filled.amount !== null) {
      const target = amountText ? (sent.increase ? "increase" : "decrease")
        : (selling ? "decrease" : "increase");
      written[target] = entryText(entryUnits(filled.amount, "dot"));
      written[target === "increase" ? "decrease" : "increase"] = "";
    }
    for (const [key, value] of Object.entries(written)) {
      if ((holder[key] || "").trim() !== sent[key]) continue;  // typed meanwhile
      holder[key] = value;
      if (nodes[key]) nodes[key].value = value;
      sent[key] = value.trim();
    }
    holder.settled = sent;
    touch();
    updateImbalance();
  };
  const tradeInputs = (holder, label, up, down, accountOf) => {
    // The Shares and Price cells of a security's row or split line.
    const shares = el("input", { value:holder.shares, inputmode:"decimal", class:"num",
      placeholder:"Shares", "aria-label":`${label} shares` });
    const price = el("input", { value:holder.price, inputmode:"decimal", class:"num",
      placeholder:"Price", "aria-label":`${label} price` });
    shares.addEventListener("input", () => { holder.shares = shares.value; touch(); });
    price.addEventListener("input", () => { holder.price = price.value; touch(); });
    const nodes = { shares, price, increase:up, decrease:down };
    const settle = (changed) => () => {
      const account = accountOf();
      if (!holdsShares.has(account)) return;
      holder.filling = fillTrade(holder, changed, nodes, account)
        .catch((error) => say(error.message, "error"));
    };
    // Enter may save before a typed cell's change event: read what is unread first.
    holder.refill = () => {
      const now = tradeCells(holder);
      const unread = ["price", "shares", "increase", "decrease"]
        .find((key) => now[key] !== (holder.settled?.[key] ?? ""));
      if (unread) settle(unread === "increase" || unread === "decrease" ? "amount" : unread)();
    };
    shares.addEventListener("change", settle("shares"));
    price.addEventListener("change", settle("price"));
    up.addEventListener("change", settle("amount"));
    down.addEventListener("change", settle("amount"));
    return [shares, price];
  };
  const quantityOf = (holder, value) => {
    // Signed shares as dot-decimal text, signed like the amount; undefined if unreadable.
    const text = entryDecimal(holder.shares);
    if (text === null || text === undefined) return text;
    const size = sizeText(text);
    if (value > 0n) return size;
    if (value < 0n) return `-${size}`;
    return text;
  };
  const accountSelect = (value, choices, label, onchange, blank) => {
    const node = el("select", { "aria-label":label },
      blank ? el("option", { value:"" }, blank) : null,
      ...choices.map((item) => el("option", { value:item.handle,
        selected:item.handle === value ? "selected" : null }, item.full_name)));
    if (!blank && !value && choices.length) node.value = choices[0].handle;
    node.addEventListener("change", () => { onchange(node.value); touch(); });
    // Typing completes account paths segment by segment: "Ex:Gr" picks
    // Expenses:Groceries, as in the desktop register.
    let typed = "";
    // The matches are listed under the choice while a path is typed.
    const list = el("div", { class:"completion", role:"listbox",
      "aria-label":`${label}: matching accounts`, hidden:"hidden" });
    const hideList = () => { list.hidden = true; list.replaceChildren(); };
    const showList = (found) => {
      if (!list.isConnected) node.after(list);
      list.replaceChildren(...found.map((item, index) => el("button", {
        type:"button", class: index === 0 ? "action primary" : "action", role:"option",
        // mousedown would otherwise blur the choice and hide the list first.
        onmousedown:(event) => event.preventDefault(),
        onclick:() => {
          node.value = item.handle; onchange(item.handle); touch();
          typed = ""; hideList(); node.focus();
        } }, item.full_name)));
      list.hidden = !found.length;
    };
    node.addEventListener("blur", () => { typed = ""; hideList(); });
    node.addEventListener("keydown", async (event) => {
      if (event.key === "Backspace" && typed) typed = typed.slice(0, -1);
      else if (plainKey(event) && (event.key !== " " || typed)) typed += event.key;
      else return;
      event.preventDefault();
      if (!typed) { hideList(); return; }
      const wanted = typed;
      try {
        const query = new URLSearchParams({ text:wanted });
        const { accounts:found } = await get(`/api/entry/accounts?${query}`);
        if (wanted !== typed) return;  // a later key is already being looked up
        const offered = found.filter((item) =>
          choices.some((choice) => choice.handle === item.handle)).slice(0, 8);
        showList(offered);
        const match = offered[0];
        if (!match) { say(`No account matches “${wanted}”.`, "error"); return; }
        node.value = match.handle;
        onchange(match.handle); touch();
        say(`“${wanted}” → ${match.full_name}`);
      } catch (error) { say(error.message, "error"); }
    });
    return node;
  };

  const dateInput = input("date", { placeholder:"YYYY-MM-DD", label:"date", size:"10",
    title:"A date such as 2026-03-15, 3/15, or 15; + and - move a day, ] and [ a month, t is today" });
  const numInput = input("num", { placeholder:"Num", label:"number", size:"5",
    title:"+ and - step the number; in an empty field, from the last one" });
  const lastDate = () => (editing ? row.date : (state.entryLastDate
    || new Date().toISOString().slice(0, 10)));
  const settleDate = async () => {
    if (ISO_DATE.test(draft.date)) return true;
    try {
      draft.date = await entryDate(draft.date, lastDate());
      dateInput.value = draft.date;
      return true;
    } catch (error) { return false; }
  };
  dateInput.addEventListener("keydown", async (event) => {
    // A shortcut applies to a whole date; while one is being typed, keys type.
    if (!plainKey(event) || !ENTRY_DATE_KEYS.has(event.key) || !ISO_DATE.test(draft.date)) return;
    event.preventDefault();
    try {
      draft.date = await entryDate(event.key, draft.date);
      dateInput.value = draft.date; touch();
    } catch (error) { say(error.message, "error"); }
  });
  dateInput.addEventListener("change", () => { settleDate(); });
  numInput.addEventListener("keydown", async (event) => {
    if (!plainKey(event) || !"+=-_".includes(event.key)) return;
    if (draft.num.trim() && !/\d$/.test(draft.num.trim())) return;
    event.preventDefault();
    try {
      const query = new URLSearchParams({ account:state.account, text:draft.num,
        step:"+=".includes(event.key) ? "1" : "-1" });
      const { num } = await get(`/api/entry/num?${query}`);
      if (num !== draft.num) { draft.num = num; numInput.value = num; touch(); }
    } catch (error) { say(error.message, "error"); }
  });
  const description = input("description", { placeholder:"Description", label:"description" });
  const transfer = accountSelect(draft.transfer, transfers, `${noun} transfer account`,
    (value) => { draft.transfer = value; draft.transferTouched = true; });
  if (!draft.transfer) draft.transfer = transfer.value;
  const [increase, decrease] = amountPair(draft, noun);
  // An expense takes money out of this account, so Tab from an expense transfer
  // goes to the Withdrawal or Charge column, once (#369).
  transfer.addEventListener("keydown", (event) => {
    if (event.key !== "Tab" || event.shiftKey || security || draft.split) return;
    const chosen = transfers.find((item) => item.handle === transfer.value);
    if (chosen?.class !== "expense") return;
    event.preventDefault();
    decrease.focus();
  });
  const [sharesInput, priceInput] = security
    ? tradeInputs(draft, noun, increase, decrease, () => state.account) : [null, null];
  const imbalance = el("td", { class:"num" });
  const splitButton = el("button", { class: draft.split ? "action primary" : "action",
    type:"button", "aria-pressed":String(draft.split),
    title:"Enter this transaction as several splits, in place" }, "Split");
  const saveButton = el("button", { class:"action primary", type:"button" },
    editing ? "Save" : "Enter");
  const cancelButton = editing ? el("button", { class:"action", type:"button" }, "Cancel") : null;

  function lineUnits() {
    return draft.lines.map((line) => {
      const up = entryUnits(line.increase);
      const down = entryUnits(line.decrease);
      if (up === undefined || down === undefined) return undefined;
      if (up !== null) return up;
      if (down !== null) return -down;
      return null;
    });
  }
  function lineIsEmpty(line) {
    return !line.account && !line.memo.trim() && !line.increase.trim()
      && !line.decrease.trim() && !(line.shares || "").trim();
  }
  function updateImbalance() {
    if (!draft.split) return;
    const values = lineUnits();
    if (values.some((value) => value === undefined)) { imbalance.textContent = "—"; return; }
    const total = values.reduce((sum, value) => sum + (value ?? 0n), 0n);
    imbalance.textContent = total === 0n ? "Balanced" : money(entryText(total).replace(",", "."));
    imbalance.className = total === 0n ? "num" : "num neg";
    // The empty last line shows the rest, in the column that balances it; choosing
    // its account takes it (#369).
    const last = draft.lines[draft.lines.length - 1];
    for (const line of draft.lines) {
      if (!line.nodes) continue;
      line.nodes.up.placeholder = "0.00";
      line.nodes.down.placeholder = "0.00";
      line.nodes.up.classList.remove("rest");
      line.nodes.down.classList.remove("rest");
    }
    const rest = total === 0n ? null : -total;
    if (last?.nodes && rest !== null && lineIsEmpty(last)) {
      const node = rest > 0n ? last.nodes.up : last.nodes.down;
      node.placeholder = entryText(rest > 0n ? rest : -rest);
      node.classList.add("rest");
    }
  }
  function takeRest(line) {
    // A line given an account with no amount yet takes what balances the entry.
    if (line.increase.trim() || line.decrease.trim()) return;
    const others = draft.lines.filter((item) => item !== line);
    const units = others.map((item) => {
      const up = entryUnits(item.increase);
      const down = entryUnits(item.decrease);
      if (up === undefined || down === undefined) return undefined;
      return up !== null ? up : down !== null ? -down : 0n;
    });
    if (units.some((value) => value === undefined)) return;
    const total = units.reduce((sum, value) => sum + value, 0n);
    if (total === 0n) return;
    if (total < 0n) line.increase = entryText(-total); else line.decrease = entryText(total);
    if (line.nodes) {
      line.nodes.up.value = line.increase;
      line.nodes.down.value = line.decrease;
    }
    touch();
  }
  function fail(message, node) {
    say(message, "error");
    node?.focus();
  }

  async function commit() {
    for (const holder of [draft, ...draft.lines]) {
      await holder.filling;
      holder.refill?.();
      await holder.filling;
    }
    if (!(await settleDate())) {
      return fail("Enter a date such as 2026-03-15, 3/15, 15, or t.", dateInput);
    }
    try {
      if (draft.split) for (const line of draft.lines) await settleAmount(line, null, null);
      else await settleAmount(draft, increase, decrease);
    } catch (error) {
      return fail("Enter an amount, or arithmetic such as 12.50+3.", draft.split ? null : increase);
    }
    if (!draft.description.trim()) return fail("Enter a description.", description);
    let splits;
    if (draft.split) {
      const values = lineUnits();
      splits = [];
      for (const [index, line] of draft.lines.entries()) {
        const value = values[index];
        if (value === undefined) return fail("Enter a valid amount.", null);
        if (value === null && !line.account) {
          if (line.memo.trim()) return fail("Choose an account for this split.", null);
          continue;
        }
        if (!line.account) return fail("Choose an account for this split.", null);
        let quantity = null;
        if (holdsShares.has(line.account)) {
          quantity = quantityOf(line, value ?? 0n);
          if (quantity === undefined) return fail("Enter shares such as 12.345.", null);
          if (quantity === null) return fail("Enter the shares for this split.", null);
        }
        if ((value === null || value === 0n) && quantity === null) {
          return fail("Enter an amount for this split.", null);
        }
        splits.push({ account:line.account, value:entryValue(value ?? 0n), memo:line.memo,
          handle:line.handle, quantity });
      }
      if (splits.length < 2) return fail("A transaction needs at least two splits.", null);
      const total = values.reduce((sum, value) => sum + (value ?? 0n), 0n);
      if (total !== 0n) {
        return fail(`The splits are out of balance by ${entryText(total)}.`, null);
      }
      // An edited transaction may move out of this account entirely, such as a
      // purchase entered on the wrong card (#364); a new one must belong here.
      if (!editing && !splits.some((split) => split.account === state.account)) {
        return fail("One split must be in this register's account.", null);
      }
    } else {
      const up = entryUnits(draft.increase);
      const down = entryUnits(draft.decrease);
      if (up === undefined || down === undefined) return fail("Enter a valid amount.", increase);
      let value = up ?? (down === null ? null : -down);
      let quantity = null;
      if (security) {
        quantity = quantityOf(draft, value ?? 0n);
        if (quantity === undefined) return fail("Enter shares such as 12.345.", sharesInput);
        if (quantity === null) {
          return fail("Enter the shares bought or sold, or a price and an amount.", sharesInput);
        }
        // Shares with no amount: a share split, or shares moved in kind.
        if (value === null) value = 0n;
      }
      if (value === null) {
        return fail(`Enter an amount under ${data.debit_label} or ${data.credit_label}.`,
          increase);
      }
      if ((up ?? down ?? 1n) <= 0n) return fail("Amount must be greater than zero.", increase);
      if (!draft.transfer) return fail("Choose a visible transfer account.", transfer);
      splits = [
        { account:state.account, value:entryValue(value), handle:draft.handles[0], quantity },
        { account:draft.transfer, value:entryValue(-value), handle:draft.handles[1] },
      ];
    }
    try {
      await post("/api/register/entry", {
        handle: editing ? row.handle : null, date:draft.date, num:draft.num,
        description:draft.description.trim(), splits,
      });
      delete state.entryDrafts[key];
      if (editing && !splits.some((split) => split.account === state.account)) {
        state.editing = null;
        say(`Saved ${draft.description.trim()}; it moved out of this register.`);
      } else if (editing) {
        state.editing = null;
        say(`Saved changes to ${draft.description.trim()}.`);
      } else {
        state.entryLastDate = draft.date;
        say(`Posted ${draft.description.trim()}.`);
      }
      render();
      return true;
    } catch (error) { say(error.message, "error"); return false; }
  }
  state.entryCommits ??= {};
  state.entryCommits[key] = commit;
  async function move(step) {
    // Up/Down: save this row if it changed, then edit the one above or below.
    // Below the last transaction is the blank row.
    const order = data.rows.map((item) => item.split);
    const index = editing ? order.indexOf(row.split) : order.length;
    const target = index + step;
    if (target < 0 || (!editing && target >= order.length)) return;
    if (!editing && draft.dirty && !(await commit())) return;
    await editRow(target < order.length ? order[target] : null);
  }
  function cancel() {
    delete state.entryDrafts[key];
    if (editing) state.editing = null;
    render();
  }
  async function propose() {
    if (editing) return;  // a stored transaction is never proposed over
    const amountsEmpty = !draft.increase.trim() && !draft.decrease.trim()
      && !draft.lines.some((line) => line.increase.trim() || line.decrease.trim());
    if (!draft.description.trim()) return;
    try {
      const query = new URLSearchParams({ account:state.account, description:draft.description });
      const { suggestion } = await get(`/api/entry/suggest?${query}`);
      if (!suggestion) return;
      const filled = [];
      if (amountsEmpty && !draft.transferTouched
          && (draft.split || suggestion.splits.length > 2)) {
        draft.split = true;
        draft.lines = suggestion.splits.map((split) => {
          const units = entryUnits(split.value, "dot");
          return { ...blankLine(), account:split.account, memo:split.memo || "",
            increase: units > 0n ? entryText(units) : "",
            decrease: units < 0n ? entryText(-units) : "" };
        });
        draft.lines.push(blankLine());
        filled.push(`${suggestion.splits.length} splits`);
        state.entryFocus = { key, field:"line-0" };
      } else if (!draft.split) {
        // Fill the fields in place, so the cursor stays where the user moved it.
        if (!draft.transferTouched && suggestion.transfer
            && transfers.some((item) => item.handle === suggestion.transfer)) {
          draft.transfer = suggestion.transfer;
          transfer.value = suggestion.transfer;
          filled.push(suggestion.transfer_name);
        }
        if (amountsEmpty && suggestion.amount) {
          const units = entryUnits(suggestion.amount, "dot");
          if (units > 0n) { draft.increase = entryText(units); increase.value = draft.increase; }
          else if (units < 0n) { draft.decrease = entryText(-units); decrease.value = draft.decrease; }
          if (units) filled.push(entryText(units < 0n ? -units : units));
        }
      }
      if (filled.length) {
        draft.dirty = true;
        say(`Proposed from ${suggestion.date} “${suggestion.description}”: `
          + `${filled.join(", ")}. Edit anything, then press Enter.`);
        if (draft.split) render();
      }
    } catch (error) { say(error.message, "error"); }
  }
  description.addEventListener("change", () => { propose(); });

  splitButton.addEventListener("click", () => {
    if (!draft.split) {
      const up = entryUnits(draft.increase);
      const down = entryUnits(draft.decrease);
      const value = up ? up : (down ? -down : null);
      draft.lines = [
        { ...blankLine(), handle:draft.handles[0], account:state.account,
          increase:value > 0n ? entryText(value) : "", decrease:value < 0n ? entryText(-value) : "",
          shares:draft.shares, price:draft.price, settled:{ ...draft.settled } },
        { ...blankLine(), handle:draft.handles[1], account:draft.transfer,
          increase:value < 0n ? entryText(-value) : "", decrease:value > 0n ? entryText(value) : "" },
        blankLine(),
      ];
      draft.split = true;
      state.entryFocus = { key, field:"line-0" };
    } else {
      const filled = draft.lines.filter((line) => line.account || line.memo.trim()
        || line.increase.trim() || line.decrease.trim());
      const mine = filled.find((line) => line.account === state.account);
      if (filled.length > 2 || (filled.length && !mine)) {
        return fail("Remove splits until two remain, one in this account, or keep splitting.",
          null);
      }
      const other = filled.find((line) => line !== mine);
      draft.increase = mine ? mine.increase : "";
      draft.decrease = mine ? mine.decrease : "";
      draft.shares = mine ? mine.shares : "";
      draft.price = mine ? mine.price : "";
      draft.settled = mine ? { ...mine.settled } : {};
      if (other?.account) { draft.transfer = other.account; draft.transferTouched = true; }
      draft.handles = [mine?.handle ?? null, other?.handle ?? null];
      draft.split = false;
      draft.lines = [];
    }
    touch();
    render();
  });
  saveButton.addEventListener("click", () => { commit(); });
  cancelButton?.addEventListener("click", cancel);

  const keys = (event) => {
    if ((event.key === "ArrowUp" || event.key === "ArrowDown")
        && event.target.tagName === "INPUT" && !event.altKey && !event.ctrlKey) {
      event.preventDefault();
      move(event.key === "ArrowUp" ? -1 : 1);
    } else if (event.key === "Enter" && event.target.tagName !== "BUTTON") {
      event.preventDefault();
      commit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      cancel();
    }
  };

  const main = el("tr", { class:"entry-row", onkeydown:keys },
    el("td", {}, dateInput), el("td", {}, numInput), el("td", {}, description),
    el("td", {}, draft.split ? el("span", { class:"muted" }, "-- Split --") : transfer),
    el("td", { class:"muted" }, editing ? row.reconcile : ""),
    security ? el("td", { class:"num" }, draft.split ? null : sharesInput) : null,
    security ? el("td", { class:"num" }, draft.split ? null : priceInput) : null,
    el("td", { class:"num" }, draft.split ? null : increase),
    el("td", { class:"num" }, draft.split ? null : decrease),
    el("td", {}),
    el("td", {}, el("div", { class:"row" }, splitButton, saveButton, cancelButton)));
  const result = [main];
  const focusable = { description, date:dateInput };
  if (draft.split) {
    const imbalanceRow = el("tr", { class:"entry-row entry-line" },
      el("td", {}), el("td", {}), el("td", { class:"muted" }, "Imbalance"),
      el("td", {}), el("td", {}), ...(security ? [el("td", {}), el("td", {})] : []),
      el("td", {}), el("td", {}), imbalance, el("td", {}));
    const lineRow = (line, index) => {
      const label = `${editing ? "Edited" : "New"} split ${index + 1}`;
      const memo = el("input", { value:line.memo, placeholder:"Memo", "aria-label":`${label} memo` });
      focusable[`line-${index}`] = memo;
      const grow = () => {
        // A trailing empty line is always ready for the next split; it is added
        // in place, so the field being typed in keeps the cursor.
        if (index !== draft.lines.length - 1) return;
        const next = blankLine();
        draft.lines.push(next);
        imbalanceRow.before(lineRow(next, draft.lines.length - 1));
      };
      memo.addEventListener("input", () => { line.memo = memo.value; touch(); grow(); });
      const account = accountSelect(line.account, accounts, `${label} account`, (value) => {
        line.account = value;
        if (value) takeRest(line);
        grow();
        updateImbalance();
      }, "(choose account)");
      const [up, down] = amountPair(line, label);
      line.nodes = { up, down };
      up.addEventListener("input", grow);
      down.addEventListener("input", grow);
      const [lineShares, linePrice] = security
        ? tradeInputs(line, label, up, down, () => line.account) : [null, null];
      lineShares?.addEventListener("input", grow);
      return el("tr", { class:"entry-row entry-line", onkeydown:keys },
        el("td", {}), el("td", {}), el("td", {}, memo),
        el("td", {}, account), el("td", {}),
        security ? el("td", { class:"num" }, lineShares) : null,
        security ? el("td", { class:"num" }, linePrice) : null,
        el("td", { class:"num" }, up),
        el("td", { class:"num" }, down),
        el("td", {}), el("td", {}));
    };
    draft.lines.forEach((line, index) => result.push(lineRow(line, index)));
    result.push(imbalanceRow);
    updateImbalance();
  }
  const wanted = state.entryFocus?.key === key ? focusable[state.entryFocus.field] : null;
  if (wanted || (editing && state.editing?.focus)) {
    state.entryFocus = null;
    if (state.editing) state.editing.focus = false;
    setTimeout(() => (wanted || description).focus(), 0);
  }
  return result;
}

async function showEntry() {
  if (!state.accounts.length) state.accounts = await get("/api/accounts");
  const claimData = await get("/api/fsa/claims");
  const openClaims = (claimData.claims || []).filter(
    (claim) => !["fully_reimbursed", "closed"].includes(claim.status));
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
        el("option", { value: "reimbursement" }, "FSA reimbursement"),
        el("option", { value: "repayment" }, "Repaid to the FSA"),
        el("option", { value: "direct_payment" }, "Paid from the FSA card"),
        el("option", { value: "direct_refund" }, "Refunded to the FSA card"))) : null,
    el("button", { class: "action primary", type: "submit" }, "Post"));

  return el("div", {},
    el("p", { class: "note" },
      "Two-split entry. For anything more involved, use the desktop interface."),
    el("div", { class: "panel panel-pad-16" }, form));
}
