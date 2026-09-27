"use strict";

const $ = (id) => document.getElementById(id);
let stations = [];
let trainsCache = [];
let currentVersion = null;

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtTime(iso) {
  return iso ? iso.replace("T", " ") : "—";
}

function fmtDuration(min) {
  if (min < 60) return `${min} 分钟`;
  const h = Math.floor(min / 60);
  const m = min % 60;
  return m ? `${h} 小时 ${m} 分钟` : `${h} 小时`;
}

function stationLabel(id) {
  const s = stations.find((x) => x.id === id);
  return s ? `${s.name}（${s.id}）` : id;
}

function transferMinutes(id) {
  const s = stations.find((x) => x.id === id);
  return s ? s.transfer_minutes : null;
}

async function loadTimetable() {
  const res = await fetch("/api/timetable");
  const data = await res.json();
  stations = data.stations;
  trainsCache = data.trains;
  const status = $("timetable-status");
  if (!stations.length) {
    status.textContent = "尚未导入时刻表，请在下方导入。";
  } else {
    status.textContent =
      `当前时刻表：${stations.length} 个站点，${data.trains.length} 个班次` +
      (data.imported_at ? `（导入于 ${data.imported_at.replace("T", " ")}）` : "");
  }
  for (const sel of [$("origin"), $("destination"), $("plan-station"), $("flow-origin"), $("flow-destination")]) {
    sel.innerHTML = "";
    for (const s of stations) {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = `${s.name}（${s.id}）`;
      sel.appendChild(opt);
    }
  }
  if (stations.length > 1) {
    $("destination").selectedIndex = stations.length - 1;
    $("flow-destination").selectedIndex = stations.length - 1;
  }
  const pairSel = $("section-pair");
  pairSel.innerHTML = "";
  for (const [a, b] of adjacentPairs(data.trains)) {
    const opt = document.createElement("option");
    opt.value = JSON.stringify([a, b]);
    opt.textContent = `${stationLabel(a)} ↔ ${stationLabel(b)}`;
    pairSel.appendChild(opt);
  }
  $("lock-list").innerHTML = "";
  $("plan-result").innerHTML = "";
  $("section-result").innerHTML = "";
  $("flow-result").innerHTML = "";
  $("delay-result").innerHTML = "";
  $("delay-attribution").innerHTML = "";
  renderTrains(data.trains);
  renderStockTrainPicker(data.trains);
  renderFlowTrainPicker(data.trains);
  renderDelayTrainPicker(data.trains);
  await loadVersions();
}

function adjacentPairs(trains) {
  // 当前时刻表中有直接相邻停靠的站点对（任一方向，去重）
  const seen = new Map();
  for (const t of trains) {
    for (let i = 0; i + 1 < t.stops.length; i++) {
      const pair = [t.stops[i].station, t.stops[i + 1].station].sort();
      const key = JSON.stringify(pair);
      if (!seen.has(key)) {
        seen.set(key, pair);
      }
    }
  }
  return [...seen.values()].sort((p, q) =>
    (p[0] + p[1]).localeCompare(q[0] + q[1]));
}

async function loadVersions() {
  const box = $("version-list");
  let data;
  try {
    const res = await fetch("/api/timetable-versions");
    data = await res.json();
  } catch (e) {
    box.innerHTML = `<p class="error">版本列表加载失败：${esc(e.message)}</p>`;
    return;
  }
  currentVersion = data.current_version;
  if (!data.versions.length) {
    box.innerHTML = '<p class="hint">尚无版本：导入时刻表后会生成首个版本。</p>';
    return;
  }
  const rows = data.versions.slice().reverse().map((v) => {
    const badge = v.is_current
      ? '<span class="badge-current">当前版本</span>'
      : "";
    const btn = v.is_current
      ? ""
      : `<button type="button" class="restore-btn" data-version="${v.version}">恢复此版本</button>`;
    return `<tr>
      <td>v${v.version} ${badge}</td>
      <td>${v.created_at.replace("T", " ")}</td>
      <td>${v.stations}</td>
      <td>${v.trains}</td>
      <td>${btn}</td>
    </tr>`;
  }).join("");
  box.innerHTML =
    `<table class="version-table"><thead><tr>` +
    `<th>版本</th><th>生成时间</th><th>站点数</th><th>班次数</th><th>操作</th>` +
    `</tr></thead><tbody>${rows}</tbody></table>`;
  box.querySelectorAll(".restore-btn").forEach((btn) => {
    btn.addEventListener("click", () => restoreVersion(parseInt(btn.dataset.version, 10)));
  });
}

