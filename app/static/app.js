/* 家长会预约系统前端（无框架，原生 JS） */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

let me = null;
let rounds = [];
let currentRound = null;
let slotsCache = [];
let studentsCache = [];

const STATUS_CN = {
  open: "可选", booked: "已约", cancelled: "已取消",
  requested: "待确认", confirmed: "已确认",
  reschedule_pending: "待改期", completed: "已完成", cancelled_b: "已取消",
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function fmtTime(ts) { return esc(ts).replace("T", " ").slice(0, 16); }
function isTeacher() { return me && me.roles.includes("teacher"); }
function isAdmin() { return me && me.roles.includes("admin"); }
function canSeeTeacher() { return isTeacher() || isAdmin(); }

async function api(method, path, body) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
  return data;
}

function showError(msg) {
  $("#global-error").textContent = msg || "";
  if (msg) setTimeout(() => { $("#global-error").textContent = ""; }, 5000);
}

// ---------- 登录 ----------

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    const r = await api("POST", "/api/login", {
      login: fd.get("login"), password: fd.get("password"),
    });
    me = r.user;
    enterApp();
  } catch (err) { $("#login-error").textContent = err.message; }
});

$("#logout-btn").addEventListener("click", async () => {
  await api("POST", "/api/logout");
  me = null;
  $("#app-view").hidden = true;
  $("#login-view").hidden = false;
});

async function boot() {
  try {
    const r = await api("GET", "/api/me");
    if (r.user) { me = r.user; enterApp(); return; }
  } catch (_) { /* 未登录 */ }
  $("#login-view").hidden = false;
}

function enterApp() {
  $("#login-view").hidden = true;
  $("#app-view").hidden = false;
  $("#user-name").textContent = `${me.name}（${me.roles.join("/")}）`;
  $$(".teacher-only").forEach(el => el.hidden = !isTeacher());
  $$(".admin-only").forEach(el => el.hidden = !isAdmin());
  // 教师日历标签/面板对管理员只读开放；发布时段等操作仍仅教师
  $('[data-tab="teacher"]').style.display = canSeeTeacher() ? "" : "none";
  $("#tab-teacher").hidden = !canSeeTeacher();
  $("#tabs .tab.admin-only").style.display = isAdmin() ? "" : "none";
  $("#leave-form").hidden = !canSeeTeacher();
  loadRounds();
  loadStudents();
  if (canSeeTeacher()) loadTeacherOptions();
  if (isAdmin()) loadAccessLogs();
}

// ---------- 轮次与标签页 ----------

async function loadRounds() {
  const r = await api("GET", "/api/rounds");
  rounds = r.items;
  const sel = $("#round-select");
  sel.innerHTML = rounds.map(x =>
    `<option value="${x.id}">${esc(x.title)}（${esc(x.meeting_date)}）</option>`
  ).join("");
  currentRound = rounds[0] ? rounds[0].id : null;
  sel.onchange = () => { currentRound = Number(sel.value); refreshAll(); };
  refreshAll();
}

async function loadStudents() {
  const r = await api("GET", "/api/students");
  studentsCache = r.items;
  $("#booking-student").innerHTML = studentsCache.map(s =>
    `<option value="${s.id}">${esc(s.name)}（${esc(s.class_name)}）</option>`
  ).join("");
  if (isAdmin()) {
    $("#revoke-student").innerHTML = studentsCache.map(s =>
      `<option value="${s.id}">${esc(s.name)}（${esc(s.class_name)}）</option>`
    ).join("");
    loadGuardianOptions();
  }
}

$$("#tabs .tab").forEach(btn => btn.addEventListener("click", () => {
  $$("#tabs .tab").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  $$(".tab-panel").forEach(p => p.hidden = true);
  $(`#tab-${btn.dataset.tab}`).hidden = false;
  refreshAll();
}));

async function refreshAll() {
  if (!currentRound) return;
  const rnd = rounds.find(x => x.id === currentRound);
  $("#window-hint").textContent =
    `预约窗口：${fmtTime(rnd.open_from)} ~ ${fmtTime(rnd.open_until)}`;
  loadSlots();
  loadMyBookings();
  loadReschedules();
  if (canSeeTeacher()) loadCalendar();
  if (isAdmin()) { loadAccessLogs(); loadNotifications(); }
}

// ---------- 可选时段 ----------

