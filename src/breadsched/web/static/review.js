// Review: matching actual transactions to planned occurrences.

async function showReview() {
  let data = await get(state.review
    ? `/api/review?transaction=${encodeURIComponent(state.review)}`
    : "/api/review");
  if (!state.review && data.actuals.length) {
    state.review = data.actuals[0].handle;
    data = await get(`/api/review?transaction=${encodeURIComponent(state.review)}`);
  }

  const actualButtons = data.actuals.map((actual) => el("button", {
    type: "button",
    class: actual.handle === state.review ? "active" : null,
    onclick: () => { state.review = actual.handle; render(); },
  },
  el("strong", {}, `${actual.date}  ${actual.description}`),
  el("span", { class: "amount" }, money(actual.amount))));

  if (!data.actuals.length) {
    state.review = null;
    return el("div", {},
      el("h2", {}, "Resolve actuals"),
      el("p", { class: "note" },
        "No unresolved actual transactions. Imported historical transactions do not enter this queue."));
  }

  const selected = data.selected;
  const help = data.action_help || {};
  const candidateRows = data.candidates.map((candidate) => {
    const variance = Number(candidate.amount_variance);
    const varianceText = `${variance > 0 ? "+" : ""}${money(candidate.amount_variance)}`;
    const dateText = `${candidate.date_variance_days >= 0 ? "+" : ""}${candidate.date_variance_days} day(s)`;
    return el("tr", {},
      el("td", {}, candidate.date),
      el("td", {},
        el("div", {}, candidate.description),
        el("div", { class: "note" }, candidate.label),
        el("ul", { class: "review-reasons" },
          (candidate.reasons || []).map((reason) => el("li", {}, reason)))),
      el("td", { class: "num" }, money(candidate.expected_amount)),
      el("td", { class: cls(candidate.amount_variance) }, varianceText),
      el("td", { class: "num" }, dateText),
      el("td", {},
        el("div", { class: "review-actions" },
          el("button", {
            class: "action primary", type: "button", title: help.match,
            onclick: async () => {
              try {
                await post("/api/review/match", {
                  transaction: selected.handle, occurrence: candidate.key,
                });
                say("Actual matched to planned occurrence.");
                state.review = null;
                render();
              } catch (error) { say(error.message, "error"); }
            },
          }, "Match"),
          el("button", {
            class: "action", type: "button", title: help.reject,
            onclick: async () => {
              try {
                await post("/api/review/reject", {
                  transaction: selected.handle, occurrence: candidate.key,
                });
                say("Candidate rejected.");
                render();
              } catch (error) { say(error.message, "error"); }
            },
          }, "Reject candidate"),
          el("button", {
            class: "action", type: "button", title: help.skip,
            onclick: async () => {
              try {
                await post("/api/review/skip", {
                  transaction: selected.handle, occurrence: candidate.key,
                });
                say("Scheduled occurrence skipped.");
                render();
              } catch (error) { say(error.message, "error"); }
            },
          }, "Skip scheduled occurrence"))));
  });

  const fsaOptions = selected.fsa || { claims: [], roles: [] };
  let fsaAttach = null;
  if (fsaOptions.claims.length && fsaOptions.roles.length) {
    const claimSelect = el("select", {},
      fsaOptions.claims.map((claim, index) => el("option", { value: claim.handle },
        `${index === 0 ? "Suggested · " : ""}${claim.label} · remaining ${money(claim.remaining)} · ${claim.reason}`)));
    const roleSelect = el("select", {},
      fsaOptions.roles.map((role) => el("option", {
        value: `${role.role}|${role.split}|${(role.years || []).join(",")}`,
      }, `${role.label || role.role.replace("_", " ")} · ${role.account}`)));
    const yearSelect = el("select", {}, el("option", { value: "" }, "Auto funding year"));
    const refreshYears = () => {
      const parts = roleSelect.value.split("|");
      const years = (parts[2] || "").split(",").filter(Boolean);
      yearSelect.replaceChildren(el("option", { value: "" }, "Auto funding year"),
        ...years.map((year) => el("option", { value: year }, year)));
    };
    const applySuggestedRole = () => {
      const claim = fsaOptions.claims.find((item)=>item.handle === claimSelect.value);
      if (!claim) return;
      const option = Array.from(roleSelect.options).find((item)=> {
        const [role, split] = item.value.split("|");
        return role === claim.suggested_role && split === claim.suggested_split;
      });
      if (option) roleSelect.value = option.value;
      refreshYears();
    };
    roleSelect.onchange = refreshYears;
    claimSelect.onchange = applySuggestedRole;
    applySuggestedRole();
    fsaAttach = el("div", { class: "panel panel-pad-10-top" },
      el("strong", {}, "FSA claim"),
      el("div", { class: "review-actions" }, claimSelect, roleSelect, yearSelect,
        el("button", { class: "action", type: "button", title: help.fsa, onclick: async () => {
          const [role, split] = roleSelect.value.split("|");
          try {
            await post("/api/review/fsa-attach", {
              transaction: selected.handle, claim: claimSelect.value, role, split,
              funding_year: yearSelect.value,
            });
            say("Transaction attached to FSA claim."); render();
          } catch (error) { say(error.message, "error"); }
        }}, "Attach to claim")));
  }

  const detail = el("div", { class: "panel review-detail" },
    el("h2", {}, "Resolve actual"),
    el("p", {}, `${selected.date} · ${selected.description}`),
    el("p", { class: "note" }, `Actual amount: ${money(selected.amount)}`),
    selected.fsa_hint ? el("p", { class: "note" }, selected.fsa_hint) : null,
    fsaAttach,
    data.candidates.length
      ? table(["Planned", "Description", { label: "Expected", num: true },
               { label: "Amount variance", num: true }, { label: "Date variance", num: true },
               "Action"], candidateRows)
      : el("p", { class: "note" }, selected.no_candidate_reason
        || "No candidate within the matching window."),
    el("div", { class: "review-actions" },
      el("button", {
        class: "action", type: "button", title: help.unexpected,
        onclick: async () => {
          try {
            await post("/api/review/unexpected", { transaction: selected.handle });
            say("Actual marked unexpected.");
            state.review = null;
            render();
          } catch (error) { say(error.message, "error"); }
        },
      }, "Mark unexpected")),
    el("details", { class: "review-help" },
      el("summary", {}, "What each action does"),
      el("ul", {}, ["match", "reject", "skip", "unexpected", "fsa"]
        .filter((key) => help[key]).map((key) => el("li", {}, help[key])))));

  return el("div", {},
    el("h2", {}, "Review"),
    el("p", { class: "note" },
      "Match entered or imported actual transactions to scheduled expectations, reject bad suggestions, "
      + "or mark transactions that are intentionally outside the plan."),
    el("div", { class: "review-grid" },
      el("div", { class: "panel review-list" }, actualButtons), detail));
}