async function restoreVersion(version) {
  const box = $("version-list");
  const expected = currentVersion;
  if (expected === null) {
    box.innerHTML = '<p class="error">当前没有可恢复的时刻表版本。</p>';
    return;
  }
  if (!window.confirm(`确定恢复 v${version} 吗？\n恢复后该版本内容会另存为新版本并设为当前。`)) {
    return;
  }
  box.insertAdjacentHTML(
    "beforeend",
    '<p class="hint" id="restore-hint">恢复中…</p>'
  );
  let res;
  try {
    res = await fetch(`/api/timetable-versions/${version}/restore`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_version: expected }),
    });
  } catch (e) {
    const hint = $("restore-hint");
    if (hint) hint.outerHTML = `<p class="error">恢复请求失败：${esc(e.message)}</p>`;
    return;
  }
  const data = await res.json();
  if (!data.ok) {
    const hint = $("restore-hint");
    if (hint) {
      hint.outerHTML =
        `<p class="error">❌ ${esc(data.error) || "恢复失败"}</p>`;
    }
    if (res.status === 409) {
      loadVersions();  // 当前版本已被他人改变，刷新列表
    }
    return;
  }
  await loadTimetable();
  box.insertAdjacentHTML(
    "beforeend",
    `<p class="success">✅ 已恢复 v${version} 的内容，保存为新版本 v${data.version}（${data.stations} 个站点，${data.trains} 个班次）。</p>`
  );
}

function renderTrains(trains) {
  const box = $("train-list");
  if (!trains.length) {
    box.innerHTML = '<p class="hint">暂无班次。</p>';
    return;
  }
  box.innerHTML = trains.map((t) => {
    const rows = t.stops.map((s) =>
      `<tr><td>${esc(s.station)}</td><td>${fmtTime(s.arrive)}</td><td>${fmtTime(s.depart)}</td></tr>`
    ).join("");
    return `<details class="train"><summary><strong>${esc(t.id)}</strong>（${t.stops.length} 站）</summary>
      <table><thead><tr><th>站点</th><th>到达</th><th>发车</th></tr></thead>
      <tbody>${rows}</tbody></table></details>`;
  }).join("");
}

async function doQuery() {
  const origin = $("origin").value;
  const destination = $("destination").value;
  const earliest = $("earliest").value; // datetime-local: YYYY-MM-DDTHH:MM
  const box = $("result");
  if (!origin || !destination || !earliest) {
    box.innerHTML = '<p class="error">请选择起点、终点并填写最早出发时间。</p>';
    return;
  }
  box.innerHTML = '<p class="hint">查询中…</p>';
  let data;
  try {
    const res = await fetch("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ origin, destination, earliest }),
    });
    data = await res.json();
  } catch (e) {
    box.innerHTML = `<p class="error">查询请求失败：${esc(e.message)}</p>`;
    return;
  }
  if (!data.ok) {
    box.innerHTML = `<p class="error">${esc(data.error)}</p>`;
    return;
  }
  if (!data.found) {
    box.innerHTML = `<p class="notfound">🚫 ${esc(data.message)}</p>`;
    return;
  }
  renderItinerary(data);
}

function renderItinerary(data) {
  const parts = [];
  parts.push(`<div class="summary">
    <strong>${esc(stationLabel(data.origin))}</strong> ${fmtTime(data.depart)} 出发 →
    <strong>${esc(stationLabel(data.destination))}</strong> ${fmtTime(data.arrive)} 到达
    ｜ 总历时 ${fmtDuration(data.duration_minutes)}
    ｜ 换乘 ${data.transfers} 次
    ｜ 班次：${data.trains.map(esc).join(" → ")}
  </div>`);
  data.segments.forEach((seg, i) => {
    if (i === 0) {
      parts.push(`<div class="wait">🕐 候车 ${fmtDuration(seg.wait_before_minutes)}</div>`);
    } else {
      const tm = transferMinutes(seg.from);
      parts.push(`<div class="wait">🔁 在 ${esc(stationLabel(seg.from))} 换乘，等待 ` +
        `${fmtDuration(seg.wait_before_minutes)}` +
        (tm !== null ? `（本站最短换乘 ${tm} 分钟）` : "") + `</div>`);
    }
    parts.push(`<div class="segment">
      <span class="train-no">${esc(seg.train)}</span>
      <span>${esc(stationLabel(seg.from))} <strong>${fmtTime(seg.depart)}</strong> 发车</span>
      <span class="arrow">→</span>
      <span>${esc(stationLabel(seg.to))} <strong>${fmtTime(seg.arrive)}</strong> 到达</span>
    </div>`);
  });
  $("result").innerHTML = parts.join("");
}

