// Quick entry: one spending or income transaction from a phone, apart from the full
// interface. It posts through the same /api/transaction as the register's simple
// entry, so the book server validates and saves it; a description already used
// fills in its category and amount from /api/entry/suggest. The account money came
// from (or went into) is remembered on this device.

const QUICK_ACCOUNT_KEY = "breadsched.quick.account";
const quickSaved = [];

const remembered = () => {
  try { return window.localStorage.getItem(QUICK_ACCOUNT_KEY) || ""; } catch { return ""; }
};
const remember = (handle) => {
  try { window.localStorage.setItem(QUICK_ACCOUNT_KEY, handle); } catch { /* private mode */ }
};
const today = () => {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 10);
};

// Where money is kept (cash, bank, cards) and what it is spent on or earned from.
function quickAccounts(all) {
  const usable = all.filter((a) => !a.placeholder && !a.hidden && a.class !== "root");
  const funds = usable.filter((a) => a.class === "asset" || a.class === "liability");
  const categories = usable.filter((a) => a.class === "expense" || a.class === "income");
  const byName = (a, b) => a.full_name.localeCompare(b.full_name);
  return { funds: funds.sort(byName), categories: categories.sort(byName),
    others: usable.filter((a) => !funds.includes(a) && !categories.includes(a)).sort(byName) };
}

function accountSelect(name, label, groups, chosen) {
  const select = el("select", { name, id: `quick-${name}`, required: "required" },
    el("option", { value: "" }, `Choose ${label.toLowerCase()}`),
    ...groups.filter(([, items]) => items.length).map(([title, items]) =>
      el("optgroup", { label: title }, ...items.map((a) => el("option", {
        value: a.handle, selected: a.handle === chosen ? "selected" : null,
      }, a.full_name)))));
  return [el("label", { for: `quick-${name}` }, label), select];
}

async function showQuick() {
  const view = document.getElementById("view");
  const all = await get("/api/accounts");
  const { funds, categories, others } = quickAccounts(all);
  const fullView = document.getElementById("full-view");
  fullView.href = `/#${tokenFragment()}`;

  let direction = "out";
  const outButton = el("button", { type: "button", class: "action active",
    "aria-pressed": "true" }, "Spent");
  const inButton = el("button", { type: "button", class: "action", "aria-pressed": "false" },
    "Received");
  const setDirection = (next) => {
    direction = next;
    for (const [button, value] of [[outButton, "out"], [inButton, "in"]]) {
      button.classList.toggle("active", value === next);
      button.setAttribute("aria-pressed", String(value === next));
    }
    fundsLabel.textContent = next === "out" ? "Paid from" : "Paid into";
  };
  outButton.addEventListener("click", () => setDirection("out"));
  inButton.addEventListener("click", () => setDirection("in"));

  const amount = el("input", { id: "quick-amount", name: "amount", inputmode: "decimal",
    autocomplete: "off", placeholder: "0.00", required: "required" });
  const description = el("input", { id: "quick-description", name: "description",
    autocomplete: "off", autocapitalize: "sentences", placeholder: "Who or what" });
  const when = el("input", { id: "quick-date", name: "date", type: "date", value: today() });
  const [fundsLabel, fundsSelect] = accountSelect("account", "Paid from",
    [["Accounts", funds], ["Other", others]], remembered());
  const [, categorySelect] = accountSelect("category", "Category",
    [["Categories", categories], ["Accounts", funds], ["Other", others]], "");
  const memo = el("input", { id: "quick-memo", name: "memo", autocomplete: "off",
    placeholder: "Optional note" });

  // A description used before fills in its category and, if none is typed, amount.
  description.addEventListener("change", async () => {
    const text = description.value.trim();
    if (!text) return;
    try {
      const query = new URLSearchParams({ description: text });
      if (fundsSelect.value) query.set("account", fundsSelect.value);
      const { suggestion } = await get(`/api/entry/suggest?${query}`);
      if (!suggestion) return;
      if (suggestion.transfer && !categorySelect.value) categorySelect.value = suggestion.transfer;
      if (suggestion.amount && !amount.value.trim()) {
        amount.value = String(Math.abs(Number(suggestion.amount)).toFixed(2));
      }
    } catch { /* a suggestion is only a convenience */ }
  });

  const recent = el("ul", { class: "quick-recent" });
  const recentHeading = el("h2", { class: "quick-recent-heading", hidden: "hidden" },
    "Saved here");
  const showRecent = () => recentHeading.removeAttribute("hidden") || recent.replaceChildren(...quickSaved.slice(-5).reverse().map(
    (item) => el("li", {}, el("span", {}, `${item.date} ${item.description || "(no description)"}`),
      el("span", { class: "num" }, money(item.amount)))));

  const save = el("button", { type: "submit", class: "action primary quick-save" }, "Save");
  const form = el("form", { class: "quick-form", novalidate: "novalidate" },
    el("div", { class: "quick-direction", role: "group", "aria-label": "Money" },
      outButton, inButton),
    el("label", { for: "quick-amount" }, "Amount"), amount,
    el("label", { for: "quick-description" }, "Description"), description,
    fundsLabel, fundsSelect,
    el("label", { for: "quick-category" }, "Category"), categorySelect,
    el("label", { for: "quick-date" }, "Date"), when,
    el("label", { for: "quick-memo" }, "Memo"), memo,
    save);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!amount.value.trim() || !fundsSelect.value || !categorySelect.value) {
      say("Enter an amount, an account, and a category.", "error");
      return;
    }
    const account = fundsSelect.value;
    const category = categorySelect.value;
    save.disabled = true;
    try {
      const saved = await post("/api/transaction", {
        from: direction === "out" ? account : category,
        to: direction === "out" ? category : account,
        amount: amount.value.trim(),
        date: when.value || today(),
        description: description.value.trim(),
        memo: memo.value.trim(),
      });
      remember(account);
      quickSaved.push({ date: saved.date, description: description.value.trim(),
        amount: direction === "out" ? `-${saved.amount}` : saved.amount });
      showRecent();
      say(`Saved ${description.value.trim() || "the transaction"}.`);
      amount.value = ""; description.value = ""; memo.value = "";
      categorySelect.value = "";
      amount.focus();
    } catch (error) {
      say(error.message, "error");
    } finally {
      save.disabled = false;
    }
  });

  view.replaceChildren(form, recentHeading, recent);
  amount.focus();
}

get("/api/summary").then((s) => {
  // Only the file's name: a phone has no room for its folder.
  document.getElementById("book").textContent = (s.book || "").split(/[\\/]/).pop();
}).catch(() => {});
showQuick().catch((error) => say(error.message, "error"));
