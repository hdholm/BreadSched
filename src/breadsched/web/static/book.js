// Verify, the packaged user guide, and your own account (decision 0004).

async function showVerify() {
  const data = await get("/api/verify");
  const rerun = el("button", {class:"action", onclick:()=>render()}, "Verify again");
  const findings = [];
  (data.sqlite || []).forEach((problem) => findings.push(
    el("li", {}, `SQLite: ${problem}`)));
  (data.issues || []).forEach((issue) => findings.push(
    el("li", {}, `${issue.code}: ${issue.message}`)));
  return el("div", {},
    el("div", {class:"toolbar"}, rerun),
    el("div", {class:"panel scenario-panel"},
      el("h2", {}, data.ok ? "No problems found" : "Book verification found problems"),
      data.ok
        ? el("p", {}, "SQLite integrity and BreadSched financial relationships are clean.")
        : el("ul", {}, findings)),
  );
}

// The packaged user guide (#181): the overview and the desktop, browser, and
// command-line parts, the same Markdown the other interfaces show. Only the small
// subset the guide uses is rendered, always as DOM nodes, never as HTML text.
const guideSlug = (text) => "guide-" + text.replace(/[`*_]/g, "").trim().toLowerCase()
  .replace(/[^\p{L}\p{N}_\- ]/gu, "").replace(/ /g, "-");

async function showGuide() {
  // A workflow's Help button names a topic; the server picks its part and heading.
  const topic = state.guideTopic;
  state.guideTopic = null;
  const data = await get(topic
    ? "/api/guide?topic=" + encodeURIComponent(topic)
    : "/api/guide?part=" + encodeURIComponent(state.guidePart || "overview"));
  state.guidePart = data.part;
  const anchor = topic ? data.anchor : state.guideAnchor;
  state.guideAnchor = null;
  const jump = (id) => document.getElementById(id)?.scrollIntoView();
  const open = (part, target) => {
    if (part === data.part) { if (target) jump(guideSlug(target)); return; }
    state.guidePart = part;
    state.guideAnchor = target || null;
    switchTo("Guide");
  };
  const inline = (text) => {
    const parts = [];
    const pattern = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)]+)\)/g;
    let last = 0;
    for (const match of text.matchAll(pattern)) {
      if (match.index > last) parts.push(text.slice(last, match.index));
      if (match[1] !== undefined) parts.push(el("code", {}, match[1]));
      else if (match[2] !== undefined) parts.push(el("strong", {}, match[2]));
      else parts.push(guideLink(match[3], match[4]));
      last = match.index + match[0].length;
    }
    if (last < text.length) parts.push(text.slice(last));
    return parts;
  };
  const guideLink = (label, href) => {
    const [path, target] = href.split("#");
    const part = path ? data.files[path.split("/").pop()] : data.part;
    if (part) {
      return el("a", { href: `#${target || ""}`, onclick: (event) => {
        event.preventDefault(); open(part, target);
      } }, ...inline(label));
    }
    return /^https:\/\//.test(href)
      ? el("a", { href, target: "_blank", rel: "noopener noreferrer" }, ...inline(label))
      : el("span", {}, ...inline(label));
  };

  const body = el("article", { class: "guide" });
  let paragraph = [];
  let list = null;
  let item = null;
  let code = null;
  const flush = () => {
    if (paragraph.length) body.append(el("p", {}, ...inline(paragraph.join(" "))));
    paragraph = [];
    list = null;
    item = null;
  };
  for (const raw of data.markdown.split("\n")) {
    const line = raw.trimEnd();
    if (line.startsWith("```")) {
      if (code === null) { flush(); code = []; }
      else { body.append(el("pre", {}, el("code", {}, code.join("\n")))); code = null; }
      continue;
    }
    if (code !== null) { code.push(line); continue; }
    if (!line) { flush(); continue; }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flush();
      const level = Math.min(heading[1].length + 1, 6);
      body.append(el(`h${level}`, { id: guideSlug(heading[2]) }, ...inline(heading[2])));
      continue;
    }
    const bullet = line.match(/^\s*(?:[-*]|(\d+)\.)\s+(.+)$/);
    if (bullet) {
      if (paragraph.length) { const kept = list; flush(); list = kept; }
      const tag = bullet[1] ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== tag) { list = el(tag); body.append(list); }
      item = { text: bullet[2], node: el("li") };
      list.append(item.node);
      item.node.append(...inline(item.text));
      continue;
    }
    if (item && /^\s+/.test(raw)) {
      item.text += " " + line.trim();
      item.node.replaceChildren(...inline(item.text));
      continue;
    }
    list = null;
    item = null;
    paragraph.push(line.trim());
  }
  flush();

  const switcher = el("div", { class: "toolbar", role: "tablist" },
    ...data.parts.map((entry) => el("button", {
      class: entry.part === data.part ? "action primary" : "action", type: "button",
      role: "tab", "aria-selected": entry.part === data.part ? "true" : "false",
      onclick: () => open(entry.part, null),
    }, entry.title)));
  if (anchor) setTimeout(() => jump(guideSlug(anchor)), 0);
  return el("div", {}, switcher, body);
}