async function doImport() {
  const text = $("import-text").value.trim();
  const box = $("import-result");
  if (!text) {
    box.innerHTML = '<p class="error">请先粘贴时刻表 JSON 或选择文件。</p>';
    return;
  }
  let payload;
  try {
    payload = JSON.parse(text);
  } catch (e) {
    box.innerHTML = `<p class="error">JSON 解析失败：${esc(e.message)}</p>`;
    return;
  }
  let data;
  try {
    const res = await fetch("/api/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    data = await res.json();
  } catch (e) {
    box.innerHTML = `<p class="error">导入请求失败：${esc(e.message)}</p>`;
    return;
  }
  if (data.ok) {
    box.innerHTML = `<p class="success">✅ 导入成功：${data.stations} 个站点，${data.trains} 个班次。</p>`;
    loadTimetable();
  } else {
    box.innerHTML = `<p class="error">❌ 导入失败，已保留原时刻表：${esc(data.error)}</p>`;
  }
}

function addLockRow() {
  const row = document.createElement("div");
  row.className = "lock-row";
  const trainSel = document.createElement("select");
  trainSel.className = "lock-train";
  for (const t of trainsCache) {
    const opt = document.createElement("option");
    opt.value = t.id;
    opt.textContent = t.id;
    trainSel.appendChild(opt);
  }
  const indexInput = document.createElement("input");
  indexInput.type = "number";
  indexInput.className = "lock-index";
  indexInput.min = "0";
  indexInput.value = "0";
  const platformInput = document.createElement("input");
  platformInput.type = "number";
  platformInput.className = "lock-platform";
  platformInput.min = "1";
  platformInput.max = "4";
  platformInput.value = "1";
  const removeBtn = document.createElement("button");
  removeBtn.type = "button";
  removeBtn.textContent = "删除";
  removeBtn.addEventListener("click", () => row.remove());
  for (const [text, el] of [["班次", trainSel], ["停靠序号", indexInput], ["站台", platformInput]]) {
    const label = document.createElement("label");
    label.append(text, el);
    row.appendChild(label);
  }
  row.appendChild(removeBtn);
  $("lock-list").appendChild(row);
}

async function doPlan() {
  const box = $("plan-result");
  const station = $("plan-station").value;
  const platformCount = parseInt($("plan-platforms").value, 10);
  if (!station) {
    box.innerHTML = '<p class="error">请选择站点。</p>';
    return;
  }
  const locks = [];
  for (const row of document.querySelectorAll("#lock-list .lock-row")) {
    const stopIndex = parseInt(row.querySelector(".lock-index").value, 10);
    const platform = parseInt(row.querySelector(".lock-platform").value, 10);
    if (Number.isNaN(stopIndex) || Number.isNaN(platform)) {
      box.innerHTML = '<p class="error">预锁的停靠序号和站台必须填写整数。</p>';
      return;
    }
    locks.push({
      train_id: row.querySelector(".lock-train").value,
      stop_index: stopIndex,
      platform,
    });
  }
  box.innerHTML = '<p class="hint">计算中…</p>';
  let data;
  try {
    const res = await fetch("/api/platform-plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        station_id: station,
        platform_count: platformCount,
        locks,
      }),
    });
    data = await res.json();
  } catch (e) {
    box.innerHTML = `<p class="error">请求失败：${esc(e.message)}</p>`;
    return;
  }
  if (!data.ok) {
    box.innerHTML = `<p class="error">${esc(data.error)}</p>`;
    return;
  }
  if (!data.feasible) {
    box.innerHTML = `<p class="notfound">🚫 ${esc(data.message)}</p>`;
    return;
  }
  renderPlan(data);
}

function renderPlan(data) {
  if (!data.assignments.length) {
    $("plan-result").innerHTML = '<p class="hint">该站在当前时刻表中没有停靠。</p>';
    return;
  }
  const rows = data.assignments.map((a) =>
    `<tr><td>${esc(a.train_id)}</td><td>${a.stop_index}</td>` +
    `<td><strong>${a.platform}</strong></td>` +
    `<td>${fmtTime(a.start)}</td><td>${fmtTime(a.end)}</td></tr>`
  ).join("");
  $("plan-result").innerHTML =
    `<p class="success">✅ ${esc(stationLabel(data.station))}：${data.platform_count} 个站台，` +
    `${data.assignments.length} 次停靠已分配。</p>` +
    `<table><thead><tr><th>班次</th><th>停靠序号</th><th>站台</th>` +
    `<th>占用开始</th><th>占用结束</th></tr></thead><tbody>${rows}</tbody></table>`;
}

async function doSectionAnalysis() {
  const box = $("section-result");
  const pairValue = $("section-pair").value;
  if (!pairValue) {
    box.innerHTML = '<p class="error">当前时刻表中没有直接相邻停靠的站点对，请先导入时刻表。</p>';
    return;
  }
  const [stationA, stationB] = JSON.parse(pairValue);
  const clearance = parseInt($("section-clearance").value, 10);
  if (Number.isNaN(clearance) || clearance < 0 || clearance > 30) {
    box.innerHTML = '<p class="error">清空间隔必须是 0～30 的整数分钟。</p>';
    return;
  }
  box.innerHTML = '<p class="hint">分析中…</p>';
  let data;
  try {
    const res = await fetch("/api/section-conflicts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        station_a: stationA,
        station_b: stationB,
        clearance_minutes: clearance,
      }),
    });
    data = await res.json();
  } catch (e) {
    box.innerHTML = `<p class="error">请求失败：${esc(e.message)}</p>`;
    return;
  }
  if (!data.ok) {
    box.innerHTML = `<p class="error">${esc(data.error)}</p>`;
    return;
  }
  renderSectionResult(data);
}

