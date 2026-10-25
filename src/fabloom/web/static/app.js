// Fabloom web UI. Plain JS over the JSON API. Configs and explanations are model output, so
// everything is rendered with textContent, never innerHTML.
"use strict";

function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined && text !== null) n.textContent = String(text);
  if (cls) n.className = cls;
  return n;
}

function usd(x) {
  return "$" + Number(x || 0).toFixed(4);
}

function plural(n, word) {
  return n + " " + word + (n === 1 ? "" : "s");
}

const PLATFORMS = { frr: "FRR", arista_eos: "Arista EOS" };

function modelName(id) {
  const m = /nemotron-3-(nano|super|ultra)/i.exec(id || "");
  return m ? "Nemotron " + m[1][0].toUpperCase() + m[1].slice(1).toLowerCase() : id;
}

// Check kinds as compiled in checks/compile.py, in the order a reader would follow them.
const CHECK_KINDS = [
  ["prelint", "One config block per planned device"],
  ["parse_clean", "Configs parse cleanly"],
  ["no_undefined_refs", "No undefined references"],
  ["l3_edges", "Fabric links up at layer 3"],
  ["bgp_compatible", "BGP peers compatible"],
  ["bgp_established", "BGP sessions establish"],
  ["route_present", "Expected routes present"],
  ["reachability", "Loopbacks reachable"],
  ["filter_denies", "Filters block what you asked"],
  ["no_forwarding_loops", "No forwarding loops"],
];

function fmtDuration(ms) {
  const s = Math.round(ms / 1000);
  return s < 60 ? s + " s" : Math.floor(s / 60) + " min " + (s % 60) + " s";
}

// One timeline entry per event worth showing; state changes only update the verdict.
function describe(e) {
  switch (e.type) {
    case "llm_call": {
      const what = {
        planner: "planned the fabric",
        generator: "wrote the configs",
        repairer: "repaired the failing configs",
        repairer_escalated: "took over the repair",
        explainer: "wrote the explanation",
      }[e.role] || e.role;
      return { cls: "t-model", title: modelName(e.model) + " " + what,
        detail: (e.prompt_tokens + e.completion_tokens).toLocaleString() + " tokens · " +
          usd(e.cost_usd) + " · " + fmtDuration(e.duration_ms) };
    }
    case "intent_planned": {
      const i = e.intent || {};
      const t = i.topology || {};
      return { title: "Intent: " + (PLATFORMS[i.platform] || i.platform) + ", " + plural(t.spines, "spine") +
        ", " + plural(t.leaves, "leaf").replace("leafs", "leaves") + ", BGP " + i.bgp_mode };
    }
    case "checks_compiled":
      return { title: "Compiled " + e.count + " checks from the intent",
        detail: "deterministic, the model never sees or edits them" +
          (e.unverifiable.length ? " · " + e.unverifiable.length + " cannot be verified here" : "") };
    case "candidate_generated":
      return { title: (e.round === 0 ? "First draft" : "Repair round " + e.round) + ": " +
        plural(e.devices.length, "device") + " configured", detail: e.devices.join(", ") };
    case "verification_completed": {
      const s = e.summary;
      const ok = s.total - s.failed.length;
      return { cls: s.failed.length ? "t-fail" : "t-pass",
        title: "Batfish: " + ok + " / " + s.total + " checks pass" + (e.cached ? " (cached)" : ""),
        detail: "level " + s.level + (s.duration_ms ? " · " + fmtDuration(s.duration_ms) : ""),
        fails: s.failed.length ? "failing: " + s.failed.slice(0, 4).join(", ") +
          (s.failed.length > 4 ? " and " + (s.failed.length - 4) + " more" : "") : "" };
    }
    case "grounding_lookup":
      return { title: "Looked up vendor docs", detail: e.queries.length + " queries, " + e.excerpts + " excerpts" };
    case "docs_found":
      return { title: "Tavily found " + plural(e.docs.length, "vendor doc page"),
        detail: e.searched ? plural(e.searched, "search") + ", the rest cached" : "all from cache" };
    case "budget_exhausted":
      return { cls: "t-fail", title: "Budget used up: " + e.limit, detail: e.detail };
    case "run_error":
      return { cls: "t-fail", title: "Error: " + e.error_type, detail: e.message };
    case "run_finished":
      return { cls: e.outcome === "passed" ? "t-done" : "t-fail",
        title: e.outcome === "passed" ? "Verified by Batfish" : "Finished without a verified config",
        detail: plural(e.repair_rounds, "repair round") + " · " + usd(e.usage.cost_usd) + " total" };
    default:
      return null;
  }
}

// Topology diagram: spines on top, leaves below, one line per spine-leaf link. Drawn when the
// intent is planned, then coloured from each Batfish round; pair keys match check ids
// such as "bgp_established:leaf1--spine1".
const SVG_NS = "http://www.w3.org/2000/svg";
const LINK_KINDS = ["l3_edges", "bgp_compatible", "bgp_established"];
const NODE_KINDS = ["parse_clean", "no_undefined_refs"];
const fabric = { links: new Map(), nodes: new Map() };

