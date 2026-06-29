/* AV Schematic Builder — frontend app (vanilla JS, no build step).
   Follows the design handoff workflow across all screens, wired to the
   backend (BOM → draw.io → EasySchematic + cable schedule). */

const API = location.origin;

const SIGNAL_COLORS = {
  hdmi:"#d6b656", sdi:"#6d8764", displayport:"#0070c0", usb:"#0070c0",
  ethernet:"#006EAF", dante:"#7030a0", ndi:"#e36c09", avb:"#833c00",
  "speaker-level":"#ff0000", "analog-audio":"#ff6600", rf:"#808080",
  fiber:"#00b0f0", hdbaset:"#70ad47", rs422:"#ffc000", gpio:"#ffc000",
};
const ACCENTS = { Blue:"#2f6fe0", Amber:"#c0922a", Green:"#1f8a5b", Mono:"#3a3a3a" };

const DEVICE_TYPES = [
  "", "codec", "camera", "display", "projector", "microphone", "wireless-mic",
  "wireless-receiver", "speaker", "amplifier", "audio-interface", "dsp",
  "control-panel", "touch-panel", "network-switch", "matrix-router", "switcher",
  "computer", "encoder", "decoder",
];
const PORT_ATTRS = [
  "hdmi_in", "hdmi_out", "sdi_in", "sdi_out", "usb_in", "usb_out",
  "dante_in", "dante_out", "hdbaset_in", "hdbaset_out", "ethernet",
  "analog_audio_in", "analog_audio_out",
];

const SAMPLE_BOM = `Name,Type,Model,Serial,Quantity,Room,Notes
Cisco Webex Codec Pro,Codec,CS-CODEC-PRO,,1,,Main room codec
Cisco PTZ 4K Camera,Camera,CTS-CAM-P60-K9,,2,,Ceiling mounted
Samsung QM90B Display,Display,QM90B,,2,,Front displays
Cisco Ceiling Mic,Microphone,CS-MIC-CLNG-T,,2,,Ceiling array
Lightware MX2 8x8,Switcher,MX2-8x8-HDMI20,,1,,Matrix switcher`;

const LANES = ["Sources", "Switching", "Distribution", "Endpoints", "Devices"];
function laneOf(node) {
  const t = ((node.data.deviceType || "") + " " + (node.data.label || "")).toLowerCase();
  const has = (...k) => k.some(x => t.includes(x));
  if (has("camera","laptop","player","blu","media player")) return "Sources";
  if (has("display","projector","screen","speaker","monitor"," tv","mic","panel")) return "Endpoints";
  if (has("switch","matrix","dsp","mixer","scaler","codec","conferenc","core","processor")) return "Switching";
  if (has("extender","hdbaset","receiver","transmit","amp","network","dante","splitter","distrib","hub")) return "Distribution";
  const ports = node.data.ports || [];
  const hasIn = ports.some(p => p.direction === "input");
  const hasOut = ports.some(p => p.direction === "output");
  if (hasIn && hasOut) return "Switching";
  if (hasOut && !hasIn) return "Sources";
  if (hasIn && !hasOut) return "Endpoints";
  return "Devices";
}

// ── tiny DOM helper ───────────────────────────────────────────────
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null) e.append(k.nodeType ? k : document.createTextNode(k));
  return e;
}
const $ = s => document.querySelector(s);
function toast(msg, kind) {
  const t = $("#toast"); t.textContent = msg; t.className = kind === "err" ? "err" : "";
  setTimeout(() => t.classList.add("hidden"), 4200);
}
async function api(path, opts) {
  const r = await fetch(API + path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    let d; try { d = await r.json(); } catch { d = { detail: r.statusText }; }
    const m = typeof d.detail === "string" ? d.detail
            : (d.detail && d.detail.message) ? d.detail.message : JSON.stringify(d.detail);
    throw new Error(m);
  }
  return r.status === 204 ? null : r.json();
}
async function apiBlob(path, opts) {
  const r = await fetch(API + path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    let d; try { d = await r.json(); } catch { d = { detail: r.statusText }; }
    throw new Error(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail));
  }
  return r.blob();
}
function saveBlob(blob, filename) {
  const a = el("a", { href: URL.createObjectURL(blob), download: filename });
  document.body.append(a); a.click(); a.remove();
}

// ── app state ─────────────────────────────────────────────────────
const state = {
  tab: "Projects",
  accent: localStorage.getItem("accent") || "Blue",
  projects: [], projectCache: {},
  room: null, ctx: null,
  inputMode: "describe",
  brief: "",
  bom: SAMPLE_BOM,
  roomName: "Boardroom",
  proposal: null, build: null,
  selectedNode: null,
  catalog: null,
};

const TABS = [
  { id: "Projects", label: "Projects" },
  { id: "New build", label: "New build" },
  { id: "Proposal", label: "Proposal", need: () => state.proposal },
  { id: "Schematic", label: "Schematic", need: () => state.build },
  { id: "Cables", label: "Cable schedule", need: () => state.build },
  { id: "Export", label: "Export", need: () => state.build },
];
function go(tab) { state.tab = tab; render(); }

