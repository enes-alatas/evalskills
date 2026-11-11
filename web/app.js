"use strict";
// Presentation follows docs/DESIGN.md. UI text uses no em dashes.
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `Request failed (${r.status})`);
  return j;
};

const state = { skills: [], meta: null, tab: "all", q: "", found: null, timer: null, openId: null, detail: null, watch: new Map(), done: null, opener: null };
const STATUS = { queued: "Queued", running: "Running", failed: "Failed", unsupported: "Unsupported", inconclusive: "Inconclusive", unknown: "Not evaluated" };
const short = (sha) => (sha ? String(sha).slice(0, 7) : "");
const day = (t) => { const m = String(t || "").match(/^(\d{4})-?(\d{2})-?(\d{2})/); return m ? `${m[1]}-${m[2]}-${m[3]}` : ""; };
const hasScore = (s) => s.structural && s.structural.score != null;

function status(st, text) { return `<span class="st st-${esc(st)}">${esc(text || STATUS[st] || st)}</span>`; }
function where(s) { return `${s.repo}${s.path !== "." ? "/" + s.path : ""}`; }
function catLabel(id) { return (state.meta?.categories.find((c) => c.id === id) || {}).label || id; }

// ---------------------------------------------------------------- board

const TOP_TABS = 3;

function renderTabs() {
  const counts = {};
  state.skills.forEach((s) => (counts[s.category] = (counts[s.category] || 0) + 1));
  const cats = state.meta.categories.filter((c) => counts[c.id]).sort((a, b) => (a.id === "other") - (b.id === "other") || counts[b.id] - counts[a.id]);
  const top = cats.slice(0, TOP_TABS);
  const more = cats.slice(TOP_TABS);
  const btn = (t, n) => `<button role="tab" data-tab="${esc(t.id)}" aria-selected="${state.tab === t.id}">${esc(t.label)}${n ? `<span class="count">${n}</span>` : ""}</button>`;
  const inMore = more.some((c) => c.id === state.tab);
  const sel = more.length
    ? `<select id="more" aria-label="More categories" class="${inMore ? "on" : ""}"><option value="">More</option>${more.map((c) => `<option value="${esc(c.id)}" ${state.tab === c.id ? "selected" : ""}>${esc(c.label)} (${counts[c.id]})</option>`).join("")}</select>`
    : "";
  const tabs = $("#tabs");
  tabs.innerHTML = btn({ id: "all", label: "All" }) + top.map((c) => btn(c, counts[c.id])).join("") + sel;
  // On narrow screens the tabs scroll sideways: keep the selected one in view.
  const on = tabs.querySelector('[aria-selected="true"], select.on');
  if (on && (on.offsetLeft < tabs.scrollLeft || on.offsetLeft + on.offsetWidth > tabs.scrollLeft + tabs.clientWidth)) {
    tabs.scrollLeft = on.offsetLeft + on.offsetWidth - tabs.clientWidth;
  }
}

function right(s) {
  if (s.state === "queued" || s.state === "running") return status(s.state);
  if (hasScore(s)) return `<span class="score">${s.structural.score.toFixed(1)}</span>`;
  if (s.state === "completed") return status("unknown", "No score");
  return status(s.state);
}

// Security pills: distinct finding types at medium or above (docs/METHODOLOGY.md), only for a completed scan.
// Each severity has its own colour (#81): critical red, high orange, medium yellow.
const SEV = ["critical", "high", "medium"];
const TIP = "Static scanner pattern matches, not confirmed vulnerabilities";
const isPartial = (sec) => !!(sec.incomplete && sec.incomplete.length);
// Rows (#81): a muted "Security" label, then the pills, wrapped as one group so the label never ends a line alone.
// "partial scan" shows only in the dialog, which explains it. "clean" only for a complete analysis: a partial scan
// never reads as clean (docs/METHODOLOGY.md).
function pills(s, inDialog) {
  const sec = s.security;
  if (s.state !== "completed" || !sec) return "";
  const group = (html) => (inDialog ? html : `<span class="secs"><span class="sec-label">Security</span>${html}</span>`);
  if (sec.scanner_error) return group(`<span class="pill sev-inc" title="A scanner crashed, so counts are incomplete">scan incomplete</span>`);
  const on = SEV.filter((k) => sec[k]);
  const partial = inDialog && isPartial(sec)
    ? `<span class="pill sev-part" title="A scanner could not finish (${esc(sec.incomplete.join(", "))}), so findings may be missing">partial scan</span>`
    : "";
  if (!on.length && isPartial(sec)) return partial;
  if (!on.length) return group(`<span class="pill sev-ok" title="No findings at medium or above. ${TIP}">clean</span>`);
  // Counts are coerced to numbers before they reach innerHTML.
  return group(on.map((k) => { const n = Number(sec[k]); return `<span class="pill sev-${k}" title="${n} distinct ${k} finding type${n > 1 ? "s" : ""}. ${TIP}">${n} ${k}</span>`; }).join("")) + partial;
}

