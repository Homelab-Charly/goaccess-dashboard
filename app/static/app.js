/* GoAccess filter UI: keeps filter state in the URL, asks the backend for a
 * regenerated report and shows it in the iframe. No dependencies. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const form = $("#filters");
  const iframe = $("#report");
  const overlay = $("#loading");
  const loadingText = $("#loading-text");
  const summaryEl = $("#summary");
  const rangeEl = $("#range");
  const errorEl = $("#error");
  const exportLink = $("#btn-export");

  const TEXT_FIELDS = [
    "status", "ip", "ip_exclude", "path", "ua", "q",
    "status_exclude", "host_contains", "path_exclude", "path_regex", "ua_exclude",
  ];
  const MULTI_FIELDS = ["host", "source", "method"];
  const PRESETS = { "1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400 };

  let preset = "all";
  let currentRequest = 0;
  let meta = null;

  // ------------------------------------------------------------------ utils
  const pad = (n) => String(n).padStart(2, "0");

  function toLocalInput(date) {
    // Date -> "YYYY-MM-DDTHH:MM:SS" in the browser's timezone (for datetime-local)
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
      `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function fromLocalInput(value) {
    if (!value) return null;
    const d = new Date(value); // datetime-local is parsed as local time
    return isNaN(d) ? null : d;
  }

  function fmtDate(ts) {
    if (!ts) return "?";
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function fmtBytes(n) {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
  }

  const fmtNum = (n) => Number(n).toLocaleString();

  function setPreset(name) {
    preset = name;
    $$(".presets button").forEach((b) => b.classList.toggle("active", b.dataset.preset === name));
    if (name !== "custom") {
      form.elements.from.value = "";
      form.elements.to.value = "";
    }
  }

  function updateCounts() {
    $$(".dropdown").forEach((dd) => {
      const n = $$('input[type="checkbox"]:checked', dd).length;
      $(".count", dd).textContent = n ? String(n) : "";
    });
  }

  // ------------------------------------------------------------ form <-> url
  function formToParams() {
    const p = new URLSearchParams();
    if (preset !== "all" && preset !== "custom") p.set("preset", preset);
    const from = fromLocalInput(form.elements.from.value);
    const to = fromLocalInput(form.elements.to.value);
    if (from) p.set("from", from.toISOString());
    if (to) p.set("to", to.toISOString());
    for (const name of TEXT_FIELDS) {
      const v = form.elements[name].value.trim();
      if (v) p.set(name, v);
    }
    for (const name of MULTI_FIELDS) {
      $$(`input[name="${name}"]:checked`, form).forEach((cb) => p.append(name, cb.value));
    }
    if (form.elements.ignore_crawlers.checked) p.set("ignore_crawlers", "1");
    return p;
  }

  function paramsToForm(p) {
    HTMLFormElement.prototype.reset.call(form);
    const pre = p.get("preset");
    if (pre && PRESETS[pre]) setPreset(pre);
    else if (p.get("from") || p.get("to")) setPreset("custom");
    else setPreset("all");

    for (const key of ["from", "to"]) {
      const v = p.get(key);
      if (v) {
        const d = new Date(v);
        if (!isNaN(d)) form.elements[key].value = toLocalInput(d);
      }
    }
    for (const name of TEXT_FIELDS) form.elements[name].value = p.get(name) || "";
    for (const name of MULTI_FIELDS) {
      const wanted = new Set(p.getAll(name).flatMap((v) => v.split(",")).map((s) => s.trim()).filter(Boolean));
      ensureOptions(name, wanted);
      $$(`input[name="${name}"]`, form).forEach((cb) => { cb.checked = wanted.has(cb.value); });
    }
    form.elements.ignore_crawlers.checked = ["1", "true", "yes", "on"].includes((p.get("ignore_crawlers") || "").toLowerCase());
    const advancedUsed = ["status_exclude", "host_contains", "path_exclude", "path_regex", "ua_exclude"].some((k) => p.get(k));
    if (advancedUsed) {
      $(".advanced", form).hidden = false;
      $("#toggle-advanced").textContent = "less";
    }
    updateCounts();
  }

  /** Resolve the relative preset to absolute from/to for the API call. */
  function apiParams(p, force) {
    const out = new URLSearchParams(p);
    const pre = out.get("preset");
    out.delete("preset");
    if (pre && PRESETS[pre]) {
      out.set("from", new Date(Date.now() - PRESETS[pre] * 1000).toISOString());
      out.delete("to");
    }
    if (force) out.set("force", "1");
    return out;
  }

  // ------------------------------------------------------------- dropdowns
  function makeCheckbox(name, value, labelText, extra) {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.name = name;
    cb.value = value;
    label.append(cb, document.createTextNode(" " + labelText));
    if (extra) {
      const small = document.createElement("small");
      small.textContent = extra;
      label.append(small);
    }
    return label;
  }

  function renderMenu(menuId, name, items) {
    const menu = $(menuId);
    const checked = new Set($$(`input[name="${name}"]:checked`, menu).map((cb) => cb.value));
    menu.replaceChildren();
    if (!items.length) {
      const em = document.createElement("em");
      em.textContent = "none found";
      menu.append(em);
      return;
    }
    const actions = document.createElement("div");
    actions.className = "actions";
    const all = document.createElement("button"); all.type = "button"; all.textContent = "all";
    const none = document.createElement("button"); none.type = "button"; none.textContent = "none";
    all.onclick = () => { $$(`input[name="${name}"]`, menu).forEach((cb) => cb.checked = true); updateCounts(); };
    none.onclick = () => { $$(`input[name="${name}"]`, menu).forEach((cb) => cb.checked = false); updateCounts(); };
    actions.append(all, none);
    menu.append(actions);
    for (const it of items) {
      const label = makeCheckbox(name, it.value, it.label, it.extra);
      if (checked.has(it.value)) $("input", label).checked = true;
      menu.append(label);
    }
  }

  /** Make sure checkboxes exist for values coming from the URL (before meta loads). */
  function ensureOptions(name, values) {
    const menu = $(`#${name}-menu`);
    if (!menu) return;
    const existing = new Set($$(`input[name="${name}"]`, menu).map((cb) => cb.value));
    for (const v of values) {
      if (!existing.has(v)) {
        if ($("em", menu)) menu.replaceChildren();
        menu.append(makeCheckbox(name, v, v));
      }
    }
  }

  document.addEventListener("click", (e) => {
    $$(".dropdown[open]").forEach((dd) => { if (!dd.contains(e.target)) dd.removeAttribute("open"); });
  });
  form.addEventListener("change", (e) => {
    if (e.target.type === "checkbox") updateCounts();
    if (e.target.name === "from" || e.target.name === "to") {
      if (form.elements.from.value || form.elements.to.value) setPreset("custom");
      else setPreset("all");
    }
  });

  // ------------------------------------------------------------------ meta
  async function loadMeta(refresh = false) {
    try {
      const res = await fetch(`/api/meta${refresh ? "?refresh=1" : ""}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      meta = await res.json();
    } catch (err) {
      rangeEl.textContent = `meta unavailable (${err.message})`;
      return;
    }
    renderMenu("#host-menu", "host", meta.hosts.map((h) => ({ value: h, label: h })));
    renderMenu("#source-menu", "source", meta.sources.map((s) => ({
      value: s.id, label: s.id, extra: `${s.files} file${s.files === 1 ? "" : "s"}, ${fmtBytes(s.bytes)}`,
    })));
    updateCounts();
    const bits = [];
    if (meta.oldest) bits.push(`logs ${fmtDate(meta.oldest)} → ${fmtDate(meta.newest)}`);
    bits.push(`${meta.files} files, ${fmtBytes(meta.total_bytes)}`);
    if (meta.skip_archived_logs) bits.push("archives skipped");
    if (meta.geoip) bits.push("GeoIP on");
    if (meta.goaccess_version) bits.push(meta.goaccess_version.replace(/^GoAccess\s*-?\s*/i, "GoAccess "));
    rangeEl.textContent = bits.join(" · ");
  }

  // ---------------------------------------------------------------- report
  function showError(msg) {
    errorEl.textContent = msg;
    errorEl.hidden = !msg;
  }

  async function runReport({ force = false, push = true } = {}) {
    const urlParams = formToParams();
    const qs = urlParams.toString();
    if (push) {
      const newUrl = location.pathname + (qs ? "?" + qs : "");
      if (newUrl !== location.pathname + location.search) history.pushState(null, "", newUrl);
    }
    const api = apiParams(urlParams, force);
    exportLink.href = "/api/export?" + apiParams(urlParams, false).toString();

    const reqId = ++currentRequest;
    overlay.hidden = false;
    loadingText.textContent = force ? "Regenerating report…" : "Filtering logs and rendering report…";
    showError("");
    $("#btn-apply").disabled = true;
    $("#btn-refresh").disabled = true;
    const started = performance.now();

    try {
      const res = await fetch("/api/report?" + api.toString());
      if (reqId !== currentRequest) return;
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const body = await res.json();
          if (Array.isArray(body.detail)) detail = body.detail.map((d) => `${d.field}: ${d.message}`).join("; ");
          else if (body.detail) detail = String(body.detail);
        } catch (_) { /* ignore */ }
        throw new Error(detail);
      }
      const r = await res.json();
      iframe.src = r.url;
      const parts = [
        `<b>${fmtNum(r.matched_lines)}</b> of ${fmtNum(r.lines_read)} requests`,
        `${r.files_scanned}/${r.files_total} files`,
        `${(r.cached ? 0 : r.duration).toFixed(1)}s${r.cached ? " (cached)" : ""}`,
      ];
      if (r.empty) parts.push("no matches");
      summaryEl.innerHTML = parts.join(" · ");
      const onLoad = () => { overlay.hidden = true; iframe.removeEventListener("load", onLoad); };
      iframe.addEventListener("load", onLoad);
      setTimeout(() => { overlay.hidden = true; }, 4000); // safety net
    } catch (err) {
      if (reqId !== currentRequest) return;
      overlay.hidden = true;
      showError(err.message);
      summaryEl.textContent = `failed after ${((performance.now() - started) / 1000).toFixed(1)}s`;
    } finally {
      if (reqId === currentRequest) {
        $("#btn-apply").disabled = false;
        $("#btn-refresh").disabled = false;
      }
    }
  }

  // --------------------------------------------------------------- events
  form.addEventListener("submit", (e) => { e.preventDefault(); runReport(); });
  $("#btn-refresh").addEventListener("click", () => runReport({ force: true }));
  $("#btn-reset").addEventListener("click", () => {
    paramsToForm(new URLSearchParams());
    runReport();
  });
  $("#toggle-advanced").addEventListener("click", (e) => {
    const adv = $(".advanced", form);
    adv.hidden = !adv.hidden;
    e.target.textContent = adv.hidden ? "more…" : "less";
  });
  $$(".presets button").forEach((b) => b.addEventListener("click", () => {
    setPreset(b.dataset.preset);
    runReport();
  }));
  window.addEventListener("popstate", () => {
    paramsToForm(new URLSearchParams(location.search));
    runReport({ push: false });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.tagName === "INPUT" && e.target.type !== "checkbox") {
      e.preventDefault();
      runReport();
    }
  });

  // ----------------------------------------------------------------- init
  paramsToForm(new URLSearchParams(location.search));
  loadMeta();
  runReport({ push: false });
})();
