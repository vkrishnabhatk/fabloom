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

function describe(e) {
  switch (e.type) {
    case "run_started": return "started; verifier " + e.verifier;
    case "state_changed": return e.from_state + " → " + e.to_state + (e.reason ? " (" + e.reason + ")" : "");
    case "intent_planned": return "intent planned (" + e.source + ")";
    case "checks_compiled": return "compiled " + e.count + " checks" +
      (e.unverifiable.length ? ", " + e.unverifiable.length + " not verifiable" : "");
    case "llm_call": return e.role + ": " + e.model + " " + e.prompt_tokens + "+" +
      e.completion_tokens + " tok " + usd(e.cost_usd) + " " + e.duration_ms + "ms";
    case "candidate_generated": return "round " + e.round + " candidate " + e.candidate +
      ": " + e.devices.length + " devices";
    case "verification_completed": {
      const s = e.summary;
      return "round " + e.round + ": " + (s.total - s.failed.length) + "/" + s.total +
        " checks pass, score " + Number(s.score).toFixed(3) + ", " + s.level + (e.cached ? " (cached)" : "");
    }
    case "grounding_lookup": return "doc grounding: " + e.queries.length + " queries, " +
      e.excerpts + " excerpts" + (e.error ? " (" + e.error + ")" : "");
    case "budget_exhausted": return "budget exhausted: " + e.limit + " (" + e.detail + ")";
    case "explanation_ready": return "explanation ready (" + e.source + ")";
    case "run_error": return "error: " + e.error_type + ": " + e.message;
    case "run_finished": return "finished: " + e.outcome + ", score " + Number(e.score).toFixed(3) +
      ", " + e.repair_rounds + " repair rounds, " + usd(e.usage.cost_usd);
    default: return e.type;
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
    }
  });
}

function renderResult(run) {
  document.getElementById("status").textContent = run.finished
    ? run.outcome + " · score " + Number(run.score).toFixed(3) + " · " + run.repair_rounds +
      " repair rounds · " + usd(run.cost_usd) + (run.level ? " · level " + run.level : "")
    : "running: " + run.state;
  document.getElementById("status").className = "status status-" + (run.outcome || "running");
  if (run.request) document.getElementById("request").textContent = run.request;
  if (!run.finished) return;
  document.getElementById("result").hidden = false;
  document.getElementById("explanation").textContent = run.explanation || "";
  if (run.unverifiable && run.unverifiable.length) {
    const u = document.getElementById("unverifiable");
    u.textContent = "Not verifiable here: " + run.unverifiable.join("; ");
    u.hidden = false;
  }
  const checks = document.getElementById("checks");
  checks.replaceChildren(...(run.checks || []).map((c) =>
    el("li", (c.passed ? "✓ " : "✗ ") + c.id, c.passed ? "pass" : "fail")));
  const configs = document.getElementById("configs");
  configs.replaceChildren(...Object.entries(run.configs || {}).map(([device, text]) => {
    const d = el("details");
    d.append(el("summary", device), el("pre", text));
    return d;
  }));
}

async function fetchRun(id) {
  const resp = await fetch("/api/v1/runs/" + encodeURIComponent(id));
  return resp.json();
}

async function initRun() {
  const id = document.body.dataset.runId;
  const list = document.getElementById("events");
  const run = await fetchRun(id);
  renderResult(run);
  const source = new EventSource("/api/v1/runs/" + encodeURIComponent(id) + "/events");
  const finish = async () => { source.close(); renderResult(await fetchRun(id)); };
  source.onmessage = null;
  const types = ["run_started", "state_changed", "intent_planned", "checks_compiled", "llm_call",
    "candidate_generated", "verification_completed", "grounding_lookup", "budget_exhausted",
    "explanation_ready", "run_error", "run_finished"];
  types.forEach((t) => source.addEventListener(t, (msg) => {
    const e = JSON.parse(msg.data);
    list.append(el("li", describe(e), "ev ev-" + e.type));
    if (e.type === "state_changed") document.getElementById("status").textContent = "running: " + e.to_state;
  }));
  source.addEventListener("end", finish);
  source.onerror = () => { if (source.readyState === EventSource.CLOSED) finish(); };
}

document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  if (page === "index") initIndex();
  if (page === "run") initRun();
});