// ── accent ────────────────────────────────────────────────────────
function applyAccent() { document.documentElement.style.setProperty("--accent", ACCENTS[state.accent]); }
function renderAccentPicker() {
  const wrap = $("#accentPick");
  wrap.querySelectorAll(".swatch").forEach(s => s.remove());
  for (const [name, col] of Object.entries(ACCENTS))
    wrap.append(el("span", { class: "swatch" + (state.accent === name ? " on" : ""),
      style: `background:${col}`, title: name,
      onclick: () => { state.accent = name; localStorage.setItem("accent", name); applyAccent(); renderAccentPicker(); } }));
}
function renderTabs() {
  const t = $("#tabs"); t.innerHTML = "";
  for (const tab of TABS) {
    const disabled = tab.need && !tab.need();
    t.append(el("button", { class: "tab" + (state.tab === tab.id ? " active" : ""),
      ...(disabled ? { disabled: "" } : {}), onclick: disabled ? null : () => go(tab.id) }, tab.label));
  }
}

// ── modal ─────────────────────────────────────────────────────────
function openModal(title, bodyNode, wide) {
  closeModal();
  const card = el("div", { class: "modal", style: wide ? "width:760px" : "" },
    el("span", { class: "close", onclick: closeModal }, "✕"),
    el("h2", {}, title), bodyNode);
  const bd = el("div", { class: "modal-backdrop", id: "modal",
    onclick: e => { if (e.target.id === "modal") closeModal(); } }, card);
  document.body.append(bd);
}
function closeModal() { const m = $("#modal"); if (m) m.remove(); }

// ── SCREEN: Projects ──────────────────────────────────────────────
function screenProjects(root) {
  const wrap = el("div", { class: "split" });
  const side = el("div", { class: "sidebar" });
  side.append(el("div", { class: "row center", style: "margin-bottom:14px" },
    el("input", { class: "input", id: "npName", placeholder: "New project name" }),
    el("button", { class: "btn accent", onclick: createProject }, "+ New")));

  if (!state.projects.length) side.append(el("div", { class: "empty" }, "No projects yet — create one to start."));
  for (const p of state.projects) {
    const full = state.projectCache[p.id];
    const proj = el("div", { class: "proj" });
    proj.append(el("div", { class: "phead" },
      el("span", { onclick: () => toggleProject(p.id) }, full ? "▾" : "▸"),
      el("span", { class: "pname", onclick: () => toggleProject(p.id) }, p.name),
      el("span", { class: "pmeta" }, `${p.status} · ${p.roomCount} rm · ${p.deviceCount} dev`),
      el("span", { class: "btn ghost sm", style: "margin-left:8px", title: "Rename",
        onclick: () => renameProject(p) }, "✎"),
      el("span", { class: "btn ghost sm", title: "Delete", onclick: () => deleteProject(p) }, "🗑")));
    if (full) {
      const rooms = el("div", { class: "rooms" });
      if (!full.rooms.length) rooms.append(el("div", { class: "empty", style: "font-size:15px" }, "no rooms yet"));
      for (const r of full.rooms)
        rooms.append(el("div", { class: "room-item" + (state.ctx?.roomId === r.id ? " active" : ""),
          onclick: () => openRoom(p.id, r.id) },
          el("span", {}, "▦"), el("span", {}, r.name),
          el("span", { class: "rmeta" }, `${r.deviceCount} dev · ${r.cableCount} cbl · ${r.status}`),
          el("span", { class: "btn ghost sm", title: "Delete room",
            onclick: e => { e.stopPropagation(); deleteRoom(p.id, r); } }, "🗑")));
      rooms.append(el("button", { class: "btn ghost sm", style: "margin-top:6px",
        onclick: () => addRoom(p.id) }, "+ add room"));
      proj.append(rooms);
    }
    side.append(proj);
  }
  const intro = el("div", { class: "grow" }, el("div", { class: "card ink" },
    el("h2", {}, "Projects & rooms"),
    el("p", { class: "lead" }, "Rooms nest under each project — your fleet rolls up here. Pick a room to open its schematic, or start a new build."),
    el("button", { class: "btn accent", onclick: () => { resetBuild(); go("New build"); } }, "Start a new build ▸"),
    el("div", { class: "note" }, "Familiar & dense. Rooms nest under each project — the “both” scope rolls up here.")));
  wrap.append(side, intro);
  root.append(wrap);
}