async function loadSlots() {
  const r = await api("GET", `/api/rounds/${currentRound}/slots`);
  slotsCache = r.items;
  $("#slots-table tbody").innerHTML = r.items.map(s => `
    <tr>
      <td>${esc(s.teacher_name)}</td>
      <td class="t">${fmtTime(s.start_ts)}</td>
      <td class="t">${fmtTime(s.end_ts)}</td>
      <td><button class="btn small primary" data-slot="${s.id}">选择</button></td>
    </tr>`).join("") || `<tr><td colspan="4" class="muted">暂无可选时段</td></tr>`;
  $$("#slots-table [data-slot]").forEach(b => b.onclick = () => {
    $("#booking-slot").value = b.dataset.slot;
  });
  const slotSel = $("#booking-slot");
  const cur = slotSel.value;
  slotSel.innerHTML = `<option value="">不指定（由教师分配）</option>` +
    r.items.map(s => `<option value="${s.id}">${esc(s.teacher_name)} ·
      ${fmtTime(s.start_ts)}</option>`).join("");
  slotSel.value = cur;
}

$("#booking-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#booking-ok").textContent = "";
  const fd = new FormData(e.target);
  try {
    await api("POST", `/api/rounds/${currentRound}/bookings`, {
      student_id: Number(fd.get("student_id")),
      slot_id: fd.get("slot_id") ? Number(fd.get("slot_id")) : null,
      mode: fd.get("mode"),
      topics: fd.get("topics"),
    });
    $("#booking-ok").textContent = "预约申请已提交，等待教师确认。";
    e.target.reset();
    refreshAll();
  } catch (err) { showError(err.message); }
});

// ---------- 我的预约 / 纪要 ----------

async function loadMyBookings() {
  const r = await api("GET", `/api/rounds/${currentRound}/bookings`);
  $("#my-bookings").innerHTML = r.items.map(b => bookingCard(b)).join("")
    || `<p class="muted">暂无预约</p>`;
  bindBookingCards(r.items);
}

function bookingCard(b) {
  const leave = b.status === "reschedule_pending";
  const cls = leave ? "leave" : (b.status === "requested" ? "pending" : b.status);
  const video = b.mode === "video"
    ? (b.video_url
        ? `<span class="meta">视频入口：<a href="${esc(b.video_url)}" target="_blank"
             rel="noopener">${esc(b.video_url)}</a></span>`
        : `<span class="meta">视频链接已失效，
             <button class="btn small" data-video-refresh="${b.id}">刷新链接</button></span>`)
    : `<span class="meta">线下面谈</span>`;
  return `
  <div class="booking-item ${cls}">
    <strong>${esc(b.student_name)}</strong>
    <span class="badge ${b.status}">${STATUS_CN[b.status] || b.status}</span>
    <div class="meta">教师：${esc(b.teacher_name || "待分配")} ·
      时间：${b.start_ts ? fmtTime(b.start_ts) + " ~ " + fmtTime(b.end_ts) : "待分配"}</div>
    ${video}
    <div class="meta">讨论事项：${esc(b.topics) || "—"}</div>
    <div class="actions">
      ${b.status === "confirmed" ? `
        <button class="btn small" data-reschedule="${b.id}">申请改期</button>
        <button class="btn small warn" data-cancel="${b.id}">取消预约</button>` : ""}
      ${b.status === "requested" ? `
        <button class="btn small warn" data-cancel="${b.id}">取消申请</button>` : ""}
      ${b.minutes_id ? `
        <button class="btn small green" data-minutes="${b.minutes_id}">查看纪要</button>` : ""}
    </div>
  </div>`;
}

function bindBookingCards(items) {
  $$("[data-cancel]").forEach(btn => btn.onclick = async () => {
    const reason = prompt("取消原因（可留空）") ?? "";
    try { await api("POST", `/api/bookings/${btn.dataset.cancel}/cancel`, { reason });
      refreshAll(); } catch (e) { showError(e.message); }
  });
  $$("[data-reschedule]").forEach(btn => btn.onclick = async () => {
    const reason = prompt("改期原因？") ?? "";
    if (!reason.trim()) return;
    try {
      await api("POST", `/api/bookings/${btn.dataset.reschedule}/reschedule`,
        { reason });
      showError(""); await refreshAll();
      alert("已进入改期队列，请在“改期队列”中选择新时段，等待教师批准。");
    } catch (e) { showError(e.message); }
  });
  $$("[data-video-refresh]").forEach(btn => btn.onclick = async () => {
    try { const r = await api("POST",
      `/api/bookings/${btn.dataset.videoRefresh}/video-refresh`, {});
      alert("新视频链接：" + r.url); refreshAll();
    } catch (e) { showError(e.message); }
  });
  $$("[data-minutes]").forEach(btn => btn.onclick = () =>
    openMinutes(Number(btn.dataset.minutes)));
}

// ---------- 改期队列 ----------