function svgEl(tag, attrs, text) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
  if (text !== undefined) n.textContent = text;
  return n;
}

function drawFabric(spines, leaves) {
  if (fabric.nodes.size || !spines || !leaves) return;
  const width = Math.max(960, 110 * Math.max(spines, leaves));
  const height = 230;
  const rows = { spine: 56, leaf: 186 };
  const x = (i, n) => (i + 0.5) * width / n;
  const svg = svgEl("svg", { viewBox: "0 0 " + width + " " + height, role: "img",
    "aria-label": spines + " spines and " + leaves + " leaves, every leaf linked to every spine" });
  svg.append(svgEl("text", { x: 4, y: 16, class: "tier" }, "SPINES"),
    svgEl("text", { x: 4, y: height - 4, class: "tier" }, "LEAVES"));
  for (let l = 0; l < leaves; l++) {
    for (let s = 0; s < spines; s++) {
      const line = svgEl("line", { x1: x(s, spines), y1: rows.spine, x2: x(l, leaves), y2: rows.leaf, class: "link" });
      fabric.links.set("leaf" + (l + 1) + "--spine" + (s + 1), line);
      svg.append(line);
    }
  }
  const node = (name, cx, cy) => {
    const g = svgEl("g", { class: "node" });
    g.append(svgEl("rect", { x: cx - 38, y: cy - 17, width: 76, height: 34, rx: 9 }), svgEl("text", { x: cx, y: cy }, name));
    fabric.nodes.set(name, g);
    svg.append(g);
  };
  for (let s = 0; s < spines; s++) node("spine" + (s + 1), x(s, spines), rows.spine);
  for (let l = 0; l < leaves; l++) node("leaf" + (l + 1), x(l, leaves), rows.leaf);
  document.getElementById("diagram").replaceChildren(svg);
  document.getElementById("topology").hidden = false;
}

// status: check id -> passed. Anything a round did not report stays as it was.
function colourFabric(status) {
  const verdict = new Map();
  for (const [id, passed] of status) {
    const [kind, subject] = id.split(":");
    const target = LINK_KINDS.includes(kind) ? fabric.links.get(subject)
      : NODE_KINDS.includes(kind) ? fabric.nodes.get(subject) : null;
    if (!target) continue;
    verdict.set(target, (verdict.get(target) ?? true) && passed);
  }
  for (const [target, ok] of verdict) {
    const base = target.tagName === "line" ? "link" : "node";
    target.setAttribute("class", base + (ok ? " ok" : " bad"));
  }
}

function initIndex() {
  const form = document.getElementById("run-form");
  const text = document.getElementById("request");
  const err = document.getElementById("form-error");
  document.querySelectorAll("button.example").forEach((b) => {
    b.addEventListener("click", () => { text.value = b.dataset.request; text.focus(); });
  });
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    err.hidden = true;
    const platform = document.getElementById("platform").value || null;
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    button.textContent = "Starting…";
    try {
      const resp = await fetch("/api/v1/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request: text.value, platform: platform }),
      });
      const body = await resp.json();
      if (!resp.ok) {
        const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail;
        throw new Error(detail || resp.statusText);
      }
      window.location = body.url;
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
      button.disabled = false;
      button.textContent = "Generate and verify";
    }
  });
}

function setVerdict(state, title, sub) {
  document.getElementById("verdict").className = "verdict verdict-" + state;
  const icon = document.getElementById("verdict-icon");
  icon.replaceChildren(state === "running" ? el("span", null, "spinner")
    : el("span", state === "passed" ? "✓" : "✗"));
  document.getElementById("verdict-title").textContent = title;
  document.getElementById("verdict-sub").textContent = sub || "";
}

function renderChecks(checks) {
  const groups = new Map();
  for (const c of checks) {
    if (!groups.has(c.kind)) groups.set(c.kind, []);
    groups.get(c.kind).push(c);
  }
  const known = CHECK_KINDS.map((k) => k[0]);
  const kinds = [...known.filter((k) => groups.has(k)), ...[...groups.keys()].filter((k) => !known.includes(k))];
  const labels = Object.fromEntries(CHECK_KINDS);
  return kinds.map((kind) => {
    const list = groups.get(kind);
    const ok = list.filter((c) => c.passed).length;
    const li = el("li", null, "check-group");
    const bar = el("span", null, "bar");
    const fill = el("i");
    fill.style.width = (100 * ok / list.length) + "%";
    bar.append(fill);
    li.append(el("span", (ok === list.length ? "✓ " : "✗ ") + (labels[kind] || kind), "name"),
      el("span", ok + " / " + list.length, "count"), bar);
    const failed = list.filter((c) => !c.passed).map((c) => c.id);
    if (failed.length) li.append(el("span", "failing: " + failed.join(", "), "failed-ids"));
    return li;
  });
}