function renderSectionResult(data) {
  const head = `${esc(stationLabel(data.station_a))} ↔ ${esc(stationLabel(data.station_b))}，` +
    `清空间隔 ${data.clearance_minutes} 分钟，共 ${data.passage_count} 次区间通过。`;
  const parts = [];
  if (!data.conflicts.length) {
    parts.push(`<p class="success">✅ ${head}无冲突，无需暂缓调整。</p>`);
  } else {
    const side = (p) =>
      `<strong>${esc(p.train_id)}</strong> ${esc(p.direction)}` +
      `（起点停靠 #${p.stop_index}，${fmtTime(p.start)} ～ ${fmtTime(p.end)}）`;
    const rows = data.conflicts.map((c, i) =>
      `<tr><td>${i + 1}</td><td>${side(c.first)}</td><td>${side(c.second)}</td></tr>`
    ).join("");
    parts.push(`<p class="notfound">⚠️ ${head}发现 ${data.conflicts.length} 对冲突。</p>`);
    parts.push(
      `<table><thead><tr><th>#</th><th>先进入区间的通过</th><th>冲突的通过</th></tr></thead>` +
      `<tbody>${rows}</tbody></table>`
    );
    parts.push(`<div class="summary">建议暂缓调整的最少班次（${data.suspended.length} 个）：` +
      `<strong>${data.suspended.map(esc).join("、")}</strong><br>` +
      `同一班次的多次通过已整体处理；暂缓后剩余班次在该区间互不冲突。</div>`);
  }
  $("section-result").innerHTML = parts.join("");
}

$("file-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (file) {
    $("import-text").value = await file.text();
  }
});
$("sample-btn").addEventListener("click", async () => {
  const res = await fetch("/api/sample");
  if (res.ok) {
    $("import-text").value = JSON.stringify(await res.json(), null, 2);
  }
});
$("import-btn").addEventListener("click", doImport);
$("search-btn").addEventListener("click", doQuery);
$("add-lock-btn").addEventListener("click", addLockRow);
$("plan-btn").addEventListener("click", doPlan);
$("section-btn").addEventListener("click", doSectionAnalysis);

// ---------- 车底接续规划 ----------

function trainEndpoints(t) {
  const first = t.stops[0];
  const last = t.stops[t.stops.length - 1];
  return {
    origin: first.station,
    destination: last.station,
    depart: first.depart,
    arrive: last.arrive,
  };
}

function renderStockTrainPicker(trains) {
  const box = $("stock-train-list");
  if (!trains.length) {
    box.innerHTML = '<p class="hint">暂无班次。</p>';
    updateStockCount();
    return;
  }
  box.innerHTML = trains.map((t) => {
    const ep = trainEndpoints(t);
    return `<label class="stock-item">
      <input type="checkbox" class="stock-check" value="${esc(t.id)}">
      <strong>${esc(t.id)}</strong>
      <span>${esc(stationLabel(ep.origin))} ${fmtTime(ep.depart)} 发车 →
        ${esc(stationLabel(ep.destination))} ${fmtTime(ep.arrive)} 到达</span>
    </label>`;
  }).join("");
  box.querySelectorAll(".stock-check").forEach((cb) => {
    cb.addEventListener("change", (e) => {
      if (e.target.checked && countStockChecked() > 60) {
        e.target.checked = false;
        window.alert("最多选择 60 个班次，请先取消部分选择。");
      }
      updateStockCount();
    });
  });
  updateStockCount();
}

function countStockChecked() {
  return document.querySelectorAll("#stock-train-list .stock-check:checked").length;
}

function updateStockCount() {
  $("stock-count").textContent = `已选 ${countStockChecked()} 个班次（1～60）`;
}

async function doRollingStockPlan() {
  const box = $("stock-result");
  const trainIds = [...document.querySelectorAll("#stock-train-list .stock-check:checked")]
    .map((cb) => cb.value);
  const turnaround = parseInt($("stock-turnaround").value, 10);
  if (!trainIds.length) {
    box.innerHTML = '<p class="error">请至少勾选 1 个班次。</p>';
    return;
  }
  if (trainIds.length > 60) {
    box.innerHTML = '<p class="error">最多选择 60 个班次。</p>';
    return;
  }
  if (Number.isNaN(turnaround) || turnaround < 1 || turnaround > 120) {
    box.innerHTML = '<p class="error">整备时间必须是 1～120 的整数分钟。</p>';
    return;
  }
  box.innerHTML = '<p class="hint">计算中…</p>';
  let data;
  try {
    const res = await fetch("/api/rolling-stock-plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ train_ids: trainIds, turnaround_minutes: turnaround }),
    });
    data = await res.json();
  } catch (e) {
    box.innerHTML = `<p class="error">请求失败：${esc(e.message)}</p>`;
    return;
  }
  if (!data.ok) {
    box.innerHTML = `<p class="error">${esc(data.error)}</p>`;
    return;
  }
  renderRollingStockPlan(data);
}

