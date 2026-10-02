"""服务端渲染的 HTML 页面。

配色：白 #ffffff / 校园蓝 #1f6f8b 系 / 温和绿 #5a9e6f 系。
窄屏：表格外包横向滚动容器，时间单元格不换行，保证时间完整显示。
"""
from __future__ import annotations

import html
import sqlite3
from urllib.parse import quote

from .services import Service, ACTIVE_BOOKING_STATES

CSS = """
:root{
  --blue:#1f6f8b; --blue-dark:#155066; --blue-soft:#e8f1f5;
  --green:#5a9e6f; --green-dark:#44784f; --green-soft:#eaf4ec;
  --ink:#243238; --muted:#6b7d84; --line:#d8e2e6; --danger:#b34b4b;
  --warn-bg:#fdf3e3; --warn:#9a6a1c;
}
*{box-sizing:border-box}
body{margin:0;font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  color:var(--ink);background:#f6f9fa;font-size:15px;line-height:1.55}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}
header.top{background:var(--blue);color:#fff;padding:10px 16px;
  display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}
header.top .brand{font-weight:700;font-size:17px}
header.top nav a{color:#dcebf1;margin-right:12px}
header.top nav a:hover{color:#fff}
header.top .me{margin-left:auto;font-size:13px;opacity:.95}
main{max-width:1080px;margin:0 auto;padding:16px}
section.card{background:#fff;border:1px solid var(--line);border-radius:10px;
  padding:14px 16px;margin-bottom:16px}
section.card h2{margin:0 0 10px;font-size:16px;color:var(--blue-dark);
  border-left:4px solid var(--green);padding-left:8px}
section.card h3{font-size:15px;color:var(--blue-dark);margin:14px 0 6px}
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;min-width:520px}
th,td{border-bottom:1px solid var(--line);padding:7px 8px;text-align:left;vertical-align:top}
th{background:var(--blue-soft);color:var(--blue-dark);font-weight:600;white-space:nowrap}
td.time,th.time{white-space:nowrap;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;white-space:nowrap}
.b-open{background:var(--green-soft);color:var(--green-dark)}
.b-held{background:var(--blue-soft);color:var(--blue-dark)}
.b-pending{background:var(--warn-bg);color:var(--warn)}
.b-completed{background:#eef1f2;color:#5a6a70}
.b-cancelled{background:#f3ecec;color:var(--danger)}
.b-leave{background:var(--warn-bg);color:var(--warn)}
.btn{display:inline-block;border:0;border-radius:6px;padding:5px 12px;font-size:13px;
  cursor:pointer;background:var(--blue);color:#fff;margin:2px 2px 2px 0}
.btn:hover{background:var(--blue-dark);text-decoration:none}
.btn.green{background:var(--green)} .btn.green:hover{background:var(--green-dark)}
.btn.ghost{background:#fff;color:var(--blue);border:1px solid var(--blue)}
.btn.danger{background:var(--danger)}
.btn.mini{padding:2px 8px;font-size:12px}
input[type=text],input[type=password],input[type=datetime-local],input[type=date],
select,textarea{border:1px solid var(--line);border-radius:6px;padding:6px 8px;
  font-size:14px;font-family:inherit;max-width:100%;background:#fff}
textarea{width:100%;min-height:54px}
label.field{display:block;margin:6px 0;font-size:13px;color:var(--muted)}
label.field input,label.field select,label.field textarea{display:block;margin-top:3px;color:var(--ink)}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
.flash{border-radius:8px;padding:9px 12px;margin-bottom:14px}
.flash.ok{background:var(--green-soft);color:var(--green-dark);border:1px solid #bfe0c6}
.flash.err{background:#fbeeee;color:var(--danger);border:1px solid #e6c3c3}
.muted{color:var(--muted);font-size:13px}
.tag-mode{font-size:12px;color:var(--muted)}
.login-box{max-width:380px;margin:8vh auto;background:#fff;border:1px solid var(--line);
  border-radius:12px;padding:24px}
.login-box h1{margin:0 0 4px;font-size:20px;color:var(--blue-dark)}
.login-box .sub{color:var(--muted);font-size:13px;margin-bottom:16px}
.demo-accounts{margin-top:14px;font-size:12.5px;color:var(--muted)}
.demo-accounts code{background:var(--blue-soft);padding:0 5px;border-radius:4px;color:var(--blue-dark)}
ul.plain{list-style:none;padding:0;margin:6px 0}
ul.plain li{padding:4px 0;border-bottom:1px dashed var(--line)}
.overdue{color:var(--danger);font-weight:600}
.done{color:var(--muted);text-decoration:line-through}
.kv{font-size:13px;color:var(--muted)}
.pill-group label{margin-right:12px;font-size:14px;white-space:nowrap}
@media (max-width:560px){
  main{padding:10px}
  header.top .me{margin-left:0;width:100%}
  section.card{padding:10px}
}
"""

