// Payees, savings goals, reimbursable expenses, and categorization rules.

async function showPayees() {
  const data = await get("/api/payees");
  const refresh = async () => { current = "Payees"; await render(); };
  const run = (action) => async () => {
    try { await action(); } catch (error) { say(error.message, "error"); }
  };
  const name = el("input", { name:"name", placeholder:"Corner Grocer" });
  const matches = el("textarea", { name:"matches", rows:"3",
    placeholder:"One example description per line, e.g. CORNER GROCER #1234" });
  let editing = null;
  const saveButton = el("button", { class:"action primary", type:"submit" }, "Add payee");
  const form = el("form", { class:"entry", onsubmit:(event) => {
    event.preventDefault();
    run(async () => {
      const lines = matches.value.split("\n").map((line) => line.trim()).filter(Boolean);
      const saved = await post("/api/payee/save",
        { name:name.value, matches:lines, handle:editing });
      say(`Saved ${saved.name}.`);
      await refresh();
    })();
  } },
    el("label", {}, "Name", name),
    el("label", {}, "Matching descriptions", matches),
    saveButton);
  const payeeRows = data.payees.map((payee) => el("tr", {},
    el("td", {}, payee.name),
    el("td", {}, payee.match_keys.join(", ") || "—"),
    el("td", { class:"num" }, String(payee.transactions)),
    el("td", {},
      el("button", { class:"action", type:"button", onclick:() => {
        editing = payee.handle;
        name.value = payee.name;
        matches.value = payee.match_keys.join("\n");
        saveButton.textContent = "Save changes";
        name.focus();
      } }, "Edit"),
      el("button", { class:"action", type:"button", onclick:run(async () => {
        if (!window.confirm(`Delete ${payee.name}? It is cleared from `
          + `${payee.transactions} transaction(s); descriptions are unchanged.`)) return;
        const deleted = await post("/api/payee/delete", { handle:payee.handle });
        say(`Deleted ${payee.name}; cleared from ${deleted.cleared} transaction(s).`);
        await refresh();
      }) }, "Delete"))));
  const chosen = new Set(data.proposals.map((item) => item.transaction));
  const proposalRows = data.proposals.map((item) => {
    const box = el("input", { type:"checkbox", checked:"checked",
      "aria-label":`Accept ${item.payee_name} for ${item.description}` });
    box.addEventListener("change", () => {
      if (box.checked) chosen.add(item.transaction); else chosen.delete(item.transaction);
    });
    return el("tr", {}, el("td", {}, box), el("td", {}, item.date),
      el("td", {}, item.description), el("td", {}, item.payee_name), el("td", {}, item.key));
  });
  const accept = el("button", { class:"action primary", type:"button",
    disabled:data.proposals.length ? null : "disabled",
    onclick:run(async () => {
      const result = await post("/api/payees/accept", { transactions:[...chosen] });
      say(`Assigned ${result.assigned} payee(s); ${result.unchanged} left unchanged.`);
      await refresh();
    }) }, "Accept selected");
  return el("div", {},
    el("p", { class:"note" },
      "A payee records who a transaction was with; descriptions are never changed. "
      + "Matching ignores case, punctuation, and words containing digits, and is otherwise "
      + "exact. Nothing is assigned until you accept, and a transaction that already has "
      + "a payee is never changed."),
    el("div", { class:"panel panel-pad-16" }, el("h2", {}, "Payees"),
      data.payees.length
        ? table(["Payee", "Matches", { label:"Transactions", num:true }, ""], payeeRows)
        : el("p", { class:"note" }, "No payees yet."),
      form),
    el("div", { class:"panel panel-pad-16" }, el("h2", {}, "Proposals"),
      data.proposals.length
        ? table(["Accept", "Date", "Description", "Payee", "Matched key"], proposalRows)
        : el("p", { class:"note" }, "No transactions without a payee match a payee."),
      el("div", { class:"toolbar" }, accept)));
}