function renderRollingStockPlan(data) {
  const parts = [];
  parts.push(`<div class="summary">所选 ${data.train_count} 个班次，
    最少需要 <strong>${data.group_count}</strong> 组可互换车底；
    接续等待合计 <strong>${fmtDuration(data.total_wait_minutes)}</strong>
    （整备 ${data.turnaround_minutes} 分钟，等待已含整备时间）。</div>`);
  data.chains.forEach((chain, ci) => {
    parts.push(`<div class="stock-chain">
      <div class="stock-chain-head">🚃 车底 ${ci + 1}：${chain.trains.length} 个班次</div>`);
    chain.trains.forEach((t, ti) => {
      parts.push(`<div class="stock-task">
        <span class="train-no">${esc(t.train_id)}</span>
        <span>${esc(stationLabel(t.origin))} <strong>${fmtTime(t.depart)}</strong> 发车</span>
        <span class="arrow">→</span>
        <span>${esc(stationLabel(t.destination))} <strong>${fmtTime(t.arrive)}</strong> 到达</span>
      </div>`);
      if (ti < chain.connections.length) {
        const conn = chain.connections[ti];
        parts.push(`<div class="wait">🕐 接续 ${esc(conn.from_train)} → ${esc(conn.to_train)}：
          等待 ${fmtDuration(conn.wait_minutes)}（含整备）</div>`);
      }
    });
    parts.push("</div>");
  });
  $("stock-result").innerHTML = parts.join("");
}

$("stock-select-all").addEventListener("click", () => {
  const boxes = [...document.querySelectorAll("#stock-train-list .stock-check")];
  boxes.forEach((cb, i) => { cb.checked = i < 60; });
  updateStockCount();
});
$("stock-clear").addEventListener("click", () => {
  document.querySelectorAll("#stock-train-list .stock-check")
    .forEach((cb) => { cb.checked = false; });
  updateStockCount();
});
$("stock-btn").addEventListener("click", doRollingStockPlan);

// ---------- 限座旅客疏运测算 ----------

function renderFlowTrainPicker(trains) {
  const box = $("flow-train-list");
  if (!trains.length) {
    box.innerHTML = '<p class="hint">暂无班次。</p>';
    updateFlowCount();
    return;
  }
  box.innerHTML = trains.map((t) => {
    const first = t.stops[0];
    const last = t.stops[t.stops.length - 1];
    return `<label class="stock-item">
      <input type="checkbox" class="flow-check" value="${esc(t.id)}">
      <input type="number" class="flow-seats" min="1" max="10000" step="1" value="100"
        title="班次 ${esc(t.id)} 的席位数（1～10000）">
      <strong>${esc(t.id)}</strong>
      <span>${esc(stationLabel(first.station))} ${fmtTime(first.depart)} 发车 →
        ${esc(stationLabel(last.station))} ${fmtTime(last.arrive)} 到达（${t.stops.length} 站）</span>
    </label>`;
  }).join("");
  box.querySelectorAll(".flow-check").forEach((cb) => {
    cb.addEventListener("change", (e) => {
      if (e.target.checked && countFlowChecked() > 60) {
        e.target.checked = false;
        window.alert("最多选择 60 个班次，请先取消部分选择。");
      }
      updateFlowCount();
    });
  });
  updateFlowCount();
}

function countFlowChecked() {
  return document.querySelectorAll("#flow-train-list .flow-check:checked").length;
}

function flowTotalStops() {
  let total = 0;
  document.querySelectorAll("#flow-train-list .flow-check:checked").forEach((cb) => {
    const t = trainsCache.find((x) => x.id === cb.value);
    if (t) total += t.stops.length;
  });
  return total;
}

function updateFlowCount() {
  $("flow-count").textContent =
    `已选 ${countFlowChecked()} 个班次，总停靠 ${flowTotalStops()} 个（1～60 班，总停靠 ≤ 300）`;
}

