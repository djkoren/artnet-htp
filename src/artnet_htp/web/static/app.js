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
    el.textContent = `v${ver}`;
    el.classList.toggle("dev", ver.includes("dev") || ver === "?");
    const titleBits = [`version ${ver}`];
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

  function renderSources(sources) {
    const tbody = document.querySelector("#sources-table tbody");
    tbody.innerHTML = "";
    // Priority sources first (visually), then by priority DESC, then by IP.
    sources.sort((a, b) => {
      const ma = a.mode === "priority" ? 0 : 1;
      const mb = b.mode === "priority" ? 0 : 1;
      if (ma !== mb) return ma - mb;
      if ((a.priority ?? 100) !== (b.priority ?? 100))
        return (b.priority ?? 100) - (a.priority ?? 100);
      return a.ip.localeCompare(b.ip);
    });
    for (const s of sources) {
      const tr = document.createElement("tr");
      tr.dataset.ip = s.ip;
      if (state.editing.source === s.ip) {
        renderSourceEditing(tr, s);
      } else {
        renderSourceRow(tr, s);
      }
      tbody.appendChild(tr);
    }
  }

  function renderSourceRow(tr, s) {
    const universesDisplay = s.universes
      .map(u => `${u.port_address}${u.active ? "" : (u.alive ? "·" : "*")}`)
      .join(", ");
    const dotClass = `dot${s.alive ? " alive" : ""}${s.active ? " active" : ""}`;
    let badge = '';
    if (!s.alive) badge = '<span class="badge dead">silent</span>';
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
      <td><span class="${dotClass}"></span></td>
      <td><code>${escapeHtml(s.ip)}</code> ${badge}</td>
      <td>${escapeHtml(s.label)}</td>
      <td>${modeBadge}</td>
      <td>${prioCell}</td>
      <td>${s.packet_count}</td>
      <td class="muted">${universesDisplay || "—"}</td>
      <td class="row-actions"></td>
    `;
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
    tr.innerHTML = `
      <td></td>
      <td><input type="text" data-field="ip" value="${escapeAttr(s.ip)}"></td>
      <td><input type="text" data-field="label" value="${escapeAttr(s.label)}"></td>
      <td>
        <select data-field="mode" class="mode-select">
          <option value="htp" ${mode === "htp" ? "selected" : ""}>HTP</option>
          <option value="priority" ${mode === "priority" ? "selected" : ""}>Priority</option>
        </select>
      </td>
      <td><input type="number" min="0" max="999" data-field="priority" value="${s.priority ?? 100}" class="prio-input-edit"></td>
      <td></td>
      <td></td>
      <td class="row-actions"></td>
    `;
    const actions = tr.lastElementChild;
    const saveBtn = document.createElement("button");
    saveBtn.textContent = "Save";
    saveBtn.onclick = async () => {
      const newIp = tr.querySelector('[data-field="ip"]').value.trim();
      const newLabel = tr.querySelector('[data-field="label"]').value;
      const newMode = tr.querySelector('[data-field="mode"]').value;
      const newPri = parseInt(tr.querySelector('[data-field="priority"]').value, 10) || 100;
      const cfg = structuredClone(state.config);
      const idx = cfg.sources.findIndex(x => x.ip === s.ip);
      if (idx === -1) return;
      if (newIp !== s.ip && cfg.sources.some(x => x.ip === newIp)) {
        alert(`Source ${newIp} already exists.`);
        return;
      }
      cfg.sources[idx] = { ip: newIp, label: newLabel, mode: newMode, priority: newPri };
      state.editing.source = null;
      try {
        await putConfig(cfg);
      } catch (e) {
        alert(`Save failed: ${e.message}`);
        state.editing.source = s.ip;
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

  function renderOutputs(outputs) {
    const tbody = document.querySelector("#outputs-table tbody");
    tbody.innerHTML = "";
    for (const o of outputs) {
      const tr = document.createElement("tr");
      tr.dataset.ip = o.ip;
      if (state.editing.output === o.ip) {
        renderOutputEditing(tr, o);
      } else {
        renderOutputRow(tr, o);
      }
      tbody.appendChild(tr);
    }
  }

  function renderOutputRow(tr, o) {
    tr.innerHTML = `
      <td><code>${escapeHtml(o.ip)}</code></td>
      <td><code>${o.port ?? 6454}</code></td>
      <td>${escapeHtml(o.label)}</td>
      <td>${o.broadcast ? "✓" : ""}</td>
      <td>${o.packet_count}</td>
      <td class="row-actions"></td>
    `;
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
    tr.innerHTML = `
      <td><input type="text" data-field="ip" value="${escapeAttr(o.ip)}"></td>
      <td><input type="number" min="1" max="65535" data-field="port" value="${o.port ?? 6454}"></td>
      <td><input type="text" data-field="label" value="${escapeAttr(o.label)}"></td>
      <td><input type="checkbox" data-field="broadcast" ${o.broadcast ? "checked" : ""}></td>
      <td></td>
      <td class="row-actions"></td>
    `;
    const actions = tr.lastElementChild;
    const saveBtn = document.createElement("button");
    saveBtn.textContent = "Save";
    saveBtn.onclick = async () => {
      const newIp = tr.querySelector('[data-field="ip"]').value.trim();
      const newPort = parseInt(tr.querySelector('[data-field="port"]').value, 10) || 6454;
      const newLabel = tr.querySelector('[data-field="label"]').value;
      const newBroadcast = tr.querySelector('[data-field="broadcast"]').checked;
      const cfg = structuredClone(state.config);
      const idx = cfg.outputs.findIndex(x => x.ip === o.ip);
      if (idx === -1) return;
      if (newIp !== o.ip && cfg.outputs.some(x => x.ip === newIp)) {
        alert(`Output ${newIp} already exists.`);
        return;
      }
      cfg.outputs[idx] = { ip: newIp, port: newPort, label: newLabel, broadcast: newBroadcast };
      state.editing.output = null;
      try {
        await putConfig(cfg);
      } catch (e) {
        alert(`Save failed: ${e.message}`);
        state.editing.output = o.ip;
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
      const newBindIp = document.getElementById("cfg-bind-ip").value.trim() || "0.0.0.0";
      const bindChanged = newBindIp !== cfg.bind_ip;
      cfg.bind_ip = newBindIp;
      cfg.send_rate_hz = parseFloat(document.getElementById("cfg-send-rate").value);
      cfg.source_timeout_s = parseFloat(document.getElementById("cfg-source-timeout").value);
      cfg.send_keepalive_when_silent = document.getElementById("cfg-keepalive").checked;
      cfg.auto_allow_unknown_sources = document.getElementById("cfg-auto-allow").checked;
      await putConfig(cfg);
      if (bindChanged) {
        document.getElementById("restart-banner").hidden = false;
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
    document.getElementById("restart-now-btn").onclick = doRestart;

    document.getElementById("check-updates-btn").onclick = async () => {
      const btn = document.getElementById("check-updates-btn");
      const badge = document.getElementById("update-badge");
      const orig = btn.textContent;
      btn.textContent = "Checking…";
      btn.disabled = true;
      badge.hidden = true;
      try {
        const r = await fetch("/api/update/status");
        if (!r.ok) throw new Error("status " + r.status);
        const j = await r.json();
        if (j.update_available) {
          badge.textContent = `Update to ${j.latest}`;
          badge.href = j.release_url || "https://github.com/djkoren/artnet-htp/releases";
          badge.hidden = false;
          btn.textContent = "Up to date check complete";
        } else if (j.error) {
          btn.textContent = "Can't reach GitHub";
          btn.title = j.error;
        } else {
          btn.textContent = "You're on the latest";
        }
      } catch (e) {
        btn.textContent = "Check failed";
        btn.title = String(e);
      } finally {
        btn.disabled = false;
        setTimeout(() => { btn.textContent = orig; btn.title = "Check GitHub for a newer release"; }, 6000);
      }
    };

    document.getElementById("add-source-form").onsubmit = async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      await api("/api/sources", {
        method: "POST",
        body: JSON.stringify({
          ip: fd.get("ip"),
          label: fd.get("label") || "",
          mode: fd.get("mode") || "htp",
          priority: parseInt(fd.get("priority"), 10) || 100,
        }),
      });
      e.target.reset();
      await loadConfig();
    };

    document.getElementById("add-output-form").onsubmit = async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      await api("/api/outputs", {
        method: "POST",
        body: JSON.stringify({
          ip: fd.get("ip"),
          port: parseInt(fd.get("port"), 10) || 6454,
          label: fd.get("label") || "",
          broadcast: !!fd.get("broadcast"),
        }),
      });
      e.target.reset();
      await loadConfig();
    };

    document.getElementById("add-universe-form").onsubmit = async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const spec = String(fd.get("universes") || "").trim();
      if (!spec) return;
      let list;
      try {
        list = parseUniverseSpec(spec);
      } catch (err) {
        alert("Invalid universe input: " + err.message);
        return;
      }
      if (list.length === 0) return;
      if (list.length > 256 && !confirm(`Add ${list.length} universes?`)) return;
      let result;
      try {
        result = await api("/api/universes", {
          method: "POST",
          body: JSON.stringify({ port_addresses: list }),
        });
      } catch (err) {
        alert("Failed to add: " + err.message);
        return;
      }
      if (result && result.skipped && result.skipped.length > 0) {
        console.log(`Skipped already-present universes: ${result.skipped.join(", ")}`);
      }
      e.target.reset();
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
