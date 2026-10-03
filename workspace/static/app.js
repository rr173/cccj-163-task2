/* HD-map console: vanilla JS, no build step. */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  status: null, candidates: [], releases: [], events: [],
  patches: [], snapshots: [], view: null,
  viewMode: "published", viewRef: null,
  selectedCandidate: null, selectedSnapshot: null,
  fullSnapshot: null,
  audit: {
    vehicle: "VH-01", seq: 1,
    imports: [], events: [], checkpoints: [], branches: [], archives: [],
    recon: null, view: null,
  },
};

async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data?.error?.message || res.statusText),
    { code: data?.error?.code, details: data?.error?.details });
  return data;
}

let toastTimer;
function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.className = "toast", 3800);
}
function showError(e) {
  console.error(e);
  let msg = e.message;
  if (e.code === "VALIDATION_FAILED" && e.details?.errors) {
    msg = `校验失败 ${e.details.errors.length} 项（见右侧报告）：` +
          e.details.errors.slice(0, 2).map(x => x.message).join("；");
  }
  toast("✗ " + msg, "error");
}
const esc = s => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ------------------------------------------------------------- loading -----
async function refreshAll() {
  await Promise.all([
    loadStatus(), loadCandidates(), loadReleases(),
    loadEvents(), loadPatches(), loadSnapshots(),
    loadAuditImports(), loadAuditEvents(), loadAuditCheckpoints(),
    loadAuditBranches(), loadAuditArchives(),
  ]);
  await refreshView();
}
async function loadStatus() {
  state.status = await api("/status");
  $("#currentVersion").textContent = state.status.current_version;
  $("#activePatches").textContent =
    state.status.active_patches.length
      ? "活动补丁: " + state.status.active_patches.join(", ")
      : "无活动补丁";
  $("#regionTable").innerHTML =
    `<tr><th>区域</th><th>当前代际</th><th>发布版本</th><th>来源候选</th><th>瓦片</th><th>发布时间</th></tr>` +
    state.status.regions.map(r => `
      <tr><td>${esc(r.region)}</td><td class="gen">gen ${r.generation}</td>
      <td>${esc(r.release_id)}</td><td>${esc(r.candidate_id)}</td>
      <td>${r.tiles}</td><td class="meta">${esc(r.published_at.replace("T", " ").replace("+00:00", "Z"))}</td></tr>`
    ).join("");
}
async function loadCandidates() {
  ({ candidates: state.candidates } = await api("/candidates"));
  renderCandidates();
  populateVehicles();
}
async function loadReleases() {
  ({ releases: state.releases } = await api("/releases"));
  $("#releaseList").innerHTML = state.releases.slice().reverse().map(r => `
    <div class="tl-item">
      <span class="tl-gen">${Object.entries(r.generations).map(([k, v]) =>
        `${esc(k)}#${v}`).join("<br>")}</span>
      <div><b>${esc(r.release_id)}</b> ← ${esc(r.candidate_id)}
        <div class="tl-meta">${esc(r.note || "")}</div>
        <div class="tl-meta">${esc(r.created_at.replace("T", " ").replace("+00:00", "Z"))}</div>
      </div></div>`).join("") || `<div class="hint">暂无发布</div>`;
}
async function loadEvents() {
  ({ events: state.events } = await api("/events?limit=80"));
  $("#eventLog").innerHTML = state.events.map(e => `
    <div class="ev"><span class="eid">#${e.event_id}</span>
      <span class="ts">${esc(e.ts.replace("T", " ").replace("+00:00", "Z"))}</span>
      <span class="typ">${esc(e.type)}</span><span>${esc(e.summary)}</span></div>`).join("");
}
async function loadPatches() {
  ({ patches: state.patches } = await api("/patches"));
  renderPatches();
}
async function loadSnapshots() {
  ({ snapshots: state.snapshots } = await api("/snapshots"));
  renderSnapshots();
  populateVehicles();
  populateRouteNodes();
}

async function refreshView() {
  const params = new URLSearchParams({ mode: state.viewMode });
  if (state.viewRef) params.set("ref", state.viewRef);
  try {
    state.view = await api("/view?" + params);
  } catch (e) { state.view = null; }
  renderMap();
  renderValidation();
  if (state.viewMode === "snapshot" && state.selectedSnapshot) {
    try { await loadFullSnapshot(state.selectedSnapshot, { keepMap: true }); } catch {}
  }
  populatePatchTargets();
  populateRouteNodes();
}

// ------------------------------------------------------------ candidates ---
function badge(status) {
  const map = { draft: "b-draft", submitted: "b-submitted", published: "b-published",
    rejected: "b-rejected", active: "b-active", proposed: "b-proposed",
    revoked: "b-revoked", completed: "b-completed", superseded: "b-superseded" };
  return `<span class="badge ${map[status] || "b-draft"}">${esc(status)}</span>`;
}
function renderCandidates() {
  const el = $("#candidateList");
  el.innerHTML = state.candidates.map(c => {
    const n = c.validation?.errors?.length || 0;
    const v = c.status === "rejected" ? `<span class="err-code">${n} 个错误</span>`
      : c.validation?.status === "passed" ? `<span style="color:var(--green)">校验通过</span>`
      : `<span class="hint">未校验</span>`;
    const sel = state.selectedCandidate === c.candidate_id ? "selected" : "";
    return `<div class="card ${sel}" data-cid="${c.candidate_id}">
      <div class="row1"><span class="id">${esc(c.candidate_id)}</span>
        ${badge(c.status)} ${v}</div>
      <div class="note">${esc(c.note || "")}</div>
      <div class="meta">区域 ${c.regions.map(esc).join(",")} ·
        ${Object.keys(c.tiles).length} 瓦片 · 基线代 ${
          Object.entries(c.based_on_generations || {}).map(([k, v]) => `${k}#${v}`).join(",")
        } · ${esc(c.created_at.replace("T", " ").slice(5, 16))}</div>
      <div class="actions">
        <button class="ok" data-act="validate">校验接缝/走廊</button>
        <button class="pub" data-act="publish"
          ${c.status === "published" ? "disabled" : ""}>按区域代发布</button>
      </div></div>`;
  }).join("") || `<div class="hint">无候选</div>`;

  $$("#candidateList .card").forEach(card => {
    card.addEventListener("click", e => {
      if (e.target.tagName === "BUTTON") return;
      const cid = card.dataset.cid;
      state.selectedCandidate = state.selectedCandidate === cid ? null : cid;
      if (state.selectedCandidate) { state.viewMode = "candidate"; state.viewRef = cid; }
      else { state.viewMode = "published"; state.viewRef = null; }
      refreshView(); renderCandidates();
    });
  });
  $$("#candidateList button").forEach(btn => btn.addEventListener("click", async e => {
    e.stopPropagation();
    const card = btn.closest(".card");
    const cid = card.dataset.cid, act = btn.dataset.act;
    try {
      if (act === "validate") {
        const v = await api(`/candidates/${cid}/validate`, { method: "POST" });
        toast(v.status === "passed"
          ? `✓ ${cid} 接缝与跨区走廊全部通过` : `✗ ${cid} ${v.errors.length} 项校验失败`,
          v.status === "passed" ? "ok" : "error");
      } else {
        const r = await api(`/candidates/${cid}/publish`, { method: "POST" });
        toast(`✓ 已发布 ${r.release_id}：` +
          Object.entries(r.generations).map(([k, v]) => `${k}→第${v}代`).join("，"), "ok");
      }
      await refreshAll();
    } catch (err) {
      await refreshAll();
      showError(err);
    }
  }));
}

// -------------------------------------------------------------- map --------
function renderMap() {
  const svg = $("#map");
  if (!state.view) { svg.innerHTML = ""; $("#viewLabel").textContent = "（无视图数据）"; return; }
  $("#viewLabel").textContent = "· " + state.view.version_label;
  drawMap(svg, state.view, {
    labelsOn: $("#toggleLabels").checked,
    corridorsOn: $("#toggleCorridors").checked,
  });
}

function drawMap(svg, v, { labelsOn = true, corridorsOn = true } = {}) {
  const P = id => v.nodes.find(n => n.id === id);

  let html = "";
  // tiles
  v.tiles.forEach((t, i) => {
    const [x1, y1, x2, y2] = t.bounds;
    html += `<rect class="tile-rect-${i % 2 ? "b" : "a"}" x="${x1}" y="${y1}"
      width="${x2 - x1}" height="${y2 - y1}" rx="10"/>
      <text class="tile-label" x="${x1 + 14}" y="${y1 + 30}">
      ${esc(t.tile_id)} · ${esc(t.region)} · ${esc(t.version_tag)}</text>`;
  });
  // corridors
  if (corridorsOn) v.corridors.forEach(cor => {
    if (!cor.ok) return;
    cor.segments.forEach(seg => {
      let cur = seg.start;
      seg.steps.forEach(st => {
        const a = P(cur), b = P(st.kind === "seam"
          ? `${st.to_tile}/${st.to_node}` : `${st.to_tile}/${st.to_node}`);
        if (a && b) html += `<line class="lane corridor" x1="${a.x}" y1="${a.y}"
          x2="${b.x}" y2="${b.y}"/>`;
        cur = b.id;
      });
    });
  });
  // lanes
  v.lanes.forEach(l => {
    let cls = "lane";
    if (l.closed) cls += " closed";
    html += `<line class="${cls}" x1="${l.x1}" y1="${l.y1}" x2="${l.x2}" y2="${l.y2}">
      <title>${esc(l.id)} speed=${l.speed}${l.closed ? " CLOSED by " + l.closed_by : ""}</title></line>`;
    if (l.closed && labelsOn)
      html += `<text class="closed-tag" x="${(l.x1 + l.x2) / 2 + 8}"
        y="${(l.y1 + l.y2) / 2 - 6}">🚧${esc(l.closed_by || "")}</text>`;
  });
  // snapshot route overlay
  if (v.route) {
    const routeNodes = [v.route.steps.length
      ? `${v.route.steps[0].from_tile}/${v.route.steps[0].from_node}` : v.route.from_node];
    v.route.steps.forEach(st => routeNodes.push(
      `${st.to_tile}/${st.to_node}`));
    routeNodes.forEach((id, i) => {
      if (!i) return;
      const a = P(routeNodes[i - 1]), b = P(id);
      if (a && b) html += `<line class="lane route" x1="${a.x}" y1="${a.y}"
        x2="${b.x}" y2="${b.y}"/>`;
    });
  }
  // seams
  v.seams.forEach(s => {
    html += `<circle class="seam ${s.ok ? "ok" : "bad"}" cx="${s.x}" cy="${s.y}" r="11"/>`;
    if (!s.ok) html += `<text class="closed-tag" x="${s.x + 14}" y="${s.y + 5}">接缝异常</text>`;
  });
  // nodes
  v.nodes.forEach(n => {
    let cls = "node" + (n.external ? " ext" : "");
    if (v.route && (n.id === v.route.from_node || n.id === v.route.to_node)) cls += " route-pt";
    html += `<circle class="${cls}" cx="${n.x}" cy="${n.y}" r="${n.external ? 6 : 7}"/>`;
    if (labelsOn) html += `<text class="nlabel" x="${n.x + 9}" y="${n.y - 9}">${
      esc(n.node)}</text>`;
  });
  svg.innerHTML = html;
}

// --------------------------------------------------------- validation ------
function renderValidation() {
  const el = $("#validationReport");
  const v = state.view;
  if (!v) { el.innerHTML = ""; return; }
  const msgs = [];
  if (v.mode === "candidate") {
    if (v.errors.length) {
      msgs.push(`<div class="err-item"><b>候选 ${esc(v.ref)} 不可发布</b>
        （发布将被原子拒绝，不产生任何新版本）</div>`);
      v.errors.forEach(e => msgs.push(`<div class="err-item">
        <span class="err-code">[${esc(e.code)}]</span>
        <span class="where"> @${esc(e.where)}</span><br>${esc(e.message)}
        ${e.segment ? `<div class="hint">断点: ${esc(e.segment[0])} → ${esc(e.segment[1])}</div>` : ""}
      </div>`));
    } else {
      msgs.push(`<div class="ok-item">✓ 候选 ${esc(v.ref)} 所有瓦片边界接缝一致
        （坐标/限速/车道数/相互声明），${v.corridors.filter(c => c.ok).length} 条跨区走廊均可贯通。</div>`);
    }
  } else {
    const okSeams = v.seams.filter(s => s.ok).length;
    msgs.push(`<div class="ok-item">当前视图 ${esc(v.version_label)}：${okSeams}/${
      v.seams.length} 条接缝匹配；${v.corridors.filter(c => c.ok).length}/${
      v.corridors.length} 条走廊贯通。</div>`);
  }
  v.corridors.forEach(c => {
    if (!c.ok) msgs.push(`<div class="err-item"><span class="err-code">[ROUTE_BROKEN]</span>
      跨区走廊 ${esc(c.corridor_id)}「${esc(c.name)}」在当前工作集无法贯通</div>`);
    else msgs.push(`<div class="note-item">走廊 ${esc(c.corridor_id)}「${esc(c.name)}」
      跨瓦片路径有效（绿线）</div>`);
  });
  el.innerHTML = msgs.join("");
}

// ------------------------------------------------------------- patches -----
function populatePatchTargets() {
  const sel = $("#patchTile");
  if (!state.view || !state.view.tiles.length) return;
  const cur = sel.value;
  sel.innerHTML = state.view.tiles.map(t =>
    `<option value="${esc(t.tile_id)}">${esc(t.tile_id)}（${esc(t.region)}）</option>`).join("");
  if (cur) sel.value = cur;
  renderLaneChecks();
}
function renderLaneChecks() {
  const tid = $("#patchTile").value;
  const lanes = state.view ? state.view.lanes.filter(l => l.tile === tid) : [];
  $("#patchLanes").innerHTML = lanes.map(l => `
    <label><input type="checkbox" value="${esc(l.lane)}" ${l.closed ? "disabled" : ""}/>
      ${esc(l.lane)} ${esc(l.from.split("/")[1])}↔${esc(l.to.split("/")[1])}
      ${l.closed ? `（已被 ${esc(l.closed_by)} 封闭）` : ""}</label>`).join("");
}
function renderPatches() {
  $("#patchList").innerHTML = state.patches.map(p => {
    const scope = Object.entries(p.scope.tiles).map(([t, ls]) =>
      ls.map(l => `${t}/${l}`).join(", ")).join("；");
    const compat = p.compatibility?.status === "compatible"
      ? `<span style="color:var(--green)">兼容·有绕行</span>`
      : `<span class="err-code">${esc(p.compatibility?.status || "未检查")}</span>`;
    return `<div class="card">
      <div class="row1"><span class="id">${esc(p.patch_id)} <span class="hint">p${
        p.priority}</span></span>${badge(p.status)} ${compat}</div>
      <div class="note">🚧 ${esc(scope)} — ${esc(p.reason)}</div>
      <div class="meta">固化基础: ${esc(p.base_version.base ?
        Object.values(p.base_version.base).map(b => `gen${b.generation}`).join("/") : "")}
        · ${esc(p.created_at.replace("T", " ").slice(5, 16))}
        ${p.revoked_at ? "· 已于 " + esc(p.revoked_at.replace("T", " ").slice(5, 16)) + " 撤销" : ""}
      </div>
      <div class="actions">
        ${p.status === "proposed"
          ? `<button class="ok" data-act="activate">② 生效（再次核对基础哈希）</button>` : ""}
        ${p.status === "active"
          ? `<button class="danger" data-act="revoke">撤销封路</button>` : ""}
      </div></div>`;
  }).join("") || `<div class="hint">暂无补丁</div>`;

  $$("#patchList button").forEach(btn => btn.addEventListener("click", async () => {
    const pid = btn.closest(".card").querySelector(".id").textContent.split(" ")[0];
    try {
      if (btn.dataset.act === "activate") {
        await api(`/patches/${pid}/activate`, { method: "POST" });
        toast(`✓ ${pid} 已生效；在途车辆将在节点处收到过渡建议，绝不中途拼接拓扑`, "ok");
      } else {
        await api(`/patches/${pid}/revoke`, { method: "POST", body: { reason: "管制解除" } });
        toast(`✓ ${pid} 已撤销，新路线恢复通行；历史快照仍按其固定版本可复算`, "ok");
      }
      await refreshAll();
      if (state.fullSnapshot) await loadFullSnapshot(state.selectedSnapshot, { keepMap: true });
    } catch (e) { await refreshAll(); showError(e); }
  }));
}

// -------------------------------------------------------------- routes -----
function populateVehicles() {
  const sel = $("#vehicleSelect");
  const ids = new Set(["VH-01", ...state.snapshots.map(s => s.vehicle_id)]);
  const cur = sel.value || "VH-01";
  sel.innerHTML = [...ids].map(id => `<option>${esc(id)}</option>`).join("");
  sel.value = cur;
  const asel = $("#auditMomentVehicle");
  if (asel) {
    const acur = asel.value || state.audit.vehicle;
    asel.innerHTML = [...ids].map(id => `<option>${esc(id)}</option>`).join("");
    asel.value = ids.has(acur) ? acur : "VH-01";
    state.audit.vehicle = asel.value;
  }
}
async function populateRouteNodes() {
  if (!state.view) return;
  const f = $("#fromNode"), t = $("#toNode");
  const nodes = state.view.nodes.filter(n => n.external);
  const opts = nodes.map(n => `<option value="${esc(n.id)}">${esc(n.id)}</option>`).join("");
  if (!f.options.length) {
    f.innerHTML = opts; t.innerHTML = opts;
    f.value = "A00/w"; t.value = "B01/e";
  }
}
function renderSnapshots() {
  const el = $("#snapshotList");
  el.innerHTML = state.snapshots.map(s => {
    const sel = state.selectedSnapshot === s.snapshot_id ? "selected" : "";
    return `<div class="card ${sel}" data-sid="${esc(s.snapshot_id)}">
      <div class="row1"><span class="id">${esc(s.snapshot_id)}</span>${badge(s.status)}</div>
      <div class="note">${esc(s.vehicle_id)} · ${esc(s.from_node)} → ${esc(s.to_node)}</div>
      <div class="meta">${esc(s.version)}</div>
      <div class="meta">${s.steps} 步 · ${s.tiles.map(t => t.tile_id).join(",")}
        ${s.parent_snapshot_id ? "· 由 " + esc(s.parent_snapshot_id) + " 过渡而来" : ""}
        ${s.children.length ? "· 已过渡到 " + s.children.map(esc).join(",") : ""}</div>
    </div>`;
  }).join("") || `<div class="hint">尚无快照——先请求一条路线</div>`;
  $$("#snapshotList .card").forEach(c => c.addEventListener("click", async () => {
    state.selectedSnapshot = c.dataset.sid;
    state.viewMode = "snapshot"; state.viewRef = state.selectedSnapshot;
    renderSnapshots();
    await refreshView();
    switchTab("routes");
  }));
}

async function loadFullSnapshot(sid, opts = {}) {
  state.fullSnapshot = await api(`/snapshots/${sid}`);
  state.selectedSnapshot = sid;
  renderTrip();
}

function renderTrip() {
  const s = state.fullSnapshot;
  const panel = $("#tripPanel");
  if (!s) { panel.innerHTML = `<span class="hint">选择或生成一个快照</span>`; return; }
  const stepDone = i => {
    const cur = s.progress.step;
    if (i < cur) return "done";
    return i === cur ? "now" : "";
  };
  panel.innerHTML = `
    <div><b>${esc(s.snapshot_id)}</b> ${badge(s.status)}
      <span class="hint">${esc(s.version.version)}</span></div>
    <div class="hint">${esc(s.from_node)} → ${esc(s.to_node)} · 进度 step=${
      s.progress.step}/${s.route.length} · ${esc(s.progress.state)}</div>
    <div class="tripcontrols">
      <button class="ghost" id="enterLaneBtn" ${s.status !== "active" ? "disabled" : ""}>
        ⬆ 进入下一车道 (on_lane)</button>
      <button class="ghost" id="reachNodeBtn" ${s.status !== "active" ? "disabled" : ""}>
        ⬇ 到达下一节点 (at_node)</button>
      <button class="primary warn" id="adoptBtn">🧭 在节点处采用绕行快照</button>
      <button class="ghost" id="finishBtn">🏁 完成行程</button>
    </div>
    ${s.route.map((st, i) => {
      const text = st.kind === "seam"
        ? `接缝 ${st.from_tile}:${st.from_node} → ${st.to_tile}:${st.to_node}`
        : `车道 ${st.tile}/${st.lane} ${st.from_node}→${st.to_node}`;
      return `<div class="step ${stepDone(i)}"><span class="bar">${
        i < s.progress.step ? "✓" : i === s.progress.step ? "▶" : ""}</span>${text}</div>`;
    }).join("")}`;

  const post = async path => {
    try { await api(path, { method: "POST" }); await refreshAll(); }
    catch (e) { await refreshAll(); showError(e); }
  };
  $("#enterLaneBtn").onclick = () => s.progress.step < s.route.length &&
    api(`/snapshots/${s.snapshot_id}/progress`, {
      method: "POST", body: { step: s.progress.step, state: "on_lane" }
    }).then(() => refreshAll()).catch(e => { refreshAll().then(() => showError(e)); });
  $("#reachNodeBtn").onclick = () => s.progress.step < s.route.length &&
    api(`/snapshots/${s.snapshot_id}/progress`, {
      method: "POST", body: { step: s.progress.step + 1, state: "at_node" }
    }).then(() => refreshAll()).catch(e => { refreshAll().then(() => showError(e)); });
  $("#adoptBtn").onclick = async () => {
    try {
      const child = await api(`/snapshots/${s.snapshot_id}/adopt-reroute`, { method: "POST" });
      toast(`✓ 已在节点处安全过渡到新快照 ${child.snapshot_id}（版本 ${child.version.version}）`, "ok");
      state.selectedSnapshot = child.snapshot_id;
      state.viewMode = "snapshot"; state.viewRef = child.snapshot_id;
      await refreshAll();
    } catch (e) { showError(e); }
  };
  $("#finishBtn").onclick = () => post(`/snapshots/${s.snapshot_id}/complete`);
  renderAdvisory();
}

function renderAdvisory() {
  const s = state.fullSnapshot;
  const el = $("#advisoryPanel");
  if (!s) { el.innerHTML = ""; return; }
  const adv = s.advisory;
  const items = [];
  adv.notes.forEach(n => items.push(`<div class="adv info"><b>[${esc(n.level)}]</b> ${
    esc(n.message)}</div>`));
  adv.closures_ahead.forEach(c => items.push(`<div class="adv ${
    c.must_finish_current_lane_first ? "danger" : "warn"}">
    <b>🚨 ${esc(c.patch_id)}</b> 封路预警：${esc(c.message)}<br>
    <span class="hint">${c.must_finish_current_lane_first
      ? "车辆正在车道上——必须先完成当前车道，禁止新旧拓扑拼接；到达节点后才允许过渡。"
      : "车辆在节点——可立即采用绕行快照，不发生任何车道中途拼接。"}</span></div>`));
  if (!items.length) items.push(`<div class="adv info">本快照固定版本与当前线上版本一致，
    无封路预警。即使区域发布了新一代地图，本快照也不会被改写。</div>`);
  el.innerHTML = items.join("");
}

// --------------------------------------------------------------- tabs ------
function switchTab(name) {
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tabpane").forEach(p => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "routes" && state.selectedSnapshot) {
    state.viewMode = "snapshot"; state.viewRef = state.selectedSnapshot;
    refreshView();
  }
  if (name === "review") {
    state.viewMode = state.selectedCandidate ? "candidate" : "published";
    state.viewRef = state.selectedCandidate;
    refreshView();
  }
  if (name === "audit") {
    refreshAuditAll().catch(showError);
  }
}
$$(".tab").forEach(t => t.addEventListener("click", () => switchTab(t.dataset.tab)));

// ------------------------------------------------------------- wiring ------
$("#viewPublished").addEventListener("click", () => {
  state.viewMode = "published"; state.viewRef = null; state.selectedCandidate = null;
  renderCandidates(); refreshView();
});
$("#toggleCorridors").addEventListener("change", renderMap);
$("#toggleLabels").addEventListener("change", renderMap);

$("#patchTile").addEventListener("change", renderLaneChecks);
$("#createPatchBtn").addEventListener("click", async () => {
  const lanes = $$("#patchLanes input:checked").map(i => i.value);
  if (!lanes.length) return toast("请至少选择一条要封闭的车道", "error");
  try {
    const p = await api("/patches", { method: "POST", body: {
      scope: { tiles: { [$("#patchTile").value]: lanes } },
      reason: $("#patchReason").value || "紧急封路",
      priority: Number($("#patchPriority").value || 100),
    }});
    if (p.compatibility.status === "compatible") {
      toast(`✓ ${p.patch_id} 兼容基础版本且存在绕行路径，可在补丁队列中生效`, "ok");
    } else {
      toast(`✗ ${p.patch_id} 无法安全过渡：${p.compatibility.reason}`, "error");
    }
    await refreshAll();
  } catch (e) { showError(e); }
});

$("#planBtn").addEventListener("click", async () => {
  try {
    const snap = await api("/routes", { method: "POST", body: {
      vehicle_id: $("#vehicleSelect").value,
      from_node: $("#fromNode").value, to_node: $("#toNode").value,
    }});
    toast(`✓ 已固定快照 ${snap.snapshot_id}（${snap.version.version}）`, "ok");
    state.selectedSnapshot = snap.snapshot_id;
    state.viewMode = "snapshot"; state.viewRef = snap.snapshot_id;
    await refreshAll();
    switchTab("routes");
  } catch (e) { showError(e); }
});

$("#syncBtn").addEventListener("click", async () => {
  const vid = $("#vehicleSelect").value;
  try {
    const before = await api(`/vehicles/${vid}`);
    const last = before.snapshots.length ? 0 : 0; // demo: replay from 0 (full offline catch-up)
    const data = await api(`/vehicles/${vid}/sync?last_event_id=${last}`);
    $("#syncResult").innerHTML =
      `<div class="note-item">车辆 <b>${esc(vid)}</b> 重连：回放 ${
        data.missed_events.length} 条错过事件；当前单一有效版本为
        <b>${esc(data.current_version.version)}</b>。
        ${data.active_snapshot ? `在途快照 ${esc(data.active_snapshot.snapshot_id)}（${
          esc(data.active_snapshot.status)}）仍固定其自身版本。` : "无在途快照。"}</div>` +
      data.missed_events.slice(-12).reverse().map(e =>
        `<div class="note-item" style="margin-bottom:4px">#${e.event_id}
          <span class="err-code">${esc(e.type)}</span> ${esc(e.summary)}</div>`).join("");
    toast(`✓ 离线追赶完成，应用 ${data.missed_events.length} 条事件`, "ok");
  } catch (e) { showError(e); }
});

$("#refreshBtn").addEventListener("click", () => refreshAll().catch(showError));

// ============================================================ AUDIT =========
const A = state.audit;
const shortHash = h => String(h || "").replace("sha256:", "").slice(0, 12);

async function loadAuditImports() {
  ({ imports: A.imports } = await api("/audit/imports"));
  renderAuditImports();
}
async function loadAuditEvents() {
  ({ events: A.events } = await api(
    `/audit/events?vehicle_id=${encodeURIComponent(A.vehicle)}`));
  renderAuditEvents();
}
async function loadAuditCheckpoints() {
  ({ checkpoints: A.checkpoints } = await api(
    `/audit/checkpoints?vehicle_id=${encodeURIComponent(A.vehicle)}`));
  renderAuditCheckpoints();
}
async function loadAuditBranches() {
  ({ branches: A.branches } = await api("/audit/branches"));
  renderAuditBranches();
}
async function loadAuditArchives() {
  ({ archives: A.archives } = await api("/audit/archives"));
  renderAuditArchives();
}
async function refreshAuditAll() {
  await Promise.all([loadAuditImports(), loadAuditEvents(), loadAuditCheckpoints(),
    loadAuditBranches(), loadAuditArchives()]);
}

function renderAuditImports() {
  $("#auditImportList").innerHTML = A.imports.map(v => {
    const ok = v.verdict === "accepted";
    return `<div class="card"><div class="row1">
      <span class="id">${esc(v.import_key)}</span>
      <span class="badge ${ok ? "b-published" : "b-rejected"}">${ok ? "accepted" : "rejected"}</span></div>
      <div class="note">${ok
        ? `车辆 ${esc(v.vehicle_id)} · 接受 ${v.accepted_events} 条（序号 ${v.first_seq}–${v.last_seq}）`
        : `车辆 ${esc(v.vehicle_id)} · <span class="err-code">[${esc(v.error.code)}]</span> ${esc(v.error.message)}`}</div>
      <div class="meta">${ok ? "链头摘要 " + esc(shortHash(v.chain_head_digest)) + " · " : ""}${esc(v.received_at.replace("T", " ").slice(5, 19))}</div>
    </div>`;
  }).join("") || `<div class="hint">暂无导入记录</div>`;
}

function renderAuditEvents() {
  $("#auditEventHint").textContent = A.events.length
    ? `车辆 ${A.vehicle} · 链头序号 ${A.events[A.events.length - 1].seq}` : "";
  $("#auditEventList").innerHTML = A.events.map(e => `
    <div class="ev"><span class="eid">#${e.seq}</span>
      <span class="typ">${esc(e.kind)}</span>
      <span>${esc(e.source)} · 摘要 ${esc(shortHash(e.digest))} ← 父 ${
        e.parent_digest ? esc(shortHash(e.parent_digest)) : "GENESIS"}</span></div>`
  ).join("") || `<div class="hint">该车辆暂无证据链——先导入事件批次</div>`;
}

function renderAuditCheckpoints() {
  $("#auditCheckpointList").innerHTML = A.checkpoints.map(c => `
    <div class="card" data-seq="${c.seq}">
      <div class="row1"><span class="id">${esc(c.checkpoint_id)}</span>
        <span class="badge b-submitted">seq ${c.seq}</span></div>
      <div class="note">${esc(c.note || "（无备注）")} · ${esc(c.version || "无版本")}</div>
      <div class="meta">状态摘要 ${esc(shortHash(c.ctx_digest))} · 链摘要 ${esc(shortHash(c.last_digest))}</div>
    </div>`).join("") || `<div class="hint">暂无检查点——重建一个时刻后可确认</div>`;
  $$("#auditCheckpointList .card").forEach(c => c.addEventListener("click", () => {
    $("#auditMomentSeq").value = c.dataset.seq;
    reconstructMoment().catch(showError);
  }));
  const sel = $("#auditBranchCp");
  const cur = sel.value;
  sel.innerHTML = A.checkpoints.map(c =>
    `<option value="${esc(c.checkpoint_id)}">${esc(c.checkpoint_id)} · ${esc(c.vehicle_id)} @ 序号 ${c.seq}</option>`).join("");
  if (cur) sel.value = cur;
}

async function reconstructMoment() {
  A.vehicle = $("#auditMomentVehicle").value;
  A.seq = Number($("#auditMomentSeq").value || 1);
  const [recon, view] = await Promise.all([
    api(`/audit/reconstruct?vehicle_id=${encodeURIComponent(A.vehicle)}&seq=${A.seq}`),
    api(`/view?mode=audit&ref=${encodeURIComponent(A.vehicle)}@${A.seq}`),
  ]);
  A.recon = recon; A.view = view;
  $("#auditViewLabel").textContent = "· " + view.version_label;
  drawMap($("#auditMap"), view, { labelsOn: true, corridorsOn: true });
  renderAuditMoment();
}

function renderAuditMoment() {
  const r = A.recon;
  if (!r) { $("#auditMomentInfo").innerHTML = ""; $("#auditMomentDetail").innerHTML = ""; return; }
  const st = r.state;
  $("#auditMomentInfo").innerHTML = `
    <div class="note-item">重建序号 <b>${r.seq}</b> · 事件摘要 <span class="err-code" style="color:var(--accent)">${esc(shortHash(r.event_digest))}</span> ·
      ${r.resumed_from_checkpoint
        ? `从持久检查点 <b>${esc(r.resumed_from_checkpoint)}</b>（序号 ${r.checkpoint_seq}）续算，应用 ${r.applied_events} 条事件`
        : `从链首回放 ${r.applied_events} 条事件`} · 当前位置 <b>${esc(st.position || "—")}</b></div>
    ${(A.view.missing_tiles || []).length ? `<div class="err-item">瓦片内容缺失：${
      A.view.missing_tiles.map(t => esc(t.tile_id)).join(", ")}</div>` : ""}`;
  const v = st.version || {};
  const closures = Object.entries(st.closures).flatMap(([t, ls]) =>
    Object.entries(ls).map(([l, i]) => `${t}/${l}（${i.patch_id}）`));
  $("#auditMomentDetail").innerHTML = `
    <div class="ok-item" style="border-left-color:var(--purple)">
      版本描述 <b>${esc(v.version || "（无版本证据）")}</b><br>
      <span class="hint">descriptor_hash: ${esc(v.descriptor_hash || "—")}</span><br>
      <span class="hint">基线: ${Object.entries(v.base || {}).map(([k, b]) =>
        `${k}#${b.generation}(${b.release_id})`).join("，") || "—"}${
        (v.patches || []).length ? " · 补丁: " + v.patches.map(p => esc(p.patch_id)).join(", ") : ""}</span></div>
    <div class="note-item">瓦片摘要（内容寻址）<br>${Object.entries(st.tile_status).map(([t, x]) =>
      `<span class="hint">${esc(t)} = ${esc(shortHash(x.hash))}${x.stored ? "" : " ⚠内容缺失"}</span>`).join("<br>") || "—"}</div>
    ${closures.length ? `<div class="adv warn" style="margin-bottom:7px">道路管制：${closures.map(esc).join("，")}</div>` : ""}
    <div class="note-item">行驶步骤（进度 step=${st.progress.step}/${st.route.length} ${esc(st.progress.state)}，目的地 ${esc(st.to_node || "—")}）</div>
    <div class="trip" style="margin-top:6px">${st.route.map((s2, i) => {
      const text = s2.kind === "seam"
        ? `接缝 ${s2.from_tile}:${s2.from_node} → ${s2.to_tile}:${s2.to_node}`
        : `车道 ${s2.tile}/${s2.lane} ${s2.from_node}→${s2.to_node}`;
      const cls = i < st.progress.step ? "done" : i === st.progress.step ? "now" : "";
      return `<div class="step ${cls}"><span class="bar">${
        i < st.progress.step ? "✓" : i === st.progress.step ? "▶" : ""}</span>${esc(text)}</div>`;
    }).join("") || '<span class="hint">该时刻无路线证据</span>'}</div>`;
}

function renderAuditBranches() {
  $("#auditBranchList").innerHTML = A.branches.map(b => {
    const r = b.result;
    const sc = b.scenario.type === "version_switch"
      ? `版本切换 → ${Object.entries(b.scenario.to.regions).map(([k, g]) => `${k}=${g}`).join(",")}`
      : `撤销管制 ${esc(b.scenario.patch_id)}`;
    return `<div class="card"><div class="row1">
      <span class="id">${esc(b.branch_id)}</span>
      <span class="badge ${r.reachable ? "b-published" : "b-rejected"}">${r.reachable ? "可达" : "不可达"}</span></div>
      <div class="note">${sc} · 基于 ${esc(b.checkpoint_id)}（${esc(b.vehicle_id)} @ 序号 ${b.base_seq}）</div>
      <div class="meta">${esc(r.version_before || "—")} → ${esc(r.version_after || "—")}</div>
      <div class="meta">位置 ${esc(r.position)} → ${esc(r.to_node)} · ${r.steps} 步${
        r.route_changed ? " · 路线有变化" : " · 路线不变"} · 已存档 ${esc(b.archive_id || "")}</div>
      ${r.steps ? `<details><summary class="hint">演算路线</summary><div class="trip">${
        r.route.map(s2 => `<div class="step"><span class="bar"></span>${esc(s2.kind === "seam"
          ? `接缝 ${s2.from_tile}:${s2.from_node} → ${s2.to_tile}:${s2.to_node}`
          : `车道 ${s2.tile}/${s2.lane} ${s2.from_node}→${s2.to_node}`)}</div>`).join("")}</div></details>` : ""}
    </div>`;
  }).join("") || `<div class="hint">暂无分支——先确认检查点再演算</div>`;
}

function renderAuditArchives() {
  $("#auditArchiveList").innerHTML = A.archives.map(a => `
    <div class="card"><div class="row1"><span class="id">${esc(a.archive_id)}</span>
      <span class="badge b-proposed">${esc(a.kind)}</span></div>
      <div class="meta">摘要 ${esc(shortHash(a.digest))} · ${esc(a.created_at.replace("T", " ").slice(5, 19))}</div>
      <div class="meta">${esc(JSON.stringify(a.params))}</div>
      <div class="actions"><button class="ok" data-aid="${esc(a.archive_id)}">查看档案</button></div>
      <pre class="archive-body" style="display:none"></pre>
    </div>`).join("") || `<div class="hint">暂无档案——分支演算或导出会产生新档案</div>`;
  $$("#auditArchiveList [data-aid]").forEach(btn => btn.addEventListener("click", async () => {
    const pre = btn.closest(".card").querySelector(".archive-body");
    if (pre.style.display === "none") {
      const full = await api(`/audit/archives/${btn.dataset.aid}`);
      pre.textContent = JSON.stringify(full.content, null, 2);
      pre.style.display = "block";
    } else pre.style.display = "none";
  }));
}

// ---- audit wiring ----
$("#auditMomentVehicle").addEventListener("change", async e => {
  A.vehicle = e.target.value; A.recon = null;
  await Promise.all([loadAuditEvents(), loadAuditCheckpoints()]);
  renderAuditMoment();
});
$("#auditReconstructBtn").addEventListener("click", () =>
  reconstructMoment().catch(showError));
$("#auditCheckpointBtn").addEventListener("click", async () => {
  const note = prompt("检查点备注（将持久化，供长序列续算）", "监管抽查确认");
  if (note === null) return;
  try {
    const cp = await api("/audit/checkpoints", { method: "POST", body: {
      vehicle_id: $("#auditMomentVehicle").value,
      seq: Number($("#auditMomentSeq").value || 1), note } });
    toast(cp.already_confirmed
      ? `该时刻已确认过：${cp.checkpoint_id}` : `✓ 检查点 ${cp.checkpoint_id} 已确认并持久化`, "ok");
    await Promise.all([loadAuditCheckpoints()]);
  } catch (e) { showError(e); }
});
$("#auditSampleBtn").addEventListener("click", async () => {
  const vid = $("#auditVehicle").value.trim() || "VH-01";
  try {
    const b = await api(`/audit/sample?vehicle_id=${encodeURIComponent(vid)}`);
    $("#auditImportKey").value = b.import_key;
    $("#auditEventsJson").value = JSON.stringify(b.events, null, 2);
    toast(`已生成 ${b.events.length} 条示例事件（导入键 ${b.import_key}）`, "ok");
  } catch (e) { showError(e); }
});
$("#auditImportBtn").addEventListener("click", async () => {
  let events;
  try { events = JSON.parse($("#auditEventsJson").value); }
  catch (e) { toast("事件 JSON 解析失败: " + e.message, "error"); return; }
  const body = { import_key: $("#auditImportKey").value.trim(),
    vehicle_id: $("#auditVehicle").value.trim(), events };
  const res = await fetch("/api/audit/imports", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await res.json().catch(() => ({}));
  const el = $("#auditImportResult");
  if (d.error && !d.verdict) {
    el.innerHTML = `<div class="err-item"><span class="err-code">[${esc(d.error.code)}]</span> ${esc(d.error.message)}</div>`;
  } else if (d.verdict === "accepted") {
    el.innerHTML = `<div class="ok-item">✓ 接受 ${d.accepted_events} 条事件（序号 ${d.first_seq}–${d.last_seq}），
      链头摘要 ${esc(shortHash(d.chain_head_digest))}${d.idempotent_replay ? " · <b>同一导入键，返回首次判定</b>" : ""}</div>`;
    toast(`✓ 批次 ${d.import_key} 已接受`, "ok");
  } else {
    el.innerHTML = `<div class="err-item">✗ 批次被拒绝，未写入任何事件：
      <span class="err-code">[${esc(d.error.code)}]</span> ${esc(d.error.message)}
      ${d.idempotent_replay ? "<br><b>同一导入键，返回首次判定</b>" : ""}</div>`;
    toast(`✗ 批次被拒绝：${d.error.code}`, "error");
  }
  await refreshAuditAll();
});
$("#auditBranchType").addEventListener("change", e => {
  const vs = e.target.value === "version_switch";
  $("#auditBranchTargetWrap").style.display = vs ? "" : "none";
  $("#auditBranchPatchWrap").style.display = vs ? "none" : "";
});
$("#auditBranchBtn").addEventListener("click", async () => {
  const cp = $("#auditBranchCp").value;
  if (!cp) return toast("请先确认一个检查点", "error");
  const type = $("#auditBranchType").value;
  let scenario;
  if (type === "version_switch") {
    const regions = {};
    $("#auditBranchTarget").value.split(",").forEach(p => {
      const [r, g] = p.split("=");
      if (r && g) regions[r.trim()] = Number(g);
    });
    if (!Object.keys(regions).length) return toast("请填写目标代际，如 north=1", "error");
    scenario = { type, to: { regions } };
  } else {
    const pid = $("#auditBranchPatch").value.trim();
    if (!pid) return toast("请填写要撤销的补丁号", "error");
    scenario = { type, patch_id: pid };
  }
  try {
    const b = await api("/audit/branches", { method: "POST",
      body: { checkpoint_id: cp, scenario } });
    toast(`✓ 分支 ${b.branch_id} 演算完成（${b.result.reachable ? "可达" : "不可达"}），已存为档案 ${b.archive_id}`, "ok");
    await Promise.all([loadAuditBranches(), loadAuditArchives()]);
  } catch (e) { showError(e); }
});
$("#auditExportMomentBtn").addEventListener("click", async () => {
  try {
    const a = await api("/audit/exports", { method: "POST", body: {
      kind: "reconstruction",
      params: { vehicle_id: $("#auditMomentVehicle").value,
                seq: Number($("#auditMomentSeq").value || 1) } } });
    toast(`✓ 时刻重建已导出为档案 ${a.archive_id}`, "ok");
    await loadAuditArchives();
  } catch (e) { showError(e); }
});
$("#auditExportChainBtn").addEventListener("click", async () => {
  try {
    const a = await api("/audit/exports", { method: "POST", body: {
      kind: "chain", params: { vehicle_id: $("#auditMomentVehicle").value } } });
    toast(`✓ 证据链已导出为档案 ${a.archive_id}`, "ok");
    await loadAuditArchives();
  } catch (e) { showError(e); }
});

refreshAll().catch(showError);