async function doPassengerFlow() {
  const box = $("flow-result");
  const origin = $("flow-origin").value;
  const destination = $("flow-destination").value;
  const earliest = $("flow-earliest").value;
  const deadline = $("flow-deadline").value;
  if (!origin || !destination || !earliest || !deadline) {
    box.innerHTML = '<p class="error">请选择起点、终点并填写最早出发、最晚到达时间。</p>';
    return;
  }
  if (origin === destination) {
    box.innerHTML = '<p class="error">起点和终点不能相同。</p>';
    return;
  }
  if (earliest >= deadline) {
    box.innerHTML = '<p class="error">最早出发时间必须严格早于最晚到达时间（按完整日期比较）。</p>';
    return;
  }
  const checks = [...document.querySelectorAll("#flow-train-list .flow-check:checked")];
  if (!checks.length) {
    box.innerHTML = '<p class="error">请至少勾选 1 个班次。</p>';
    return;
  }
  if (checks.length > 60) {
    box.innerHTML = '<p class="error">最多选择 60 个班次。</p>';
    return;
  }
  const capacities = {};
  for (const cb of checks) {
    const seats = parseInt(cb.closest(".stock-item").querySelector(".flow-seats").value, 10);
    if (Number.isNaN(seats) || seats < 1 || seats > 10000) {
      box.innerHTML = `<p class="error">班次 ${cb.value} 的席位数必须是 1～10000 的整数。</p>`;
      return;
    }
    capacities[cb.value] = seats;
  }
  if (flowTotalStops() > 300) {
    box.innerHTML = '<p class="error">所选班次总停靠数超过 300，请减少勾选班次。</p>';
    return;
  }
  box.innerHTML = '<p class="hint">测算中…</p>';
  let data;
  try {
    const res = await fetch("/api/passenger-flow", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ origin, destination, earliest, deadline, capacities }),
    });
    data = await res.json();
  } catch (e) {
    box.innerHTML = `<p class="error">请求失败：${esc(e.message)}</p>`;
    return;
  }
  if (!data.ok) {
    box.innerHTML = `<p class="error">${esc(data.error)}</p>`;
    return;
  }
  renderPassengerFlow(data);
}

function renderPassengerFlow(data) {
  const parts = [];
  parts.push(`<div class="summary">${esc(stationLabel(data.origin))} →
    ${esc(stationLabel(data.destination))}，时间窗 ${fmtTime(data.earliest)} ～ ${fmtTime(data.deadline)}<br>
    最多可送达 <strong style="font-size:1.15em">${data.total}</strong> 人` +
    (data.itineraries.length ? `，共 ${data.itineraries.length} 条行程。` : "。") + `</div>`);
  if (!data.total) {
    parts.push('<p class="notfound">🚫 时间窗内没有可送达的行程，最多 0 人。</p>');
    $("flow-result").innerHTML = parts.join("");
    return;
  }
  data.itineraries.forEach((item, i) => {
    parts.push(`<div class="stock-chain">
      <div class="stock-chain-head">行程 ${i + 1}：<strong>${item.people}</strong> 人
        ｜ ${fmtTime(item.depart)} 出发 → ${fmtTime(item.arrive)} 到达
        ｜ 换乘 ${item.transfers} 次</div>`);
    item.segments.forEach((seg) => {
      parts.push(`<div class="stock-task">
        <span class="train-no">${esc(seg.train_id)}</span>
        <span>${esc(stationLabel(seg.from))}（停靠 #${seg.board_index}）
          <strong>${fmtTime(seg.depart)}</strong> 上车</span>
        <span class="arrow">→</span>
        <span>${esc(stationLabel(seg.to))}（停靠 #${seg.alight_index}）
          <strong>${fmtTime(seg.arrive)}</strong> 下车</span>
      </div>`);
    });
    parts.push("</div>");
  });
  $("flow-result").innerHTML = parts.join("");
}

$("flow-select-all").addEventListener("click", () => {
  const boxes = [...document.querySelectorAll("#flow-train-list .flow-check")];
  boxes.forEach((cb, i) => { cb.checked = i < 60; });
  updateFlowCount();
});
$("flow-clear").addEventListener("click", () => {
  document.querySelectorAll("#flow-train-list .flow-check")
    .forEach((cb) => { cb.checked = false; });
  updateFlowCount();
});
$("flow-btn").addEventListener("click", doPassengerFlow);

// ---------- 区间延误传播排程 ----------

function renderDelayTrainPicker(trains) {
  const box = $("delay-train-list");
  if (!trains.length) {
    box.innerHTML = '<p class="hint">暂无班次。</p>';
    updateDelayCount();
    return;
  }
  box.innerHTML = trains.map((t) => {
    const first = t.stops[0];
    const last = t.stops[t.stops.length - 1];
    return `<label class="stock-item">
      <input type="checkbox" class="delay-check" value="${esc(t.id)}">
      <input type="number" class="delay-minutes" min="0" max="120" step="1" value="0"
        title="班次 ${esc(t.id)} 的首发延误分钟数（0～120）">
      <strong>${esc(t.id)}</strong>
      <span>${esc(stationLabel(first.station))} ${fmtTime(first.depart)} 发车 →
        ${esc(stationLabel(last.station))} ${fmtTime(last.arrive)} 到达（${t.stops.length} 站）</span>
    </label>`;
  }).join("");
  box.querySelectorAll(".delay-check").forEach((cb) => {
    cb.addEventListener("change", (e) => {
      if (e.target.checked && countDelayChecked() > 40) {
        e.target.checked = false;
        window.alert("最多选择 40 个班次，请先取消部分选择。");
      }
      updateDelayCount();
    });
  });
  updateDelayCount();
}

function countDelayChecked() {
  return document.querySelectorAll("#delay-train-list .delay-check:checked").length;
}

function delayTotalStops() {
  let total = 0;
  document.querySelectorAll("#delay-train-list .delay-check:checked").forEach((cb) => {
    const t = trainsCache.find((x) => x.id === cb.value);
    if (t) total += t.stops.length;
  });
  return total;
}