STATUS_LABELS = {
    "pending": "待确认", "confirmed": "已确认",
    "reschedule_requested": "改期申请中", "teacher_reschedule": "教师请假·待重选",
    "completed": "已完成", "cancelled": "已取消",
    "open": "可约", "held": "已锁定", "teacher_leave": "教师请假",
}
SLOT_BADGE = {"open": "b-open", "held": "b-held", "completed": "b-completed",
              "cancelled": "b-cancelled", "teacher_leave": "b-leave"}
BOOKING_BADGE = {"pending": "b-pending", "confirmed": "b-held",
                 "reschedule_requested": "b-pending",
                 "teacher_reschedule": "b-leave", "completed": "b-completed",
                 "cancelled": "b-cancelled"}


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def badge(status: str, table: dict | None = None) -> str:
    cls = (table or BOOKING_BADGE).get(status, "b-completed")
    label = STATUS_LABELS.get(status, status)
    return f'<span class="badge {cls}">{esc(label)}</span>'


def layout(title: str, user: sqlite3.Row | None, content: str,
           flash_ok: str = "", flash_err: str = "") -> str:
    if user:
        if user["role"] == "teacher":
            nav = ('<nav><a href="/teacher">教师日历</a>'
                   '<a href="/teacher#queue">改期队列</a>'
                   '<a href="/teacher#minutes">纪要</a>'
                   '<a href="/logs">访问记录</a></nav>')
        elif user["role"] == "admin":
            nav = '<nav><a href="/logs">访问记录</a></nav>'
        else:
            nav = ('<nav><a href="/parent#slots">预约时段</a>'
                   '<a href="/parent#mine">我的预约</a>'
                   '<a href="/parent#minutes">我的纪要</a>'
                   '<a href="/parent#todos">后续待办</a></nav>')
        role_label = {"teacher": "教师", "guardian": "监护人", "admin": "管理员"}[user["role"]]
        top = (f'<header class="top"><span class="brand">家长会预约系统</span>{nav}'
               f'<span class="me">{esc(user["display_name"])}（{role_label}）'
               f'<form method="post" action="/logout" style="display:inline;margin-left:10px">'
               f'<button class="btn mini ghost" style="border-color:#bcd5de;color:#fff">退出</button>'
               f'</form></span></header>')
    else:
        top = '<header class="top"><span class="brand">家长会预约系统</span></header>'
    flashes = ""
    if flash_ok:
        flashes += f'<div class="flash ok">{esc(flash_ok)}</div>'
    if flash_err:
        flashes += f'<div class="flash err">{esc(flash_err)}</div>'
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{esc(title)}</title><style>{CSS}</style></head><body>'
            f'{top}<main>{flashes}{content}</main></body></html>')


def login_page(err: str = "") -> str:
    content = f"""
    <div class="login-box">
      <h1>家长会预约系统</h1>
      <div class="sub">班主任发布预约窗口 · 线下 / 视频会谈 · 纪要与后续待办</div>
      <form method="post" action="/login">
        <label class="field">用户名
          <input type="text" name="username" required autofocus style="width:100%"></label>
        <label class="field">密码
          <input type="password" name="password" required style="width:100%"></label>
        <button class="btn green" style="width:100%;padding:8px;margin-top:8px">登录</button>
      </form>
      <div class="demo-accounts">
        演示账号（密码均为 <code>pass1234</code>）：<br>
        班主任 <code>wangzl</code>（王正梁） · 数学教师 <code>lijing</code><br>
        家长 <code>zhang_wei</code> / <code>zhang_min</code>（同一学生两位监护人）<br>
        家长 <code>lin_mu</code> · 已撤销授权 <code>zhou_lan</code>
      </div>
    </div>"""
    return layout("登录", None, content, flash_err=err or "")


def _dt_local(ts: str | None) -> str:
    return ("" if not ts else ts.replace(" ", "T"))[:16]


# --------------------------------------------------------------------- 教师端