// ── SCREEN: New build ─────────────────────────────────────────────
function screenNewBuild(root) {
  const projOpts = [el("option", { value: "" }, "— stateless (don’t save) —")];
  for (const p of state.projects) projOpts.push(el("option", { value: p.id, ...(state.ctx?.projectId === p.id ? { selected: "" } : {}) }, p.name));

  const seg = el("div", { class: "seg" },
    ...[["describe", "✎ Describe the room"], ["bom", "▦ Paste / upload BOM"], ["catalog", "☷ Pick from catalog"]]
      .map(([m, lab]) => el("button", { class: state.inputMode === m ? "on" : "",
        onclick: () => { state.inputMode = m; if (m === "catalog") startFromCatalog(); else render(); } }, lab)));

  const head = el("div", { class: "row" },
    el("div", { class: "grow" }, el("label", { class: "label" }, "Room name"),
      el("input", { class: "input", id: "roomName", value: state.roomName })),
    el("div", { style: "width:260px" }, el("label", { class: "label" }, "Save into project"),
      el("select", { class: "input", id: "targetProj" }, ...projOpts)));

  const card = el("div", { class: "card ink" }, el("h2", {}, "What room are we building?"), seg, head);

  if (state.inputMode === "describe") {
    card.append(el("label", { class: "label" }, "Describe it in plain language"),
      el("textarea", { class: "input", id: "brief",
        placeholder: "e.g. A 12-person boardroom, dual displays, 4 ceiling mics, table cubby for BYOD, touch control…" }, state.brief),
      el("div", { class: "row center", style: "margin-top:14px" },
        el("button", { class: "btn accent", id: "genBtn", onclick: describe }, "Generate ▸"),
        el("span", { class: "muted", id: "genBusy", style: "font-size:13px" })),
      el("div", { class: "note" }, "AI-first. Set ANTHROPIC_API_KEY for smart proposals; works offline with a keyword starter."));
  } else { // bom
    card.append(el("label", { class: "label" }, "BOM CSV — paste, or upload a .csv"),
      el("textarea", { class: "input", id: "bom" }, state.bom),
      el("div", { class: "row center", style: "margin-top:8px;flex-wrap:wrap" },
        el("input", { type: "file", accept: ".csv,text/csv", id: "bomFile",
          onchange: loadBomFile, style: "max-width:230px" }),
        el("span", { class: "muted", style: "font-size:12px" }, "or try:"),
        ...["Huddle room", "Lecture hall", "Boardroom"].map(s =>
          el("span", { class: "chip", onclick: () => { $("#roomName").value = s; } }, s))),
      el("div", { class: "row center", style: "margin-top:14px" },
        el("button", { class: "btn accent", id: "genBtn", onclick: generate }, "Generate ▸"),
        el("span", { class: "muted", id: "genBusy", style: "font-size:13px" })),
      el("div", { class: "note" }, "Power-user path — paste a spreadsheet, fix only what’s flagged, build."));
  }
  root.append(card);
}

// ── SCREEN: Proposal ──────────────────────────────────────────────
function screenProposal(root) {
  const p = state.proposal;
  const v = p.validation || { ok: true, errors: [], warnings: [] };
  const vCls = v.ok ? "ok" : (v.errors.length ? "err" : "warn");
  root.append(el("div", { class: "card ink" },
    el("h2", {}, `Proposed equipment — ${p.devices.length} items`),
    el("p", { class: "lead" }, p.note || "Confirm the roles, tweak quantities/ports, then build."),
    el("div", { style: "margin-bottom:10px" },
      el("span", { class: `pill ${vCls}` }, `BOM ${v.ok ? "ok" : "needs attention"}`),
      el("span", { class: "muted", style: "margin-left:8px;font-size:12px" },
        `${v.errors.length} errors · ${v.warnings.length} warnings · source: ${p.source || "bom"}`)),
    proposalTable(p.devices),
    el("div", { class: "row center", style: "margin-top:12px;flex-wrap:wrap" },
      el("button", { class: "btn ghost sm", onclick: () => editDevice(null) }, "+ Add device"),
      el("button", { class: "btn ghost sm", onclick: openCatalog }, "☷ From catalog")),
    el("div", { class: "row center", style: "margin-top:16px" },
      el("button", { class: "btn ghost", onclick: () => go("New build") }, "◂ Regenerate"),
      el("button", { class: "btn accent", id: "buildBtn", onclick: buildSchematic,
        ...(p.devices.length ? {} : { disabled: "" }) }, "Build schematic ▸"),
      el("span", { class: "muted", id: "buildBusy", style: "font-size:13px" })),
    el("div", { class: "note" }, "AI flags only what it’s unsure of. Accepting choices teaches your standards.")));
}
function proposalTable(devices) {
  const body = el("tbody");
  devices.forEach((d, i) => {
    const sel = el("select", { class: "cell", onchange: e => { d.type = e.target.value; d.confidence = e.target.value ? "confirmed" : "unknown"; } },
      ...DEVICE_TYPES.map(t => el("option", { value: t, ...(d.type === t ? { selected: "" } : {}) }, t || "— role —")));
    body.append(el("tr", {},
      el("td", {}, el("input", { class: "cell", value: d.name, onchange: e => d.name = e.target.value })),
      el("td", {}, sel),
      el("td", {}, el("input", { class: "cell", value: d.model || "", onchange: e => d.model = e.target.value })),
      el("td", {}, el("input", { class: "cell qty", type: "number", min: "1", value: d.quantity, onchange: e => d.quantity = Math.max(1, +e.target.value || 1) })),
      el("td", {}, el("span", { class: `pill ${d.confidence === "unknown" ? "warn" : "ok"}` }, d.confidence)),
      el("td", {},
        el("button", { class: "btn ghost sm", onclick: () => editDevice(i) }, "ports"),
        el("button", { class: "btn ghost sm", onclick: () => { state.proposal.devices.splice(i, 1); render(); } }, "✕"))));
  });
  return el("table", {}, el("thead", {}, el("tr", {}, ...["Device", "Role", "Model", "Qty", "Confidence", ""].map(h => el("th", {}, h)))), body);
}