function rowHtml(s, rank) {
  return `<button class="row" data-id="${esc(s.id)}" aria-haspopup="dialog">
    <span class="rank">${rank ?? ""}</span>
    <span class="nm">${esc(s.name)}<span class="meta"><span class="src">${esc(s.repo)}</span>${s.where ? `<span class="where" title="${esc(s.path)}">${esc(s.where)}</span>` : ""}${pills(s)}</span></span>
    ${right(s)}</button>`;
}

function renderBoard() {
  const q = state.q.toLowerCase();
  const rows = state.skills
    .filter((s) => state.tab === "all" || s.category === state.tab)
    .filter((s) => !q || [s.name, s.repo, s.path, s.description, s.category].join(" ").toLowerCase().includes(q))
    .sort((a, b) => (b.structural?.score ?? -1) - (a.structural?.score ?? -1) || a.name.localeCompare(b.name));
  let rank = 0;
  $("#board").innerHTML = rows.map((s) => {
    const r = hasScore(s) ? ++rank : null;
    return `<li data-id="${esc(s.id)}">${rowHtml(s, r)}</li>`;
  }).join("");
  $("#empty").hidden = rows.length > 0;
}

async function load() {
  state.skills = await api("/api/skills");
  renderTabs();
  noticeFinished();
  if (state.openId) { await fetchDetail(state.openId, true); renderResult(); }
  renderBoard();
  const busy = state.skills.some((s) => s.state === "queued" || s.state === "running");
  clearTimeout(state.timer);
  state.timer = setTimeout(() => load().catch(() => {}), busy ? 4000 : 30000);
}

// ---------------------------------------------------------------- result view

function securityLine(s, t1) {
  const sec = s.security;
  if (!t1 || !sec) return "";
  if (sec.scanner_error) {
    return `<div class="br"><b>Security</b><span class="f">${status("inconclusive", "Incomplete")}</span>
      <span class="d">A scanner crashed during this run, so counts would be incomplete and are not shown</span></div>`;
  }
  // Each part keeps its own severity colour (#81).
  const lvl = SEV.filter((k) => sec[k]);
  const fig = lvl.length ? lvl.map((k) => `<span class="sv-${k}">${Number(sec[k])} ${k}</span>`).join(", ") : "None";
  const partial = isPartial(sec)
    ? ` Partial scan: ${esc(sec.incomplete.join(", "))} could not finish its analysis, so findings may be missing.`
    : "";
  const top = sec.top.length
    ? `<details><summary>Findings</summary><ul class="checks">${sec.top.map((f) => `<li><span>${esc(f.title)}${f.count > 1 ? ` <span class="hint">×${f.count}</span>` : ""}</span><span class="mono sv-${esc(f.severity)}">${esc(f.severity)}</span></li>`).join("")}</ul></details>`
    : "";
  return `<div class="br"><b>Security</b><span class="f">${fig}</span>
    <span class="d">${lvl.length ? "Distinct finding types at medium or above." : "No findings at medium or above."} Static pattern matches, not confirmed vulnerabilities.${partial}</span>${top}</div>`;
}