def teacher_page(svc: Service, user: sqlite3.Row, round_id: int | None,
                 flash_ok: str = "", flash_err: str = "") -> str:
    classes = svc.classes_for_teacher(user)
    all_rounds = []
    for c in classes:
        for r in svc.rounds_for_class(c["id"]):
            all_rounds.append((c, r))
    all_rounds.sort(key=lambda x: x[1]["id"], reverse=True)
    if round_id is None and all_rounds:
        round_id = all_rounds[0][1]["id"]
    parts = [f'<section class="card"><h2>预约窗口</h2>'
             f'<form method="get" action="/teacher" class="row"><label class="field">选择窗口'
             f'<select name="round_id" onchange="this.form.submit()">']
    for c, r in all_rounds:
        sel = " selected" if r["id"] == round_id else ""
        parts.append(f'<option value="{r["id"]}"{sel}>{esc(c["name"])} · {esc(r["title"])}'
                     f'（{esc(r["open_at"][5:])} ~ {esc(r["close_at"][5:])}）</option>')
    parts.append('</select></label><noscript><button class="btn">切换</button></noscript></form>')

    parts.append('<h3>为班级发布新窗口</h3><form method="post" action="/rounds/publish" class="row">'
                 '<label class="field">班级<select name="class_id">')
    for c in classes:
        parts.append(f'<option value="{c["id"]}">{esc(c["name"])}</option>')
    parts.append('</select></label>'
                 '<label class="field">窗口标题<input type="text" name="title" value="2026秋季家长会" required></label>'
                 '<label class="field">开放起<input type="datetime-local" name="open_at" required></label>'
                 '<label class="field">开放止<input type="datetime-local" name="close_at" required></label>'
                 '<button class="btn green">发布窗口</button></form>')
    parts.append('</section>')

    rnd = svc.get_round(round_id) if round_id else None
    if rnd is None:
        parts.append('<section class="card"><h2>日历</h2><p class="muted">还没有预约窗口，请先发布。</p></section>')
        return layout("教师端", user, "".join(parts), flash_ok=flash_ok, flash_err=flash_err)
    if not svc.is_class_teacher(user, rnd["class_id"]):
        return layout("教师端", user,
                      '<section class="card"><p class="overdue">您不是该班级的任课教师，无权查看。</p></section>',
                      flash_err="访问被拒绝")

    cls = svc.conn.execute("SELECT * FROM classes WHERE id=?", (rnd["class_id"],)).fetchone()

    # 新建时段
    teachers = svc.conn.execute(
        "SELECT t.* FROM teachers t JOIN class_teachers ct ON ct.teacher_id=t.id "
        "WHERE ct.class_id=? ORDER BY t.id", (rnd["class_id"],)).fetchall()
    parts.append(f'<section class="card"><h2>新增班级时段 · {esc(cls["name"])} / {esc(rnd["title"])}</h2>'
                 '<form method="post" action="/slots/create" class="row">'
                 f'<input type="hidden" name="round_id" value="{rnd["id"]}">'
                 '<label class="field">教师<select name="teacher_id">')
    for t in teachers:
        parts.append(f'<option value="{t["id"]}">{esc(t["name"])}（{esc(t["subject"])}）</option>')
    parts.append('</select></label>'
                 '<label class="field">开始<input type="datetime-local" name="start_at" required></label>'
                 '<label class="field">结束<input type="datetime-local" name="end_at" required></label>'
                 '<label class="field">地点/方式<input type="text" name="location" value="本班教室"></label>'
                 '<button class="btn green">添加时段</button></form>'
                 '<p class="muted">同一教师时间重叠的时段会被拒绝。</p></section>')

    # 日历：按教师与时间排列
    rows = svc.calendar(rnd["id"])
    by_teacher: dict[str, list] = {}
    for row in rows:
        by_teacher.setdefault(row["teacher_name"], []).append(row)
    parts.append('<section class="card"><h2>班级日历（按教师 / 时间排列）</h2>')
    for tname in sorted(by_teacher):
        trows = by_teacher[tname]
        subject = trows[0]["subject"]
        parts.append(f'<h3>{esc(tname)} · {esc(subject)}</h3><div class="table-wrap"><table>'
                     '<tr><th class="time">时间</th><th>地点</th><th>状态</th><th>学生 / 监护人</th>'
                     '<th>事项与操作</th></tr>')
        for s in trows:
            time_cell = (f'<td class="time">{esc(s["start_at"][5:])}'
                         f' ～ {esc(s["end_at"][11:])}</td>')
            loc = f'<td>{esc(s["location"])}</td>'
            status_cell = f'<td>{badge(s["status"], SLOT_BADGE)}</td>'
            if s["booking_id"]:
                mode = "视频" if s["mode"] == "video" else "线下"
                who = (f'<td>{esc(s["student_name"])}<br><span class="muted">{esc(s["guardian_name"])}'
                       f' · {badge(s["booking_status"])}</span></td>')
                acts = [f'<div class="tag-mode">{mode} · {esc(s["topics"] or "（未填写讨论事项）")}</div>']
                bid = s["booking_id"]
                if s["booking_status"] == "pending":
                    acts.append(f'<form method="post" action="/bookings/{bid}/confirm" style="display:inline">'
                                f'<button class="btn mini green">确认并生成时段</button></form>')
                if s["booking_status"] == "confirmed":
                    acts.append(f'<form method="post" action="/bookings/{bid}/complete" style="display:inline">'
                                f'<button class="btn mini">结束会谈</button></form>')
                    if s["mode"] == "video":
                        acts.append(f'<a class="btn mini ghost" href="/video/{bid}">视频入口</a>')
                        acts.append(f'<form method="post" action="/video/{bid}/rotate" style="display:inline">'
                                    f'<button class="btn mini ghost" title="链接失效后重新生成">轮换链接</button></form>')
                if s["booking_status"] in ("confirmed", "reschedule_requested", "completed"):
                    acts.append(f'<a class="btn mini ghost" href="/bookings/{bid}/minutes">会谈纪要</a>')
                if s["booking_status"] not in ("completed", "cancelled"):
                    acts.append(f'<form method="post" action="/bookings/{bid}/cancel" style="display:inline">'
                                f'<button class="btn mini danger">取消</button></form>')
                detail = f'<td>{"".join(acts)}</td>'
            else:
                who = '<td class="muted">—</td>'
                detail = '<td class="muted">无预约</td>'
            parts.append(f'<tr>{time_cell}{loc}{status_cell}{who}{detail}</tr>')
        parts.append('</table></div>')
    parts.append('</section>')

    # 改期队列
    q = [x for x in svc.reschedule_queue(rnd["class_id"])]
    parts.append('<section class="card" id="queue"><h2>改期队列</h2>')
    if not q:
        parts.append('<p class="muted">暂无改期申请。</p>')
    else:
        open_slots = svc.open_slots(rnd["id"])
        opts = "".join(f'<option value="{s["id"]}">{esc(s["start_at"])} '
                       f'{esc(s["teacher_name"])}</option>' for s in open_slots)
        parts.append('<div class="table-wrap"><table><tr><th>学生</th><th>来源</th><th>原时间</th>'
                     '<th>期望时间</th><th>原因</th><th>状态</th><th>处理</th></tr>')
        for item in q:
            src = "监护人申请" if item["source"] == "guardian" else "教师请假"
            tgt = esc(item["target_start"] or "（待选）")
            st = {"pending": "待处理", "approved": "已通过", "rejected": "已驳回",
                  "cancelled": "已取消"}[item["status"]]
            if item["status"] == "pending":
                action = (f'<form method="post" action="/reschedule/{item["id"]}/approve" class="row">'
                          f'<select name="target_slot_id"><option value="">选择新时段…</option>{opts}</select>'
                          f'<button class="btn mini green">通过</button></form>'
                          f'<form method="post" action="/reschedule/{item["id"]}/reject" style="margin-top:4px">'
                          f'<button class="btn mini danger">驳回</button></form>')
            else:
                action = esc(st) + (f'<div class="muted">{esc(item["note"])}</div>' if item["note"] else "")
            parts.append(f'<tr><td>{esc(item["student_name"])}</td><td>{src}</td>'
                         f'<td class="time">{esc(item["old_start"])}</td>'
                         f'<td class="time">{tgt}</td><td>{esc(item["reason"])}</td>'
                         f'<td>{esc(st)}</td><td>{action}</td></tr>')
        parts.append('</table></div>')
    parts.append('</section>')

    # 教师请假
    parts.append('<section class="card"><h2>教师请假登记</h2>'
                 '<form method="post" action="/leave" class="row">'
                 '<label class="field">教师<select name="teacher_id">')
    my_tid = svc.teacher_id_of(user)
    for t in teachers:
        sel = " selected" if t["id"] == my_tid else ""
        parts.append(f'<option value="{t["id"]}"{sel}>{esc(t["name"])}</option>')
    parts.append('</select></label>'
                 '<label class="field">请假起<input type="datetime-local" name="start_at" required></label>'
                 '<label class="field">请假止<input type="datetime-local" name="end_at" required></label>'
                 '<label class="field">原因<input type="text" name="reason" value="临时教研会议"></label>'
                 '<button class="btn">登记请假并通知家长改期</button></form>'
                 '<p class="muted">请假时段内的有效预约自动进入改期队列，空闲时段取消，家长端可自助重选。</p>'
                 '</section>')

    # 监护授权管理
    students = svc.conn.execute(
        "SELECT * FROM students WHERE class_id=? ORDER BY id", (rnd["class_id"],)).fetchall()
    parts.append('<section class="card"><h2>监护授权管理</h2><div class="table-wrap"><table>'
                 '<tr><th>学生</th><th>监护人</th><th>关系</th><th>状态</th><th>操作</th></tr>')
    for st in students:
        gs = svc.conn.execute(
            "SELECT g.*, u.display_name FROM guardianships g JOIN users u ON u.id=g.guardian_user_id"
            " WHERE g.student_id=? ORDER BY g.id", (st["id"],)).fetchall()
        for g in gs:
            state = ('<span class="badge b-open">有效</span>' if not g["revoked"]
                     else '<span class="badge b-cancelled">已撤销</span>')
            btn = (f'<form method="post" action="/guardians/revoke">'
                   f'<input type="hidden" name="student_id" value="{st["id"]}">'
                   f'<input type="hidden" name="guardian_user_id" value="{g["guardian_user_id"]}">'
                   f'<button class="btn mini danger" '
                   f'{"disabled" if g["revoked"] else ""}>撤销授权</button></form>'
                   if not g["revoked"] else "")
            parts.append(f'<tr><td>{esc(st["name"])}</td><td>{esc(g["display_name"])}</td>'
                         f'<td>{esc(g["relation"])}</td><td>{state}</td><td>{btn}</td></tr>')
    parts.append('</table></div><p class="muted">撤销后旧账号立即无法查看该学生的任何纪要与资料。</p></section>')

    # 纪要 + 后续待办
    minutes = svc.minutes_visible_to(user)
    minutes = [m for m in minutes if m["class_id"] == rnd["class_id"]]
    parts.append('<section class="card" id="minutes"><h2>纪要与后续待办</h2>')
    if not minutes:
        parts.append('<p class="muted">暂无纪要。</p>')
    else:
        parts.append('<div class="table-wrap"><table><tr><th class="time">会谈时间</th><th>学生</th>'
                     '<th>教师</th><th>结论</th><th>后续事项</th><th>锁定</th><th></th></tr>')
        for m in minutes:
            fus = svc.follow_ups_for_minute(m["id"])
            fu_spans = []
            for f in fus:
                is_overdue = f["status"] == "open" and f["due_date"] < "2026-10-02"
                cls = "overdue" if is_overdue else ""
                mark = "✓" if f["status"] == "done" else "○"
                fu_spans.append(
                    f'<span class="{cls}">{mark} {esc(f["content"])}'
                    f'（负责人：{esc(f["owner_label"])}；期限 {esc(f["due_date"])}）</span>')
            fu_html = "<br>".join(fu_spans) or '<span class="muted">无</span>'
            lock = "🔒 已锁定" if m["finalized"] else "草稿"
            parts.append(f'<tr><td class="time">{esc(m["start_at"])}</td><td>{esc(m["student_name"])}</td>'
                         f'<td>{esc(m["teacher_name"])}</td>'
                         f'<td>{esc(m["conclusions"][:60])}{"…" if len(m["conclusions"])>60 else ""}</td>'
                         f'<td>{fu_html}</td><td>{lock}</td>'
                         f'<td><a class="btn mini ghost" href="/bookings/{m["booking_id"]}/minutes">打开</a></td></tr>')
        parts.append('</table></div>')
    parts.append('</section>')

    return layout("教师端", user, "".join(parts), flash_ok=flash_ok, flash_err=flash_err)