async function loadReschedules() {
  const r = await api("GET", `/api/rounds/${currentRound}/reschedules?state=pending`);
  const editable = slotsCache;
  $("#reschedule-list").innerHTML = r.items.map(q => {
    const opts = editable.map(s =>
      `<option value="${s.id}" ${s.id === q.new_slot_id ? "selected" : ""}>
        ${esc(s.teacher_name)} · ${fmtTime(s.start_ts)}</option>`).join("");
    return `
    <div class="booking-item ${q.kind === "teacher_leave" ? "leave" : "pending"}">
      <strong>${esc(q.student_name)}</strong>
      ${q.kind === "teacher_leave"
        ? '<span class="badge cancelled">教师请假</span>'
        : '<span class="badge reschedule_pending">家长改期</span>'}
      <div class="meta">原时段：${q.old_start ? fmtTime(q.old_start) : "—"}
        ${q.new_start ? " → 新意向：" + fmtTime(q.new_start) : ""}</div>
      <div class="meta">原因：${esc(q.reason)}</div>
      <div class="actions">
        <select data-propose="${q.id}"><option value="">选择新时段…</option>${opts}</select>
        ${isTeacher() ? `
          <button class="btn small green" data-approve="${q.id}">批准改期</button>
          ${q.kind === "guardian"
            ? `<button class="btn small warn" data-reject-rq="${q.id}">驳回</button>`
            : ""}` : ""}
      </div>
    </div>`;
  }).join("") || `<p class="muted">改期队列为空</p>`;

  $$("[data-propose]").forEach(sel => sel.onchange = async () => {
    if (!sel.value) return;
    try {
      await api("POST", `/api/reschedules/${sel.dataset.propose}/propose-slot`,
        { slot_id: Number(sel.value) });
      refreshAll();
    } catch (e) { showError(e.message); }
  });
  $$("[data-approve]").forEach(btn => btn.onclick = async () => {
    try {
      const r = await api("POST",
        `/api/reschedules/${btn.dataset.approve}/approve`, {});
      if (r.video_url) alert("视频会谈已生成新链接：" + r.video_url);
      refreshAll();
    } catch (e) { showError(e.message); }
  });
  $$("[data-reject-rq]").forEach(btn => btn.onclick = async () => {
    const reason = prompt("驳回原因（可留空）") ?? "";
    try {
      await api("POST", `/api/reschedules/${btn.dataset.rejectRq}/reject`,
        { reason });
      refreshAll();
    } catch (e) { showError(e.message); }
  });
}

// ---------- 教师日历 ----------

async function loadCalendar() {
  const r = await api("GET", `/api/rounds/${currentRound}/calendar`);
  const groups = {};
  for (const row of r.items) {
    (groups[row.teacher_name] ??= []).push(row);
  }
  $("#calendar").innerHTML = Object.entries(groups).map(([name, rows]) => `
    <div class="cal-group">
      <div class="cal-teacher">${esc(name)}</div>
      ${rows.map(s => `
        <div class="cal-row">
          <span class="cal-time">${fmtTime(s.start_ts)} – ${fmtTime(s.end_ts)}</span>
          <span class="cal-who">
            <span class="badge ${s.status}">${STATUS_CN[s.status]}</span>
            ${s.student_name ? esc(s.student_name) + "（" + (s.mode === "video" ? "视频" : "线下") + "）" : ""}
          </span>
          <span class="cal-actions">${teacherSlotActions(s)}</span>
        </div>`).join("")}
    </div>`).join("");

  $$("[data-confirm]").forEach(btn => btn.onclick = async () => {
    const id = btn.dataset.confirm;
    const slotId = prompt("分配时段 ID（留空使用申请中已选时段）：", "");
    if (slotId === null) return;
    try {
      await api("POST", `/api/bookings/${id}/confirm`,
        slotId ? { slot_id: Number(slotId) } : {});
      refreshAll();
    } catch (e) { showError(e.message); }
  });
  $$("[data-reject]").forEach(btn => btn.onclick = async () => {
    const reason = prompt("驳回原因（可留空）") ?? "";
    try {
      await api("POST", `/api/bookings/${btn.dataset.reject}/reject`, { reason });
      refreshAll();
    } catch (e) { showError(e.message); }
  });
  $$("[data-complete]").forEach(btn => btn.onclick = () =>
    openComplete(Number(btn.dataset.complete)));
  $$("[data-entry]").forEach(btn => btn.onclick = async () => {
    try {
      const r = await api("GET", `/api/bookings/${btn.dataset.entry}/video-entry`);
      alert(r.active ? "视频入口：" + r.url : "视频链接已失效，请让对方刷新后再进入。");
    } catch (e) { showError(e.message); }
  });
}