function detailHtml(s) {
  const e = s.evaluation;
  const t1 = e?.tier1;
  let left = "";
  let rightCol = "";
  let lead = s.description ? `<p class="lead">${esc(s.description)}</p>` : "";
  if (state.done === s.id && s.state === "completed") lead += `<p class="callout done">Evaluation complete</p>`;
  if (state.done === s.id && s.state === "failed") lead += `<p class="callout bad">Evaluation finished without a result</p>`;

  // A queued job may say why it waits, e.g. for the scanner (#103).
  if (s.state === "queued") lead += `<p class="callout">${status("queued")} ${s.job && s.job.note ? esc(s.job.note) : "Waiting in the evaluation queue."} This view updates on its own.</p>`;
  if (s.state === "running") lead += `<p class="callout">${status("running")} SkillEvaluator is running static checks now, usually under a minute. This view updates on its own.</p>`;
  if (s.job && s.job.note && !["queued", "running"].includes(s.state)) lead += `<p class="callout">${esc(s.job.note)}</p>`;
  if (e && e.status === "failed") lead += `<p class="callout bad">The evaluation did not produce a report: ${esc(e.error || "unknown error")}. No score is shown.</p>`;

  if (t1) {
    const q = t1.quality;
    const dims = Object.entries(q.dimensions || {}).map(([k, v]) => `<span>${esc(k)}</span><span>${Number(v).toFixed(0)}</span>`).join("");
    left += `<h3>Score breakdown</h3>
      <div class="br"><b>Structure</b><span class="f">${q.score != null ? q.score.toFixed(1) : "None"}</span>
        <span class="d">SkillEvaluator's static quality rubric on SKILL.md and the folder layout. Copies of the same skill can differ by 2 to 3 points, so read smaller gaps as a tie. Says nothing about how an agent performs with the skill.</span>
        ${dims ? `<details><summary>Dimensions</summary><div class="dims">${dims}</div></details>` : ""}</div>
      ${securityLine(s, t1)}
      <div class="br"><b>Live tasks</b><span class="f">${status("unknown", "Not run")}</span><span class="d">${esc(s.live.reason)}</span></div>
      <p class="hint" style="margin-top:8px">Ranked by structure until live runs exist.</p>`;
  } else if (!["queued", "running"].includes(s.state)) {
    left += `<h3>Score breakdown</h3><p class="hint">No result for this skill yet.</p>`;
  }

  if (e) {
    rightCol += `<h3>Evaluation</h3><dl class="kv">
      <dt>Commit</dt><dd>${esc(short(e.source.commit))}</dd>
      <dt>Evaluator</dt><dd>SkillEvaluator ${esc(e.evaluator.version)}</dd>
      <dt>Mode</dt><dd>${esc(e.mode)}, no agent or model</dd>
      <dt>Cases</dt><dd>None (static)</dd>
      <dt>Runtime</dt><dd>${esc(e.duration_s)} s</dd>
      <dt>Cost</dt><dd>$${(e.cost_usd ?? 0).toFixed(2)}</dd>
      <dt>Evaluated</dt><dd>${esc(day(e.started_at))}</dd>
      <dt>Category</dt><dd>${esc(catLabel(s.category))}</dd></dl>`;
    const links = [`<a href="${esc(e.source.tree_url)}" target="_blank" rel="noopener">Source at ${esc(short(e.source.commit))}</a>`];
    for (const [k, v] of Object.entries(e.reports || {})) links.push(`<a href="${esc(v)}" target="_blank" rel="noopener">Full report (${esc(k)})</a>`);
    links.push(`<a href="/docs/METHODOLOGY.md">How scores work</a>`);
    rightCol += `<div class="links">${links.join("")}</div>`;
    if (t1) {
      rightCol += `<details><summary>All static checks</summary><ul class="checks">${t1.checks.map((c) => `<li><span>${esc(c.name)}</span>${checkStatus(c)}</li>`).join("")}</ul>
        <p class="hint" style="margin-top:8px">Schema &amp; Repository Governance fails for most third-party skills, because the default policy treats a missing metadata.author as high severity. It is shown but not ranked.</p></details>`;
    }
    if (s.history && s.history.length > 1) {
      rightCol += `<details><summary>History</summary><ul class="checks">${s.history.map((h) => `<li><span class="mono">${esc(short(h.commit))} · ${esc(day(h.started_at))}</span>${h.score != null ? `<span class="mono">${h.score.toFixed(1)}</span>` : status(h.status)}</li>`).join("")}</ul></details>`;
    }
  } else {
    rightCol += `<h3>Source</h3><div class="links"><a href="https://github.com/${esc(s.repo)}" target="_blank" rel="noopener">${esc(s.repo)}</a></div>`;
  }
  return `${lead}<div>${left}</div><div>${rightCol}</div>`;
}