// Savings goals from the shared service: each income sets aside a share until the
// target date, and extra money can be allocated. The page only gathers input.
async function showGoals() {
  const showClosed = state.goalsShowClosed ? "?closed=1" : "";
  const data = await get(`/api/savings-goals${showClosed}`);
  const refresh = async () => { current = "Goals"; await render(); };
  const run = (action) => async () => {
    try { await action(); } catch (error) { say(error.message, "error"); }
  };
  const today = new Date().toISOString().slice(0, 10);
  const name = el("input", { name:"name", placeholder:"New roof" });
  const account = el("select", { name:"account" },
    ...data.accounts.map((item) => el("option", { value:item.handle }, item.name)));
  const target = el("input", { name:"target_amount", inputmode:"decimal", placeholder:"12000.00" });
  const start = el("input", { name:"start_date", type:"date", value:today });
  const by = el("input", { name:"target_date", type:"date" });
  const description = el("input", { name:"description", placeholder:"Optional" });
  let editing = null;
  const saveButton = el("button", { class:"action primary", type:"submit" }, "Add goal");
  const form = el("form", { class:"entry goal-form", onsubmit:(event) => {
    event.preventDefault();
    run(async () => {
      const saved = await post("/api/savings-goal/save", {
        handle:editing, name:name.value, account:account.value, target_amount:target.value,
        start_date:start.value, target_date:by.value, description:description.value });
      say(`Saved ${saved.name}.`);
      await refresh();
    })();
  } },
    el("label", {}, "Name", name), el("label", {}, "Held in", account),
    el("label", {}, "Target amount", target), el("label", {}, "Start saving", start),
    el("label", {}, "Target date", by), el("label", {}, "Description", description),
    saveButton);
  // Goals apply to every scenario unless one changes them; Edit picks the goal.
  const overrideGoal = el("strong", {}, "");
  const overrideScenario = el("select", { name:"scenario" },
    ...data.scenarios.map((item) => el("option", { value:item.handle }, item.name)));
  const overrideTarget = el("input", { name:"override_target", inputmode:"decimal",
    placeholder:"Target amount" });
  const overrideDate = el("input", { name:"override_date", type:"date" });
  const overrideExcluded = el("input", { name:"override_excluded", type:"checkbox" });
  // Optionally model the purchase: the target is spent on this date, in this scenario only.
  const purchaseOn = el("input", { name:"purchase_on", type:"date" });
  const purchaseAccount = el("select", { name:"purchase_account" },
    el("option", { value:"" }, "(no purchase)"),
    ...(data.purchase_accounts || []).map((item) => el("option", { value:item.handle }, item.name)));
  const overrideForm = el("form", { class:"entry goal-override", hidden:"hidden",
    onsubmit:(event) => {
      event.preventDefault();
      run(async () => {
        const saved = await post("/api/savings-goal/override", {
          handle:editing, scenario:overrideScenario.value, target_amount:overrideTarget.value,
          target_date:overrideDate.value, excluded:overrideExcluded.checked,
          purchase_on:purchaseOn.value, purchase_account:purchaseAccount.value });
        say(saved.text + ".");
        await refresh();
      })();
    } },
    el("p", { class:"note" }, "Change ", overrideGoal, " in one scenario. To model what it is "
      + "saved for, give a purchase date on or after the target date and the expense or asset "
      + "account it buys into. Leave every field empty and Leave out unchecked to follow the "
      + "goal unchanged."),
    el("label", {}, "Scenario", overrideScenario),
    el("label", {}, "Target amount", overrideTarget),
    el("label", {}, "Target date", overrideDate),
    el("label", {}, overrideExcluded, " Leave out"),
    el("label", {}, "Buy on", purchaseOn),
    el("label", {}, "Buy into", purchaseAccount),
    el("button", { class:"action", type:"submit" }, "Apply to scenario"));
  const rows = data.goals.map((goal) => {
    const amount = el("input", { inputmode:"decimal", placeholder:"Amount", size:"8",
      "aria-label":`Amount to allocate to ${goal.name}` });
    const closed = goal.status === "closed";
    return el("tr", {},
      el("td", {}, goal.name), el("td", {}, goal.account_name), el("td", {}, goal.target_date),
      el("td", { class:"num" }, money(goal.target)),
      el("td", { class:"num" }, money(goal.set_aside)),
      el("td", { class:"num" }, money(goal.remaining)),
      el("td", {}, goal.status_text),
      el("td", { class:"goal-changes" },
        (goal.overrides || []).map((item) => item.text).join("; ") || "—"),
      el("td", {},
        el("button", { class:"action", type:"button", onclick:() => {
          editing = goal.handle;
          overrideGoal.textContent = goal.name;
          overrideForm.hidden = !data.scenarios.length;
          name.value = goal.name; account.value = goal.account;
          target.value = goal.target; start.value = goal.start_date; by.value = goal.target_date;
          description.value = goal.description;
          saveButton.textContent = "Save changes";
          name.focus();
        } }, "Edit"),
        closed ? null : amount,
        closed ? null : el("button", { class:"action", type:"button", onclick:run(async () => {
          await post("/api/savings-goal/allocate", { handle:goal.handle, amount:amount.value });
          say(`Allocated ${amount.value} to ${goal.name}.`);
          await refresh();
        }) }, "Allocate"),
        el("button", { class:"action", type:"button", onclick:run(async () => {
          await post(closed ? "/api/savings-goal/reopen" : "/api/savings-goal/close",
            { handle:goal.handle });
          say(`${closed ? "Reopened" : "Closed"} ${goal.name}.`);
          await refresh();
        }) }, closed ? "Reopen" : "Close"),
        el("button", { class:"action", type:"button", onclick:run(async () => {
          if (!window.confirm(`Delete ${goal.name}? No transaction is changed.`)) return;
          await post("/api/savings-goal/delete", { handle:goal.handle });
          say(`Deleted ${goal.name}.`);
          await refresh();
        }) }, "Delete")));
  });
  const toggle = el("input", { type:"checkbox", checked:state.goalsShowClosed ? "checked" : null,
    onchange:(event) => { state.goalsShowClosed = event.target.checked; refresh(); } });
  const totals = data.goals.length
    ? `Set aside for goals: ${money(data.set_aside)}`
      + (Number(data.held) !== Number(data.set_aside)
        ? `; held from spendable cash: ${money(data.held)}` : "")
    : "";
  return el("div", { class:"savings-goals" },
    el("p", { class:"note" },
      "From its start date, each income received sets aside a share of what a goal still "
      + "needs, so the whole target is set aside by its target date. Extra money you "
      + "allocate is set aside in full. Goal money is held out of the Dashboard's Available, "
      + "like a bill's reserve; nothing is spent on the target date. Close a goal to release "
      + "its money."),
    el("div", { class:"panel panel-pad-16" }, el("h2", {}, "Savings goals"),
      el("label", {}, toggle, " Show closed goals"),
      data.goals.length
        ? table(["Goal", "Held in", "Target date", { label:"Target", num:true },
          { label:"Set aside", num:true }, { label:"Remaining", num:true }, "Status",
          "Scenario changes", ""], rows)
        : el("p", { class:"note" }, "No savings goals yet."),
      totals ? el("p", { class:"note" }, totals) : null,
      form, overrideForm));
}

