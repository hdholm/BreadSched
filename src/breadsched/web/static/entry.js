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

function registerEntry(data, usable, row) {
  const key = row ? row.split : "blank";
  const editing = Boolean(row);
  const selected = usable.find((item) => item.handle === state.account);
  const referenced = new Set(editing ? row.splits.map((split) => split.account) : []);
  const accounts = usable.filter((item) => !item.hidden || referenced.has(item.handle));
  const transfers = accounts.filter((item) => item.handle !== state.account);
  if (!editing && (!selected || selected.hidden)) {
    return [el("tr", { class:"entry-row" }, el("td", { colspan:"8", class:"note" },
      "Hidden accounts remain readable but take no new transactions."))];
  }
  if (!editing && !transfers.length) {
    return [el("tr", { class:"entry-row" }, el("td", { colspan:"8", class:"note" },
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
      split: editing && !simple,
      lines: editing && !simple ? [mine, ...others].filter(Boolean).map((split) => {
        const units = entryUnits(split.value, "dot");
        return { handle:split.handle, account:split.account, memo:split.memo || "",
          increase: units > 0n ? entryText(units) : "",
          decrease: units < 0n ? entryText(-units) : "" };
      }) : [],
    };
    if (draft.split) draft.lines.push({ handle:null, account:"", memo:"", increase:"", decrease:"" });
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
    node.addEventListener("blur", () => { typed = ""; });
    node.addEventListener("keydown", async (event) => {
      if (event.key === "Backspace" && typed) typed = typed.slice(0, -1);
      else if (plainKey(event) && (event.key !== " " || typed)) typed += event.key;
      else return;
      event.preventDefault();
      if (!typed) return;
      const wanted = typed;
      try {
        const query = new URLSearchParams({ text:wanted });
        const { accounts:found } = await get(`/api/entry/accounts?${query}`);
        if (wanted !== typed) return;  // a later key is already being looked up
        const match = found.find((item) => choices.some((choice) => choice.handle === item.handle));
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
  function updateImbalance() {
    if (!draft.split) return;
    const values = lineUnits();
    if (values.some((value) => value === undefined)) { imbalance.textContent = "—"; return; }
    const total = values.reduce((sum, value) => sum + (value ?? 0n), 0n);
    imbalance.textContent = total === 0n ? "Balanced" : money(entryText(total).replace(",", "."));
    imbalance.className = total === 0n ? "num" : "num neg";
  }
  function fail(message, node) {
    say(message, "error");
    node?.focus();
  }

  async function commit() {
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
        if (value === null || value === 0n) return fail("Enter an amount for this split.", null);
        splits.push({ account:line.account, value:entryValue(value), memo:line.memo,
          handle:line.handle });
      }
      if (splits.length < 2) return fail("A transaction needs at least two splits.", null);
      const total = values.reduce((sum, value) => sum + (value ?? 0n), 0n);
      if (total !== 0n) {
        return fail(`The splits are out of balance by ${entryText(total)}.`, null);
      }
      if (!splits.some((split) => split.account === state.account)) {
        return fail("One split must be in this register's account.", null);
      }
    } else {
      const up = entryUnits(draft.increase);
      const down = entryUnits(draft.decrease);
      if (up === undefined || down === undefined) return fail("Enter a valid amount.", increase);
      const value = up ?? (down === null ? null : -down);
      if (value === null) {
        return fail(`Enter an amount under ${data.debit_label} or ${data.credit_label}.`,
          increase);
      }
      if ((up ?? down) <= 0n) return fail("Amount must be greater than zero.", increase);
      if (!draft.transfer) return fail("Choose a visible transfer account.", transfer);
      splits = [
        { account:state.account, value:entryValue(value), handle:draft.handles[0] },
        { account:draft.transfer, value:entryValue(-value), handle:draft.handles[1] },
      ];
    }
    try {
      await post("/api/register/entry", {
        handle: editing ? row.handle : null, date:draft.date, num:draft.num,
        description:draft.description.trim(), splits,
      });
      delete state.entryDrafts[key];
      if (editing) {
        state.editing = null;
        say(`Saved changes to ${draft.description.trim()}.`);
      } else {
        state.entryLastDate = draft.date;
        say(`Posted ${draft.description.trim()}.`);
      }
      render();
    } catch (error) { say(error.message, "error"); }
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
          return { handle:null, account:split.account, memo:split.memo || "",
            increase: units > 0n ? entryText(units) : "",
            decrease: units < 0n ? entryText(-units) : "" };
        });
        draft.lines.push({ handle:null, account:"", memo:"", increase:"", decrease:"" });
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
        { handle:draft.handles[0], account:state.account, memo:"",
          increase:value > 0n ? entryText(value) : "", decrease:value < 0n ? entryText(-value) : "" },
        { handle:draft.handles[1], account:draft.transfer, memo:"",
          increase:value < 0n ? entryText(-value) : "", decrease:value > 0n ? entryText(value) : "" },
        { handle:null, account:"", memo:"", increase:"", decrease:"" },
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
    if (event.key === "Enter" && event.target.tagName !== "BUTTON") {
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
    el("td", { class:"num" }, draft.split ? null : increase),
    el("td", { class:"num" }, draft.split ? null : decrease),
    el("td", {}),
    el("td", {}, el("div", { class:"row" }, splitButton, saveButton, cancelButton)));
  const result = [main];
  const focusable = { description };
  if (draft.split) {
    const imbalanceRow = el("tr", { class:"entry-row entry-line" },
      el("td", {}), el("td", {}), el("td", { class:"muted" }, "Imbalance"),
      el("td", {}), el("td", {}), el("td", {}), imbalance, el("td", {}));
    const lineRow = (line, index) => {
      const label = `${editing ? "Edited" : "New"} split ${index + 1}`;
      const memo = el("input", { value:line.memo, placeholder:"Memo", "aria-label":`${label} memo` });
      focusable[`line-${index}`] = memo;
      const grow = () => {
        // A trailing empty line is always ready for the next split; it is added
        // in place, so the field being typed in keeps the cursor.
        if (index !== draft.lines.length - 1) return;
        const next = { handle:null, account:"", memo:"", increase:"", decrease:"" };
        draft.lines.push(next);
        imbalanceRow.before(lineRow(next, draft.lines.length - 1));
      };
      memo.addEventListener("input", () => { line.memo = memo.value; touch(); grow(); });
      const account = accountSelect(line.account, accounts, `${label} account`, (value) => {
        line.account = value; grow();
      }, "(choose account)");
      const [up, down] = amountPair(line, label);
      up.addEventListener("input", grow);
      down.addEventListener("input", grow);
      return el("tr", { class:"entry-row entry-line", onkeydown:keys },
        el("td", {}), el("td", {}), el("td", {}, memo),
        el("td", {}, account), el("td", { class:"num" }, up), el("td", { class:"num" }, down),
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
