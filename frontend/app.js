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
  // Fallback by port direction: pass-through → Switching, source → Sources, sink → Endpoints
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
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (v != null) e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null) e.append(k.nodeType ? k : document.createTextNode(k));
  return e;
}
const $ = s => document.querySelector(s);
function toast(msg, kind) {
  const t = $("#toast"); t.textContent = msg; t.className = kind === "err" ? "err" : "";
  setTimeout(() => t.classList.add("hidden"), 3800);
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

// ── app state ─────────────────────────────────────────────────────
const state = {
  tab: "Projects",
  accent: localStorage.getItem("accent") || "Blue",
  projects: [],
  project: null,      // full project (with rooms)
  room: null,         // current room object
  ctx: null,          // {projectId, roomId} when building inside a project
  proposal: null,     // { devices, validation }
  build: null,        // { schematic, cableSchedule, devices, counts, validation }
  roomName: "Boardroom",
  bom: SAMPLE_BOM,
  selectedNode: null,
  titleBlock: { jobNo: "CCS1042", client: "", drawnBy: "", revision: "A" },
};

const TABS = [
  { id: "Projects",  label: "Projects" },
  { id: "New build", label: "New build" },
  { id: "Proposal",  label: "Proposal", need: () => state.proposal },
  { id: "Schematic", label: "Schematic", need: () => state.build },
  { id: "Cables",    label: "Cable schedule", need: () => state.build },
  { id: "Export",    label: "Export", need: () => state.build },
];

function go(tab) { state.tab = tab; render(); }

// ── accent ────────────────────────────────────────────────────────
function applyAccent() {
  document.documentElement.style.setProperty("--accent", ACCENTS[state.accent]);
}
function renderAccentPicker() {
  const wrap = $("#accentPick");
  wrap.querySelectorAll(".swatch").forEach(s => s.remove());
  for (const [name, col] of Object.entries(ACCENTS)) {
    wrap.append(el("span", {
      class: "swatch" + (state.accent === name ? " on" : ""),
      style: `background:${col}`, title: name,
      onclick: () => { state.accent = name; localStorage.setItem("accent", name); applyAccent(); renderAccentPicker(); },
    }));
  }
}

// ── tabs ──────────────────────────────────────────────────────────
function renderTabs() {
  const t = $("#tabs"); t.innerHTML = "";
  for (const tab of TABS) {
    const disabled = tab.need && !tab.need();
    t.append(el("button", {
      class: "tab" + (state.tab === tab.id ? " active" : ""),
      ...(disabled ? { disabled: "" } : {}),
      onclick: disabled ? null : () => go(tab.id),
    }, tab.label));
  }
}

// ── SCREEN: Projects (list + room tree) ───────────────────────────
async function screenProjects(root) {
  const wrap = el("div", { class: "split" });
  const side = el("div", { class: "sidebar" });
  side.append(el("div", { class: "row center", style: "margin-bottom:14px" },
    el("input", { class: "input", id: "npName", placeholder: "New project name" }),
    el("button", { class: "btn accent", onclick: createProject }, "+ New build"),
  ));

  if (!state.projects.length) {
    side.append(el("div", { class: "empty" }, "No projects yet — create one to start."));
  }
  for (const p of state.projects) {
    const full = state.projectCache?.[p.id];
    const proj = el("div", { class: "proj" });
    const head = el("div", { class: "phead", onclick: () => toggleProject(p.id) },
      el("span", {}, full ? "▾" : "▸"),
      el("span", { class: "pname" }, p.name),
      el("span", { class: "pmeta" }, `${p.status} · ${p.roomCount} rooms · ${p.deviceCount} devices`),
    );
    proj.append(head);
    if (full) {
      const rooms = el("div", { class: "rooms" });
      if (!full.rooms.length) rooms.append(el("div", { class: "empty", style: "font-size:15px" }, "no rooms yet"));
      for (const r of full.rooms) {
        rooms.append(el("div", {
          class: "room-item" + (state.room?.id === r.id ? " active" : ""),
          onclick: () => openRoom(p.id, r.id),
        },
          el("span", {}, "▦"),
          el("span", {}, r.name),
          el("span", { class: "rmeta" }, `${r.deviceCount} dev · ${r.cableCount} cbl · ${r.status}`),
        ));
      }
      rooms.append(el("button", { class: "btn ghost sm", style: "margin-top:6px",
        onclick: () => addRoom(p.id) }, "+ add room"));
      proj.append(rooms);
    }
    side.append(proj);
  }

  const intro = el("div", { class: "grow" },
    el("div", { class: "card ink" },
      el("h2", {}, "Projects & rooms"),
      el("p", { class: "lead" }, "Rooms nest under each building/project — your fleet rolls up here. Pick a room to open its schematic, or start a new build."),
      el("button", { class: "btn accent", onclick: () => go("New build") }, "Start a new build ▸"),
      el("div", { class: "note" }, "Familiar & dense. Rooms nest under each project — the “both” scope rolls up here."),
    ),
  );
  wrap.append(side, intro);
  root.append(wrap);
}

// ── SCREEN: New build (conversational / BOM paste) ────────────────
function screenNewBuild(root) {
  const projOpts = [el("option", { value: "" }, "— stateless (don’t save) —")];
  for (const p of state.projects) projOpts.push(el("option", { value: p.id, ...(state.ctx?.projectId === p.id ? { selected: "" } : {}) }, p.name));

  root.append(el("div", { class: "card ink" },
    el("h2", {}, "What room are we building?"),
    el("p", { class: "lead" }, "Paste a BOM (CSV) and I’ll map the devices, infer ports & cabling, and draft the system."),
    el("div", { class: "row" },
      el("div", { class: "grow" },
        el("label", { class: "label" }, "Room name"),
        el("input", { class: "input", id: "roomName", value: state.roomName }),
      ),
      el("div", { style: "width:260px" },
        el("label", { class: "label" }, "Save into project"),
        el("select", { class: "input", id: "targetProj" }, ...projOpts),
      ),
    ),
    el("label", { class: "label" }, "BOM CSV"),
    el("textarea", { class: "input", id: "bom" }, state.bom),
    el("div", { class: "row center", style: "margin-top:8px;flex-wrap:wrap" },
      el("span", { class: "muted", style: "font-size:12px" }, "Try a starting point:"),
      ...["Huddle room", "Lecture hall", "Worship venue", "Boardroom"].map(s =>
        el("span", { class: "chip", onclick: () => { $("#roomName").value = s; } }, s)),
    ),
    el("div", { class: "row center", style: "margin-top:14px" },
      el("button", { class: "btn accent", id: "genBtn", onclick: generate }, "Generate ▸"),
      el("span", { class: "muted", id: "genBusy", style: "font-size:13px" }),
    ),
    el("div", { class: "note" }, "Lowest-friction input — paste a spreadsheet, fix only what AI flags, build."),
  ));
}

// ── SCREEN: Proposal (approve the proposal) ───────────────────────
function screenProposal(root) {
  const p = state.proposal;
  const v = p.validation;
  const vCls = v.ok ? "ok" : (v.errors.length ? "err" : "warn");
  root.append(el("div", { class: "card ink" },
    el("h2", {}, `Proposed equipment — ${p.devices.length} items`),
    el("p", { class: "lead" }, "I mapped your BOM. Confirm what’s flagged, then build the schematic."),
    el("div", { style: "margin-bottom:10px" },
      el("span", { class: `pill ${vCls}` }, `BOM ${v.ok ? "ok" : "needs attention"}`),
      el("span", { class: "muted", style: "margin-left:8px;font-size:12px" },
        `${v.errors.length} errors · ${v.warnings.length} warnings`),
    ),
    proposalTable(p.devices),
    el("div", { class: "row center", style: "margin-top:16px" },
      el("button", { class: "btn ghost", onclick: () => go("New build") }, "◂ Regenerate"),
      el("button", { class: "btn accent", id: "buildBtn", onclick: buildSchematic }, "Build schematic ▸"),
      el("span", { class: "muted", id: "buildBusy", style: "font-size:13px" }),
    ),
    el("div", { class: "note" }, "AI flags only what it’s unsure of. Accepting choices teaches your standards for next time."),
  ));
}
function proposalTable(devices) {
  const body = el("tbody");
  devices.forEach((d, i) => {
    const cls = d.confidence === "unknown" ? "warn" : "ok";
    const mark = d.confidence === "unknown" ? "?" : "✓";
    body.append(el("tr", {},
      el("td", {}, `${mark} ${d.name}`),
      el("td", {}, d.type || el("span", { class: "muted" }, "needs a role")),
      el("td", { class: "muted" }, d.model || ""),
      el("td", {}, String(d.quantity)),
      el("td", {}, el("span", { class: `pill ${cls}` }, d.confidence)),
      el("td", {}, el("button", { class: "btn ghost sm", onclick: () => { state.proposal.devices.splice(i, 1); render(); } }, "remove")),
    ));
  });
  return el("table", {},
    el("thead", {}, el("tr", {},
      ...["Device", "Role", "Model", "Qty", "Confidence", ""].map(h => el("th", {}, h)))),
    body);
}

// ── SCREEN: Schematic (signal-flow lanes + inspector) ─────────────
function screenSchematic(root) {
  const b = state.build;
  const top = el("div", { class: "row center", style: "margin-bottom:16px;flex-wrap:wrap" },
    statBox(b.counts.devices, "devices"), statBox(b.counts.nodes, "nodes"),
    statBox(b.counts.edges, "edges"), statBox(b.counts.cables, "cables"),
    el("div", { class: "grow" }),
    el("button", { class: "btn ghost sm", onclick: () => go("Cables") }, "Cable schedule ▸"),
  );
  root.append(top);

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
  wrap.append(svg, lanesEl);
  canvasCard.append(wrap);
  split.append(canvasCard, inspectorPanel());
  root.append(split);
  root.append(el("div", { class: "note" }, "Auto-tidy: devices laid into signal-flow lanes; wires colored by signal type. Click a device to inspect ports."));

  requestAnimationFrame(() => drawEdges(b.schematic));
}
function statBox(n, l) { return el("div", { class: "stat" }, el("div", { class: "n" }, String(n)), el("div", { class: "l" }, l)); }

function deviceBlock(node) {
  const ins = node.data.ports.filter(p => p.direction === "input" || p.direction === "bidirectional");
  const outs = node.data.ports.filter(p => p.direction === "output");
  const sel = state.selectedNode === node.id;
  const blk = el("div", { class: "devblock" + (sel ? " sel" : ""), id: "n-" + node.id,
    onclick: () => { state.selectedNode = node.id; render(); } },
    el("div", { class: "dtitle" }, node.data.label),
    el("div", { class: "dsub" }, node.data.deviceType || "device"),
    el("div", { class: "ports" },
      el("div", { class: "col" }, ...ins.map(p => portRow(p, "in"))),
      el("div", { class: "col" }, ...outs.map(p => portRow(p, "out"))),
    ),
  );
  return blk;
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
  svg.setAttribute("width", wrap.scrollWidth);
  svg.setAttribute("height", wrap.scrollHeight);
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
      const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
      p.setAttribute("d", d); p.setAttribute("fill", "none");
      p.setAttribute("stroke", stroke); p.setAttribute("stroke-width", w);
      p.setAttribute("stroke-opacity", op); p.setAttribute("stroke-linecap", "round");
      return p;
    };
    svg.append(mk("#fdfdfb", "6", "0.9"));   // halo for contrast on paper
    svg.append(mk(col, "3", "0.95"));         // colored wire
    // endpoint nubs
    for (const pt of [a, z]) {
      const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      c.setAttribute("cx", pt.x); c.setAttribute("cy", pt.y); c.setAttribute("r", "3");
      c.setAttribute("fill", col);
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
      el("div", { class: "empty", style: "font-size:16px" }, "Select a device to edit its ports."));
    return card;
  }
  card.append(el("div", { class: "section-title" }, node.data.label),
    el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" }, node.data.deviceType || "device"));
  card.append(el("div", { style: "font-weight:700;font-size:12px;margin:8px 0 4px" }, "Ports"));
  const tb = el("tbody");
  for (const p of node.data.ports) {
    tb.append(el("tr", {},
      el("td", {}, el("span", { class: "sigdot", style: `background:${SIGNAL_COLORS[p.signalType] || "#999"}` }), p.label),
      el("td", { class: "muted" }, p.direction),
      el("td", { class: "muted" }, p.connectorType || ""),
    ));
  }
  card.append(el("table", {}, tb));
  // connections involving this node
  const conns = state.build.cableSchedule.filter(c =>
    c.fromRef.startsWith(node.data.label) || c.toRef.startsWith(node.data.label));
  card.append(el("div", { style: "font-weight:700;font-size:12px;margin:12px 0 4px" }, `Connections (${conns.length})`));
  for (const c of conns) card.append(el("div", { class: "muted", style: "font-size:12px" }, `${c.fromRef} → ${c.toRef}`));
  card.append(el("div", { class: "note" }, "Side inspector — tweak ports without leaving the canvas."));
  return card;
}

