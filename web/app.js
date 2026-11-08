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

const state = { skills: [], meta: null, tab: "all", q: "", found: null, timer: null, openId: null, detail: null };
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
  $("#tabs").innerHTML = btn({ id: "all", label: "All" }) + top.map((c) => btn(c, counts[c.id])).join("") + sel;
}

function right(s) {
  if (s.state === "queued" || s.state === "running") return status(s.state);
  if (hasScore(s)) return `<span class="score">${s.structural.score.toFixed(1)}</span>`;
  if (s.state === "completed") return status("unknown", "No score");
  return status(s.state);
}

function rowHtml(s, rank) {
  const sha = short(s.commit);
  return `<button class="row" data-id="${esc(s.id)}" aria-expanded="${state.openId === s.id}">
    <span class="rank">${rank ?? ""}</span>
    <span class="nm">${esc(s.name)}<span class="meta">${esc(where(s))}${sha ? ` @ ${esc(sha)}` : ""}</span></span>
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
    const open = state.openId === s.id;
    const inner = rowHtml(s, r);
    return `<li data-id="${esc(s.id)}"${open ? ' class="open"' : ""}>${open ? `<div class="open-wrap">${inner}<div class="detail">${detailHtml(state.detail || s)}</div></div>` : inner}</li>`;
  }).join("");
  $("#empty").hidden = rows.length > 0;
}

async function load() {
  state.skills = await api("/api/skills");
  renderTabs();
  if (state.openId) await fetchDetail(state.openId, true);
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
  const lvl = ["critical", "high", "medium"].filter((k) => sec[k]).map((k) => `${sec[k]} ${k}`);
  const fig = lvl.length ? lvl.join(", ") : "None";
  const partial = sec.incomplete.length ? ` Partial analysis: ${esc(sec.incomplete.join(", "))}.` : "";
  const top = sec.top.length
    ? `<details><summary>Findings</summary><ul class="checks">${sec.top.map((f) => `<li><span>${esc(f.title)}${f.count > 1 ? ` <span class="hint">×${f.count}</span>` : ""}</span><span class="mono">${esc(f.severity)}</span></li>`).join("")}</ul></details>`
    : "";
  return `<div class="br"><b>Security</b><span class="f">${esc(fig)}</span>
    <span class="d">${lvl.length ? "Distinct finding types at medium or above." : "No findings at medium or above."} Static pattern matches, not confirmed vulnerabilities.${partial}</span>${top}</div>`;
}

function detailHtml(s) {
  const e = s.evaluation;
  const t1 = e?.tier1;
  let left = "";
  let rightCol = "";
  let lead = s.description ? `<p class="lead">${esc(s.description)}</p>` : "";

  if (s.state === "queued") lead += `<p class="callout">Waiting in the evaluation queue. This view updates on its own.</p>`;
  if (s.state === "running") lead += `<p class="callout">SkillEvaluator is running static checks now, usually under a minute. This view updates on its own.</p>`;
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

async function toggle(id) {
  if (state.openId === id) {
    state.openId = null; state.detail = null;
    history.replaceState(null, "", "/");
    renderBoard();
    return;
  }
  state.openId = id; state.detail = null;
  history.replaceState(null, "", `/s/${encodeURIComponent(id)}`);
  renderBoard();
  await fetchDetail(id);
  renderBoard();
}

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

function updateEvaluateLabel() {
  const n = document.querySelectorAll("#found-list input:checked").length;
  const b = $("#submit-skills");
  b.textContent = n === 1 ? "Evaluate 1 skill" : `Evaluate ${n} skills`;
  b.disabled = n === 0;
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
    $("#found-meta").textContent = `${n} skill${n > 1 ? "s" : ""} found at ${short(res.commit)}. Choose up to ${state.meta.limits.skills_per_submission}`;
    $("#found-list").innerHTML = res.skills.map((s, i) => `<li><input type="checkbox" id="sk${i}" value="${esc(s.path)}" ${n === 1 ? "checked" : ""}>
      <label for="sk${i}">${esc(s.name)}<span class="mono">${esc(s.path)}</span>${s.listed ? `<span class="hint">Already listed, re-evaluated only if changed</span>` : ""}</label></li>`).join("");
    $("#found").hidden = false;
    updateEvaluateLabel();
  } catch (err) {
    addError(err.message);
  } finally { btn.disabled = false; btn.textContent = "Find skills"; }
}

async function submitSkills() {
  const paths = [...document.querySelectorAll("#found-list input:checked")].map((i) => i.value);
  if (!paths.length) return addError("Choose at least one skill");
  const btn = $("#submit-skills"); btn.disabled = true;
  try {
    const res = await api("/api/submit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: state.found.repo, paths, category: $("#cat").value }) });
    showAdd(false);
    state.tab = "all"; state.q = ""; $("#q").value = "";
    const first = res.submitted.find((x) => x.skill_id);
    if (first) { state.openId = first.skill_id; history.replaceState(null, "", `/s/${encodeURIComponent(first.skill_id)}`); }
    await load();
    const li = first && document.querySelector(`#board li[data-id="${CSS.escape(first.skill_id)}"]`);
    if (li) li.scrollIntoView({ block: "center" });
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
  $("#board").addEventListener("click", (e) => { const b = e.target.closest("button.row"); if (b) toggle(b.dataset.id); });
  $("#add-btn").addEventListener("click", () => showAdd(true));
  $("#add-cancel").addEventListener("click", () => showAdd(false));
  $("#add").addEventListener("submit", lookup);
  $("#add").addEventListener("keydown", (e) => { if (e.key === "Escape") showAdd(false); });
  $("#found-list").addEventListener("change", updateEvaluateLabel);
  $("#submit-skills").addEventListener("click", submitSkills);
  const m = location.pathname.match(/^\/s\/(.+)$/);
  if (m) state.openId = decodeURIComponent(m[1]);
  await load();
  if (state.openId) document.querySelector(`#board li[data-id="${CSS.escape(state.openId)}"]`)?.scrollIntoView({ block: "center" });
}
init().catch((e) => { document.body.insertAdjacentHTML("beforeend", `<p class="err">${esc(e.message)}</p>`); });