// Your own account: your password, two-step sign-in, and signing out. Nothing here
// touches the book.
async function showAccount() {
  const me = await get("/api/v1/me");
  const wrap = el("section", { class: "account-view" },
    el("h2", {}, "Your account"),
    el("p", {}, `Signed in as ${me.display_name || me.name} (${me.name}).`));

  // Password: the current one once there is one; the two new entries must agree.
  const current = el("input", { type: "password", name: "current", autocomplete: "current-password" });
  const fresh = el("input", { type: "password", name: "new", autocomplete: "new-password" });
  const again = el("input", { type: "password", name: "again", autocomplete: "new-password" });
  const passwordNote = el("p", { class: "note", role: "status" });
  const passwordForm = el("form", { class: "account-form" },
    el("h3", {}, me.has_password ? "Change your password" : "Set a password"),
    me.has_password ? el("label", {}, "Current password", current) : null,
    el("label", {}, "New password (10 characters or more)", fresh),
    el("label", {}, "New password again", again),
    passwordNote,
    el("button", { type: "submit", class: "action" }, "Save password"));
  passwordForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (fresh.value !== again.value) {
      passwordNote.textContent = "The two new passwords differ.";
      return;
    }
    try {
      await post("/api/v1/me/password", { current: current.value, new: fresh.value });
      passwordNote.textContent = "Your password is saved.";
      current.value = fresh.value = again.value = "";
    } catch (error) {
      passwordNote.textContent = error.message;
    }
  });

  const twoStep = el("section", { class: "two-step" });
  const showTwoStep = (on) => {
    const note = el("p", { class: "note", role: "status" });
    if (on) {
      const password = el("input", { type: "password", name: "password", autocomplete: "current-password" });
      const off = el("form", { class: "account-form" },
        el("h3", {}, "Two-step sign-in is on"),
        el("p", {}, "Signing in from another device asks for a code from your authenticator app."),
        el("label", {}, "Password, to turn it off", password), note,
        el("button", { type: "submit", class: "action" }, "Turn off"));
      off.addEventListener("submit", async (event) => {
        event.preventDefault();
        try {
          await post("/api/v1/me/two-step/off", { password: password.value });
          showTwoStep(false);
        } catch (error) { note.textContent = error.message; }
      });
      twoStep.replaceChildren(off);
      return;
    }
    const start = el("button", { type: "button", class: "action" }, "Turn on two-step sign-in");
    start.addEventListener("click", async () => {
      let setup;
      try { setup = await post("/api/v1/me/two-step", {}); } catch (error) {
        note.textContent = error.message;
        return;
      }
      const code = el("input", { type: "text", name: "code", inputmode: "numeric",
        autocomplete: "one-time-code" });
      const confirm = el("form", { class: "account-form" },
        el("h3", {}, "Add BreadSched to your authenticator app"),
        el("p", {}, "Open this on the phone with the app, or type the secret into it:"),
        el("p", {}, el("a", { href: setup.uri, class: "action" }, "Open in authenticator app")),
        el("p", {}, el("code", { class: "two-step-secret" }, setup.secret)),
        el("label", {}, "The code the app shows now", code), note,
        el("button", { type: "submit", class: "action" }, "Turn on"));
      confirm.addEventListener("submit", async (event) => {
        event.preventDefault();
        try {
          const turned = await post("/api/v1/me/two-step/confirm", { code: code.value });
          showTwoStep(true);
          twoStep.prepend(el("div", { class: "recovery-codes" },
            el("h3", {}, "Your recovery codes"),
            el("p", {}, "Keep these somewhere safe. Each signs you in once without the app; "
              + "they are not shown again."),
            el("ul", {}, ...turned.recovery_codes.map((item) => el("li", {}, el("code", {}, item))))));
        } catch (error) { note.textContent = error.message; }
      });
      twoStep.replaceChildren(confirm);
      code.focus();
    });
    twoStep.replaceChildren(el("div", { class: "account-form" },
      el("h3", {}, "Two-step sign-in is off"),
      el("p", {}, "Turn it on to need a code from an authenticator app as well as your "
        + "password when you sign in from another device."),
      note, start));
  };
  showTwoStep(Boolean(me.two_step));

  wrap.append(passwordForm, twoStep);
  // A page opened with a token (by BreadSched itself) has no session to end.
  if (!apiToken) {
    const out = el("button", { type: "button", class: "action" }, "Sign out");
    out.addEventListener("click", async () => {
      await fetch("/api/v1/session", { method: "DELETE", headers: apiHeaders() });
      window.location.reload();
    });
    wrap.append(el("div", { class: "account-form" }, out));
  }
  return wrap;
}