// device add/edit modal (name/type/model/qty + port-count attrs)
function editDevice(index) {
  const isNew = index == null;
  const d = isNew ? { name: "", type: "", model: "", quantity: 1, confidence: "confirmed", attrs: {} }
                  : JSON.parse(JSON.stringify(state.proposal.devices[index]));
  d.attrs = d.attrs || {};
  const grid = el("div", { class: "field-grid" },
    field("Name", el("input", { class: "input", id: "d_name", value: d.name })),
    field("Role / type", el("select", { class: "input", id: "d_type" },
      ...DEVICE_TYPES.map(t => el("option", { value: t, ...(d.type === t ? { selected: "" } : {}) }, t || "— role —")))),
    field("Model", el("input", { class: "input", id: "d_model", value: d.model || "" })),
    field("Quantity", el("input", { class: "input", id: "d_qty", type: "number", min: "1", value: d.quantity })));
  const ports = el("div", { class: "field-grid ports" },
    ...PORT_ATTRS.map(a => field(a.replace(/_/g, " "),
      el("input", { class: "input", id: "a_" + a, type: "number", min: "0", value: d.attrs[a] || "" }))));
  const body = el("div", {}, grid,
    el("div", { class: "cat-head" }, "Port counts (optional — overrides defaults inferred from role)"), ports,
    el("div", { class: "row center", style: "margin-top:16px;justify-content:flex-end" },
      el("button", { class: "btn ghost", onclick: closeModal }, "Cancel"),
      el("button", { class: "btn accent", onclick: () => {
        const nd = {
          name: $("#d_name").value.trim() || "Device",
          type: $("#d_type").value,
          model: $("#d_model").value.trim(),
          quantity: Math.max(1, +$("#d_qty").value || 1),
          confidence: $("#d_type").value ? "confirmed" : "unknown",
          attrs: {},
        };
        for (const a of PORT_ATTRS) { const val = $("#a_" + a).value.trim(); if (val && val !== "0") nd.attrs[a] = val; }
        if (!state.proposal) state.proposal = { devices: [], validation: { ok: true, errors: [], warnings: [] } };
        if (isNew) state.proposal.devices.push(nd); else state.proposal.devices[index] = nd;
        closeModal(); render();
      } }, "Save device")));
  openModal(isNew ? "Add device" : "Edit device", body);
}
function field(label, input) { return el("div", {}, el("label", { class: "label" }, label), input); }