async function showReimbursables() {
  // Every rule and write is in the shared receivables service; this page only
  // gathers input. The service keeps the Receivable-account reclassifications
  // (#170); nothing here changes a linked transaction.
  const data = await get("/api/receivables");
  const refresh = async () => { current = "Reimbursables"; await render(); };
  const run = (action) => async () => {
    try { await action(); } catch (error) { say(error.message, "error"); }
  };
  const from = state.receivableFrom
    ? data.costs.find((item) => item.transaction === state.receivableFrom) : null;
  const unavailable = state.receivableFrom && !from;
  state.receivableFrom = null;
  const today = new Date().toISOString().slice(0, 10);
  const selected = data.receivables.find((item) => item.handle === state.receivableOpen) || null;
  const field = (name, attrs = {}) => el("input", { name, ...attrs });
  // A saved scenario may expect less, nothing, or later from the payer.
  const scenarioRow = (selected) => {
    if (!(data.scenarios || []).length) {
      return el("p", { class:"note" },
        "Save a scenario to plan for a payer paying less, nothing, or later.");
    }
    const picker = el("select", { "aria-label":"Scenario" },
      data.scenarios.map((item) => el("option", { value:item.handle }, item.name)));
    const amount = field("scenario_amount", { inputmode:"decimal", placeholder:"as expected",
      "aria-label":"Amount expected in scenario" });
    const when = field("scenario_on", { type:"date", "aria-label":"Date expected in scenario" });
    const changes = selected.scenario_changes || [];
    return el("div", {},
      el("div", { class:"toolbar" }, el("strong", {}, "In scenario"), picker, amount, when,
        el("button", { class:"action", type:"button",
          title:"Leave both fields empty to expect what the receivable says",
          onclick:run(async () => {
            const changed = await post("/api/receivable/scenario", {
              scenario:picker.value, receivable:selected.handle,
              amount:amount.value, on:when.value });
            say(`${changed.text}.`);
            await refresh();
          }) }, "Apply to scenario")),
      el("p", { class:"note" }, changes.length
        ? `Scenario changes: ${changes.map((item) => item.text).join("; ")}`
        : "Every scenario expects what this receivable says."));
  };
  const payer = field("payer", { placeholder:"Acme Insurance",
    value:selected?.payer || "", "aria-label":"Payer" });
  const description = field("description", { placeholder:"What it was for",
    value:selected?.description ?? from?.description ?? "", "aria-label":"Description" });
  const incurred = field("incurred_date", { type:"date",
    value:selected?.incurred_date || from?.date || today, "aria-label":"Incurred" });
  const expected = field("expected_amount", { inputmode:"decimal", placeholder:"Optional",
    value:selected?.expected_amount ?? from?.value ?? "", "aria-label":"Expected back" });
  const expectedBy = field("expected_cash_date", { type:"date",
    value:selected?.expected_cash_date || "", "aria-label":"Expected by" });
  const heldIn = el("select", { name:"account", "aria-label":"Held in" },
    el("option", { value:"" }, "Default Receivable account"),
    ...data.accounts.map((account) => el("option", { value:account.handle,
      selected:selected?.account === account.handle ? "selected" : null }, account.name)));
  const form = el("form", { class:"entry", onsubmit:(event) => {
    event.preventDefault();
    run(async () => {
      const saved = await post("/api/receivable/save", {
        handle:selected?.handle || null, payer:payer.value, description:description.value,
        incurred_date:incurred.value, expected_amount:expected.value.trim() || null,
        expected_cash_date:expectedBy.value || null, account:heldIn.value || null,
        link_expense: from ? { transaction:from.transaction, split:from.split } : null,
      });
      state.receivableOpen = saved.handle;
      say(`Saved ${saved.payer}.` + (saved.linked === true ? " The expense is linked."
        : saved.linked === false ? " The expense could not be linked." : ""));
      await refresh();
    })();
  } },
    el("label", {}, "Payer", payer), el("label", {}, "Description", description),
    el("label", {}, "Incurred", incurred), el("label", {}, "Expected back", expected),
    el("label", {}, "Expected by", expectedBy), el("label", {}, "Held in", heldIn),
    el("div", { class:"toolbar" },
      el("button", { class:"action primary", type:"submit" },
        selected ? "Save changes" : "Add receivable"),
      selected ? el("button", { class:"action", type:"button", onclick:() => {
        state.receivableOpen = null; refresh();
      } }, "New") : null));

  const rows = data.receivables.map((item) => el("tr", {},
    el("td", {}, item.payer), el("td", {}, item.description || "—"),
    el("td", {}, item.incurred_date),
    el("td", { class:"num" }, money(item.expense_total)),
    el("td", { class:"num" }, money(item.reimbursed)),
    el("td", { class:"num" }, money(item.written_off)),
    el("td", { class:cls(item.remaining) }, money(item.remaining)),
    el("td", {}, item.status_label), el("td", { class:"num" }, `${item.age_days} d`),
    el("td", {}, item.expected_cash_date || "—"),
    el("td", {}, el("button", { class:"action", type:"button", onclick:() => {
      state.receivableOpen = item.handle; refresh();
    } }, "Open"))));

  const accepted = new Set(data.proposals.map((item) =>
    `${item.receivable}/${item.transaction}/${item.split}`));
  const proposalRows = data.proposals.map((item) => {
    const key = `${item.receivable}/${item.transaction}/${item.split}`;
    const box = el("input", { type:"checkbox", checked:"checked",
      "aria-label":`Accept ${item.description} for ${item.payer}` });
    box.addEventListener("change", () => {
      if (box.checked) accepted.add(key); else accepted.delete(key);
    });
    return el("tr", {}, el("td", {}, box), el("td", {}, item.date),
      el("td", {}, item.description), el("td", {}, item.payer),
      el("td", { class:"num" }, money(item.amount)),
      el("td", { class:cls(item.remaining_after) }, money(item.remaining_after)),
      el("td", { class:"muted" }, item.reason));
  });
  const proposals = el("div", { class:"panel panel-pad-16" },
    el("h2", {}, "Proposed reimbursements"),
    data.proposals.length
      ? table(["Accept", "Date", "Description", "Payer", { label:"Amount", num:true },
        { label:"Remaining after", num:true }, "Why"], proposalRows)
      : el("p", { class:"note" },
        "No unlinked credits clearly reimburse an open receivable."),
    el("div", { class:"toolbar" }, el("button", { class:"action primary", type:"button",
      disabled:data.proposals.length ? null : "disabled",
      onclick:run(async () => {
        const result = await post("/api/receivables/accept",
          { links:[...accepted].map((key) => key.split("/")) });
        say(`Linked ${result.linked} reimbursement(s); ${result.unchanged} left unchanged.`);
        await refresh();
      }) }, "Accept selected")));

  let detail = null;
  if (selected) {
    const linked = [
      ...selected.expenses.map((link) => ["Expense", link]),
      ...selected.reimbursements.map((link) => ["Reimbursement", link]),
    ].map(([role, link]) => el("tr", {},
      el("td", {}, role), el("td", {}, link.date || "—"),
      el("td", {}, link.description ?? "Linked split no longer exists"),
      el("td", {}, link.account || ""),
      el("td", { class:"num" }, link.value == null ? "" : money(link.value)),
      el("td", {}, el("button", { class:"action", type:"button", onclick:run(async () => {
        await post("/api/receivable/unlink", { receivable:selected.handle,
          transaction:link.transaction, split:link.split });
        say("Unlinked; the transaction itself is unchanged.");
        await refresh();
      }) }, "Unlink"))));
    const taken = new Set([...selected.expenses, ...selected.reimbursements]
      .map((link) => `${link.transaction}/${link.split}`));
    const picker = (items, label) => el("select", { "aria-label":label },
      ...items.filter((item) => !taken.has(`${item.transaction}/${item.split}`))
        .map((item) => el("option", { value:`${item.transaction}/${item.split}` },
          `${item.date} · ${item.description} · ${item.account} · ${item.value}`)));
    const costPicker = picker(data.costs, "Expense split to link");
    const creditPicker = picker(data.credits, "Reimbursement split to link");
    const link = (role, select) => run(async () => {
      if (!select.value) { say("There is no split to link.", "error"); return; }
      const [transaction, split] = select.value.split("/");
      await post("/api/receivable/link", { receivable:selected.handle, role, transaction, split });
      say(`Linked the ${role}.`);
      await refresh();
    });
    const disputeDate = field("disputed_on", { type:"date",
      value:selected.disputed_on || today, "aria-label":"Dispute date" });
    const disputeNote = field("note", { value:selected.dispute_note || "",
      placeholder:"Why the payer contests it", "aria-label":"Dispute note" });
    const writeOffAmount = field("amount", { inputmode:"decimal", placeholder:"0.00",
      "aria-label":"Write-off amount" });
    const writeOffDate = field("written_off_on", { type:"date", value:today,
      "aria-label":"Write-off date" });
    const writeOffReason = field("reason", { placeholder:"Reason",
      "aria-label":"Write-off reason" });
    detail = el("div", { class:"panel panel-pad-16" },
      el("h2", {}, `${selected.payer} — linked splits`),
      selected.account_name ? el("p", { class:"note" },
        `Owed ${money(selected.owed)}, held in ${selected.account_name}.`) : null,
      selected.fsa_claims.length ? el("p", { class:"note negative" },
        "An expense linked here is also on an FSA claim. Check that the same cost is not "
        + "expected back from both.") : null,
      ...(selected.shared_costs || []).map((shared) => el("p",
        { class:shared.needs_review ? "note negative" : "note" }, shared.text)),
      linked.length ? table(["Role", "Date", "Description", "Account",
        { label:"Amount", num:true }, ""], linked)
        : el("p", { class:"note" }, "Nothing linked yet."),
      el("div", { class:"toolbar" }, costPicker,
        el("button", { class:"action", type:"button", onclick:link("expense", costPicker) },
          "Link expense")),
      el("div", { class:"toolbar" }, creditPicker,
        el("button", { class:"action", type:"button",
          onclick:link("reimbursement", creditPicker) }, "Link reimbursement")),
      el("div", { class:"toolbar" }, el("strong", {}, "Dispute"), disputeDate, disputeNote,
        el("button", { class:"action", type:"button", onclick:run(async () => {
          await post("/api/receivable/dispute", { handle:selected.handle,
            disputed_on:disputeDate.value, note:disputeNote.value });
          say("Marked disputed.");
          await refresh();
        }) }, "Mark disputed"),
        el("button", { class:"action", type:"button", onclick:run(async () => {
          await post("/api/receivable/dispute", { handle:selected.handle, clear:true });
          say("Dispute cleared.");
          await refresh();
        }) }, "Clear dispute")),
      el("div", { class:"toolbar" }, el("strong", {}, "Write off"), writeOffAmount,
        writeOffDate, writeOffReason,
        el("button", { class:"action", type:"button", onclick:run(async () => {
          await post("/api/receivable/write-off", { receivable:selected.handle,
            amount:writeOffAmount.value, written_off_on:writeOffDate.value,
            reason:writeOffReason.value });
          say("Wrote off the amount; it is back in the expense account.");
          await refresh();
        }) }, "Record write-off")),
      scenarioRow(selected),
      el("div", { class:"toolbar" },
        el("button", { class:"action", type:"button", onclick:run(async () => {
          if (!window.confirm(`Delete the receivable from ${selected.payer}? `
            + "Its transactions are unchanged.")) return;
          await post("/api/receivable/delete", { handle:selected.handle });
          state.receivableOpen = null;
          say("Deleted the receivable; its transactions are unchanged.");
          await refresh();
        }) }, "Delete receivable")));
  }

  return el("div", {},
    el("p", { class:"note" },
      "Track an expense you paid that an insurer, employer, or other payer owes back. "
      + "What is owed moves from the expense into a Receivable account: part of net worth, "
      + "never of liquidity, because it cannot be spent yet. A reimbursement is an ordinary "
      + "credit to the same expense account, never income; a write-off returns the balance "
      + "to the expense, and a dispute posts nothing. The original expense is never changed."),
    unavailable ? el("p", { class:"note negative" },
      "That transaction has no expense to track as reimbursable.") : null,
    from ? el("p", { class:"note" }, `Saving links the ${from.value} expense from `
      + `${from.date} “${from.description}”.`) : null,
    el("div", { class:"panel panel-pad-16" }, el("h2", {}, "Reimbursable expenses"),
      data.receivables.length
        ? table(["Payer", "Description", "Incurred", { label:"Expense", num:true },
          { label:"Reimbursed", num:true }, { label:"Written off", num:true },
          { label:"Remaining", num:true }, "Status", { label:"Age", num:true },
          "Expected by", ""], rows)
        : el("p", { class:"note" }, "No reimbursable expenses yet."),
      form),
    proposals,
    detail);
}