function renderDocs(docs) {
  const safe = docs.filter((d) => /^https:\/\//.test(d.url));
  document.getElementById("docs-card").hidden = safe.length === 0;
  document.getElementById("docs").replaceChildren(...safe.map((d) => {
    const a = el("a");
    a.href = d.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.append(el("b", d.title), el("span", new URL(d.url).host + " · " + d.query));
    const li = el("li");
    li.append(a);
    return li;
  }));
}

function renderConfigs(configs) {
  const tabs = document.getElementById("tabs");
  const pre = document.getElementById("config");
  const names = Object.keys(configs);
  const show = (name) => {
    pre.textContent = configs[name];
    tabs.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.name === name)));
  };
  tabs.replaceChildren(...names.map((name) => {
    const b = el("button", name, "tab");
    b.type = "button";
    b.setAttribute("role", "tab");
    b.dataset.name = name;
    b.addEventListener("click", () => show(name));
    return b;
  }));
  if (names.length) show(names[0]);
  const copy = document.getElementById("copy");
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(pre.textContent);
      copy.textContent = "Copied";
    } catch (e) {
      copy.textContent = "Copy failed";
    }
    setTimeout(() => { copy.textContent = "Copy"; }, 1500);
  };
}

function renderRun(run) {
  if (run.request) document.getElementById("request").textContent = run.request;
  if (run.fabric) {
    const f = run.fabric;
    document.getElementById("fabric").replaceChildren(
      el("span", PLATFORMS[f.platform] || f.platform, "badge badge-platform"),
      el("span", f.spines + " spines · " + f.leaves + " leaves", "badge"),
      el("span", "verifier " + run.verifier, "badge"));
  }
  if (!run.finished) {
    setVerdict("running", "Running…", "state: " + run.state);
    return;
  }
  const passed = run.outcome === "passed";
  setVerdict(passed ? "passed" : run.outcome,
    passed ? "Verified by Batfish" : "No verified config",
    passed ? "Every check compiled from the request passes" + (run.level ? " (level " + run.level + ")" : "") + "."
      : "The run stopped (" + run.outcome + ") before every check passed.");
  const checks = run.checks || [];
  document.getElementById("m-checks").textContent = checks.filter((c) => c.passed).length + " / " + checks.length;
  document.getElementById("m-rounds").textContent = run.repair_rounds;
  document.getElementById("m-cost").textContent = usd(run.cost_usd);
  document.getElementById("result").hidden = false;
  document.getElementById("explanation").textContent = run.explanation || "";
  if (run.unverifiable && run.unverifiable.length) {
    const u = document.getElementById("unverifiable");
    u.textContent = "Not verifiable here: " + run.unverifiable.join("; ");
    u.hidden = false;
  }
  renderDocs(run.docs || []);
  document.getElementById("checks").replaceChildren(...renderChecks(checks));
  if (run.fabric) drawFabric(run.fabric.spines, run.fabric.leaves);
  colourFabric(new Map(checks.map((c) => [c.id, c.passed])));
  renderConfigs(run.configs || {});
}

async function fetchRun(id) {
  const resp = await fetch("/api/v1/runs/" + encodeURIComponent(id));
  return resp.json();
}

async function initRun() {
  const id = document.body.dataset.runId;
  const list = document.getElementById("events");
  renderRun(await fetchRun(id));
  let first = null;
  const source = new EventSource("/api/v1/runs/" + encodeURIComponent(id) + "/events");
  const finish = async () => { source.close(); renderRun(await fetchRun(id)); };
  const types = ["run_started", "state_changed", "intent_planned", "checks_compiled", "llm_call",
    "candidate_generated", "verification_completed", "grounding_lookup", "docs_found", "budget_exhausted",
    "explanation_ready", "run_error", "run_finished"];
  types.forEach((t) => source.addEventListener(t, (msg) => {
    const e = JSON.parse(msg.data);
    const ts = Date.parse(e.ts);
    if (first === null) first = ts;
    document.getElementById("m-time").textContent = fmtDuration(ts - first);
    if (e.type === "state_changed") {
      if (e.to_state !== "DONE") setVerdict("running", "Running…", "now: " + e.to_state.toLowerCase().replace(/_/g, " "));
      return;
    }
    if (e.type === "intent_planned") {
      const t = (e.intent || {}).topology || {};
      drawFabric(t.spines, t.leaves);
    }
    if (e.type === "verification_completed") {
      // Rounds report only failures: every planned link and node not listed passed.
      const failed = new Set(e.summary.failed);
      const all = [...fabric.links.keys()].flatMap((k) => LINK_KINDS.map((kind) => kind + ":" + k))
        .concat([...fabric.nodes.keys()].flatMap((k) => NODE_KINDS.map((kind) => kind + ":" + k)));
      colourFabric(new Map(all.map((id) => [id, !failed.has(id)])));
    }
    const d = describe(e);
    if (!d) return;
    const li = el("li", null, d.cls || "");
    li.append(el("div", d.title, "t-title"));
    if (d.detail) li.append(el("div", d.detail, "t-detail"));
    if (d.fails) li.append(el("div", d.fails, "t-fails"));
    list.append(li);
  }));
  source.addEventListener("end", finish);
  source.onerror = () => { if (source.readyState === EventSource.CLOSED) finish(); };
}

document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  if (page === "index") initIndex();
  if (page === "run") initRun();
});