function checkStatus(c) {
  const map = { passed: ["passed", "Passed"], incomplete: ["inconclusive", "Incomplete"], skipped: ["unknown", "Skipped"] };
  const [cls, label] = map[c.status] || ["failed", "Failed"];
  return status(cls, label + (c.incomplete.length ? ` (${c.incomplete.join(", ")})` : ""));
}

async function fetchDetail(id, quiet) {
  try { state.detail = await api(`/api/skills/${encodeURIComponent(id)}`); }
  catch (err) { if (!quiet) state.detail = null; }
}

// ---------------------------------------------------------------- result dialog

const dlg = () => $("#result");
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

function renderResult() {
  const id = state.openId;
  if (!id) return false;
  const s = (state.detail && state.detail.id === id ? state.detail : null) || state.skills.find((x) => x.id === id);
  if (!s) return false;
  const sha = short(s.commit);
  $("#r-title").textContent = s.name;
  $("#r-meta").innerHTML = `<span class="src">${esc(where(s))}${sha ? ` @ ${esc(sha)}` : ""}</span>${pills(s, true)}`;
  $("#r-body").innerHTML = detailHtml(s);
  return true;
}

async function openResult(id, opener) {
  state.openId = id;
  state.detail = null;
  state.opener = opener || null;
  if (!state.skills.some((x) => x.id === id)) {
    // Unknown id (e.g. a stale /s/ link): no empty dialog.
    state.openId = null;
    history.replaceState(null, "", "/");
    return;
  }
  history.replaceState(null, "", `/s/${encodeURIComponent(id)}`);
  renderResult();
  const d = dlg();
  if (!d.open) {
    d.showModal();
    $("#r-body").scrollTop = 0;
    requestAnimationFrame(() => d.classList.add("in"));
  }
  await fetchDetail(id);
  renderResult();
}

function forget() {
  const was = state.openId;
  state.openId = null; state.detail = null;
  if (state.done === was) state.done = null;
  if (location.pathname !== "/") history.replaceState(null, "", "/");
  return was;
}

function closeResult() {
  const d = dlg();
  if (!d.open) return;
  const was = forget();
  d.classList.remove("in");
  const finish = () => {
    d.close();
    // Polling re-renders the board, so the row that opened the dialog may be gone: re-query it then.
    const back = state.opener && state.opener.isConnected
      ? state.opener
      : document.querySelector(`#board button.row[data-id="${CSS.escape(was || "")}"]`);
    back?.focus({ preventScroll: true });
  };
  if (reduced()) finish(); else setTimeout(finish, 180);
}

// Submitted skills are watched until their evaluation ends. The open dialog shows the result as soon as it lands;
// if the dialog was closed meanwhile, a notice offers to open it.
function noticeFinished() {
  for (const [id, prev] of state.watch) {
    const s = state.skills.find((x) => x.id === id);
    if (!s || s.state === prev) continue;
    if (s.state === "queued" || s.state === "running") { state.watch.set(id, s.state); continue; }
    state.watch.delete(id);
    if (state.openId === id) { state.done = id; continue; }
    const what = s.state === "completed" && hasScore(s) ? `scored ${s.structural.score.toFixed(1)}` : (STATUS[s.state] || s.state).toLowerCase();
    toast(`${s.name} ${what}`, id);
  }
}

function toast(text, id) {
  const t = $("#toast");
  t.innerHTML = `<span>${esc(text)}</span><button type="button" class="link" data-open="${esc(id)}">View</button><button type="button" class="link" data-dismiss aria-label="Dismiss">Close</button>`;
  t.hidden = false;
  requestAnimationFrame(() => t.classList.add("in"));
}

function hideToast() { const t = $("#toast"); t.classList.remove("in"); setTimeout(() => (t.hidden = true), reduced() ? 0 : 180); }

