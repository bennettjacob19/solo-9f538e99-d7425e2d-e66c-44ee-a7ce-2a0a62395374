"use strict";

const $ = (id) => document.getElementById(id);
let selected = null;
let lastActual = null;
let dirtySet = new Set();
let firstRefresh = true;
let flushBusyMsg = null;
let flushBusyUntil = 0;

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
  return body;
}

function log(msg, cls = "") {
  const line = document.createElement("div");
  line.innerHTML = `<span class="t">${new Date().toLocaleTimeString()}</span> ` +
    `<span class="${cls}">${msg}</span>`;
  const box = $("log");
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function fmt(n) {
  return Number(n).toLocaleString("en-US");
}

function renderGrid(blocks) {
  const grid = $("grid");
  if (grid.children.length !== blocks.length) {
    grid.innerHTML = "";
    blocks.forEach((state, i) => {
      const cell = document.createElement("div");
      cell.className = `cell ${state}`;
      cell.textContent = i;
      cell.title = `块 ${i}：${state}`;
      cell.addEventListener("click", () => selectBlock(i));
      grid.appendChild(cell);
    });
  } else {
    blocks.forEach((state, i) => {
      const cell = grid.children[i];
      const dirty = dirtySet.has(i);
      cell.className = `cell ${state}${dirty ? " dirty" : ""}${i === selected ? " selected" : ""}`;
      cell.title = `块 ${i}：${state}${dirty ? "（待刷盘）" : ""}`;
    });
  }
}

async function selectBlock(i) {
  selected = i;
  $("sel-index").textContent = i;
  [...$("grid").children].forEach((c, j) =>
    c.classList.toggle("selected", j === i)
  );
  await refreshBlockDetail();
}

async function refreshBlockDetail() {
  if (selected === null) return;
  try {
    const info = await api(`/api/blocks/${selected}`);
    $("sel-state").textContent = info.state;
    $("sel-hex").textContent = info.hex.slice(0, 256) +
      (info.hex.length > 256 ? " …（截断显示）" : "");
    $("sel-ascii").textContent = info.ascii_preview;
  } catch (err) {
    log(`读取块 ${selected} 失败：${err.message}`, "warn");
  }
}

async function refresh() {
  let status;
  try {
    status = await api("/api/status");
  } catch (err) {
    log(`状态刷新失败：${err.message}`, "warn");
    return;
  }
  const s = status.stats;
  $("f-path").textContent = s.path;
  $("f-capacity").textContent = `${fmt(s.capacity_bytes)} B`;
  $("f-actual").textContent = `${fmt(s.actual_bytes)} B`;
  $("block-count").textContent = s.block_count;

  const f = status.flush;
  dirtySet = new Set(f.dirty_blocks || []);
  $("f-pending").textContent = fmt(f.pending_blocks);
  $("fl-pending").textContent = f.pending_blocks;
  $("fl-last").textContent = f.last_finished_at
    ? `${f.last_batch_blocks} 块 / ${f.last_duration_ms} ms`
    : "-";
  $("fl-rounds").textContent = f.rounds_completed;
  $("fl-recovered").textContent = f.recovered_blocks;
  $("fl-error").textContent = f.last_error || "-";
  let flushText = "空闲";
  if (f.running) flushText = "刷盘进行中…";
  else if (f.last_error) flushText = "上次刷盘失败";
  else if (f.last_finished_at) flushText = `上次刷盘完成（提交 ${f.last_batch_blocks} 块）`;
  if (flushBusyMsg && Date.now() < flushBusyUntil) flushText = flushBusyMsg;
  $("flush-state").textContent = flushText;
  if (firstRefresh && f.recovered_blocks > 0) {
    log(`启动恢复：上一进程在刷盘中途退出，已从恢复记录重放 ${f.recovered_blocks} 块（整批保留）`, "warn");
  }

  const r = status.reclaim;
  $("f-reclaimed").textContent = `${fmt(r.reclaimed_bytes)} B`;
  const denom = r.total_blocks || 1;
  const pct = Math.round((r.scanned_blocks / denom) * 100);
  $("bar-fill").style.width = `${pct}%`;
  $("progress-text").textContent =
    `${r.scanned_blocks} / ${r.total_blocks} 块（${pct}%），打洞 ${r.punched_blocks}`;
  $("r-scanned").textContent = r.scanned_blocks;
  $("r-punched").textContent = r.punched_blocks;
  $("r-skipped").textContent = r.skipped_data_blocks;
  $("r-before-after").textContent =
    `${fmt(r.actual_bytes_before)} / ${fmt(r.actual_bytes_now)}`;
  $("r-error").textContent = r.error || "-";

  let stateText = "空闲";
  if (r.running) stateText = r.cancel_requested ? "正在停止…" : "回收进行中…";
  else if (r.finished) stateText = r.cancel_requested ? "已中断（已打的洞保留）" : "回收完成";
  $("reclaim-state").textContent = stateText;
  $("btn-start").disabled = r.running;
  $("btn-cancel").disabled = !r.running;

  renderGrid(status.blocks);

  if (lastActual !== null && lastActual !== s.actual_bytes) {
    const delta = s.actual_bytes - lastActual;
    log(`宿主实占变化：${fmt(lastActual)} → ${fmt(s.actual_bytes)} B（${delta > 0 ? "+" : ""}${fmt(delta)}）`);
  }
  lastActual = s.actual_bytes;
  firstRefresh = false;
}

async function busy(fn, okMsg) {
  try {
    await fn();
    if (okMsg) log(okMsg, "ok");
    await refresh();
    await refreshBlockDetail();
  } catch (err) {
    log(err.message, "warn");
  }
}

$("btn-flush").addEventListener("click", async () => {
  try {
    const r = await api("/api/flush", { method: "POST" });
    if (r.committed_blocks === 0) {
      log("刷盘完成：缓存为空，无需提交", "ok");
    } else {
      log(`刷盘完成：本批 ${r.committed_blocks} 块已写入 raw 镜像（耗时 ${r.duration_ms} ms）`, "ok");
    }
  } catch (err) {
    // 一轮未结束时再次发起：服务端返回 409，页面提示忙（保留约 3 秒）
    log(`刷盘未执行：${err.message}`, "warn");
    flushBusyMsg = `忙：${err.message}`;
    flushBusyUntil = Date.now() + 3000;
  }
  await refresh();
  await refreshBlockDetail();
});
$("btn-start").addEventListener("click", () =>
  busy(async () => {
    const r = await api("/api/reclaim/start", { method: "POST" });
    log(`发起回收，起始实占 ${fmt(r.actual_bytes_before)} B`);
  })
);
$("btn-cancel").addEventListener("click", () =>
  busy(async () => {
    await api("/api/reclaim/cancel", { method: "POST" });
    log("已请求中断，等待当前块处理完…", "warn");
  })
);
$("btn-read").addEventListener("click", () =>
  busy(async () => {
    if (selected === null) throw new Error("请先在网格中选择一个块");
    const info = await api(`/api/blocks/${selected}/read`, { method: "POST" });
    log(`读块 ${selected}：状态=${info.state}，前 16 字节=${info.hex.slice(0, 32)}`);
  })
);
$("btn-write-zero").addEventListener("click", () =>
  busy(async () => {
    if (selected === null) throw new Error("请先在网格中选择一个块");
    const r = await api(`/api/blocks/${selected}/write-pattern/zero`, { method: "POST" });
    log(`写块 ${selected}：4KiB 全零（已进缓存，待刷盘块数=${r.pending_blocks}；刷盘后成为可回收零块）`);
  })
);
$("btn-write-data").addEventListener("click", () =>
  busy(async () => {
    if (selected === null) throw new Error("请先在网格中选择一个块");
    const r = await api(`/api/blocks/${selected}/write-pattern/data`, { method: "POST" });
    log(`写块 ${selected}：非零图案（已进缓存，待刷盘块数=${r.pending_blocks}）`);
  })
);
$("btn-write-hex").addEventListener("click", () =>
  busy(async () => {
    if (selected === null) throw new Error("请先在网格中选择一个块");
    const hex = $("hex-input").value.trim();
    const r = await api(`/api/blocks/${selected}/write`, {
      method: "POST",
      body: JSON.stringify({ hex }),
    });
    log(`写块 ${selected}：自定义 Hex 共 ${hex.length / 2} 字节（已进缓存，待刷盘块数=${r.pending_blocks}）`);
  })
);

refresh().then(() => log("页面就绪，镜像已加载"));
setInterval(refresh, 1000);