// ── SCREEN: Schematic ─────────────────────────────────────────────
function screenSchematic(root) {
  const b = state.build;
  root.append(el("div", { class: "row center", style: "margin-bottom:16px;flex-wrap:wrap" },
    statBox(b.counts.devices, "devices"), statBox(b.counts.nodes, "nodes"),
    statBox(b.counts.edges, "edges"), statBox(b.counts.cables, "cables"),
    el("div", { class: "grow" }),
    el("button", { class: "btn ghost sm", onclick: () => go("Cables") }, "Cable schedule ▸")));

  const split = el("div", { class: "split" });
  const canvasCard = el("div", { class: "card grow", style: "overflow:auto" });
  canvasCard.append(el("div", { class: "section-title" }, `${b.schematic.name} · signal flow`));
  const wrap = el("div", { class: "canvas-wrap", id: "canvasWrap" });
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "edges-svg"); svg.id = "edgesSvg";
  const lanesEl = el("div", { class: "lanes", id: "lanes" });
  const byLane = {}; LANES.forEach(l => byLane[l] = []);
  for (const n of b.schematic.nodes) byLane[laneOf(n)].push(n);
  for (const lane of LANES) {
    if (!byLane[lane].length) continue;
    const col = el("div", { class: "lane" }, el("h3", {}, lane));
    for (const n of byLane[lane]) col.append(deviceBlock(n));
    lanesEl.append(col);
  }
  wrap.append(svg, lanesEl); canvasCard.append(wrap);
  split.append(canvasCard, inspectorPanel());
  root.append(split);
  root.append(el("div", { class: "note" }, "Auto-tidy: devices laid into signal-flow lanes; wires colored by signal type. Click a device to inspect/edit."));
  requestAnimationFrame(() => drawEdges(b.schematic));
}
function statBox(n, l) { return el("div", { class: "stat" }, el("div", { class: "n" }, String(n)), el("div", { class: "l" }, l)); }
function deviceBlock(node) {
  const ins = node.data.ports.filter(p => p.direction === "input" || p.direction === "bidirectional");
  const outs = node.data.ports.filter(p => p.direction === "output");
  return el("div", { class: "devblock" + (state.selectedNode === node.id ? " sel" : ""), id: "n-" + node.id,
    onclick: () => { state.selectedNode = node.id; render(); } },
    el("div", { class: "dtitle" }, node.data.label),
    el("div", { class: "dsub" }, node.data.deviceType || "device"),
    el("div", { class: "ports" },
      el("div", { class: "col" }, ...ins.map(p => portRow(p, "in"))),
      el("div", { class: "col" }, ...outs.map(p => portRow(p, "out")))));
}
function portRow(p, dir) {
  const dot = el("span", { class: "sigdot", style: `background:${SIGNAL_COLORS[p.signalType] || "#999"}` });
  const lbl = el("span", {}, p.label);
  return el("div", { class: "port " + dir, id: "p-" + p.id }, dir === "out" ? [lbl, dot] : [dot, lbl]);
}
function drawEdges(schematic) {
  const wrap = $("#canvasWrap"), svg = $("#edgesSvg");
  if (!wrap || !svg) return;
  const wr = wrap.getBoundingClientRect();
  svg.setAttribute("width", wrap.scrollWidth); svg.setAttribute("height", wrap.scrollHeight);
  svg.innerHTML = "";
  const center = (id, side) => {
    const e = document.getElementById(id); if (!e) return null;
    const r = e.getBoundingClientRect();
    const x = side === "right" ? r.right - wr.left + wrap.scrollLeft : r.left - wr.left + wrap.scrollLeft;
    return { x, y: r.top - wr.top + wrap.scrollTop + r.height / 2 };
  };
  for (const ed of schematic.edges) {
    let a = ed.sourceHandle ? center("p-" + ed.sourceHandle, "right") : center("n-" + ed.source, "right");
    let z = ed.targetHandle ? center("p-" + ed.targetHandle, "left") : center("n-" + ed.target, "left");
    if (!a) a = center("n-" + ed.source, "right");
    if (!z) z = center("n-" + ed.target, "left");
    if (!a || !z) continue;
    const col = SIGNAL_COLORS[ed.data?.signalType] || "#777";
    const dx = Math.max(30, Math.abs(z.x - a.x) * 0.4);
    const d = `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${z.x - dx} ${z.y}, ${z.x} ${z.y}`;
    const mk = (stroke, w, op) => {
      const pth = document.createElementNS("http://www.w3.org/2000/svg", "path");
      pth.setAttribute("d", d); pth.setAttribute("fill", "none"); pth.setAttribute("stroke", stroke);
      pth.setAttribute("stroke-width", w); pth.setAttribute("stroke-opacity", op); pth.setAttribute("stroke-linecap", "round");
      return pth;
    };
    svg.append(mk("#fdfdfb", "6", "0.9"), mk(col, "3", "0.95"));
    for (const pt of [a, z]) {
      const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      c.setAttribute("cx", pt.x); c.setAttribute("cy", pt.y); c.setAttribute("r", "3"); c.setAttribute("fill", col);
      svg.append(c);
    }
  }
}
window.addEventListener("resize", () => { if (state.tab === "Schematic" && state.build) drawEdges(state.build.schematic); });

