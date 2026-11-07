"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `Request failed (${r.status})`);
  return j;
};

const state = { skills: [], meta: null, tab: "all", q: "", found: null, timer: null };
const STATUS_LABEL = { completed: "Evaluated", queued: "Queued", running: "Running", failed: "Failed", unsupported: "Unsupported", unknown: "—" };

function pill(st) { return `<span class="pill s-${esc(st)}">${esc(STATUS_LABEL[st] || st)}</span>`; }

function sevCell(sec) {
  if (!sec) return `<span class="hint">—</span>`;
  if (sec.scanner_error) return `<span class="hint" title="SkillSpector exited with an error; counts would be incomplete">scanner error</span>`;
  const parts = [];
  if (sec.critical) parts.push(`<span class="c"><b>${sec.critical}</b> critical</span>`);
  if (sec.high) parts.push(`<span class="h"><b>${sec.high}</b> high</span>`);
  if (sec.medium) parts.push(`<span class="m"><b>${sec.medium}</b> med</span>`);
  if (!parts.length) parts.push(`<span>none ≥ medium</span>`);
  // counts are distinct finding types, not raw occurrences
  const inc = sec.incomplete.length ? ` <span class="hint" title="Scanner did not finish: ${esc(sec.incomplete.join(", "))}">· partial</span>` : "";
  return `<span class="sev">${parts.join(" · ")}${inc}</span>`;
}

function catLabel(id) { return (state.meta?.categories.find((c) => c.id === id) || {}).label || id; }

function renderTabs() {
  const counts = {};
  state.skills.forEach((s) => (counts[s.category] = (counts[s.category] || 0) + 1));
  const tabs = [{ id: "all", label: "All" }, ...state.meta.categories.filter((c) => counts[c.id])];
  $("#tabs").innerHTML = tabs.map((t) =>
    `<button role="tab" data-tab="${t.id}" aria-selected="${state.tab === t.id}">${esc(t.label)}${t.id === "all" ? "" : ` <span class="hint">${counts[t.id]}</span>`}</button>`).join("");
}

function sortKey(s) { return s.structural?.score ?? -1; }

function renderBoard() {
  const q = state.q.toLowerCase();
  const rows = state.skills
    .filter((s) => state.tab === "all" || s.category === state.tab)
    .filter((s) => !q || [s.name, s.repo, s.path, s.description, s.category].join(" ").toLowerCase().includes(q))
    .sort((a, b) => sortKey(b) - sortKey(a) || a.name.localeCompare(b.name));
  let rank = 0;
  $("#board tbody").innerHTML = rows.map((s) => {
    const scored = s.structural && s.structural.score != null;
    if (scored) rank++;
    const score = scored ? `<span class="score">${s.structural.score.toFixed(1)}</span><span class="grade">${esc(s.structural.grade)}</span>` : `<span class="hint">no score</span>`;
    return `<tr data-id="${esc(s.id)}" tabindex="0">
      <td class="rank">${scored ? rank : ""}</td>
      <td><div class="name">${esc(s.name)} ${s.state !== "completed" ? `<span class="show-sm">${pill(s.state)}</span>` : ""}</div><div class="repo">${esc(s.repo)}${s.path !== "." ? " / " + esc(s.path) : ""}</div><div class="desc">${esc(s.description)}</div></td>
      <td class="num">${score}</td>
      <td class="hide-sm">${sevCell(s.security)}</td>
      <td class="hide-sm"><span class="pill s-na" title="${esc(s.live.reason)}">not run</span></td>
      <td class="hide-sm">${pill(s.state)}</td></tr>`;
  }).join("");
  $("#empty").hidden = rows.length > 0;
}

async function load() {
  state.skills = await api("/api/skills");
  renderTabs();
  renderBoard();
  const busy = state.skills.some((s) => s.state === "queued" || s.state === "running");
  clearTimeout(state.timer);
  state.timer = setTimeout(load, busy ? 4000 : 30000);
  if ($("#detail").open && state.openId) refreshDetail(state.openId, true);
}

function bars(dims) {
  return `<div class="bars">${Object.entries(dims || {}).map(([k, v]) =>
    `<span>${esc(k)}</span><span class="bar"><i style="width:${Math.max(0, Math.min(100, v))}%"></i></span><span class="num">${v.toFixed(0)}</span>`).join("")}</div>`;
}