// ---------------------------------------------------------------- add a skill, in place

function showAdd(on) {
  $("#add").hidden = !on;
  $("#add-btn").hidden = on;
  $("#add-btn").setAttribute("aria-expanded", String(on));
  state.found = null;
  $("#found").hidden = true; $("#add-err").hidden = true;
  if (on) { $("#url").value = ""; $("#url").focus(); } else { $("#add-btn").focus(); }
}

function addError(msg) { $("#add-err").textContent = msg; $("#add-err").hidden = false; }

// The limit counts only skills that need a run (#83). Up-to-date ones show ticked and disabled, and are not sent.
const openPicks = () => [...document.querySelectorAll("#found-list li:not(.done) input")];

function updateEvaluateLabel() {
  const picks = openPicks();
  const n = picks.filter((i) => i.checked).length;
  const max = state.meta.limits.skills_per_submission;
  const full = n >= max;
  picks.forEach((i) => { i.disabled = full && !i.checked; });
  const lim = $("#found-limit");
  lim.hidden = !(full && picks.length > max);
  lim.textContent = `Up to ${max} new or changed skills per submission. Look the repository up again afterwards to add more.`;
  const b = $("#submit-skills");
  b.textContent = n === 1 ? "Evaluate 1 skill" : `Evaluate ${n} skills`;
  b.disabled = n === 0;
  // At the limit the link clears the selection instead, so a filtered view can be re-picked.
  const sa = $("#select-all");
  sa.textContent = full ? "Clear" : "Select all";
  sa.disabled = !full && !picks.some((i) => !i.checked && !i.closest("li").hidden);
}

function filterFound() {
  const q = $("#found-q").value.trim().toLowerCase();
  document.querySelectorAll("#found-list li").forEach((li) => { li.hidden = !!q && !li.dataset.q.includes(q); });
  updateEvaluateLabel();
}

function selectAll() {
  const max = state.meta.limits.skills_per_submission;
  let n = openPicks().filter((i) => i.checked).length;
  if (n >= max) {
    openPicks().forEach((i) => { i.checked = false; });
    return updateEvaluateLabel();
  }
  for (const i of openPicks()) {
    if (n >= max) break;
    if (!i.checked && !i.closest("li").hidden) { i.checked = true; n++; }
  }
  updateEvaluateLabel();
}