function inspectorPanel() {
  const card = el("div", { class: "card inspector" });
  const node = state.build.schematic.nodes.find(n => n.id === state.selectedNode);
  if (!node) {
    card.append(el("div", { class: "section-title" }, "Inspector"),
      el("div", { class: "empty", style: "font-size:16px" }, "Select a device to inspect its ports & connections."));
    return card;
  }
  card.append(el("div", { class: "section-title" }, node.data.label),
    el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" }, node.data.deviceType || "device"));
  card.append(el("div", { style: "font-weight:700;font-size:12px;margin:8px 0 4px" }, "Ports"));
  const tb = el("tbody");
  for (const p of node.data.ports)
    tb.append(el("tr", {},
      el("td", {}, el("span", { class: "sigdot", style: `background:${SIGNAL_COLORS[p.signalType] || "#999"}` }), p.label),
      el("td", { class: "muted" }, p.direction), el("td", { class: "muted" }, p.connectorType || "")));
  card.append(el("table", {}, tb));
  const conns = state.build.cableSchedule.filter(c => c.fromRef.startsWith(node.data.label) || c.toRef.startsWith(node.data.label));
  card.append(el("div", { style: "font-weight:700;font-size:12px;margin:12px 0 4px" }, `Connections (${conns.length})`));
  for (const c of conns) card.append(el("div", { class: "muted", style: "font-size:12px" }, `${c.fromRef} → ${c.toRef}`));
  card.append(el("div", { class: "row", style: "margin-top:12px" },
    el("button", { class: "btn ghost sm", onclick: () => editFromInspector(node) }, "Edit device & rebuild")));
  card.append(el("div", { class: "note" }, "Side inspector — edit a device, then rebuild to re-route."));
  return card;
}
function editFromInspector(node) {
  // find matching proposal device by label/model and open editor; saving rebuilds
  const idx = (state.proposal?.devices || []).findIndex(d =>
    node.data.label.startsWith(d.name) && (!d.model || node.data.label.includes(d.name)));
  if (idx >= 0) { state.tab = "Proposal"; render(); setTimeout(() => editDevice(idx), 50); }
  else { toast("Edit devices on the Proposal tab, then rebuild", "err"); go("Proposal"); }
}

// ── SCREEN: Cable schedule ────────────────────────────────────────
function screenCables(root) {
  const cs = state.build.cableSchedule;
  root.append(el("div", { class: "card ink" },
    el("div", { class: "row center", style: "margin-bottom:8px" },
      el("h2", { style: "margin:0" }, `Cable schedule · ${cs.length} runs`),
      el("div", { class: "grow" }),
      el("button", { class: "btn ghost sm", onclick: () => doExport("csv") }, "Export CSV")),
    cableTable(cs),
    el("div", { class: "note" }, "Auto-derived from the canvas — every wire becomes a row.")));
}
function cableTable(cs) {
  const tb = el("tbody");
  for (const c of cs)
    tb.append(el("tr", {},
      el("td", { class: "muted" }, c.id.replace("cbl-", "")),
      el("td", {}, c.fromRef), el("td", {}, c.toRef),
      el("td", {}, el("span", { class: "sigdot", style: `background:${SIGNAL_COLORS[c.signalType] || "#999"}` }), c.signalType),
      el("td", { class: "muted" }, c.length || "—")));
  return el("table", {}, el("thead", {}, el("tr", {}, ...["ID", "From", "To", "Signal", "Len"].map(h => el("th", {}, h)))), tb);
}

// ── SCREEN: Export ────────────────────────────────────────────────
function screenExport(root) {
  const tb = (state.room && state.room.titleBlock) || { jobNo: "", client: "", drawnBy: "", revision: "A" };
  root.append(el("div", { class: "split" },
    el("div", { class: "card grow ink" },
      el("h2", {}, "Export & share"),
      el("p", { class: "lead" }, "Round-trip to CAD, hand off a schedule, or download the EasySchematic project."),
      el("div", { class: "stats", style: "flex-direction:column;gap:10px;align-items:stretch" },
        exportRow("📄", "EasySchematic JSON", "open in EasySchematic", () => doExport("json")),
        exportRow("☷", "Cable schedule CSV", "for the install crew", () => doExport("csv")),
        exportRow("▦", "draw.io diagram", "editable source", () => doExport("drawio")),
        exportRow("📐", "DWG / DXF (AutoCAD)", "editable CAD", () => doExport("dxf")),
        exportRow("📑", "PDF drawing set", "print-ready schedule + equipment", () => doExport("pdf"))),
      el("div", { class: "note" }, "The Visio/Miro replacement angle — round-trip to CAD, share, edit the title block.")),
    el("div", { class: "card inspector" },
      el("div", { class: "section-title" }, "Title block"),
      ...[["Job no.", "jobNo"], ["Client", "client"], ["Drawn by", "drawnBy"], ["Revision", "revision"]].map(([lab, key]) =>
        el("div", {}, el("label", { class: "label" }, lab),
          el("input", { class: "input", id: "tb_" + key, value: tb[key] || "" }))),
      el("button", { class: "btn accent sm", style: "margin-top:12px",
        ...(state.ctx?.roomId ? {} : { disabled: "" }),
        onclick: saveTitleBlock }, "Save title block"),
      state.ctx?.roomId ? null : el("div", { class: "muted", style: "font-size:11px;margin-top:6px" }, "Save a room into a project to persist the title block."),
      el("div", { class: "section-title", style: "margin-top:16px" }, "Sheet"),
      el("div", { class: "muted", style: "font-size:13px" }, `${state.build.schematic.name} — System Schematic`),
      el("div", { class: "muted", style: "font-size:13px" }, `${state.build.counts.cables} cables · ${state.build.counts.devices} devices`))));
}
function exportRow(icon, title, sub, onClick) {
  return el("div", { class: "stat row center", style: "justify-content:space-between;cursor:pointer", onclick: onClick },
    el("div", {}, el("span", { style: "font-size:18px;margin-right:8px" }, icon),
      el("span", { style: "font-weight:700" }, title),
      el("div", { class: "muted", style: "font-size:11px;margin-left:26px" }, sub)),
    el("span", { class: "muted" }, "▸"));
}

// ── actions: projects/rooms ───────────────────────────────────────
async function loadProjects() { state.projects = (await api("/projects")).projects; }
async function createProject() {
  const name = $("#npName").value.trim(); if (!name) return toast("Enter a project name", "err");
  try { const p = await api("/projects", { method: "POST", body: JSON.stringify({ name }) });
    await loadProjects(); await toggleProject(p.id, true); toast("Project created"); }
  catch (e) { toast(e.message, "err"); }
}
async function renameProject(p) {
  const name = prompt("Rename project:", p.name); if (!name || name === p.name) return;
  try { await api(`/projects/${p.id}`, { method: "PATCH", body: JSON.stringify({ name }) });
    await loadProjects(); if (state.projectCache[p.id]) state.projectCache[p.id] = await api(`/projects/${p.id}`); render(); }
  catch (e) { toast(e.message, "err"); }
}
async function deleteProject(p) {
  if (!confirm(`Delete project “${p.name}” and all its rooms?`)) return;
  try { await api(`/projects/${p.id}`, { method: "DELETE" }); delete state.projectCache[p.id];
    if (state.ctx?.projectId === p.id) resetBuild(); await loadProjects(); render(); toast("Project deleted"); }
  catch (e) { toast(e.message, "err"); }
}
async function toggleProject(pid, forceOpen) {
  if (state.projectCache[pid] && !forceOpen) { delete state.projectCache[pid]; render(); return; }
  state.projectCache[pid] = await api(`/projects/${pid}`); render();
}
async function addRoom(pid) {
  const name = prompt("Room name?"); if (!name) return;
  try { await api(`/projects/${pid}/rooms`, { method: "POST", body: JSON.stringify({ name }) });
    await loadProjects(); state.projectCache[pid] = await api(`/projects/${pid}`); render(); toast("Room added"); }
  catch (e) { toast(e.message, "err"); }
}
async function deleteRoom(pid, r) {
  if (!confirm(`Delete room “${r.name}”?`)) return;
  try { await api(`/projects/${pid}/rooms/${r.id}`, { method: "DELETE" });
    await loadProjects(); state.projectCache[pid] = await api(`/projects/${pid}`);
    if (state.ctx?.roomId === r.id) resetBuild(); render(); toast("Room deleted"); }
  catch (e) { toast(e.message, "err"); }
}
async function openRoom(pid, rid) {
  try {
    const room = await api(`/projects/${pid}/rooms/${rid}`);
    state.room = room; state.ctx = { projectId: pid, roomId: rid }; state.roomName = room.name;
    state.proposal = room.devices.length ? { devices: room.devices.map(cloneDev), validation: { ok: true, errors: [], warnings: [] }, source: "saved" } : null;
    if (room.schematic) {
      state.build = { schematic: room.schematic, cableSchedule: room.cableSchedule, devices: room.devices,
        counts: { devices: room.devices.length, nodes: room.schematic.nodes.length, edges: room.schematic.edges.length, cables: room.cableSchedule.length },
        validation: { bom: { ok: true, errors: [], warnings: [] }, drawio: null } };
      state.selectedNode = null; go("Schematic");
    } else { state.build = null; go(state.proposal ? "Proposal" : "New build"); }
  } catch (e) { toast(e.message, "err"); }
}
function cloneDev(d) { return { name: d.name, type: d.type, model: d.model, quantity: d.quantity, confidence: d.confidence || "confirmed", attrs: { ...(d.attrs || {}) } }; }
function resetBuild() { state.proposal = null; state.build = null; state.room = null; state.ctx = null; state.selectedNode = null; }

// ── actions: build flow ───────────────────────────────────────────
function readNewBuildHeader() {
  state.roomName = ($("#roomName")?.value || "Room").trim();
  const pid = $("#targetProj")?.value || "";
  state.ctx = pid ? { projectId: pid, roomId: null } : null;
}
function loadBomFile(e) {
  const f = e.target.files[0]; if (!f) return;
  const r = new FileReader();
  r.onload = () => { $("#bom").value = r.result; state.bom = r.result; toast(`Loaded ${f.name}`); };
  r.readAsText(f);
}
async function describe() {
  readNewBuildHeader(); state.brief = $("#brief").value;
  if (!state.brief.trim()) return toast("Describe the room first", "err");
  $("#genBusy").textContent = "thinking…"; $("#genBtn").disabled = true;
  try {
    const r = await api("/builds/describe", { method: "POST", body: JSON.stringify({ brief: state.brief }) });
    state.proposal = { devices: r.devices, validation: { ok: true, errors: [], warnings: [] }, source: r.source, note: r.note };
    toast(`Proposed ${r.devices.length} devices (${r.source})`); go("Proposal");
  } catch (e) { toast(e.message, "err"); $("#genBusy").textContent = ""; $("#genBtn").disabled = false; }
}
async function generate() {
  readNewBuildHeader(); state.bom = $("#bom").value;
  $("#genBusy").textContent = "mapping devices…"; $("#genBtn").disabled = true;
  try {
    const r = await api("/builds/parse-bom", { method: "POST", body: JSON.stringify({ csv: state.bom, name: state.roomName }) });
    state.proposal = { ...r, source: "bom" };
    toast(`Mapped ${r.devices.length} devices`); go("Proposal");
  } catch (e) { toast(e.message, "err"); $("#genBusy").textContent = ""; $("#genBtn").disabled = false; }
}
function startFromCatalog() {
  readNewBuildHeader();
  state.proposal = { devices: [], validation: { ok: true, errors: [], warnings: [] }, source: "catalog" };
  go("Proposal"); openCatalog();
}
function devicesToCsv(devices) {
  const attrKeys = [...new Set(devices.flatMap(d => Object.keys(d.attrs || {})))];
  const cols = ["Name", "Type", "Model", "Quantity", ...attrKeys];
  const rows = [cols.join(",")];
  for (const d of devices) {
    const r = [d.name, d.type, d.model || "", d.quantity, ...attrKeys.map(k => (d.attrs || {})[k] || "")];
    rows.push(r.map(x => `"${String(x).replace(/"/g, '""')}"`).join(","));
  }
  return rows.join("\n");
}
async function buildSchematic() {
  if (!state.proposal?.devices.length) return toast("Add at least one device", "err");
  $("#buildBusy").textContent = "building…"; $("#buildBtn").disabled = true;
  try {
    let res;
    const pid = state.ctx?.projectId;
    if (pid) {
      let rid = state.ctx.roomId;
      if (!rid) { const room = await api(`/projects/${pid}/rooms`, { method: "POST", body: JSON.stringify({ name: state.roomName }) }); rid = room.id; state.ctx.roomId = rid; }
      await api(`/projects/${pid}/rooms/${rid}/devices`, { method: "PUT", body: JSON.stringify({ devices: state.proposal.devices }) });
      res = await api(`/projects/${pid}/rooms/${rid}/build`, { method: "POST", body: JSON.stringify({ strict: false }) });
      state.room = await api(`/projects/${pid}/rooms/${rid}`);
      await loadProjects(); state.projectCache[pid] = await api(`/projects/${pid}`);
      toast("Built + saved to project");
    } else {
      res = await api("/builds/run", { method: "POST", body: JSON.stringify({ csv: devicesToCsv(state.proposal.devices), name: state.roomName }) });
      toast("Build complete");
    }
    state.build = res; state.selectedNode = null; go("Schematic");
  } catch (e) { toast(e.message, "err"); $("#buildBusy").textContent = ""; $("#buildBtn").disabled = false; }
}

// ── actions: catalog ──────────────────────────────────────────────
async function openCatalog() {
  if (!state.catalog) { try { state.catalog = (await api("/catalog")).items; } catch (e) { return toast(e.message, "err"); } }
  const body = el("div", {});
  const byCat = {};
  for (const it of state.catalog) (byCat[it.type || "other"] ||= []).push(it);
  for (const cat of Object.keys(byCat).sort()) {
    body.append(el("div", { class: "cat-head" }, cat));
    for (const it of byCat[cat])
      body.append(el("div", { class: "catalog-item", onclick: () => addFromCatalog(it) },
        el("div", {}, el("div", { class: "ci-name" }, it.name),
          el("div", { class: "ci-meta" }, [it.manufacturer, it.model].filter(Boolean).join(" · ") || it.type)),
        el("span", { class: "btn accent sm ci-add" }, "+ add")));
  }
  openModal("Product library", body, true);
}
function addFromCatalog(it) {
  if (!state.proposal) state.proposal = { devices: [], validation: { ok: true, errors: [], warnings: [] }, source: "catalog" };
  state.proposal.devices.push({ name: it.name, type: it.type, model: it.model || "", quantity: 1, confidence: "confirmed", attrs: { ...(it.ports || {}) } });
  toast(`Added ${it.name}`); render();
}

// ── actions: export + title block ─────────────────────────────────
async function doExport(fmt) {
  try {
    let blob, fname;
    if (state.ctx?.roomId) {
      blob = await apiBlob(`/projects/${state.ctx.projectId}/rooms/${state.ctx.roomId}/export/${fmt}`);
    } else {
      const csv = devicesToCsv(state.proposal.devices);
      blob = await apiBlob("/builds/export", { method: "POST", body: JSON.stringify({ csv, name: state.roomName, format: fmt }) });
    }
    fname = `${state.build.schematic.name.replace(/\s+/g, "_")}.${fmt}`;
    saveBlob(blob, fname); toast(`${fmt.toUpperCase()} downloaded`);
  } catch (e) { toast(e.message, "err"); }
}
async function saveTitleBlock() {
  if (!state.ctx?.roomId) return;
  const tb = {}; for (const k of ["jobNo", "client", "drawnBy", "revision"]) tb[k] = $("#tb_" + k).value;
  try { state.room = await api(`/projects/${state.ctx.projectId}/rooms/${state.ctx.roomId}`, { method: "PATCH", body: JSON.stringify({ titleBlock: tb }) });
    toast("Title block saved"); } catch (e) { toast(e.message, "err"); }
}

// ── render ────────────────────────────────────────────────────────
function render() {
  renderTabs(); renderAccentPicker();
  const root = $("#screen"); root.innerHTML = "";
  try {
    if (state.tab === "Projects") return void screenProjects(root);
    if (state.tab === "New build") return void screenNewBuild(root);
    if (state.tab === "Proposal" && state.proposal) return void screenProposal(root);
    if (state.tab === "Schematic" && state.build) return void screenSchematic(root);
    if (state.tab === "Cables" && state.build) return void screenCables(root);
    if (state.tab === "Export" && state.build) return void screenExport(root);
    go("Projects");
  } catch (e) { root.append(el("div", { class: "card" }, "Render error: " + e.message)); console.error(e); }
}

// ── boot ──────────────────────────────────────────────────────────
(async () => {
  applyAccent();
  try { const h = await api("/health"); $("#health").textContent = `API ${h.status} · v${h.version}`; }
  catch { $("#health").textContent = "API unreachable"; $("#health").classList.add("bad"); }
  try { await loadProjects(); } catch (e) {}
  render();
})();