function detailHtml(s) {
  const e = s.evaluation;
  const t1 = e?.tier1;
  const src = e ? e.source.tree_url : `https://github.com/${s.repo}`;
  let body = `<button class="close" data-close aria-label="Close">×</button>
    <h2>${esc(s.name)} ${pill(s.state)}</h2>
    <p class="muted">${esc(s.description)}</p>
    <p><a href="${esc(src)}" target="_blank" rel="noopener">${esc(s.repo)}${s.path !== "." ? "/" + esc(s.path) : ""}</a> · ${esc(catLabel(s.category))}</p>`;
  if (s.state === "queued" || s.state === "running") {
    body += `<p class="notice">${s.state === "queued" ? "Waiting in the evaluation queue." : "SkillEvaluator is running static checks now (usually under a minute)."} This view updates automatically.</p>`;
  }
  if (s.job && s.job.note && !["queued", "running"].includes(s.state)) body += `<p class="hint">${esc(s.job.note)}</p>`;
  if (e && e.status === "failed") body += `<p class="err">The evaluation did not produce a report: ${esc(e.error || "unknown error")}. No score is shown.</p>`;
  if (t1) {
    body += `<h3>Structural quality · ${t1.quality.score?.toFixed(1)} (${esc(t1.quality.grade)})</h3>${bars(t1.quality.dimensions)}
      <p class="hint">Deterministic SkillEvaluator quality rubric on SKILL.md and folder layout. Says nothing about how well an agent performs with the skill.</p>
      <h3>Security findings</h3>
      ${secHtml(t1)}
      <h3>All static checks</h3>
      <ul class="checks">${t1.checks.map((c) => `<li><span>${esc(c.name)}</span><span>${checkPill(c)}</span></li>`).join("")}</ul>
      <p class="hint">"Schema & Repository Governance" fails for most third-party skills because SkillEvaluator's default publication policy treats a missing <code>metadata.author</code> as high severity. It is shown, but not used for ranking.</p>`;
  }
  body += `<h3>Live performance</h3><p class="hint">Not evaluated. ${esc(s.live.reason)}</p>`;
  if (e) {
    body += `<h3>Evaluation context</h3><dl class="kv">
      <dt>Commit</dt><dd><code>${esc(e.source.commit)}</code></dd>
      <dt>Content digest</dt><dd><code>${esc(e.source.tree_digest.slice(0, 23))}…</code></dd>
      <dt>Evaluator</dt><dd>SkillEvaluator ${esc(e.evaluator.version)} (<code>${esc(e.evaluator.commit.slice(0, 12))}</code>)</dd>
      <dt>Mode</dt><dd>${esc(e.mode)} — no agent, no model, no test cases</dd>
      <dt>Run</dt><dd>${esc(e.started_at)} · ${esc(e.duration_s)} s · cost $${(e.cost_usd ?? 0).toFixed(2)}</dd>
      <dt>Reports</dt><dd>${Object.entries(e.reports || {}).map(([k, v]) => `<a href="${esc(v)}" target="_blank" rel="noopener">${k}</a>`).join(" · ") || "—"}</dd></dl>`;
  }
  if (s.install && e && e.status === "completed") {
    body += `<h3>Install (Claude Code)</h3><pre>${esc(s.install.claude_code)}</pre><p class="hint">Verified: ${esc(s.install.verified)}. Pin to the commit above for the evaluated version.</p>`;
  }
  if (s.history && s.history.length > 1) {
    body += `<h3>History</h3><ul class="checks">${s.history.map((h) => `<li><span><code>${esc(h.commit.slice(0, 10))}</code> ${esc(h.started_at)}</span><span>${h.score != null ? h.score.toFixed(1) : esc(h.status)}</span></li>`).join("")}</ul>`;
  }
  return body;
}

function checkPill(c) {
  const cls = c.status === "passed" ? "completed" : c.status === "incomplete" ? "queued" : c.status === "skipped" ? "na" : "failed";
  const extra = c.incomplete.length ? ` · ${c.incomplete.join(", ")} incomplete` : "";
  return `<span class="pill s-${cls}">${esc(c.status + extra)}</span>`;
}

function secHtml(t1) {
  const sec = t1.security;
  if (!sec) return `<p class="hint">Not available for this evaluation.</p>`;
  const inc = t1.checks.filter((c) => c.incomplete.length && c.name === "Security Scan").length;
  if (t1.scanner_errors && t1.scanner_errors.length) return `<p class="err">A security scanner crashed during this run, so security findings are incomplete and not shown as counts. (${esc(t1.scanner_errors[0])})</p>`;
  const list = sec.top.length ? `<ul class="checks">${sec.top.map((f) => `<li><span>${esc(f.title)}${f.count > 1 ? ` <span class="hint">×${f.count}</span>` : ""}</span><span class="sev"><span class="${f.severity[0]}">${esc(f.severity)}</span></span></li>`).join("")}</ul>` : `<p class="hint">No findings from security scanners.</p>`;
  return list + `<p class="hint">From SkillSpector, Gitleaks, Bandit/Semgrep, PII, dependency and Unicode scanners. Findings are static pattern matches and need human judgement; they are not confirmed vulnerabilities.${inc ? " SkillSpector reported partial analysis for this skill (often because code samples mention example file names it cannot resolve)." : ""}</p>`;
}