// ── SCREEN: Cable schedule ────────────────────────────────────────
function screenCables(root) {
  const cs = state.build.cableSchedule;
  root.append(el("div", { class: "card ink" },
    el("div", { class: "row center", style: "margin-bottom:8px" },
      el("h2", { style: "margin:0" }, `Cable schedule · ${cs.length} runs`),
      el("div", { class: "grow" }),
      el("button", { class: "btn ghost sm", onclick: exportCablesCsv }, "Export CSV"),
    ),
    cableTable(cs),
    el("div", { class: "note" }, "Auto-derived from the canvas — every wire becomes a row. Sort, filter, export."),
  ));
}
function cableTable(cs) {
  const tb = el("tbody");
  for (const c of cs) {
    tb.append(el("tr", {},
      el("td", { class: "muted" }, c.id.replace("cbl-", "")),
      el("td", {}, c.fromRef),
      el("td", {}, c.toRef),
      el("td", {}, el("span", { class: "sigdot", style: `background:${SIGNAL_COLORS[c.signalType] || "#999"}` }), c.signalType),
      el("td", { class: "muted" }, c.length || "—"),
    ));
  }
  return el("table", {},
    el("thead", {}, el("tr", {}, ...["ID", "From", "To", "Signal", "Len"].map(h => el("th", {}, h)))),
    tb);
}