function updateDelayCount() {
  $("delay-count").textContent =
    `已选 ${countDelayChecked()} 个班次，总停靠 ${delayTotalStops()} 个（1～40 班，总停靠 ≤ 200）`;
}

function collectDelayRequest(box) {
  // 收集并前端校验“区间延误传播排程”卡片的输入（排程与归因共用）
  const checks = [...document.querySelectorAll("#delay-train-list .delay-check:checked")];
  if (!checks.length) {
    box.innerHTML = '<p class="error">请至少勾选 1 个班次。</p>';
    return null;
  }
  if (checks.length > 40) {
    box.innerHTML = '<p class="error">最多选择 40 个班次。</p>';
    return null;
  }
  const clearance = parseInt($("delay-clearance").value, 10);
  if (Number.isNaN(clearance) || clearance < 0 || clearance > 30) {
    box.innerHTML = '<p class="error">区间清空时间必须是 0～30 的整数分钟。</p>';
    return null;
  }
  const trainIds = [];
  const initialDelays = {};
  for (const cb of checks) {
    trainIds.push(cb.value);
    const minutes = parseInt(
      cb.closest(".stock-item").querySelector(".delay-minutes").value, 10);
    if (Number.isNaN(minutes) || minutes < 0 || minutes > 120) {
      box.innerHTML = `<p class="error">班次 ${esc(cb.value)} 的首发延误必须是 0～120 的整数分钟。</p>`;
      return null;
    }
    if (minutes > 0) initialDelays[cb.value] = minutes;
  }
  if (delayTotalStops() > 200) {
    box.innerHTML = '<p class="error">所选班次总停靠数超过 200，请减少勾选班次。</p>';
    return null;
  }
  return {
    train_ids: trainIds,
    initial_delays: initialDelays,
    clearance_minutes: clearance,
  };
}