function teacherSlotActions(s) {
  if (!isTeacher()) return "";   // 管理员只读
  if (s.status === "cancelled") return "";
  if (!s.booking_id) return "";
  if (s.booking_status === "requested")
    return `<button class="btn small green" data-confirm="${s.booking_id}">确认预约</button>
            <button class="btn small warn" data-reject="${s.booking_id}">驳回</button>`;
  if (s.booking_status === "confirmed")
    return (s.mode === "video"
      ? `<button class="btn small" data-entry="${s.booking_id}">视频入口</button>` : "")
      + `<button class="btn small primary" data-complete="${s.booking_id}">结束·记纪要</button>`;
  if (s.booking_status === "reschedule_pending")
    return `<span class="muted small">等待改期（见改期队列）</span>`;
  return "";
}

$("#publish-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const lines = new FormData(e.target).get("lines").split("\n")
    .map(x => x.trim()).filter(Boolean);
  const slots = lines.map(line => {
    const [start, minutes] = line.split(/\s+/);
    const dur = Number(minutes || 20);
    const d = new Date(start.length === 16 ? start + ":00" : start);
    const end = new Date(d.getTime() + dur * 60000);
    // 按本地时间生成 YYYY-MM-DDTHH:MM:SS，避免 toISOString 的时区偏移
    const toLocal = x => {
      const p = n => String(n).padStart(2, "0");
      return `${x.getFullYear()}-${p(x.getMonth() + 1)}-${p(x.getDate())}`
        + `T${p(x.getHours())}:${p(x.getMinutes())}:${p(x.getSeconds())}`;
    };
    return { start_ts: toLocal(d), end_ts: toLocal(end) };
  });
  try {
    await api("POST", `/api/rounds/${currentRound}/slots`, { slots });
    e.target.reset(); refreshAll();
  } catch (err2) { showError(err2.message); }
});

async function loadTeacherOptions() {
  try {
    const p = await api("GET", `/api/rounds/${currentRound}/people`);
    const tlist = p.items.filter(x => x.kind === "教师");
    $("#leave-teacher").innerHTML = tlist.map(t =>
      `<option value="${t.id}">${esc(t.name)}</option>`).join("");
  } catch (_) { /* 非本班教师 */ }
}

$("#leave-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  if (!confirm("确认登记请假？该教师本轮未来时段将全部取消，受影响预约进入改期队列。"))
    return;
  try {
    const r = await api("POST", `/api/rounds/${currentRound}/teacher-leave`, {
      teacher_id: Number(fd.get("teacher_id")), reason: fd.get("reason"),
    });
    alert(`已取消 ${r.cancelled_slots} 个时段，${r.affected_bookings.length} 份预约进入改期队列。`);
    refreshAll();
  } catch (err3) { showError(err3.message); }
});

// ---------- 纪要 ----------

let peopleCache = [];

async function openMinutes(id) {
  let m;
  try { m = await api("GET", `/api/minutes/${id}`); }
  catch (e) { showError(e.message); return; }
  $("#minutes-title").textContent = "会谈纪要";
  $("#minutes-body").innerHTML = `
    <p>${esc(m.summary)}</p>
    ${m.overtime ? '<p><span class="badge cancelled">会谈超时</span></p>' : ""}
    <h4>后续待办（负责人 / 期限）</h4>
    <ul class="todo">${m.action_items.map(a => `
      <li>${esc(a.content)}
        <div class="due">负责人：${esc(a.owner_label)} · 期限：${esc(a.due_date)}
          ${a.state === "done"
            ? '<span class="done-tag">已完成</span>'
            : (a.overdue ? '<span class="overdue-tag">已逾期未完成</span>'
                         : '<span class="due">进行中</span>')}
          <button class="btn small" data-item="${a.id}"
            data-done="${a.state === "open" ? 1 : 0}">
            ${a.state === "open" ? "标记完成" : "重新打开"}</button>
        </div>
      </li>`).join("") || "<li class='muted'>无</li>"}
    </ul>
    ${m.attachments.length ? `<p class="muted small">附件：
      ${m.attachments.map(a => esc(a.filename)).join("、")}（本地虚构附件）</p>` : ""}
    <textarea id="minutes-edit" rows="4" style="display:none"
      ${isTeacher() ? "" : "disabled"}>${esc(m.summary)}</textarea>
  `;
  $("#save-minutes").style.display = isTeacher() ? "" : "none";
  $$("#minutes-body [data-item]").forEach(btn => btn.onclick = async () => {
    try {
      await api("POST",
        `/api/action-items/${btn.dataset.item}/state`,
        { done: btn.dataset.done === "1" });
      $("#minutes-dialog").close(); refreshAll();
      openMinutes(id);
    } catch (e) { showError(e.message); }
  });
  $("#save-minutes").onclick = async () => {
    const summary = $("#minutes-edit").value;
    try {
      await api("POST", `/api/minutes/${id}`, { summary });
      refreshAll(); openMinutes(id);
    } catch (e) { showError(e.message); }
  };
  // 教师可就地切换编辑
  if (isTeacher()) {
    const p = $("#minutes-body p");
    p.style.cursor = "pointer";
    p.title = "点击修改结论";
    p.onclick = () => {
      p.style.display = "none";
      $("#minutes-edit").style.display = "";
    };
  }
  $("#minutes-dialog").showModal();
}

