"use strict";

// v0.3.0: spreadsheet-style editor.
//
// All Source / Output / Settings inputs are bound to `state.draft` — a
// client-side mutable copy of the last-saved config. Editing anything just
// updates the draft. The Save button at the top PUTs the whole draft;
// Discard reverts to the last-saved snapshot. A "N unsaved" pill in the
// header counts the diff.
//
// Live snapshot data (status dot, packet count) is layered on TOP of the
// editable rows via targeted DOM updates — we never tear the tbody down on
// a snapshot tick, so the operator's typing focus / cursor stays put.

(() => {
  const DMX_HEADER_LEN = 3;
  const DMX_CHANNELS = 512;

  const state = {
    ws: null,
    config: null,          // last server-saved config; never mutated
    draft: null,           // editable copy; what Save sends
    snapshot: null,        // runtime data
    network: null,         // GET /api/network result
    canvases: new Map(),
    watchedUniverses: new Set(),
    version: null,
    pendingNetworkTarget: null,  // {newIp, port} after Apply Network; UI polls
  };

  function cloneCfg(cfg) {
    return cfg ? JSON.parse(JSON.stringify(cfg)) : null;
  }

  function dirtyKeys() {
    if (!state.config || !state.draft) return [];
    const keys = [];
    if (JSON.stringify(state.config.sources || []) !== JSON.stringify(state.draft.sources || []))
      keys.push("sources");
    if (JSON.stringify(state.config.outputs || []) !== JSON.stringify(state.draft.outputs || []))
      keys.push("outputs");
    const settingsFields = ["bind_ip", "send_rate_hz", "source_timeout_s",
                            "send_keepalive_when_silent", "auto_allow_unknown_sources"];
    for (const f of settingsFields) {
      if (state.config[f] !== state.draft[f]) { keys.push("settings"); break; }
    }
    return keys;
  }

  function updateDirtyPill() {
    const pill = document.getElementById("dirty-pill");
    const saveBtn = document.getElementById("save-all-btn");
    const discardBtn = document.getElementById("discard-btn");
    if (!pill || !saveBtn || !discardBtn) return;
    const keys = dirtyKeys();
    if (keys.length === 0) {
      pill.hidden = true;
      saveBtn.disabled = true;
      discardBtn.disabled = true;
      window.removeEventListener("beforeunload", _beforeUnloadGuard);
    } else {
      pill.hidden = false;
      // "3 unsaved" or "1 unsaved" — the integer is the count of sub-areas
      // that differ from saved, NOT the count of individual fields.
      pill.textContent = `${keys.length} unsaved`;
      pill.title = `Changes in: ${keys.join(", ")}`;
      saveBtn.disabled = false;
      discardBtn.disabled = false;
      window.addEventListener("beforeunload", _beforeUnloadGuard);
    }
  }
  function _beforeUnloadGuard(e) {
    // Trigger the browser's "leave site?" confirm if the operator has
    // unsaved edits. The custom message text is ignored on modern browsers,
    // but returning a string is what flips the prompt on.
    e.preventDefault();
    e.returnValue = "Unsaved changes. Leave anyway?";
    return e.returnValue;
  }

  // Called by input.onchange handlers anywhere in the page after mutating
  // state.draft. Updates the dirty pill + save/discard buttons.
  function markDirty() { updateDirtyPill(); }

  // ---- Toast ----
  function toast(message, { kind = "ok", durationMs = 2500 } = {}) {
    const stack = document.getElementById("toast-stack");
    if (!stack) return;
    const el = document.createElement("div");
    el.className = `toast toast-${kind}`;
    el.textContent = message;
    stack.appendChild(el);
    requestAnimationFrame(() => el.classList.add("show"));
    setTimeout(() => {
      el.classList.remove("show");
      setTimeout(() => el.remove(), 300);
    }, durationMs);
  }

  // ---- API ----
  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`${res.status}: ${text}`);
    }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res.text();
  }
  async function loadConfig() {
    state.config = await api("/api/config");
    state.draft = cloneCfg(state.config);
    renderEverything();
  }
  async function loadVersion() {
    try { state.version = await api("/api/version"); }
    catch (e) { state.version = { version: "?", git_sha: null }; }
    renderVersion();
  }
  async function loadNetwork() {
    try { state.network = await api("/api/network"); }
    catch (e) { state.network = { available: false, mode: "dhcp", error: String(e) }; }
    renderNetwork();
  }

  function renderVersion() {
    const el = document.getElementById("version-badge");
    if (!el) return;
    const v = state.version || {};
    const ver = (v.version || "?").replace(/^v+/i, "");
    el.textContent = `v${ver}`;
    el.classList.toggle("dev", ver.includes("dev") || ver === "?");
    const titleBits = [`version ${ver}`];
    if (v.git_sha) titleBits.push(`git ${String(v.git_sha).slice(0, 7)}`);
    if (v.built_at) titleBits.push(`built ${v.built_at}`);
    if (v.image) titleBits.push(`image ${v.image}`);
    el.title = titleBits.join(" • ");

    const firstbootBanner = document.getElementById("firstboot-error");
    const firstbootText = document.getElementById("firstboot-error-text");
    if (v.last_firstboot_error) {
      firstbootText.textContent = v.last_firstboot_error;
      firstbootBanner.hidden = false;
    } else {
      firstbootBanner.hidden = true;
    }
  }

  // ---- WebSocket ----
  function connectWS() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/ws/state`;
    state.ws = new WebSocket(url);
    state.ws.binaryType = "arraybuffer";
    state.ws.onopen = () => {
      setConnectionPill(true);
      for (const u of state.watchedUniverses) {
        state.ws.send(JSON.stringify({ type: "watch", universe: u }));
      }
    };
    state.ws.onclose = () => {
      setConnectionPill(false);
      setTimeout(connectWS, 1500);
    };
    state.ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        const msg = JSON.parse(ev.data);
        if (msg.type === "snapshot") {
          state.snapshot = msg.data;
          renderSnapshotOverlay();
        }
      } else {
        handleDmxFrame(new Uint8Array(ev.data));
      }
    };
  }
  function setConnectionPill(connected) {
    const el = document.getElementById("conn-pill");
    el.textContent = connected ? "connected" : "disconnected";
    el.className = `pill ${connected ? "connected" : "disconnected"}`;
  }

  // ---- DMX preview ----
  function handleDmxFrame(buf) {
    if (buf.length < DMX_HEADER_LEN + DMX_CHANNELS) return;
    const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
    const universe = view.getUint16(0, true);
    const dmx = buf.subarray(DMX_HEADER_LEN, DMX_HEADER_LEN + DMX_CHANNELS);
    const c = state.canvases.get(universe);
    if (!c) return;
    drawDmx(c.canvas, c.ctx, dmx);
  }
  function drawDmx(canvas, ctx, dmx) {
    const w = canvas.width, h = canvas.height;
    ctx.fillStyle = "#1a1d23";
    ctx.fillRect(0, 0, w, h);
    const bw = w / DMX_CHANNELS;
    for (let i = 0; i < DMX_CHANNELS; i++) {
      const v = dmx[i];
      if (v === 0) continue;
      const bh = (v / 255) * h;
      const hue = 200 - (v / 255) * 200;
      ctx.fillStyle = `hsl(${hue}, 70%, 55%)`;
      ctx.fillRect(i * bw, h - bh, Math.max(1, bw), bh);
    }
  }
  function setupCanvas(universe, parent) {
    const canvas = document.createElement("canvas");
    canvas.className = "dmx-canvas";
    canvas.width = 1024;
    canvas.height = 80;
    parent.appendChild(canvas);
    const ctx = canvas.getContext("2d");
    state.canvases.set(universe, { canvas, ctx });
    state.watchedUniverses.add(universe);
    if (state.ws?.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({ type: "watch", universe }));
    }
  }
  function clearCanvases() {
    if (state.ws?.readyState === WebSocket.OPEN) {
      for (const u of state.watchedUniverses) {
        state.ws.send(JSON.stringify({ type: "unwatch", universe: u }));
      }
    }
    state.watchedUniverses.clear();
    state.canvases.clear();
  }

  // ---- Live preview tiles ----
  // The set of universes shown comes from snapshot.universes (the union of
  // every enabled output's universes, computed server-side). We render tiles
  // whenever that set changes.
  function renderPreviewList() {
    const container = document.getElementById("preview-list");
    const universes = (state.snapshot?.universes || []).slice();
    // Stable check: same set + same order → don't tear down canvases.
    const currentKeys = Array.from(state.canvases.keys()).sort((a, b) => a - b).join(",");
    const nextKeys = universes.slice().sort((a, b) => a - b).join(",");
    if (currentKeys === nextKeys) return;
    clearCanvases();
    container.innerHTML = "";
    if (universes.length === 0) {
      container.innerHTML = '<span class="muted">Configure an output with a universe range to see live DMX preview.</span>';
      return;
    }
    for (const u of universes) {
      const div = document.createElement("div");
      div.className = "universe-preview";
      const header = document.createElement("div");
      header.className = "header";
      const net = (u >> 8) & 0x7F;
      const sub = (u >> 4) & 0x0F;
      const uni = u & 0x0F;
      header.innerHTML = `
        <span class="label">Universe ${u}</span>
        <span class="net-sub-uni">Net ${net} / Sub ${sub} / Uni ${uni}</span>
      `;
      div.appendChild(header);
      container.appendChild(div);
      setupCanvas(u, div);
    }
  }

  // ---- Settings inputs → draft ----
  function bindSettingsInputs() {
    const bindings = [
      ["cfg-send-rate", "send_rate_hz", (v) => parseFloat(v)],
      ["cfg-source-timeout", "source_timeout_s", (v) => parseFloat(v)],
      ["cfg-keepalive", "send_keepalive_when_silent", null],  // checkbox
      ["cfg-auto-allow", "auto_allow_unknown_sources", null], // checkbox
    ];
    for (const [id, key, coerce] of bindings) {
      const el = document.getElementById(id);
      if (!el) continue;
      const isCheckbox = el.type === "checkbox";
      el.addEventListener("input", () => {
        state.draft[key] = isCheckbox ? el.checked : (coerce ? coerce(el.value) : el.value);
        markDirty();
      });
    }
  }
  function fillSettingsInputs() {
    if (!state.draft) return;
    document.getElementById("cfg-send-rate").value = state.draft.send_rate_hz;
    document.getElementById("cfg-source-timeout").value = state.draft.source_timeout_s;
    document.getElementById("cfg-keepalive").checked = !!state.draft.send_keepalive_when_silent;
    document.getElementById("cfg-auto-allow").checked = !!state.draft.auto_allow_unknown_sources;
  }

  // ---- Sources table (always editable, draft-bound) ----
  function renderSources() {
    const tbody = document.querySelector("#sources-table tbody");
    tbody.innerHTML = "";
    if (!state.draft) return;
    // Iterate draft.sources in array order — operator-controlled. No auto-sort
    // here; would be visually disruptive while editing. The runtime sorts
    // priority-first anyway when picking winners.
    state.draft.sources.forEach((s, i) => {
      tbody.appendChild(buildSourceRow(s, i));
    });
  }
  function buildSourceRow(s, idx) {
    const tr = document.createElement("tr");
    tr.dataset.rowIndex = String(idx);
    tr.dataset.kind = "source";
    if (s.enabled === false) tr.classList.add("row-disabled");

    // On toggle
    const onTd = document.createElement("td");
    onTd.className = "col-active";
    const toggleLabel = document.createElement("label");
    toggleLabel.className = "toggle";
    const toggleInput = document.createElement("input");
    toggleInput.type = "checkbox";
    toggleInput.checked = s.enabled !== false;
    toggleInput.addEventListener("change", () => {
      state.draft.sources[idx].enabled = toggleInput.checked;
      tr.classList.toggle("row-disabled", !toggleInput.checked);
      markDirty();
    });
    const toggleTrack = document.createElement("span");
    toggleTrack.className = "track";
    toggleLabel.append(toggleInput, toggleTrack);
    onTd.appendChild(toggleLabel);
    tr.appendChild(onTd);

    // Status dot (snapshot-driven)
    const statusTd = document.createElement("td");
    statusTd.className = "col-status";
    statusTd.innerHTML = '<span class="dot" data-cell="dot"></span>';
    tr.appendChild(statusTd);

    // IP input
    tr.appendChild(buildCellInput("col-ip", "text", s.ip, "192.168.1.10", (v) => {
      state.draft.sources[idx].ip = v.trim();
      markDirty();
    }));

    // Label input
    tr.appendChild(buildCellInput("col-label", "text", s.label || "", "Console", (v) => {
      state.draft.sources[idx].label = v;
      markDirty();
    }));

    // Mode select
    const modeTd = document.createElement("td");
    modeTd.className = "col-mode";
    const modeSelect = document.createElement("select");
    modeSelect.className = "mode-select";
    for (const m of ["htp", "priority"]) {
      const opt = document.createElement("option");
      opt.value = m;
      opt.textContent = m === "htp" ? "HTP" : "Priority";
      if ((s.mode || "htp") === m) opt.selected = true;
      modeSelect.appendChild(opt);
    }
    modeSelect.addEventListener("change", () => {
      state.draft.sources[idx].mode = modeSelect.value;
      markDirty();
    });
    modeTd.appendChild(modeSelect);
    tr.appendChild(modeTd);

    // Priority input
    tr.appendChild(buildCellInput("col-prio", "number", s.priority ?? 100, "100", (v) => {
      state.draft.sources[idx].priority = parseInt(v, 10) || 100;
      markDirty();
    }, { min: 0, max: 999 }));

    // Packets (snapshot-driven)
    const pktTd = document.createElement("td");
    pktTd.className = "col-packets";
    pktTd.dataset.cell = "packets";
    pktTd.textContent = "—";
    tr.appendChild(pktTd);

    // Actions: Delete
    const actionsTd = document.createElement("td");
    actionsTd.className = "col-actions row-actions";
    const delBtn = document.createElement("button");
    delBtn.className = "subtle";
    delBtn.textContent = "Remove";
    delBtn.addEventListener("click", () => {
      const ip = state.draft.sources[idx].ip || "(new)";
      if (!confirm(`Remove source ${ip}?`)) return;
      state.draft.sources.splice(idx, 1);
      renderSources();
      markDirty();
    });
    actionsTd.appendChild(delBtn);
    tr.appendChild(actionsTd);

    return tr;
  }

  // ---- Outputs table (always editable, draft-bound) ----
  function renderOutputs() {
    const tbody = document.querySelector("#outputs-table tbody");
    tbody.innerHTML = "";
    if (!state.draft) return;
    state.draft.outputs.forEach((o, i) => {
      tbody.appendChild(buildOutputRow(o, i));
    });
  }
  function buildOutputRow(o, idx) {
    const tr = document.createElement("tr");
    tr.dataset.rowIndex = String(idx);
    tr.dataset.kind = "output";
    if (o.enabled === false) tr.classList.add("row-disabled");

    // On toggle
    const onTd = document.createElement("td");
    onTd.className = "col-active";
    const toggleLabel = document.createElement("label");
    toggleLabel.className = "toggle";
    const toggleInput = document.createElement("input");
    toggleInput.type = "checkbox";
    toggleInput.checked = o.enabled !== false;
    toggleInput.addEventListener("change", () => {
      state.draft.outputs[idx].enabled = toggleInput.checked;
      tr.classList.toggle("row-disabled", !toggleInput.checked);
      markDirty();
    });
    const toggleTrack = document.createElement("span");
    toggleTrack.className = "track";
    toggleLabel.append(toggleInput, toggleTrack);
    onTd.appendChild(toggleLabel);
    tr.appendChild(onTd);

    // IP input
    tr.appendChild(buildCellInput("col-ip", "text", o.ip, "192.168.1.100", (v) => {
      state.draft.outputs[idx].ip = v.trim();
      markDirty();
    }));

    // Label input
    tr.appendChild(buildCellInput("col-label", "text", o.label || "", "Controller", (v) => {
      state.draft.outputs[idx].label = v;
      markDirty();
    }));

    // Universe Start input. Universes are stored as an explicit list; the UI
    // exposes Start + End, which we expand to a contiguous range on edit.
    const univs = Array.isArray(o.universes) ? o.universes : [];
    const start = univs.length ? Math.min(...univs) : 0;
    const end = univs.length ? Math.max(...univs) : 0;
    tr.appendChild(buildCellInput("col-univ-start", "number", start, "1", (v) => {
      updateOutputUniverses(idx, parseInt(v, 10), null);
      markDirty();
    }, { min: 0, max: 32767 }));
    tr.appendChild(buildCellInput("col-univ-end", "number", end, "1", (v) => {
      updateOutputUniverses(idx, null, parseInt(v, 10));
      markDirty();
    }, { min: 0, max: 32767 }));

    // Broadcast checkbox
    const bcTd = document.createElement("td");
    bcTd.className = "col-broadcast";
    const bcLabel = document.createElement("label");
    const bcInput = document.createElement("input");
    bcInput.type = "checkbox";
    bcInput.checked = !!o.broadcast;
    bcInput.addEventListener("change", () => {
      state.draft.outputs[idx].broadcast = bcInput.checked;
      markDirty();
    });
    bcLabel.append(bcInput, document.createTextNode(" Broadcast"));
    bcTd.appendChild(bcLabel);
    tr.appendChild(bcTd);

    // Packets (snapshot-driven)
    const pktTd = document.createElement("td");
    pktTd.className = "col-packets";
    pktTd.dataset.cell = "packets";
    pktTd.textContent = "—";
    tr.appendChild(pktTd);

    // Actions: Delete
    const actionsTd = document.createElement("td");
    actionsTd.className = "col-actions row-actions";
    const delBtn = document.createElement("button");
    delBtn.className = "subtle";
    delBtn.textContent = "Remove";
    delBtn.addEventListener("click", () => {
      const ip = state.draft.outputs[idx].ip || "(new)";
      if (!confirm(`Remove output ${ip}?`)) return;
      state.draft.outputs.splice(idx, 1);
      renderOutputs();
      markDirty();
    });
    actionsTd.appendChild(delBtn);
    tr.appendChild(actionsTd);

    return tr;
  }

  // Recompute the universes list for an output when Start or End changes.
  // Passing null for either side preserves the current min/max.
  function updateOutputUniverses(idx, newStart, newEnd) {
    const o = state.draft.outputs[idx];
    const cur = Array.isArray(o.universes) ? o.universes : [];
    const curStart = cur.length ? Math.min(...cur) : 0;
    const curEnd = cur.length ? Math.max(...cur) : 0;
    const s = newStart === null ? curStart : Math.max(0, Math.min(32767, newStart || 0));
    const e = newEnd === null ? curEnd : Math.max(0, Math.min(32767, newEnd || 0));
    if (e < s) {
      // Don't refuse the typing — just let the list stay empty until the
      // operator fixes End. UI shows the bad values but no universes get
      // sent until valid.
      o.universes = [];
      return;
    }
    const range = [];
    for (let u = s; u <= e; u++) range.push(u);
    o.universes = range;
  }

  // Helper: an editable <td> with a single <input> inside, bound via onInput.
  function buildCellInput(colClass, type, value, placeholder, onChange, extra = {}) {
    const td = document.createElement("td");
    td.className = colClass;
    const inp = document.createElement("input");
    inp.type = type;
    inp.value = value ?? "";
    if (placeholder) inp.placeholder = placeholder;
    if (extra.min !== undefined) inp.min = String(extra.min);
    if (extra.max !== undefined) inp.max = String(extra.max);
    inp.addEventListener("input", () => onChange(inp.value));
    td.appendChild(inp);
    return td;
  }

  // ---- Snapshot overlay: update read-only cells without tearing down rows ----
  function renderSnapshotOverlay() {
    const snap = state.snapshot;
    if (!snap) return;
    document.getElementById("stat-malformed").textContent = snap.malformed_count;
    document.getElementById("stat-artpoll").textContent = snap.artpoll_count;

    // Sources: match draft rows to snap entries by IP — for new/edited rows
    // the IP may not match anything, so just leave their stats blank.
    const sourceSnapByIp = new Map();
    for (const s of (snap.sources || [])) sourceSnapByIp.set(s.ip, s);
    document.querySelectorAll('#sources-table tbody tr').forEach(tr => {
      const idx = parseInt(tr.dataset.rowIndex, 10);
      const cfgIp = state.draft?.sources?.[idx]?.ip;
      const s = cfgIp ? sourceSnapByIp.get(cfgIp) : null;
      const dot = tr.querySelector('[data-cell="dot"]');
      const pkts = tr.querySelector('[data-cell="packets"]');
      const enabled = state.draft?.sources?.[idx]?.enabled !== false;
      if (dot) {
        dot.className = "dot";
        if (!enabled) {
          dot.title = "off";
        } else if (s) {
          if (s.alive) dot.classList.add("alive");
          if (s.active) dot.classList.add("active");
          dot.title = s.active ? "live" : (s.alive ? "blackout" : "silent");
        }
      }
      if (pkts) pkts.textContent = s ? String(s.packet_count) : "—";
    });

    // Outputs: same pattern.
    const outputSnapByIp = new Map();
    for (const o of (snap.outputs || [])) outputSnapByIp.set(o.ip, o);
    document.querySelectorAll('#outputs-table tbody tr').forEach(tr => {
      const idx = parseInt(tr.dataset.rowIndex, 10);
      const cfgIp = state.draft?.outputs?.[idx]?.ip;
      const o = cfgIp ? outputSnapByIp.get(cfgIp) : null;
      const pkts = tr.querySelector('[data-cell="packets"]');
      if (pkts) pkts.textContent = o ? String(o.packet_count) : "—";
    });

    renderUnknown(snap.unknown_sources || []);
    renderPreviewList();
  }

  function renderUnknown(unknown) {
    const section = document.getElementById("unknown-section");
    const tbody = section.querySelector("tbody");
    tbody.innerHTML = "";
    if (!unknown.length) {
      section.hidden = true;
      return;
    }
    section.hidden = false;
    for (const u of unknown) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><code>${escapeHtml(u.ip)}</code></td>
        <td>${u.packet_count}</td>
        <td class="muted">${formatAge(u.last_seen_age_s)}</td>
        <td></td>
      `;
      const btn = document.createElement("button");
      btn.textContent = "Add as source";
      btn.title = "Adds to the draft. Click Save changes above to commit.";
      btn.addEventListener("click", () => {
        const ip = u.ip;
        if (!state.draft.sources.some(x => x.ip === ip)) {
          state.draft.sources.push({ ip, label: "", mode: "htp", priority: 100, enabled: true });
          renderSources();
          markDirty();
          toast(`Added ${ip} to sources — click Save changes`, { kind: "warn" });
        }
      });
      tr.lastElementChild.appendChild(btn);
      tbody.appendChild(tr);
    }
  }

  // ---- Network section ----
  function renderNetwork() {
    if (!state.network) return;
    const mode = state.network.mode || "dhcp";
    document.getElementById("net-mode-dhcp").checked = mode === "dhcp";
    document.getElementById("net-mode-static").checked = mode === "static";
    document.getElementById("net-static-fields").hidden = mode !== "static";
    document.getElementById("net-ip").value = state.network.ip || "";
    document.getElementById("net-prefix").value = state.network.prefix || 24;
    document.getElementById("net-gw").value = state.network.gateway || "";
    document.getElementById("net-dns").value = state.network.dns || "";

    const hint = document.getElementById("net-current");
    if (!state.network.available) {
      hint.textContent = "(running off a system without nmcli — dev mode)";
    } else if (state.network.error) {
      hint.textContent = `(nmcli reported: ${state.network.error})`;
    } else {
      hint.textContent = "";
    }
  }
  function bindNetworkInputs() {
    for (const id of ["net-mode-dhcp", "net-mode-static"]) {
      const el = document.getElementById(id);
      el.addEventListener("change", () => {
        const isStatic = document.getElementById("net-mode-static").checked;
        document.getElementById("net-static-fields").hidden = !isStatic;
      });
    }
    document.getElementById("net-apply-btn").addEventListener("click", async () => {
      const isStatic = document.getElementById("net-mode-static").checked;
      const body = isStatic
        ? {
            mode: "static",
            ip: document.getElementById("net-ip").value.trim(),
            prefix: parseInt(document.getElementById("net-prefix").value, 10) || 24,
            gateway: document.getElementById("net-gw").value.trim(),
            dns: document.getElementById("net-dns").value.trim(),
          }
        : { mode: "dhcp" };
      try {
        await api("/api/network", { method: "POST", body: JSON.stringify(body) });
      } catch (e) {
        toast(`Network apply failed: ${e.message}`, { kind: "err", durationMs: 6000 });
        return;
      }
      toast("Network settings saved. Reboot the Pi to apply.", { durationMs: 5000 });
      document.getElementById("net-banner").hidden = false;
      if (isStatic && body.ip) {
        state.pendingNetworkTarget = { newIp: body.ip, port: window.location.port || "80" };
      } else {
        state.pendingNetworkTarget = null;
      }
    });
    document.getElementById("net-reboot-btn").addEventListener("click", async () => {
      if (!confirm("Reboot the Pi now? The UI will reconnect when it comes back.")) return;
      try {
        await api("/api/network/reboot", { method: "POST" });
      } catch (e) {
        // The server might close the socket before responding — that's OK.
      }
      toast("Rebooting… waiting for the Pi to come back.", { durationMs: 8000 });
      pollForReboot();
    });
  }
  async function pollForReboot() {
    // The Pi may come back on a new IP if the operator changed Static IP, or
    // the same IP otherwise. Poll BOTH the current URL and the pending new
    // IP (if any). First to respond wins.
    const target = state.pendingNetworkTarget;
    const newUrl = target
      ? `${window.location.protocol}//${target.newIp}${target.port && target.port !== "80" ? ":" + target.port : ""}/`
      : null;
    const start = Date.now();
    while (Date.now() - start < 180000) {  // 3 min budget
      await new Promise(r => setTimeout(r, 3000));
      // Same IP
      try {
        const r = await fetch("/api/state", { cache: "no-store" });
        if (r.ok) {
          if (newUrl && newUrl !== window.location.href) {
            window.location.href = newUrl;
          } else {
            window.location.reload();
          }
          return;
        }
      } catch (_e) {}
      // New IP (CORS may block this, so we use no-cors; we just need the
      // browser to know "something answered"). Best-effort.
      if (newUrl) {
        try {
          await fetch(newUrl, { mode: "no-cors", cache: "no-store" });
          // If the fetch resolved (didn't throw) the server is up. Redirect.
          window.location.href = newUrl;
          return;
        } catch (_e) {}
      }
    }
    toast("Pi didn't come back within 3 min. Check power + network + console.", { kind: "err", durationMs: 15000 });
  }

  // ---- Render everything ----
  function renderEverything() {
    fillSettingsInputs();
    renderSources();
    renderOutputs();
    renderPreviewList();
    updateDirtyPill();
  }

  // ---- Save / Discard ----
  async function saveAll() {
    if (!state.draft) return;
    const btn = document.getElementById("save-all-btn");
    btn.disabled = true;
    btn.textContent = "Saving…";
    try {
      await api("/api/config", { method: "PUT", body: JSON.stringify(state.draft) });
      state.config = cloneCfg(state.draft);
      toast("Saved");
      updateDirtyPill();
    } catch (e) {
      toast(`Save failed: ${e.message}`, { kind: "err", durationMs: 6000 });
    } finally {
      btn.disabled = false;
      btn.textContent = "Save changes";
    }
  }
  function discardAll() {
    if (dirtyKeys().length === 0) return;
    if (!confirm("Discard all unsaved changes?")) return;
    state.draft = cloneCfg(state.config);
    renderEverything();
    toast("Reverted");
  }

  // ---- Misc helpers ----
  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }
  function formatAge(s) {
    if (s == null) return "—";
    if (s < 60) return `${s.toFixed(1)}s ago`;
    if (s < 3600) return `${(s / 60).toFixed(1)}m ago`;
    return `${(s / 3600).toFixed(1)}h ago`;
  }

  // ---- Config export / import ----
  function bindConfigIO() {
    const exportBtn = document.getElementById("export-config");
    if (exportBtn) exportBtn.onclick = () => { window.location.href = "/api/config/export"; };

    const fileInput = document.getElementById("import-config-file");
    const importBtn = document.getElementById("import-config-btn");
    if (importBtn && fileInput) {
      const click = () => fileInput.click();
      importBtn.onclick = click;
      importBtn.onkeydown = (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); click(); }
      };
      fileInput.onchange = async () => {
        const f = fileInput.files && fileInput.files[0];
        if (!f) return;
        if (dirtyKeys().length > 0 &&
            !confirm("You have unsaved changes. Importing will overwrite them. Continue?")) {
          fileInput.value = ""; return;
        }
        const text = await f.text();
        try {
          const res = await fetch("/api/config/import", {
            method: "POST", headers: { "Content-Type": "application/x-yaml" }, body: text,
          });
          if (!res.ok) {
            const err = await res.text();
            toast(`Import failed: ${err}`, { kind: "err", durationMs: 8000 });
          } else {
            await loadConfig();
            await loadVersion();
            toast(`Imported ${f.name}`);
          }
        } catch (e) {
          toast(`Import failed: ${e.message}`, { kind: "err", durationMs: 8000 });
        }
        fileInput.value = "";
      };
    }
  }

  // ---- Boot wiring ----
  function bindGlobalButtons() {
    document.getElementById("save-all-btn").addEventListener("click", saveAll);
    document.getElementById("discard-btn").addEventListener("click", discardAll);

    document.getElementById("add-source-btn").addEventListener("click", () => {
      state.draft.sources.push({ ip: "", label: "", mode: "htp", priority: 100, enabled: true });
      renderSources();
      markDirty();
      // Focus the IP input of the row we just appended.
      const rows = document.querySelectorAll("#sources-table tbody tr");
      const last = rows[rows.length - 1];
      last?.querySelector(".col-ip input")?.focus();
    });

    document.getElementById("add-output-btn").addEventListener("click", () => {
      state.draft.outputs.push({
        ip: "", label: "", broadcast: false, port: 6454,
        enabled: true, universes: [],
      });
      renderOutputs();
      markDirty();
      const rows = document.querySelectorAll("#outputs-table tbody tr");
      const last = rows[rows.length - 1];
      last?.querySelector(".col-ip input")?.focus();
    });

    document.getElementById("restart-service").addEventListener("click", async () => {
      if (!confirm("Restart the merger? The UI will reconnect in a few seconds.")) return;
      try { await api("/api/restart", { method: "POST" }); } catch (_e) {}
      const start = Date.now();
      while (Date.now() - start < 30000) {
        await new Promise(r => setTimeout(r, 1500));
        try {
          const r = await fetch("/api/state");
          if (r.ok) { window.location.reload(); return; }
        } catch (_e) {}
      }
      toast("Service didn't come back within 30s. Check journalctl.", { kind: "err", durationMs: 10000 });
    });

    document.getElementById("check-updates-btn").addEventListener("click", async () => {
      const btn = document.getElementById("check-updates-btn");
      const badge = document.getElementById("update-badge");
      const installBtn = document.getElementById("install-update-btn");
      const orig = btn.textContent;
      btn.textContent = "Checking…";
      btn.disabled = true;
      badge.hidden = true;
      installBtn.hidden = true;
      try {
        const j = await api("/api/update/status");
        if (j.update_available) {
          badge.textContent = `${j.latest} available`;
          badge.href = j.release_url || "https://github.com/djkoren/artnet-htp/releases";
          badge.hidden = false;
          if (j.can_install) {
            installBtn.textContent = `Install ${j.latest}`;
            installBtn.dataset.tag = j.latest;
            installBtn.hidden = false;
          }
          btn.textContent = "Check complete";
        } else if (j.error) {
          btn.textContent = "Can't reach GitHub";
          btn.title = j.error;
        } else {
          btn.textContent = "On latest";
        }
      } catch (e) {
        btn.textContent = "Check failed";
        btn.title = String(e);
      } finally {
        btn.disabled = false;
        setTimeout(() => { btn.textContent = orig; btn.title = "Check GitHub for a newer release"; }, 6000);
      }
    });

    document.getElementById("install-update-btn").addEventListener("click", async () => {
      const btn = document.getElementById("install-update-btn");
      const tag = btn.dataset.tag || "latest";
      if (!confirm(`Install ${tag}? Pi will download + install + restart. UI reconnects in ~30s.`)) return;
      btn.disabled = true;
      btn.textContent = "Installing…";
      toast(`Installing ${tag}…`, { durationMs: 8000 });
      try {
        await api("/api/update/install", { method: "POST" });
      } catch (e) {
        toast(`Install failed: ${e.message}`, { kind: "err", durationMs: 10000 });
        btn.disabled = false;
        btn.textContent = `Install ${tag}`;
        return;
      }
      const start = Date.now();
      while (Date.now() - start < 90000) {
        await new Promise(r => setTimeout(r, 2000));
        try {
          const r = await fetch("/api/state");
          if (r.ok) { window.location.reload(); return; }
        } catch (_e) {}
      }
      toast("Service didn't come back within 90s.", { kind: "err", durationMs: 15000 });
      btn.disabled = false;
      btn.textContent = `Install ${tag}`;
    });
  }

  async function boot() {
    bindGlobalButtons();
    bindSettingsInputs();
    bindNetworkInputs();
    bindConfigIO();
    await loadVersion();
    setInterval(loadVersion, 30_000);
    try {
      await loadConfig();
      await loadNetwork();
    } catch (e) {
      console.error("boot failed:", e);
      toast("Failed to load config — check console.", { kind: "err", durationMs: 8000 });
    }
    connectWS();
  }
  boot();
})();