async function showRules() {
  const data = await get("/api/rules");
  const refresh = async () => { current = "Rules"; await render(); };
  const run = (action) => async () => {
    try { await action(); } catch (error) { say(error.message, "error"); }
  };
  const kind = el("select", { name:"kind" },
    el("option", { value:"description" }, "Description"),
    el("option", { value:"payee" }, "Payee"));
  const description = el("input", { name:"description",
    placeholder:"Example description, e.g. CORNER GROCER #1234" });
  const payee = el("select", { name:"payee" },
    ...data.payees.map((item) => el("option", { value:item.handle }, item.name)));
  const category = el("select", { name:"category" },
    ...data.categories.map((item) => el("option", { value:item.handle }, item.name)));
  const setPayee = el("select", { name:"set_payee" },
    el("option", { value:"" }, "(no payee)"),
    ...data.payees.map((item) => el("option", { value:item.handle }, item.name)));
  const setPayeeLabel = el("label", {}, "Also set payee", setPayee);
  const syncKind = () => {
    description.hidden = kind.value !== "description";
    payee.hidden = kind.value !== "payee";
    setPayeeLabel.hidden = kind.value !== "description";
  };
  kind.addEventListener("change", syncKind);
  syncKind();
  const form = el("form", { class:"entry", onsubmit:(event) => {
    event.preventDefault();
    run(async () => {
      const body = { category:category.value };
      if (kind.value === "payee") body.payee = payee.value || null;
      else {
        body.description = description.value;
        if (setPayee.value) body.set_payee = setPayee.value;
      }
      await post("/api/rule/add", body);
      say("Rule added.");
      await refresh();
    })();
  } },
    el("label", {}, "Match", kind),
    el("label", {}, "Matching", description, payee),
    el("label", {}, "Category", category),
    setPayeeLabel,
    el("button", { class:"action primary", type:"submit" }, "Add rule"));
  const last = data.rules.length;
  const ruleRows = data.rules.map((rule) => el("tr", {},
    el("td", { class:"num" }, String(rule.position)),
    el("td", {}, rule.payee ? `Payee ${rule.payee_name || rule.payee}` : `Description ${rule.key}`),
    el("td", {}, rule.category_name),
    el("td", {}, rule.set_payee ? (rule.set_payee_name || rule.set_payee) : "—"),
    el("td", {},
      el("button", { class:"action", type:"button", disabled:rule.position === 1 ? "disabled" : null,
        onclick:run(async () => {
          await post("/api/rule/move", { handle:rule.handle, position:rule.position - 1 });
          await refresh();
        }) }, "Up"),
      el("button", { class:"action", type:"button", disabled:rule.position === last ? "disabled" : null,
        onclick:run(async () => {
          await post("/api/rule/move", { handle:rule.handle, position:rule.position + 1 });
          await refresh();
        }) }, "Down"),
      el("button", { class:"action", type:"button", onclick:run(async () => {
        await post("/api/rule/delete", { handle:rule.handle });
        say(`Deleted rule ${rule.position}.`);
        await refresh();
      }) }, "Delete"))));
  const chosen = new Set(data.proposals.map((item) => item.transaction));
  const proposalRows = data.proposals.map((item) => {
    const box = el("input", { type:"checkbox", checked:"checked",
      "aria-label":`Accept ${item.category_name} for ${item.description}` });
    box.addEventListener("change", () => {
      if (box.checked) chosen.add(item.transaction); else chosen.delete(item.transaction);
    });
    const conflicts = item.conflicts.map((c) => `rule ${c.rule_position}: ${c.category_name}`)
      .join("; ");
    return el("tr", {}, el("td", {}, box), el("td", {}, item.date),
      el("td", {}, item.description), el("td", { class:"num" }, money(item.amount)),
      el("td", {}, item.payee ? `${item.category_name}; payee ${item.payee_name || item.payee}`
        : item.category_name),
      el("td", { class:"num" }, String(item.rule_position)),
      el("td", {}, conflicts || "—"));
  });
  const accept = el("button", { class:"action primary", type:"button",
    disabled:data.proposals.length ? null : "disabled",
    onclick:run(async () => {
      const result = await post("/api/rules/accept", { transactions:[...chosen] });
      say(`Categorized ${result.assigned} transaction(s); ${result.unchanged} left unchanged`
        + (result.payees_set ? `; set the payee on ${result.payees_set}.` : "."));
      await refresh();
    }) }, "Accept selected");
  return el("div", {},
    el("p", { class:"note" },
      "Rules propose a category for imported transactions still in Uncategorized CSV or "
      + "Uncategorized OFX. The first matching rule decides; a later rule that would choose "
      + "differently is listed as a conflict. A category you chose is never replaced, and "
      + "nothing changes until you accept. A description rule can also set a payee on a "
      + "transaction that has none."),
    el("div", { class:"panel panel-pad-16" }, el("h2", {}, "Rules"),
      data.rules.length
        ? table([{ label:"Rule", num:true }, "Matches", "Category", "Sets payee", ""], ruleRows)
        : el("p", { class:"note" }, "No rules yet."),
      form),
    el("div", { class:"panel panel-pad-16" }, el("h2", {}, "Proposals"),
      data.proposals.length
        ? table(["Accept", "Date", "Description", { label:"Amount", num:true }, "Category",
          { label:"Rule", num:true }, "Also matched"], proposalRows)
        : el("p", { class:"note" }, "No uncategorized imported transactions match a rule."),
      el("div", { class:"toolbar" }, accept)));
}