async function openComplete(bookingId) {
  const dlg = $("#complete-dialog");
  const form = $("#complete-form");
  form.reset();
  form.booking_id.value = bookingId;
  const p = await api("GET", `/api/rounds/${currentRound}/people`);
  peopleCache = p.items;
  const box = $("#action-items-edit");
  box.innerHTML = "";
  const addRow = () => {
    const row = document.createElement("div");
    row.className = "action-row";
    row.innerHTML = `
      <input type="text" placeholder="待办内容" class="grow" data-f="content">
      <select data-f="owner_id">${peopleCache.map(x =>
        `<option value="${x.id}">${esc(x.kind)}</option>`).join("")}</select>
      <input type="date" data-f="due_date" style="width:150px">
      <button type="button" class="btn small warn">×</button>`;
    row.querySelector("button").onclick = () => row.remove();
    box.appendChild(row);
  };
  addRow();
  $("#add-action").onclick = addRow;
  dlg.showModal();
}

$("#complete-form").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const form = e.target;
  const items = $$("#action-items-edit .action-row").map(row => {
    const get = f => row.querySelector(`[data-f="${f}"]`).value;
    return { content: get("content"),
             owner_id: Number(get("owner_id")),
             due_date: get("due_date") };
  }).filter(x => x.content || x.due_date);
  const atts = (form.attachments.value || "").split(/[，,]/)
    .map(x => x.trim()).filter(Boolean);
  try {
    await api("POST", `/api/bookings/${form.booking_id.value}/complete`, {
      summary: form.summary.value,
      overtime: form.overtime.checked,
      action_items: items,
      attachments: atts,
    });
    $("#complete-dialog").close();
    refreshAll();
  } catch (err4) { showError(err4.message); }
});

// ---------- 管理：撤销授权 / 访问记录 / 通知 ----------

async function loadGuardianOptions() {
  const sid = Number($("#revoke-student").value);
  // 用预约/people 接口不合适：直接展示种子中的已知监护人——改为后端通用接口
  const r = await api("GET", `/api/students/${sid}/guardians`).catch(() => null);
  if (!r) return;
  $("#revoke-guardian").innerHTML = r.items.map(g =>
    `<option value="${g.user_id}">${esc(g.name)}（${esc(g.relation)}·${g.active ? "在权" : "已撤销"}）</option>`
  ).join("");
  $("#revoke-student").onchange = loadGuardianOptions;
}

$("#revoke-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  if (!confirm("撤销后该账号立即失去该生全部资料与纪要的访问权，确认？")) return;
  try {
    await api("POST", "/api/guardianships/revoke", {
      student_id: Number(fd.get("student_id")),
      guardian_id: Number(fd.get("guardian_id")),
    });
    loadGuardianOptions();
    alert("授权已撤销");
  } catch (err5) { showError(err5.message); }
});

async function loadAccessLogs() {
  if (!isAdmin() && !isTeacher()) return;
  const r = await api("GET", "/api/access-logs");
  $("#access-table tbody").innerHTML = r.items.map(l => `
    <tr><td>${fmtTime(l.at)}</td><td>${esc(l.user_name)}</td>
    <td>${esc(l.action)}</td>
    <td>${l.ok ? '<span class="badge completed">允许</span>'
               : '<span class="badge cancelled">拒绝</span>'}</td></tr>`
  ).join("");
}

async function loadNotifications() {
  const r = await api("GET", "/api/notifications");
  $("#notifications").innerHTML = r.items.slice(0, 12).map(n => `
    <div class="booking-item completed">
      <span class="badge reschedule_pending">${esc(n.kind)}</span>
      <div class="meta">${fmtTime(n.at)}</div>
      <div>${esc(n.body)}</div>
    </div>`).join("") || `<p class="muted">暂无通知</p>`;
}

boot();