def minute_editor(svc: Service, user: sqlite3.Row, booking: sqlite3.Row,
                  flash_ok: str, flash_err: str) -> str:
    minute = svc.get_minute_by_booking(booking["id"])
    slot = svc.conn.execute(
        "SELECT ts.*, t.name AS teacher_name FROM time_slots ts JOIN teachers t ON t.id=ts.teacher_id"
        " WHERE ts.id=?", (booking["slot_id"],)).fetchone()
    head = (f'<section class="card"><h2>会谈纪要 · {esc(booking["student_name"])}</h2>'
            f'<p class="kv">时间 {esc(slot["start_at"])} ～ {esc(slot["end_at"])} · '
            f'教师 {esc(slot["teacher_name"])} · {"视频" if booking["mode"]=="video" else "线下"} · '
            f'{badge(booking["status"])}</p></section>')

    is_teacher = user["role"] == "teacher" and svc.is_class_teacher(user, booking["class_id"])
    is_guardian = svc.active_guardianship(user["id"], booking["student_id"]) is not None
    if not (is_teacher or is_guardian):
        return layout("无权访问", user, head +
                      '<section class="card"><p class="overdue">纪要只对本班任课教师和监护人开放。'
                      '本次访问已记录。</p></section>', flash_err="访问被拒绝")

    conclusions = minute["conclusions"] if minute else ""
    fus = svc.follow_ups_for_minute(minute["id"]) if minute else []
    locked = bool(minute and minute["finalized"])

    parts = [head]
    if is_teacher:
        if locked:
            parts.append('<section class="card"><h2>结论（已锁定）</h2>'
                         f'<p>{esc(conclusions).replace(chr(10), "<br>") or "—"}</p>'
                         '<p class="muted">纪要锁定后不可修改。</p></section>')
        else:
            parts.append(
                '<section class="card"><h2>记录双方约定的后续事项</h2>'
                f'<form method="post" action="/bookings/{booking["id"]}/minutes">'
                '<label class="field">会谈结论'
                f'<textarea name="conclusions" required>{esc(conclusions)}</textarea></label>'
                '<h3>后续事项（每条必须写明负责人与期限）</h3>'
                '<div id="fus">')
            rows = fus or [None]
            for i in range(max(3, len(rows))):
                f = rows[i] if i < len(rows) else None
                parts.append(
                    '<div class="row" style="margin-bottom:6px">'
                    '<input type="text" name="content" placeholder="事项内容" style="flex:2;min-width:160px"'
                    f' value="{esc(f["content"] if f else "")}">'
                    '<input type="text" name="owner_label" placeholder="负责人" style="flex:1;min-width:90px"'
                    f' value="{esc(f["owner_label"] if f else "")}">'
                    '<input type="date" name="due_date" style="flex:1" '
                    f'value="{esc(f["due_date"] if f else "")}"></div>')
            parts.append('</div>'
                         '<label class="field">会谈超时（分钟，可选）'
                         '<input type="text" name="overtime_minutes" inputmode="numeric"'
                         f' value="{minute["overtime_minutes"] if minute and minute["overtime_minutes"] else ""}"></label>'
                         '<button class="btn green" name="action" value="save">保存纪要</button> '
                         '<button class="btn" name="action" value="finalize">保存并锁定</button>'
                         '</form></section>')
    else:
        conclusions_html = esc(conclusions).replace(chr(10), "<br>") or "（教师尚未记录）"
        lock_note = "<p class='muted'>🔒 纪要已锁定</p>" if locked else ""
        parts.append('<section class="card"><h2>会谈结论</h2>'
                     f'<p>{conclusions_html}</p>{lock_note}</section>')

    # 后续待办
    parts.append('<section class="card"><h2>后续待办</h2>')
    if not fus:
        parts.append('<p class="muted">暂无后续事项。</p>')
    else:
        parts.append('<ul class="plain">')
        for f in fus:
            cls = "done" if f["status"] == "done" else (
                "overdue" if f["due_date"] < "2026-10-02" else "")
            mark = "✓" if f["status"] == "done" else "○"
            done_btn = ""
            if f["status"] == "open" and (f["owner_user_id"] == user["id"] or is_teacher):
                done_btn = (f'<form method="post" action="/followups/{f["id"]}/done" style="display:inline">'
                            '<button class="btn mini green">标记完成</button></form>')
            parts.append(f'<li class="{cls}">{mark} {esc(f["content"])} '
                         f'（负责人：{esc(f["owner_label"])}；期限 {esc(f["due_date"])}） {done_btn}</li>')
        parts.append('</ul>')
    parts.append('</section>')

    # 附件
    try:
        atts = svc.attachments_for_student(user, booking["student_id"])
    except Exception:
        atts = []
    parts.append('<section class="card"><h2>学生资料（虚构附件）</h2>')
    if atts:
        parts.append('<ul class="plain">')
        for a in atts:
            parts.append(f'<li>📎 {esc(a["filename"])} '
                         f'<a class="btn mini ghost" href="/attachments/{a["id"]}/download">查看</a></li>')
        parts.append('</ul>')
    else:
        parts.append('<p class="muted">暂无附件。</p>')
    parts.append('</section>')

    return layout("会谈纪要", user, "".join(parts), flash_ok=flash_ok, flash_err=flash_err)