// ── SCREEN: Export & handoff ──────────────────────────────────────
function screenExport(root) {
  const tb = state.titleBlock;
  root.append(el("div", { class: "split" },
    el("div", { class: "card grow ink" },
      el("h2", {}, "Export & share"),
      el("p", { class: "lead" }, "Round-trip to CAD, hand off a schedule, or download the EasySchematic project."),
      el("div", { class: "stats", style: "flex-direction:column;gap:10px;align-items:stretch" },
        exportRow("📄", "EasySchematic JSON", "open in EasySchematic", () => downloadJson()),
        exportRow("☷", "Cable schedule CSV", "for the install crew", () => exportCablesCsv()),
        exportRow("▦", "DWG / Visio", "editable CAD", () => toast("DXF export endpoint is stubbed — coming next")),
        exportRow("📑", "PDF drawing set", "print-ready sheets", () => toast("PDF export is stubbed — coming next")),
      ),
      el("div", { class: "note" }, "The Visio/Miro replacement angle — round-trip to CAD, share a live link, edit the title block."),
    ),
    el("div", { class: "card inspector" },
      el("div", { class: "section-title" }, "Title block"),
      ...[["Job no.", "jobNo"], ["Client", "client"], ["Drawn by", "drawnBy"], ["Revision", "revision"]].map(([lab, key]) =>
        el("div", {}, el("label", { class: "label" }, lab),
          el("input", { class: "input", value: tb[key], oninput: e => { tb[key] = e.target.value; } }))),
      el("div", { class: "section-title", style: "margin-top:16px" }, "Sheet"),
      el("div", { class: "muted", style: "font-size:13px" }, `${state.build.schematic.name} — System Schematic`),
      el("div", { class: "muted", style: "font-size:13px" }, `Rev ${tb.revision} · ${state.build.counts.cables} cables`),
    ),
  ));
}
function exportRow(icon, title, sub, onClick) {
  return el("div", { class: "stat row center", style: "justify-content:space-between;cursor:pointer", onclick: onClick },
    el("div", {}, el("span", { style: "font-size:18px;margin-right:8px" }, icon),
      el("span", { style: "font-weight:700" }, title),
      el("div", { class: "muted", style: "font-size:11px;margin-left:26px" }, sub)),
    el("span", { class: "muted" }, "▸"));
}