async function doDelayPlan() {
  const box = $("delay-result");
  const payload = collectDelayRequest(box);
  if (!payload) return;
  $("delay-attribution").innerHTML = "";
  box.innerHTML = '<p class="hint">计算中…</p>';
  let data;
  try {
    const res = await fetch("/api/delay-plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    data = await res.json();
  } catch (e) {
    box.innerHTML = `<p class="error">请求失败：${esc(e.message)}</p>`;
    return;
  }
  if (!data.ok) {
    box.innerHTML = `<p class="error">${esc(data.error)}</p>`;
    return;
  }
  renderDelayPlan(data);
}

function delayTimeCell(adjusted, original) {
  // 调整时刻与原定时刻对比：推迟的用醒目样式标出
  if (!adjusted) return "<td>—</td>";
  const cls = original && adjusted > original ? ' class="delayed"' : "";
  return `<td${cls}>${fmtTime(adjusted)}</td>`;
}

function renderDelayPlan(data) {
  const parts = [];
  parts.push(`<div class="summary">所选 ${data.train_count} 个班次（总停靠 ${data.total_stops} 个），
    区间清空时间 ${data.clearance_minutes} 分钟：已生成逐项最早的可行排程，
    共 ${data.trains.filter((t) => t.final_delay_minutes > 0).length} 个班次末到延误。
    <button id="delay-attr-inline" type="button">查看归因</button></div>`);
  data.trains.forEach((t) => {
    const badge = t.final_delay_minutes > 0
      ? `<span class="badge-delayed">末到延误 ${fmtDuration(t.final_delay_minutes)}</span>`
      : '<span class="badge-ontime">正点</span>';
    const rows = t.stops.map((s) =>
      `<tr><td>${esc(stationLabel(s.station))}</td>` +
      `<td>${fmtTime(s.original_arrive)}</td><td>${fmtTime(s.original_depart)}</td>` +
      `${delayTimeCell(s.arrive, s.original_arrive)}` +
      `${delayTimeCell(s.depart, s.original_depart)}</tr>`
    ).join("");
    parts.push(`<div class="stock-chain">
      <div class="stock-chain-head"><span class="train-no">${esc(t.train_id)}</span>
        首发延误 ${t.initial_delay_minutes} 分钟 ${badge}</div>
      <table><thead><tr><th>站点</th><th>原定到达</th><th>原定发车</th>
        <th>调整到达</th><th>调整发车</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`);
  });
  $("delay-result").innerHTML = parts.join("");
  $("delay-attr-inline").addEventListener("click", doDelayAttribution);
}

// ---------- 晚点来源追踪（归因） ----------

const CONSTRAINT_LABELS = {
  initial_delay: "首发延误",
  section_conflict: "区间冲突",
  run: "班内运行",
  dwell: "站内停留",
  clearance: "区间清空",
};

async function doDelayAttribution() {
  const box = $("delay-attribution");
  const payload = collectDelayRequest(box);
  if (!payload) return;
  box.innerHTML = '<p class="hint">归因分析中…</p>';
  let data;
  try {
    const res = await fetch("/api/delay-attribution", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    data = await res.json();
  } catch (e) {
    box.innerHTML = `<p class="error">请求失败：${esc(e.message)}</p>`;
    return;
  }
  if (!data.ok) {
    box.innerHTML = `<p class="error">${esc(data.error)}</p>`;
    return;
  }
  renderDelayAttribution(data);
}

function renderAttrRootHead(root) {
  if (root.type === "initial_delay") {
    return `首发延误 — 班次 <strong>${esc(root.train_id)}</strong> 在
      ${esc(stationLabel(root.station))} 首发延误 ${fmtDuration(root.delay_minutes)}
      （原定 ${fmtTime(root.original_depart)} → 调整 ${fmtTime(root.adjusted_depart)}）`;
  }
  // 前车也可能本次已晚点（原表冲突与前车晚点可并列）：按原定与调整时刻如实标注
  const aheadDelayed = root.ahead.depart > root.ahead.original_depart
    || root.ahead.arrive > root.ahead.original_arrive;
  const aheadStatus = aheadDelayed
    ? `本次 ${fmtTime(root.ahead.depart)} 发 → ${fmtTime(root.ahead.arrive)} 到（晚点）`
    : `本次 ${fmtTime(root.ahead.depart)} 发 → ${fmtTime(root.ahead.arrive)} 到（正点）`;
  return `区间冲突 — ${esc(stationLabel(root.section[0]))} ↔ ${esc(stationLabel(root.section[1]))}
    区间原时刻表已违反清空 ${root.clearance_minutes} 分钟（缺口 ${root.shortfall_minutes} 分钟）：
    前车 <strong>${esc(root.ahead.train_id)}</strong>（停靠 #${root.ahead.stop_index}，
    原定 ${fmtTime(root.ahead.original_depart)} 发 → ${fmtTime(root.ahead.original_arrive)} 到，
    ${aheadStatus}），
    后车 <strong>${esc(root.follow.train_id)}</strong>（停靠 #${root.follow.stop_index}）
    原定 ${fmtTime(root.follow.original_depart)} 发车，被迫推迟至 ${fmtTime(root.follow.depart)}`;
}

function renderAttrChain(chain) {
  const rows = chain.map((s, i) => {
    const last = i === chain.length - 1;
    return `<tr>
      <td>${i + 1}</td>
      <td><span class="attr-constraint attr-${esc(s.constraint)}">${CONSTRAINT_LABELS[s.constraint]}</span></td>
      <td><strong>${esc(s.train_id)}</strong></td>
      <td>#${s.stop_index}</td>
      <td>${esc(stationLabel(s.station))}</td>
      <td>${s.event === "depart" ? "发车" : "到达"}${last ? "（末到）" : ""}</td>
      <td>${fmtTime(s.time)}</td>
    </tr>`;
  }).join("");
  return `<table class="attr-table"><thead><tr>
    <th>#</th><th>约束</th><th>班次</th><th>停靠序号</th><th>站点</th><th>事件</th><th>时刻</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

function renderDelayAttribution(data) {
  const parts = [];
  const delayed = data.trains.filter((t) => t.final_delay_minutes > 0);
  parts.push(`<div class="summary">晚点来源归因（与排程同一输入：${data.train_count} 个班次，
    区间清空时间 ${data.clearance_minutes} 分钟）：共 ${delayed.length} 个班次末到延误。</div>`);
  if (!delayed.length) {
    parts.push('<p class="success">✅ 全部班次末到正点，无晚点根源。</p>');
  }
  delayed.forEach((t) => {
    parts.push(`<div class="stock-chain">
      <div class="stock-chain-head"><span class="train-no">${esc(t.train_id)}</span>
        末到延误 ${fmtDuration(t.final_delay_minutes)}
        （原定 ${fmtTime(t.original_final_arrive)} → 调整 ${fmtTime(t.final_arrive)}），
        共 ${t.roots.length} 个根源</div>`);
    t.roots.forEach((root, i) => {
      parts.push(`<div class="attr-root">🔗 根源 ${i + 1}：${renderAttrRootHead(root)}</div>`);
      parts.push('<div class="attr-chain-title">传导链（根源 → 末到）：</div>');
      parts.push(renderAttrChain(root.chain));
    });
    parts.push("</div>");
  });
  $("delay-attribution").innerHTML = parts.join("");
}

$("delay-select-all").addEventListener("click", () => {
  const boxes = [...document.querySelectorAll("#delay-train-list .delay-check")];
  boxes.forEach((cb, i) => { cb.checked = i < 40; });
  updateDelayCount();
});
$("delay-clear").addEventListener("click", () => {
  document.querySelectorAll("#delay-train-list .delay-check")
    .forEach((cb) => { cb.checked = false; });
  updateDelayCount();
});
$("delay-btn").addEventListener("click", doDelayPlan);
$("delay-attr-btn").addEventListener("click", doDelayAttribution);

loadTimetable();