# --------------------------------------------------------------------- 家长端

def parent_page(svc: Service, user: sqlite3.Row,
                flash_ok: str = "", flash_err: str = "") -> str:
    my_students = svc.students_for_guardian(user)
    parts = []
    flashes = ""
    if flash_ok:
        flashes += f'<div class="flash ok">{esc(flash_ok)}</div>'
    if flash_err:
        flashes += f'<div class="flash err">{esc(flash_err)}</div>'
    parts.append(flashes)
    if not my_students:
        parts.append('<section class="card"><h2>我的孩子</h2>'
                     '<p class="muted">您名下当前没有有效的监护授权。若授权被撤销，'
                     '历史纪要也将无法查看。</p></section>')
        return layout("家长端", user, "".join(parts), flash_ok=flash_ok, flash_err=flash_err)

    # 通知
    notes = svc.notifications_for(user)
    unread = [n for n in notes if not n["is_read"]]
    if unread:
        rows = "".join(f'<li><b>{esc(n["title"])}</b>：{esc(n["body"])}'
                       f'<span class="muted">（{esc(n["created_at"][5:16])}）</span></li>'
                       for n in unread[:8])
        parts.append(f'<section class="card"><h2>最新通知（{len(unread)}）</h2>'
                     f'<ul class="plain">{rows}</ul>'
                     '<form method="post" action="/notifications/read">'
                     '<button class="btn mini ghost">全部标为已读</button></form></section>')
        svc.mark_notifications_read(user)

    # 可选时段（按学生班级当前窗口）
    parts.append('<section class="card" id="slots"><h2>可选时段（家长端仅显示可约时段）</h2>')
    for st in my_students:
        rnd = svc.latest_round(st["class_id"])
        if not rnd:
            continue
        slots = svc.open_slots(rnd["id"])
        parts.append(f'<h3>{esc(st["name"])}（{esc(st["relation"])}）· {esc(rnd["title"])}</h3>')
        active = svc.active_booking_for_student(st["id"], rnd["id"])
        if active:
            parts.append(f'<p class="muted">该轮已有一份有效预约'
                         f'（{badge(active["status"])}），取消或改期后才能重新预约。</p>')
        elif not slots:
            parts.append('<p class="muted">暂无可选时段。</p>')
        else:
            opts = "".join(f'<option value="{s["id"]}">{esc(s["start_at"])} ～ {esc(s["end_at"][11:])}'
                           f' {esc(s["teacher_name"])}（{esc(s["subject"])}） @ {esc(s["location"])}</option>'
                           for s in slots)
            parts.append(
                f'<form method="post" action="/bookings" class="row">'
                f'<input type="hidden" name="student_id" value="{st["id"]}">'
                f'<label class="field">选择时段<select name="slot_id">{opts}</select></label>'
                '<span class="pill-group field">会谈方式'
                '<label><input type="radio" name="mode" value="offline" checked> 线下</label>'
                '<label><input type="radio" name="mode" value="video"> 视频</label></span>'
                '<button class="btn green">预约</button>'
                '<label class="field" style="flex-basis:100%">想讨论的事项'
                '<textarea name="topics" placeholder="例如：课堂专注情况、数学作业安排……"></textarea></label>'
                '</form>')
    parts.append('</section>')

    # 我的预约
    bookings = svc.bookings_for_guardian(user)
    parts.append('<section class="card" id="mine"><h2>我的预约</h2>')
    if not bookings:
        parts.append('<p class="muted">还没有预约记录。</p>')
    else:
        parts.append('<div class="table-wrap"><table><tr><th class="time">时间</th><th>班级/学生</th>'
                     '<th>教师</th><th>方式</th><th>状态</th><th>操作</th></tr>')
        for b in bookings:
            open_slots = svc.open_slots(b["round_id"])
            opts = "".join(f'<option value="{s["id"]}">{esc(s["start_at"])}</option>'
                           for s in open_slots)
            acts = []
            if b["status"] in ("pending", "confirmed"):
                if opts:
                    acts.append(
                        f'<form method="post" action="/bookings/{b["id"]}/reschedule" class="row">'
                        f'<select name="target_slot_id">{opts}</select>'
                        f'<button class="btn mini">申请改期</button></form>')
                acts.append(f'<form method="post" action="/bookings/{b["id"]}/cancel" style="display:inline">'
                            '<button class="btn mini danger">取消</button></form>')
            if b["status"] == "teacher_reschedule":
                if opts:
                    acts.append('<div class="badge b-leave">教师请假，请重选时段</div>')
                    queue_item = svc.conn.execute(
                        "SELECT id FROM reschedule_queue WHERE booking_id=? AND source='teacher_leave'"
                        " AND status='pending' ORDER BY id DESC LIMIT 1", (b["id"],)).fetchone()
                    if queue_item:
                        acts.append(
                            f'<form method="post" action="/reschedule/{queue_item["id"]}/pick" class="row">'
                            f'<select name="target_slot_id">{opts}</select>'
                            f'<button class="btn mini green">自助重选</button></form>')
                else:
                    acts.append('<span class="overdue">暂无可换时段，请等待教师新增</span>')
            if b["status"] == "confirmed" and b["mode"] == "video":
                acts.append(f'<a class="btn mini ghost" href="/video/{b["id"]}">进入视频</a>')
            if b["status"] in ACTIVE_BOOKING_STATES + ("completed",):
                acts.append(f'<a class="btn mini ghost" href="/bookings/{b["id"]}/minutes">我的纪要</a>')
            mode = "📹 视频" if b["mode"] == "video" else "🏫 线下"
            parts.append(f'<tr><td class="time">{esc(b["start_at"])}<br>{esc(b["start_at"][:10])} '
                         f'～ {esc(b["end_at"][11:])}</td>'
                         f'<td>{esc(b["class_name"])}<br>{esc(b["student_name"])}</td>'
                         f'<td>{esc(b["teacher_name"])}</td><td>{mode}</td>'
                         f'<td>{badge(b["status"])}</td><td>{"".join(acts) or "—"}</td></tr>')
        parts.append('</table></div>')
    parts.append('</section>')

    # 我的改期队列
    q = svc.guardian_reschedule_queue(user)
    if q:
        parts.append('<section class="card"><h2>改期记录</h2><div class="table-wrap"><table>'
                     '<tr><th>学生</th><th>来源</th><th class="time">原时间</th>'
                     '<th class="time">目标时间</th><th>状态</th><th>备注</th></tr>')
        smap = {"pending": "待处理", "approved": "已通过", "rejected": "已驳回", "cancelled": "已取消"}
        for item in q:
            src = "我发起的" if item["source"] == "guardian" else "教师请假"
            parts.append(f'<tr><td>{esc(item["student_name"])}</td><td>{src}</td>'
                         f'<td class="time">{esc(item["old_start"])}</td>'
                         f'<td class="time">{esc(item["target_start"] or "—")}</td>'
                         f'<td>{smap[item["status"]]}</td><td>{esc(item["note"])}</td></tr>')
        parts.append('</table></div></section>')

    # 我的纪要（只可能看到当前有效监护关系下的纪要）
    minutes = svc.minutes_visible_to(user)
    parts.append('<section class="card" id="minutes"><h2>我的纪要</h2>')
    if not minutes:
        parts.append('<p class="muted">没有可见纪要。授权撤销后，旧账号不能再查看历史纪要。</p>')
    else:
        parts.append('<ul class="plain">')
        for m in minutes:
            fus = svc.follow_ups_for_minute(m["id"])
            fu = "；".join(f'{esc(f["content"])}（{esc(f["owner_label"])}，{esc(f["due_date"])}）' for f in fus)
            parts.append(f'<li><a href="/bookings/{m["booking_id"]}/minutes">'
                         f'{esc(m["start_at"])} · {esc(m["student_name"])} · {esc(m["teacher_name"])}</a>'
                         f'<div class="muted">{esc(m["conclusions"][:70])}</div>'
                         f'<div class="muted">后续：{fu or "无"}</div></li>')
        parts.append('</ul>')
    parts.append('</section>')

    # 后续待办
    todos = svc.my_follow_ups(user)
    parts.append('<section class="card" id="todos"><h2>后续待办（负责人是我）</h2>')
    if not todos:
        parts.append('<p class="muted">没有分派给您的待办。</p>')
    else:
        parts.append('<ul class="plain">')
        for f in todos:
            cls = "done" if f["status"] == "done" else (
                "overdue" if f["due_date"] < "2026-10-02" else "")
            btn = ""
            if f["status"] == "open":
                btn = (f'<form method="post" action="/followups/{f["id"]}/done" style="display:inline">'
                       '<button class="btn mini green">完成</button></form>')
            parts.append(f'<li class="{cls}">{esc(f["student_name"])}：{esc(f["content"])} '
                         f'（期限 {esc(f["due_date"])}） {btn}</li>')
        parts.append('</ul>')
    parts.append('</section>')

    return layout("家长端", user, "".join(parts), flash_ok=flash_ok, flash_err=flash_err)