// ── actions ───────────────────────────────────────────────────────
async function loadProjects() {
  const { projects } = await api("/projects");
  state.projects = projects;
}
async function createProject() {
  const name = $("#npName").value.trim(); if (!name) return toast("Enter a project name", "err");
  try {
    const p = await api("/projects", { method: "POST", body: JSON.stringify({ name }) });
    await loadProjects(); await toggleProject(p.id, true);
    toast("Project created"); render();
  } catch (e) { toast(e.message, "err"); }
}
async function toggleProject(pid, forceOpen) {
  state.projectCache = state.projectCache || {};
  if (state.projectCache[pid] && !forceOpen) { delete state.projectCache[pid]; render(); return; }
  state.projectCache[pid] = await api(`/projects/${pid}`);
  render();
}
async function addRoom(pid) {
  const name = prompt("Room name?"); if (!name) return;
  try {
    await api(`/projects/${pid}/rooms`, { method: "POST", body: JSON.stringify({ name }) });
    await loadProjects(); state.projectCache[pid] = await api(`/projects/${pid}`);
    toast("Room added"); render();
  } catch (e) { toast(e.message, "err"); }
}
async function openRoom(pid, rid) {
  try {
    const room = await api(`/projects/${pid}/rooms/${rid}`);
    state.room = room; state.ctx = { projectId: pid, roomId: rid }; state.roomName = room.name;
    if (room.schematic) {
      state.build = {
        schematic: room.schematic, cableSchedule: room.cableSchedule, devices: room.devices,
        counts: { devices: room.devices.length, nodes: room.schematic.nodes.length,
                  edges: room.schematic.edges.length, cables: room.cableSchedule.length },
        validation: { bom: { ok: true, errors: [], warnings: [] }, drawio: null },
      };
      state.selectedNode = null;
      go("Schematic");
    } else {
      go("New build");
    }
  } catch (e) { toast(e.message, "err"); }
}
async function generate() {
  state.roomName = $("#roomName").value || "Room";
  state.bom = $("#bom").value;
  const pid = $("#targetProj").value;
  state.ctx = pid ? { projectId: pid, roomId: null } : null;
  $("#genBusy").textContent = "mapping devices…"; $("#genBtn").disabled = true;
  try {
    const r = await api("/builds/parse-bom", { method: "POST",
      body: JSON.stringify({ csv: state.bom, name: state.roomName }) });
    state.proposal = r;
    toast(`Mapped ${r.devices.length} devices`); go("Proposal");
  } catch (e) { toast(e.message, "err"); $("#genBusy").textContent = ""; $("#genBtn").disabled = false; }
}
async function buildSchematic() {
  $("#buildBusy").textContent = "building…"; $("#buildBtn").disabled = true;
  try {
    let res;
    if (state.ctx?.projectId) {
      const pid = state.ctx.projectId;
      const room = await api(`/projects/${pid}/rooms`, { method: "POST", body: JSON.stringify({ name: state.roomName }) });
      await api(`/projects/${pid}/rooms/${room.id}/devices`, { method: "PUT", body: JSON.stringify({ devices: state.proposal.devices }) });
      res = await api(`/projects/${pid}/rooms/${room.id}/build`, { method: "POST", body: JSON.stringify({ strict: false }) });
      state.ctx.roomId = room.id;
      await loadProjects(); state.projectCache && (state.projectCache[pid] = await api(`/projects/${pid}`));
      toast("Built + saved to project");
    } else {
      res = await api("/builds/run", { method: "POST", body: JSON.stringify({ csv: state.bom, name: state.roomName }) });
      toast("Build complete");
    }
    state.build = res; state.selectedNode = null; go("Schematic");
  } catch (e) { toast(e.message, "err"); $("#buildBusy").textContent = ""; $("#buildBtn").disabled = false; }
}
function download(filename, text, mime) {
  const blob = new Blob([text], { type: mime || "text/plain" });
  const a = el("a", { href: URL.createObjectURL(blob), download: filename });
  document.body.append(a); a.click(); a.remove();
}
function exportCablesCsv() {
  const rows = [["ID", "From", "To", "Signal", "Length"]];
  for (const c of state.build.cableSchedule) rows.push([c.id, c.fromRef, c.toRef, c.signalType, c.length || ""]);
  download(`${state.build.schematic.name}_cables.csv`, rows.map(r => r.map(x => `"${x}"`).join(",")).join("\n"), "text/csv");
  toast("Cable schedule CSV downloaded");
}
function downloadJson() {
  download(`${state.build.schematic.name}.json`, JSON.stringify(state.build.schematic, null, 2), "application/json");
  toast("EasySchematic JSON downloaded");
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
  } catch (e) { root.append(el("div", { class: "card" }, "Render error: " + e.message)); }
}

// ── boot ──────────────────────────────────────────────────────────
(async () => {
  applyAccent();
  try {
    const h = await api("/health");
    $("#health").textContent = `API ${h.status} · v${h.version}`;
  } catch { $("#health").textContent = "API unreachable"; $("#health").classList.add("bad"); }
  try { await loadProjects(); } catch (e) { /* show empty */ }
  render();
})();
