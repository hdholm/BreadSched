// Verify and the packaged user guide.

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
