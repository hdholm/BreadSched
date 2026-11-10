// Match to Plan: match actuals to planned occurrences by split and amount, as a
// statement is reconciled. Both windows write through /api/v1.

async function postV1(path, body) {
  // The book server's own JSON, with no browser-only fields added.
  const response = await fetch(path, {
    method: "POST",
    headers: { ...apiHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.message || payload.error || response.statusText);
    error.code = payload.code;
    throw error;
  }
  return payload;
}

function matchAmount(text) {
  // The amount typed, in the book server's plain decimal form, or null.
  let cleaned = String(text || "").trim().replace(/[$\s]/g, "");
  if (!cleaned) return null;
  if (/^\(.*\)$/.test(cleaned)) cleaned = `-${cleaned.slice(1, -1)}`;
  const decimal = browserNumberFormat === "comma"
    ? cleaned.replace(/\./g, "").replace(",", ".")
    : cleaned.replace(/,/g, "");
  return /^-?\d*\.?\d+$/.test(decimal) ? decimal : null;
}

function rational(value) {
  // A stored amount: decimal text, or the book's [numerator, denominator] pair.
  return Array.isArray(value) ? String(value[0] / value[1]) : value;
}

function addAmounts(a, b) {
  // Sum two decimal amounts to the cent without floating drift.
  return (Math.round(Number(a) * 100 + Number(b) * 100) / 100).toFixed(2);
}

function matchBackdrop(label, onClose) {
  document.querySelector(".match-backdrop")?.remove();
  const backdrop = el("div", { class: "detail-backdrop match-backdrop" });
  const close = () => { backdrop.remove(); if (onClose) onClose(); };
  backdrop.addEventListener("keydown", (event) => { if (event.key === "Escape") close(); });
  const dialog = el("section", { class: "detail-dialog match-dialog", role: "dialog",
    "aria-label": label });
  backdrop.append(dialog);
  document.body.append(backdrop);
  return { backdrop, dialog, close };
}

async function openOccurrenceMatch(key, onClose) {
  const { dialog, close } = matchBackdrop("Match to Plan", onClose);
  let message = "";
  const draw = async () => {
    let view;
    try {
      view = await get(`/api/v1/plan/matching?${new URLSearchParams({ occurrence: key })}`);
    } catch (error) { dialog.replaceChildren(el("p", { class: "error" }, error.message)); return; }
    const period = view.period_end ? `${view.planned_date} to ${view.period_end}` : view.planned_date;
    const allocate = async (candidate, amount) => {
      try {
        await postV1(`/api/v1/transactions/${encodeURIComponent(candidate.transaction)}`
          + `/splits/${encodeURIComponent(candidate.split)}/allocations`,
        { occurrence: key, amount });
        message = "";
      } catch (error) { message = error.message; }
      draw();
    };
    const rows = view.candidates.map((candidate) => {
      const amount = el("input", { class: "match-amount", inputmode: "decimal",
        "aria-label": "Amount to count",
        value: money(Number(candidate.allocated) ? candidate.allocated : candidate.unallocated) });
      const check = el("input", { type: "checkbox", "aria-label": "Counts toward it" });
      check.checked = Boolean(Number(candidate.allocated));
      check.addEventListener("change", () => {
        if (!check.checked) { allocate(candidate, "0"); return; }
        const typed = matchAmount(amount.value);
        if (typed == null) { message = "Type an amount to count toward it."; draw(); return; }
        allocate(candidate, typed);
      });
      return el("tr", { class: "match-candidate" },
        el("td", {}, check),
        el("td", {}, candidate.date),
        el("td", {},
          el("div", {}, [candidate.num, candidate.description].filter(Boolean).join("  ")),
          el("div", { class: "note" },
            `${candidate.account_name} ← ${candidate.other_accounts.join(", ")}`),
          [candidate.memo, candidate.notes].some(Boolean)
            ? el("div", { class: "note" }, [candidate.memo, candidate.notes].filter(Boolean).join(" · "))
            : null),
        el("td", { class: cls(candidate.amount) }, money(candidate.amount)),
        el("td", { class: "num" }, amount));
    });
    const toggle = el("button", { class: "action", type: "button",
      disabled: view.closed && !view.closed_early ? "disabled" : null,
      title: "What remains of it is no longer expected; what was matched still counts.",
      onclick: async () => {
        try {
          await postV1(`/api/v1/plan/occurrences/${view.closed_early ? "reopen" : "close"}`,
            { occurrence: key });
          message = "";
        } catch (error) { message = error.message; }
        draw();
      } }, view.closed_early ? "Reopen occurrence" : "Close occurrence");
    dialog.replaceChildren(...[
      el("div", { class: "detail-heading" },
        el("div", {},
          el("h2", {}, `${view.estimate ? "Estimate" : "Scheduled"}: ${view.description}`),
          el("div", { class: "note" }, period)),
        el("button", { class: "action", type: "button", onclick: close }, "Done")),
      el("div", { class: "cards detail-summary match-totals" },
        [["Expected", view.expected], ["Applied", view.applied], ["Remaining", view.remaining]]
          .map(([label, value]) => el("div", { class: "card" },
            el("div", { class: "label" }, label),
            el("div", { class: "value" }, money(value))))),
      view.closed ? el("p", { class: "note" }, "Closed: what remains no longer counts.") : null,
      rows.length
        ? table(["", "Date", "Description", { label: "Split", num: true },
          { label: "Counts", num: true }], rows)
        : el("p", { class: "note" }, "No splits in its period could count toward it."),
      message ? el("p", { class: "error" }, message) : null,
      el("div", { class: "toolbar" }, toggle)].filter(Boolean));
  };
  await draw();
}

async function openTransactionMatch(handle, onClose) {
  const { dialog, close } = matchBackdrop("Match to Plan", onClose);
  let message = "";
  const base = `/api/v1/transactions/${encodeURIComponent(handle)}`;
  const draw = async () => {
    let view;
    try { view = await get(`${base}/matching`); }
    catch (error) { dialog.replaceChildren(el("p", { class: "error" }, error.message)); return; }
    const send = async (path, body) => {
      try { await postV1(path, body); message = ""; } catch (error) { message = error.message; }
      draw();
    };
    const legs = view.legs.map((leg) => {
      const where = `${base}/splits/${encodeURIComponent(leg.split)}`;
      const choose = el("select", { "aria-label": `Planned item for ${leg.account_name}`,
        disabled: leg.not_planned || !leg.candidates.length ? "disabled" : null },
      ...(leg.candidates.length
        ? leg.candidates.map((item) => el("option", { value: item.key },
          `${item.planned_date} ${item.description} (${money(item.remaining)} remains)`))
        : [el("option", { value: "" }, "No open planned item")]));
      const amount = el("input", { class: "match-amount", inputmode: "decimal",
        "aria-label": `Amount of ${leg.account_name}`, value: money(leg.unallocated) });
      const notPlanned = el("input", { type: "checkbox", "aria-label": "Not planned" });
      notPlanned.checked = leg.not_planned;
      notPlanned.addEventListener("change", () =>
        send(`${where}/not-planned`, { not_planned: notPlanned.checked }));
      return el("div", { class: "panel match-leg" },
        el("strong", {}, `${leg.account_name}${leg.memo ? ` (${leg.memo})` : ""}`),
        el("span", { class: "note" },
          ` ${money(leg.amount)} · unallocated ${money(leg.unallocated)}`),
        ...leg.allocations.map((item) => el("div", { class: "row match-allocation" },
          el("span", {}, `→ ${(leg.candidates.find((c) => c.key === item.occurrence) || {}).description
            || item.occurrence}  ${money(rational(item.amount))}`),
          el("button", { class: "action", type: "button",
            onclick: () => send(`${where}/allocations`, { occurrence: item.occurrence, amount: "0" }) },
          "Remove"))),
        el("div", { class: "row" }, choose, amount,
          el("button", { class: "action primary", type: "button",
            disabled: leg.not_planned || !leg.candidates.length || !Number(leg.unallocated)
              ? "disabled" : null,
            onclick: () => {
              const typed = matchAmount(amount.value);
              if (typed == null) { message = "Type an amount to match."; draw(); return; }
              // Match counts this much more toward the item chosen.
              const already = leg.allocations
                .filter((item) => item.occurrence === choose.value)
                .reduce((sum, item) => addAmounts(sum, rational(item.amount)), "0");
              send(`${where}/allocations`,
                { occurrence: choose.value, amount: addAmounts(already, typed) });
            } }, "Match")),
        el("label", {}, notPlanned, " Not planned"));
    });
    const state = { matched: "Matched", unexpected: "Not planned" }[view.resolution] || "Still to match";
    dialog.replaceChildren(...[
      el("div", { class: "detail-heading" },
        el("div", {},
          el("h2", {}, `${view.date} · ${view.description}`),
          el("div", { class: "note" }, state + (view.notes ? ` · Notes: ${view.notes}` : ""))),
        el("button", { class: "action", type: "button", onclick: close }, "Done")),
      ...(legs.length ? legs : [el("p", { class: "note" }, "This transaction has nothing to match.")]),
      message ? el("p", { class: "error" }, message) : null].filter(Boolean));
  };
  await draw();
}
