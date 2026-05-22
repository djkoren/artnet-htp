"use strict";

(() => {
  const DMX_HEADER_LEN = 3;
  const DMX_CHANNELS = 512;

  const state = {
    ws: null,
    config: null,
    snapshot: null,
    canvases: new Map(),       // port_address -> {canvas, ctx}
    watchedUniverses: new Set(),
    editing: { source: null, output: null }, // ip currently being edited
    version: null,             // {version, git_sha, built_at, image, last_firstboot_error}
  };

  // ---- Toast notifications ----
  // Stack of small notifications in the bottom-right corner. Auto-dismiss
  // after `durationMs`. `kind` is "ok" | "warn" | "err" — drives the
  // background color via CSS class.
  function toast(message, { kind = "ok", durationMs = 2500 } = {}) {
    const stack = document.getElementById("toast-stack");
    if (!stack) return; // before-DOM-ready safety
    const el = document.createElement("div");
    el.className = `toast toast-${kind}`;
    el.textContent = message;
    stack.appendChild(el);
    // Trigger CSS transition (next frame).
    requestAnimationFrame(() => el.classList.add("show"));
    setTimeout(() => {
      el.classList.remove("show");
      setTimeout(() => el.remove(), 300);
    }, durationMs);
  }

  // ---- Helpers ----
  // Parse a universe spec like "1-9", "0", "1,3,5-10" into a sorted, deduped
  // array of port_address ints. Throws Error with a human-readable message on
  // malformed input. Caps individual range size at 4096 so a typo like "1-999999"
  // can't lock the browser.
  function parseUniverseSpec(spec) {
    const out = new Set();
    const parts = spec.split(",").map((s) => s.trim()).filter(Boolean);
    if (parts.length === 0) throw new Error("empty");
    for (const part of parts) {
      const rangeMatch = part.match(/^(\d+)\s*-\s*(\d+)$/);
      if (rangeMatch) {
        const a = parseInt(rangeMatch[1], 10);
        const b = parseInt(rangeMatch[2], 10);
        if (a > b) throw new Error(`bad range "${part}" — start > end`);
        if (b - a + 1 > 4096) throw new Error(`range "${part}" too large (max 4096)`);
        for (let i = a; i <= b; i++) {
          if (i > 32767) throw new Error(`universe ${i} out of range (max 32767)`);
          out.add(i);
        }
      } else if (/^\d+$/.test(part)) {
        const n = parseInt(part, 10);
        if (n > 32767) throw new Error(`universe ${n} out of range (max 32767)`);
        out.add(n);
      } else {
        throw new Error(`"${part}" isn't a number or a range like 1-9`);
      }
    }
    return Array.from(out).sort((x, y) => x - y);
  }
  // Exposed for ad-hoc browser-console testing; harmless otherwise.
  window.__parseUniverseSpec = parseUniverseSpec;

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
    renderConfig();
  }
  async function putConfig(cfg) {
    await api("/api/config", { method: "PUT", body: JSON.stringify(cfg) });
    await loadConfig();
  }
  async function loadVersion() {
    try {
      state.version = await api("/api/version");
    } catch (e) {
      state.version = { version: "?", git_sha: null };
    }
    renderVersion();
  }

  function renderVersion() {
    const el = document.getElementById("version-badge");
    if (!el) return;
    const v = state.version || {};
    const ver = v.version || "?";
    // Strip any leading "v"/"V" so we don't end up rendering "vv0.2.6" when the
    // baked version string is itself "v0.2.6" (tags are "v"-prefixed, but the
    // python package version is bare). Always render exactly one "v".
    const cleanVer = ver.replace(/^v+/i, "");
    el.textContent = `v${cleanVer}`;
    el.classList.toggle("dev", cleanVer.includes("dev") || cleanVer === "?");
    const titleBits = [`version ${cleanVer}`];
    if (v.git_sha) titleBits.push(`git ${String(v.git_sha).slice(0, 7)}`);
    if (v.built_at) titleBits.push(`built ${v.built_at}`);
    if (v.image) titleBits.push(`image ${v.image}`);
    el.title = titleBits.join(" • ");

    // First-boot error surfacing
    const errBox = document.getElementById("firstboot-error");
    const errText = document.getElementById("firstboot-error-text");
    if (errBox && errText) {
      if (v.last_firstboot_error) {
        errText.textContent = v.last_firstboot_error;
        errBox.hidden = false;
      } else {
        errBox.hidden = true;
      }
    }
  }

  // ---- WebSocket ----
  function connectWS() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/state`);
    ws.binaryType = "arraybuffer";
    state.ws = ws;
    setConnectionPill(false);

    ws.onopen = () => {
      setConnectionPill(true);
      for (const u of state.watchedUniverses) {
        ws.send(JSON.stringify({ type: "watch", universe: u }));
      }
    };
    ws.onclose = () => {
      setConnectionPill(false);
      setTimeout(connectWS, 1000);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        const msg = JSON.parse(ev.data);
        if (msg.type === "status") {
          state.snapshot = msg.data;
          renderSnapshot();
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

  // ---- Render: config ----
  function renderConfig() {
    const cfg = state.config;
    if (!cfg) return;
    document.getElementById("cfg-bind-ip").value = cfg.bind_ip;
    document.getElementById("cfg-send-rate").value = cfg.send_rate_hz;
    document.getElementById("cfg-source-timeout").value = cfg.source_timeout_s;
    document.getElementById("cfg-keepalive").checked = cfg.send_keepalive_when_silent;
    document.getElementById("cfg-auto-allow").checked = cfg.auto_allow_unknown_sources;
    renderUniverses();
    renderPreviewList();
    // Source/output tables now key off state.config (persisted, includes
    // disabled rows) joined with state.snapshot (runtime data). After config
    // loads we need to repaint them — otherwise a row you just disabled or
    // added wouldn't reflect until the next WS tick.
    if (state.snapshot) renderSnapshot();
  }

  function renderUniverses() {
    const container = document.getElementById("universe-chips");
    container.innerHTML = "";
    if (!state.config) return;
    if (state.config.universes.length === 0) {
      container.innerHTML = '<span class="muted">No universes configured yet.</span>';
      return;
    }
    for (const u of state.config.universes) {
      const chip = document.createElement("span");
      chip.className = "chip";
      const net = (u >> 8) & 0x7F;
      const sub = (u >> 4) & 0x0F;
      const uni = u & 0x0F;
      chip.title = `Net ${net} / Sub ${sub} / Universe ${uni}`;
      chip.innerHTML = `<span>Universe <b>${u}</b></span><span class="breakdown">${net}/${sub}/${uni}</span>`;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "×";
      btn.title = "Remove universe";
      btn.onclick = async () => {
        await api(`/api/universes/${u}`, { method: "DELETE" });
        await loadConfig();
      };
      chip.appendChild(btn);
      container.appendChild(chip);
    }
  }

  function renderPreviewList() {
    const container = document.getElementById("preview-list");
    clearCanvases();
    container.innerHTML = "";
    if (!state.config) return;
    if (state.config.universes.length === 0) {
      container.innerHTML = '<span class="muted">Add a universe above to see live DMX preview.</span>';
      return;
    }
    for (const u of state.config.universes) {
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

  // ---- Render: live snapshot ----
  function renderSnapshot() {
    const snap = state.snapshot;
    if (!snap) return;
    document.getElementById("stat-malformed").textContent = snap.malformed_count;
    document.getElementById("stat-artpoll").textContent = snap.artpoll_count;
    renderSources(snap.sources);
    renderOutputs(snap.outputs);
    renderUnknown(snap.unknown_sources);
  }

  function renderSources(snapSources) {
    const tbody = document.querySelector("#sources-table tbody");
    tbody.innerHTML = "";

    // We iterate the PERSISTED source list (state.config.sources) so disabled
    // rows still show up — and join in runtime data from the snapshot by IP.
    // Disabled sources are filtered out of the runtime in to_source_specs(),
    // so they don't appear in snapSources at all.
    const cfgSources = (state.config?.sources || []).slice();
    const snapByIp = new Map();
    for (const s of snapSources) snapByIp.set(s.ip, s);

    // Priority first, then by priority DESC, then by IP. Disabled rows sink
    // to the bottom so the operator's attention lands on live sources first.
    cfgSources.sort((a, b) => {
      const ea = a.enabled !== false, eb = b.enabled !== false;
      if (ea !== eb) return ea ? -1 : 1;
      const ma = a.mode === "priority" ? 0 : 1;
      const mb = b.mode === "priority" ? 0 : 1;
      if (ma !== mb) return ma - mb;
      if ((a.priority ?? 100) !== (b.priority ?? 100))
        return (b.priority ?? 100) - (a.priority ?? 100);
      return a.ip.localeCompare(b.ip);
    });

    if (state.editing.source === "__new__") {
      const tr = document.createElement("tr");
      renderSourceEditing(tr, { ip: "", label: "", mode: "htp", priority: 100, enabled: true, _isNew: true });
      tbody.appendChild(tr);
    }
    for (const cfgEntry of cfgSources) {
      const snapEntry = snapByIp.get(cfgEntry.ip);
      // Build a unified row object: runtime data when enabled, stub data when
      // disabled (so the renderer doesn't need to special-case).
      const row = snapEntry || {
        ip: cfgEntry.ip,
        label: cfgEntry.label || cfgEntry.ip,
        mode: cfgEntry.mode || "htp",
        priority: cfgEntry.priority ?? 100,
        alive: false,
        active: false,
        packet_count: 0,
        universes: [],
      };
      const tr = document.createElement("tr");
      tr.dataset.ip = cfgEntry.ip;
      if (state.editing.source === cfgEntry.ip) {
        renderSourceEditing(tr, row);
      } else {
        renderSourceRow(tr, row);
      }
      tbody.appendChild(tr);
    }
  }

  // Toggle enabled flag on a source/output IP via a config PUT.
  async function setSourceEnabled(ip, enabled) {
    const cfg = structuredClone(state.config);
    const s = cfg.sources.find(x => x.ip === ip);
    if (!s) return;
    s.enabled = enabled;
    try {
      await putConfig(cfg);
      toast(`Source ${ip} ${enabled ? "enabled" : "disabled"}`);
    } catch (e) {
      toast(`Failed: ${e.message}`, { kind: "err" });
    }
  }
  async function setOutputEnabled(ip, enabled) {
    const cfg = structuredClone(state.config);
    const o = cfg.outputs.find(x => x.ip === ip);
    if (!o) return;
    o.enabled = enabled;
    try {
      await putConfig(cfg);
      toast(`Output ${ip} ${enabled ? "enabled" : "disabled"}`);
    } catch (e) {
      toast(`Failed: ${e.message}`, { kind: "err" });
    }
  }

  function renderSourceRow(tr, s) {
    // Look up the persisted `enabled` flag from state.config (snapshot doesn't
    // carry it). Default true so legacy configs without the field stay on.
    const cfgEntry = state.config?.sources?.find(x => x.ip === s.ip);
    const enabled = cfgEntry ? cfgEntry.enabled !== false : true;
    if (!enabled) tr.classList.add("row-disabled");

    const universesDisplay = s.universes
      .map(u => `${u.port_address}${u.active ? "" : (u.alive ? "·" : "*")}`)
      .join(", ");
    const dotClass = `dot${s.alive ? " alive" : ""}${s.active ? " active" : ""}`;
    let badge = '';
    if (!enabled) badge = '<span class="badge dead">off</span>';
    else if (!s.alive) badge = '<span class="badge dead">silent</span>';
    else if (!s.active) badge = '<span class="badge idle">blackout</span>';
    else badge = '<span class="badge live">live</span>';
    const mode = s.mode || "htp";
    const modeBadge = mode === "priority"
      ? '<span class="mode-badge prio">PRIORITY</span>'
      : '<span class="mode-badge htp">HTP</span>';
    const prioCell = mode === "priority"
      ? `<b>${s.priority ?? 100}</b>`
      : `<span class="muted">${s.priority ?? 100}</span>`;

    tr.innerHTML = `
      <td class="col-active"><label class="toggle"><input type="checkbox" ${enabled ? "checked" : ""}><span class="track"></span></label></td>
      <td class="col-status"><span class="${dotClass}"></span></td>
      <td class="col-ip"><code>${escapeHtml(s.ip)}</code> ${badge}</td>
      <td class="col-label">${escapeHtml(s.label)}</td>
      <td class="col-mode">${modeBadge}</td>
      <td class="col-prio">${prioCell}</td>
      <td class="col-packets">${s.packet_count}</td>
      <td class="col-universes muted">${universesDisplay || "—"}</td>
      <td class="col-actions row-actions"></td>
    `;
    tr.querySelector('input[type="checkbox"]').onchange = (e) => setSourceEnabled(s.ip, e.target.checked);
    const actions = tr.lastElementChild;
    const editBtn = document.createElement("button");
    editBtn.className = "subtle";
    editBtn.textContent = "Edit";
    editBtn.onclick = () => { state.editing.source = s.ip; renderSnapshot(); };
    const delBtn = document.createElement("button");
    delBtn.className = "subtle";
    delBtn.textContent = "Remove";
    delBtn.onclick = async () => {
      if (!confirm(`Remove source ${s.ip}?`)) return;
      await api(`/api/sources/${s.ip}`, { method: "DELETE" });
      await loadConfig();
    };
    actions.appendChild(editBtn);
    actions.appendChild(delBtn);
  }

  function renderSourceEditing(tr, s) {
    tr.classList.add("editing");
    const mode = s.mode || "htp";
    const isNew = s._isNew === true;
    tr.innerHTML = `
      <td class="col-active"></td>
      <td class="col-status"></td>
      <td class="col-ip"><input type="text" data-field="ip" value="${escapeAttr(s.ip)}" placeholder="192.168.1.10"></td>
      <td class="col-label"><input type="text" data-field="label" value="${escapeAttr(s.label)}" placeholder="Console"></td>
      <td class="col-mode">
        <select data-field="mode" class="mode-select">
          <option value="htp" ${mode === "htp" ? "selected" : ""}>HTP</option>
          <option value="priority" ${mode === "priority" ? "selected" : ""}>Priority</option>
        </select>
      </td>
      <td class="col-prio"><input type="number" min="0" max="999" data-field="priority" value="${s.priority ?? 100}" class="prio-input-edit"></td>
      <td class="col-packets"></td>
      <td class="col-universes"></td>
      <td class="col-actions row-actions"></td>
    `;
    const ipInput = tr.querySelector('[data-field="ip"]');
    if (isNew) ipInput.focus();
    const actions = tr.lastElementChild;
    const saveBtn = document.createElement("button");
    saveBtn.textContent = "Save";
    saveBtn.onclick = async () => {
      const newIp = ipInput.value.trim();
      if (!newIp) { toast("IP is required", { kind: "err" }); ipInput.focus(); return; }
      const newLabel = tr.querySelector('[data-field="label"]').value;
      const newMode = tr.querySelector('[data-field="mode"]').value;
      const newPri = parseInt(tr.querySelector('[data-field="priority"]').value, 10) || 100;
      const cfg = structuredClone(state.config);
      if (isNew) {
        if (cfg.sources.some(x => x.ip === newIp)) {
          toast(`Source ${newIp} already exists`, { kind: "err" });
          return;
        }
        cfg.sources.push({ ip: newIp, label: newLabel, mode: newMode, priority: newPri, enabled: true });
      } else {
        const idx = cfg.sources.findIndex(x => x.ip === s.ip);
        if (idx === -1) return;
        if (newIp !== s.ip && cfg.sources.some(x => x.ip === newIp)) {
          toast(`Source ${newIp} already exists`, { kind: "err" });
          return;
        }
        // Preserve enabled flag through edit.
        cfg.sources[idx] = { ip: newIp, label: newLabel, mode: newMode, priority: newPri, enabled: cfg.sources[idx].enabled !== false };
      }
      state.editing.source = null;
      try {
        await putConfig(cfg);
        toast(isNew ? `Source ${newIp} added` : "Source saved");
      } catch (e) {
        toast(`Save failed: ${e.message}`, { kind: "err" });
        state.editing.source = isNew ? "__new__" : s.ip;
        renderSnapshot();
      }
    };
    const cancelBtn = document.createElement("button");
    cancelBtn.className = "subtle";
    cancelBtn.textContent = "Cancel";
    cancelBtn.onclick = () => { state.editing.source = null; renderSnapshot(); };
    actions.appendChild(saveBtn);
    actions.appendChild(cancelBtn);
  }

  function renderOutputs(snapOutputs) {
    const tbody = document.querySelector("#outputs-table tbody");
    tbody.innerHTML = "";

    // Same pattern as renderSources: iterate persisted config so disabled rows
    // remain visible, join in runtime data from the snapshot.
    const cfgOutputs = (state.config?.outputs || []).slice();
    const snapByIp = new Map();
    for (const o of snapOutputs) snapByIp.set(o.ip, o);

    cfgOutputs.sort((a, b) => {
      const ea = a.enabled !== false, eb = b.enabled !== false;
      if (ea !== eb) return ea ? -1 : 1;
      return a.ip.localeCompare(b.ip);
    });

    if (state.editing.output === "__new__") {
      const tr = document.createElement("tr");
      renderOutputEditing(tr, { ip: "", label: "", broadcast: false, port: 6454, enabled: true, _isNew: true });
      tbody.appendChild(tr);
    }
    for (const cfgEntry of cfgOutputs) {
      const snapEntry = snapByIp.get(cfgEntry.ip);
      const row = snapEntry || {
        ip: cfgEntry.ip,
        label: cfgEntry.label || cfgEntry.ip,
        broadcast: !!cfgEntry.broadcast,
        port: cfgEntry.port ?? 6454,
        packet_count: 0,
      };
      const tr = document.createElement("tr");
      tr.dataset.ip = cfgEntry.ip;
      if (state.editing.output === cfgEntry.ip) {
        renderOutputEditing(tr, row);
      } else {
        renderOutputRow(tr, row);
      }
      tbody.appendChild(tr);
    }
  }

  function renderOutputRow(tr, o) {
    const cfgEntry = state.config?.outputs?.find(x => x.ip === o.ip);
    const enabled = cfgEntry ? cfgEntry.enabled !== false : true;
    if (!enabled) tr.classList.add("row-disabled");

    tr.innerHTML = `
      <td class="col-active"><label class="toggle"><input type="checkbox" ${enabled ? "checked" : ""}><span class="track"></span></label></td>
      <td class="col-ip"><code>${escapeHtml(o.ip)}</code></td>
      <td class="col-label">${escapeHtml(o.label)}</td>
      <td class="col-broadcast">${o.broadcast ? "✓" : ""}</td>
      <td class="col-packets">${o.packet_count}</td>
      <td class="col-actions row-actions"></td>
    `;
    tr.querySelector('input[type="checkbox"]').onchange = (e) => setOutputEnabled(o.ip, e.target.checked);
    const actions = tr.lastElementChild;
    const editBtn = document.createElement("button");
    editBtn.className = "subtle";
    editBtn.textContent = "Edit";
    editBtn.onclick = () => { state.editing.output = o.ip; renderSnapshot(); };
    const delBtn = document.createElement("button");
    delBtn.className = "subtle";
    delBtn.textContent = "Remove";
    delBtn.onclick = async () => {
      if (!confirm(`Remove output ${o.ip}?`)) return;
      await api(`/api/outputs/${o.ip}`, { method: "DELETE" });
      await loadConfig();
    };
    actions.appendChild(editBtn);
    actions.appendChild(delBtn);
  }

  function renderOutputEditing(tr, o) {
    tr.classList.add("editing");
    const isNew = o._isNew === true;
    // Port stays in the schema (6454 by default) but isn't surfaced — ArtNet
    // is always UDP 6454 in the field; exposing the input was just visual noise.
    tr.innerHTML = `
      <td class="col-active"></td>
      <td class="col-ip"><input type="text" data-field="ip" value="${escapeAttr(o.ip)}" placeholder="192.168.1.100"></td>
      <td class="col-label"><input type="text" data-field="label" value="${escapeAttr(o.label)}" placeholder="Controller"></td>
      <td class="col-broadcast"><label><input type="checkbox" data-field="broadcast" ${o.broadcast ? "checked" : ""}> Broadcast</label></td>
      <td class="col-packets"></td>
      <td class="col-actions row-actions"></td>
    `;
    const ipInput = tr.querySelector('[data-field="ip"]');
    if (isNew) ipInput.focus();
    const actions = tr.lastElementChild;
    const saveBtn = document.createElement("button");
    saveBtn.textContent = "Save";
    saveBtn.onclick = async () => {
      const newIp = ipInput.value.trim();
      if (!newIp) { toast("IP is required", { kind: "err" }); ipInput.focus(); return; }
      const newLabel = tr.querySelector('[data-field="label"]').value;
      const newBroadcast = tr.querySelector('[data-field="broadcast"]').checked;
      const cfg = structuredClone(state.config);
      if (isNew) {
        if (cfg.outputs.some(x => x.ip === newIp)) {
          toast(`Output ${newIp} already exists`, { kind: "err" });
          return;
        }
        cfg.outputs.push({ ip: newIp, port: 6454, label: newLabel, broadcast: newBroadcast, enabled: true });
      } else {
        const idx = cfg.outputs.findIndex(x => x.ip === o.ip);
        if (idx === -1) return;
        if (newIp !== o.ip && cfg.outputs.some(x => x.ip === newIp)) {
          toast(`Output ${newIp} already exists`, { kind: "err" });
          return;
        }
        cfg.outputs[idx] = {
          ip: newIp,
          port: cfg.outputs[idx].port ?? 6454,
          label: newLabel,
          broadcast: newBroadcast,
          enabled: cfg.outputs[idx].enabled !== false,
        };
      }
      state.editing.output = null;
      try {
        await putConfig(cfg);
        toast(isNew ? `Output ${newIp} added` : "Output saved");
      } catch (e) {
        toast(`Save failed: ${e.message}`, { kind: "err" });
        state.editing.output = isNew ? "__new__" : o.ip;
        renderSnapshot();
      }
    };
    const cancelBtn = document.createElement("button");
    cancelBtn.className = "subtle";
    cancelBtn.textContent = "Cancel";
    cancelBtn.onclick = () => { state.editing.output = null; renderSnapshot(); };
    actions.appendChild(saveBtn);
    actions.appendChild(cancelBtn);
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
      btn.textContent = "Allow";
      btn.onclick = async () => {
        await api("/api/sources/allow", {
          method: "POST",
          body: JSON.stringify({ ip: u.ip, label: "" }),
        });
        await loadConfig();
      };
      tr.lastElementChild.appendChild(btn);
      tbody.appendChild(tr);
    }
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }
  function escapeAttr(s) {
    return String(s || "").replace(/"/g, "&quot;");
  }
  function formatAge(s) {
    if (s == null) return "—";
    if (s < 60) return `${s.toFixed(1)}s ago`;
    if (s < 3600) return `${(s / 60).toFixed(1)}m ago`;
    return `${(s / 3600).toFixed(1)}h ago`;
  }

  // ---- Config Export / Import ----
  function bindConfigIO() {
    const exportBtn = document.getElementById("export-config");
    if (exportBtn) {
      exportBtn.onclick = () => {
        // Direct download — server sets Content-Disposition so the browser
        // saves it instead of rendering it as text.
        window.location.href = "/api/config/export";
      };
    }

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
        const text = await f.text();
        try {
          const res = await fetch("/api/config/import", {
            method: "POST",
            headers: { "Content-Type": "application/x-yaml" },
            body: text,
          });
          if (!res.ok) {
            const err = await res.text();
            alert(`Import failed (HTTP ${res.status}):\n${err}`);
          } else {
            await loadConfig();
            await loadVersion();
            alert(`Imported ${f.name}. New config is live.`);
          }
        } catch (e) {
          alert(`Import failed: ${e.message}`);
        }
        // Reset input so selecting the same file again still fires `change`.
        fileInput.value = "";
      };
    }
  }

  // ---- Forms ----
  function bindForms() {
    document.getElementById("save-settings").onclick = async () => {
      const cfg = structuredClone(state.config);
      // Advertised IP (lives under Advanced disclosure): blank = 0.0.0.0
      // = auto-detect. As of v0.2.5 this applies live — no restart needed.
      // The receiver always listens on all interfaces regardless of this
      // value. (See the Advanced section copy for the full story.)
      cfg.bind_ip = document.getElementById("cfg-bind-ip").value.trim() || "0.0.0.0";
      cfg.send_rate_hz = parseFloat(document.getElementById("cfg-send-rate").value);
      cfg.source_timeout_s = parseFloat(document.getElementById("cfg-source-timeout").value);
      cfg.send_keepalive_when_silent = document.getElementById("cfg-keepalive").checked;
      cfg.auto_allow_unknown_sources = document.getElementById("cfg-auto-allow").checked;
      try {
        await putConfig(cfg);
        toast("Settings saved");
      } catch (err) {
        toast("Save failed: " + err.message, { kind: "err", durationMs: 5000 });
      }
    };

    const doRestart = async () => {
      if (!confirm("Restart the merger? The UI will reconnect in a few seconds.")) return;
      try {
        await api("/api/restart", { method: "POST" });
      } catch (e) {
        // The server may close the socket before responding; that's normal.
      }
      // Poll /api/state until it answers again, then reload.
      const start = Date.now();
      while (Date.now() - start < 30000) {
        await new Promise((r) => setTimeout(r, 1500));
        try {
          const r = await fetch("/api/state");
          if (r.ok) { window.location.reload(); return; }
        } catch (_e) { /* still down */ }
      }
      alert("Service didn't come back within 30 seconds. SSH in and check `journalctl -u artnet-htp`.");
    };
    document.getElementById("restart-service").onclick = doRestart;

    document.getElementById("check-updates-btn").onclick = async () => {
      const btn = document.getElementById("check-updates-btn");
      const badge = document.getElementById("update-badge");
      const installBtn = document.getElementById("install-update-btn");
      const orig = btn.textContent;
      btn.textContent = "Checking…";
      btn.disabled = true;
      badge.hidden = true;
      installBtn.hidden = true;
      try {
        const r = await fetch("/api/update/status");
        if (!r.ok) throw new Error("status " + r.status);
        const j = await r.json();
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
    };

    document.getElementById("install-update-btn").onclick = async () => {
      const btn = document.getElementById("install-update-btn");
      const tag = btn.dataset.tag || "latest";
      if (!confirm(`Install ${tag}? The merger will download, install, and restart. UI reconnects in ~30s.`)) return;
      btn.disabled = true;
      const orig = btn.textContent;
      btn.textContent = "Installing…";
      toast(`Installing ${tag}… download + install + restart`, { durationMs: 8000 });
      try {
        await api("/api/update/install", { method: "POST" });
      } catch (e) {
        toast(`Install failed: ${e.message}`, { kind: "err", durationMs: 10000 });
        btn.disabled = false;
        btn.textContent = orig;
        return;
      }
      // The server is exiting; the next /api/state call will fail until
      // systemd brings us back. Poll until it answers, then reload.
      const start = Date.now();
      while (Date.now() - start < 90000) {
        await new Promise(r => setTimeout(r, 2000));
        try {
          const r = await fetch("/api/state");
          if (r.ok) { window.location.reload(); return; }
        } catch (_e) {}
      }
      toast("Service didn't come back within 90s. SSH in and check journalctl.", { kind: "err", durationMs: 15000 });
      btn.disabled = false;
      btn.textContent = orig;
    };

    // "Add source/output" buttons open a blank editing row inline at the top
    // of their respective tables. The Save handler in renderSourceEditing /
    // renderOutputEditing performs the actual POST, with validation +
    // toast feedback. Cancelling the row removes it.
    document.getElementById("add-source-btn").onclick = () => {
      if (state.editing.source) return; // already editing something
      state.editing.source = "__new__";
      renderSnapshot();
    };
    document.getElementById("add-output-btn").onclick = () => {
      if (state.editing.output) return;
      state.editing.output = "__new__";
      renderSnapshot();
    };

    // Live preview of which universes will be added as the operator types.
    const previewEl = document.getElementById("add-universe-preview");
    const startEl = document.querySelector('#add-universe-form [name="start"]');
    const countEl = document.querySelector('#add-universe-form [name="count"]');
    function updateUniversePreview() {
      const start = parseInt(startEl.value, 10);
      const count = parseInt(countEl.value, 10);
      if (Number.isNaN(start) || Number.isNaN(count) || count < 1) {
        previewEl.textContent = "";
        return;
      }
      const end = start + count - 1;
      if (end > 32767) {
        previewEl.textContent = `→ would exceed max universe 32767`;
        return;
      }
      previewEl.textContent = count === 1
        ? `→ universe ${start}`
        : `→ universes ${start}–${end}`;
    }
    startEl.addEventListener("input", updateUniversePreview);
    countEl.addEventListener("input", updateUniversePreview);
    updateUniversePreview();

    document.getElementById("add-universe-form").onsubmit = async (e) => {
      e.preventDefault();
      const start = parseInt(startEl.value, 10);
      const count = parseInt(countEl.value, 10);
      if (Number.isNaN(start) || start < 0 || start > 32767) {
        toast("Start must be 0–32767", { kind: "err" });
        return;
      }
      if (Number.isNaN(count) || count < 1 || count > 4096) {
        toast("Count must be 1–4096", { kind: "err" });
        return;
      }
      if (start + count - 1 > 32767) {
        toast(`Start ${start} + count ${count} exceeds max universe 32767`, { kind: "err", durationMs: 4000 });
        return;
      }
      const list = [];
      for (let i = 0; i < count; i++) list.push(start + i);
      let result;
      try {
        result = await api("/api/universes", {
          method: "POST",
          body: JSON.stringify({ port_addresses: list }),
        });
      } catch (err) {
        toast("Failed to add: " + err.message, { kind: "err", durationMs: 5000 });
        return;
      }
      const added = (result && result.added) || [];
      const skipped = (result && result.skipped) || [];
      if (added.length === 0 && skipped.length > 0) {
        toast(`Already had universe${skipped.length > 1 ? "s" : ""} ${skipped.join(", ")}`, { kind: "warn" });
      } else if (skipped.length > 0) {
        toast(`Added ${added.length}, skipped ${skipped.length} duplicate${skipped.length > 1 ? "s" : ""}`, { kind: "warn" });
      } else {
        toast(`Added ${added.length} universe${added.length > 1 ? "s" : ""}`);
      }
      // Advance Start to the next slot so consecutive clicks keep walking.
      startEl.value = String(start + count);
      countEl.value = "1";
      updateUniversePreview();
      await loadConfig();
    };
  }

  // ---- Boot ----
  async function boot() {
    bindForms();
    bindConfigIO();
    await loadVersion();
    // Refresh version periodically so a firstboot error that just appeared
    // (or got cleared) is reflected in the UI without a page reload.
    setInterval(loadVersion, 30_000);
    try {
      await loadConfig();
    } catch (e) {
      console.error("Failed to load config:", e);
    }
    connectWS();
  }
  boot();
})();