async function refreshDetail(id, quiet) {
  try {
    const s = await api(`/api/skills/${encodeURIComponent(id)}`);
    $("#detail-body").innerHTML = detailHtml(s);
  } catch (err) { if (!quiet) $("#detail-body").innerHTML = `<p class="err">${esc(err.message)}</p>`; }
}

function openDetail(id) {
  state.openId = id;
  history.replaceState(null, "", `/s/${encodeURIComponent(id)}`);
  $("#detail-body").innerHTML = `<p class="muted">Loading…</p>`;
  $("#detail").showModal();
  refreshDetail(id);
}

// --- Add skill flow
function resetAdd() {
  state.found = null;
  $("#found").hidden = true; $("#add-err").hidden = true; $("#url").value = "";
}

async function lookup(ev) {
  ev.preventDefault();
  const btn = $("#lookup");
  btn.disabled = true; btn.textContent = "Looking…"; $("#add-err").hidden = true; $("#found").hidden = true;
  try {
    const res = await api("/api/discover", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: $("#url").value }) });
    state.found = res;
    if (!res.skills.length) throw new Error("No SKILL.md found in this repository.");
    $("#found-meta").textContent = `${res.skills.length} skill${res.skills.length > 1 ? "s" : ""} in ${res.repo} @ ${res.commit.slice(0, 10)}. Choose up to ${state.meta.limits.skills_per_submission}.`;
    $("#found-list").innerHTML = res.skills.map((s, i) => `<li><input type="checkbox" id="sk${i}" value="${esc(s.path)}" ${res.skills.length === 1 ? "checked" : ""}>
      <label for="sk${i}" style="margin:0;font-weight:500">${esc(s.name)} ${s.listed ? '<span class="hint">· already listed (re-evaluates if changed)</span>' : ""}<small>${esc(s.path)}</small><small>${esc(s.description.slice(0, 140))}</small></label></li>`).join("");
    $("#found").hidden = false;
  } catch (err) {
    $("#add-err").textContent = err.message; $("#add-err").hidden = false;
  } finally { btn.disabled = false; btn.textContent = "Find skills"; }
}

async function submitSkills() {
  const paths = [...document.querySelectorAll("#found-list input:checked")].map((i) => i.value);
  if (!paths.length) { $("#add-err").textContent = "Choose at least one skill."; $("#add-err").hidden = false; return; }
  const btn = $("#submit-skills"); btn.disabled = true;
  try {
    const res = await api("/api/submit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: state.found.repo, paths, category: $("#cat").value }) });
    $("#add").close(); resetAdd();
    await load();
    const first = res.submitted.find((x) => x.skill_id);
    if (first) openDetail(first.skill_id);
  } catch (err) {
    $("#add-err").textContent = err.message; $("#add-err").hidden = false;
  } finally { btn.disabled = false; }
}

async function init() {
  state.meta = await api("/api/meta");
  $("#cat").innerHTML = state.meta.categories.map((c) => `<option value="${c.id}" ${c.id === "other" ? "selected" : ""}>${esc(c.label)}</option>`).join("");
  $("#q").addEventListener("input", (e) => { state.q = e.target.value; renderBoard(); });
  $("#tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) { state.tab = b.dataset.tab; renderTabs(); renderBoard(); } });
  $("#board tbody").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openDetail(tr.dataset.id); });
  $("#board tbody").addEventListener("keydown", (e) => { if (e.key === "Enter") { const tr = e.target.closest("tr[data-id]"); if (tr) openDetail(tr.dataset.id); } });
  $("#add-btn").addEventListener("click", () => { resetAdd(); $("#add").showModal(); $("#url").focus(); });
  $("#add-form").addEventListener("submit", lookup);
  $("#submit-skills").addEventListener("click", submitSkills);
  document.addEventListener("click", (e) => { if (e.target.matches("[data-close]")) e.target.closest("dialog").close(); });
  $("#detail").addEventListener("close", () => { state.openId = null; history.replaceState(null, "", "/"); });
  for (const d of document.querySelectorAll("dialog")) d.addEventListener("click", (e) => { if (e.target === d) d.close(); });
  await load();
  const m = location.pathname.match(/^\/s\/(.+)$/);
  if (m) openDetail(decodeURIComponent(m[1]));
}
init().catch((e) => { document.body.insertAdjacentHTML("beforeend", `<p class="err" style="text-align:center">${esc(e.message)}</p>`); });