def video_page(svc: Service, user: sqlite3.Row, booking_id: int,
               flash_ok: str = "", flash_err: str = "") -> str:
    b = svc.get_booking(booking_id)
    content = '<section class="card"><h2>视频入口</h2>'
    if not b:
        content += '<p class="overdue">预约不存在。</p></section>'
        return layout("视频", user, content, flash_err=flash_err)
    entry = svc.video.active_entry(booking_id)
    content += f'<p class="kv">学生 {esc(b["student_name"])} · 预约状态 {badge(b["status"])}</p>'
    if entry is None:
        content += ('<p class="overdue">当前视频链接已失效。</p>'
                    '<p class="muted">请联系班主任点击“轮换链接”生成新入口；改期后系统也会自动换新链接。</p>')
    else:
        content += (f'<p>会议号：<code>{esc(entry["meeting_code"])}</code></p>'
                    f'<p>模拟链接：<a href="{esc(entry["join_url"])}">{esc(entry["join_url"])}</a></p>'
                    f'<form method="post" action="/video/{booking_id}/join">'
                    '<button class="btn green">进入模拟视频间</button></form>'
                    '<p class="muted">本系统使用模拟视频适配器，链接不会真正呼起外部会议软件。</p>')
    content += '</section>'
    return layout("视频入口", user, content, flash_ok=flash_ok, flash_err=flash_err)


def logs_page(svc: Service, user: sqlite3.Row) -> str:
    rows = svc.conn.execute(
        "SELECT l.* FROM access_logs l ORDER BY l.id DESC LIMIT 200").fetchall()
    body = ['<section class="card"><h2>访问记录（含被拒绝的尝试）</h2><div class="table-wrap"><table>'
            '<tr><th>时间</th><th>用户</th><th>动作</th><th>对象</th><th>结果</th><th>详情</th></tr>']
    for r in rows:
        res = ('<span class="badge b-open">允许</span>' if r["granted"]
               else '<span class="badge b-cancelled">拒绝</span>')
        body.append(f'<tr><td class="time">{esc(r["ts"][5:])}</td><td>{esc(r["username"] or "—")}</td>'
                    f'<td>{esc(r["action"])}</td><td>{esc(r["entity_type"])} {esc(r["entity_id"] or "")}</td>'
                    f'<td>{res}</td><td>{esc(r["detail"])}</td></tr>')
    body.append('</table></div></section>')
    return layout("访问记录", user, "".join(body))