async function lookup(ev) {
  ev.preventDefault();
  const btn = $("#lookup");
  let url = $("#url").value.trim();
  if (url && !/^https?:\/\//i.test(url)) url = "https://" + url;
  btn.disabled = true; btn.textContent = "Looking"; $("#add-err").hidden = true; $("#found").hidden = true;
  try {
    const res = await api("/api/discover", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
    state.found = res;
    if (!res.skills.length) throw new Error("No SKILL.md found in this repository");
    const n = res.skills.length;
    const done = res.skills.filter((s) => s.current).length;
    const todo = n - done;
    const max = state.meta.limits.skills_per_submission;
    $("#found-meta").textContent = `${n} skill${n > 1 ? "s" : ""} found at ${short(res.commit)}. `
      + (todo === 0 ? "All are up to date on the board." : `${done ? `${done} up to date. ` : ""}Choose up to ${max} to evaluate.`);
    // A cut list (#87) leads with the real total, then says how to narrow it.
    if (res.truncated) {
      $("#found-meta").textContent = `${res.total} skills found at ${short(res.commit)}, showing the first ${n}. `
        + `Paste a subfolder URL (…/tree/<branch>/<folder>) to see the rest. ${done ? `${done} up to date. ` : ""}Choose up to ${max} to evaluate.`;
    }
    // Skills that need a run first, then the up-to-date ones.
    const order = [...res.skills].sort((a, b) => a.current - b.current);
    $("#found-list").innerHTML = order.map((s, i) => {
      const note = s.current ? "Up to date, already on the board" : s.listed ? "Changed since its last evaluation" : "";
      return `<li${s.current ? ' class="done"' : ""} data-q="${esc(`${s.name} ${s.path}`.toLowerCase())}">
        <input type="checkbox" id="sk${i}" value="${esc(s.path)}" ${s.current ? "checked disabled" : todo === 1 ? "checked" : ""}>
        <label for="sk${i}">${esc(s.name)}<span class="mono">${esc(s.path)}</span>${note ? `<span class="hint">${note}</span>` : ""}</label></li>`;
    }).join("");
    $("#found-q").value = "";
    $(".pick-tools").hidden = todo <= 1;
    $("#found").hidden = false;
    updateEvaluateLabel();
  } catch (err) {
    addError(err.message);
  } finally { btn.disabled = false; btn.textContent = "Find skills"; }
}

async function submitSkills() {
  const paths = openPicks().filter((i) => i.checked).map((i) => i.value);
  if (!paths.length) return addError("Choose at least one skill");
  const btn = $("#submit-skills"); btn.disabled = true;
  try {
    const res = await api("/api/submit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: state.found.repo, paths, category: $("#cat").value }) });
    showAdd(false);
    state.tab = "all"; state.q = ""; $("#q").value = "";
    res.submitted.filter((x) => x.job).forEach((x) => state.watch.set(x.skill_id, "queued"));
    const first = res.submitted.find((x) => x.job) || res.submitted.find((x) => x.skill_id);
    await load();
    if (first) await openResult(first.skill_id, $("#add-btn"));
  } catch (err) {
    addError(err.message);
  } finally { btn.disabled = false; }
}

// ---------------------------------------------------------------- init

async function init() {
  state.meta = await api("/api/meta");
  $("#cat").innerHTML = state.meta.categories.map((c) => `<option value="${esc(c.id)}" ${c.id === "other" ? "selected" : ""}>${esc(c.label)}</option>`).join("");
  $("#why-btn").addEventListener("click", (e) => {
    const open = $("#why").hidden;
    $("#why").hidden = !open;
    e.currentTarget.setAttribute("aria-expanded", String(open));
  });
  $("#q").addEventListener("input", (e) => { state.q = e.target.value; renderBoard(); });
  $("#tabs").addEventListener("change", (e) => { if (e.target.id === "more") { state.tab = e.target.value || "all"; renderTabs(); renderBoard(); } });
  $("#tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) { state.tab = b.dataset.tab; renderTabs(); renderBoard(); } });
  $("#board").addEventListener("click", (e) => { const b = e.target.closest("button.row"); if (b) openResult(b.dataset.id, b); });
  $("#r-close").addEventListener("click", closeResult);
  dlg().addEventListener("cancel", (e) => { e.preventDefault(); closeResult(); });
  dlg().addEventListener("click", (e) => { if (e.target === dlg()) closeResult(); });
  // A browser can close the modal itself (e.g. a repeated Esc) without our cancel handler: keep state in step.
  dlg().addEventListener("close", () => { dlg().classList.remove("in"); if (state.openId) forget(); });
  $("#toast").addEventListener("click", (e) => {
    const o = e.target.closest("[data-open]");
    if (o) { hideToast(); state.done = o.dataset.open; openResult(o.dataset.open); }
    else if (e.target.closest("[data-dismiss]")) hideToast();
  });
  if (state.meta.source_repo_url) {
    for (const id of ["#gh", "#foot-gh"]) { $(id).href = state.meta.source_repo_url; $(id).hidden = false; }
  }
  $("#add-btn").addEventListener("click", () => showAdd(true));
  $("#add-cancel").addEventListener("click", () => showAdd(false));
  $("#add").addEventListener("submit", lookup);
  $("#add").addEventListener("keydown", (e) => { if (e.key === "Escape") showAdd(false); });
  $("#found-list").addEventListener("change", updateEvaluateLabel);
  $("#found-q").addEventListener("input", filterFound);
  // Enter in the filter must not re-run the lookup (the filter sits inside the add form).
  $("#found-q").addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
  $("#select-all").addEventListener("click", selectAll);
  $("#submit-skills").addEventListener("click", submitSkills);
  const m = location.pathname.match(/^\/s\/(.+)$/);
  await load();
  if (m) await openResult(decodeURIComponent(m[1]));
}
init().catch((e) => { document.body.insertAdjacentHTML("beforeend", `<p class="err">${esc(e.message)}</p>`); });
