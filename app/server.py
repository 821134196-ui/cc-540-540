"""HTTP 入口：表单页面 + JSON API，全部使用标准库。"""
from __future__ import annotations

import json
import sqlite3
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse, quote

from .adapters import get_adapters
from .db import connect, init_db
from .pages import (login_page, teacher_page, parent_page, minute_editor,
                    video_page, logs_page, layout)
from .services import Service, ServiceError

DB_PATH = "data/app.db"
ATTACH_ROOT = "data/attachments"


def make_service(db_path: str = DB_PATH, attach_root: str = ATTACH_ROOT):
    conn = connect(db_path, check_same_thread=False)
    init_db(conn)
    _, notifier, video, _store = get_adapters(conn, db_path, attach_root)
    return Service(conn, notifier, video)


class App:
    def __init__(self, svc: Service):
        self.svc = svc

    # -------------------------------------------------------------- 工具

    def _token(self, handler) -> str | None:
        cookie = SimpleCookie(handler.headers.get("Cookie", ""))
        if "session" in cookie:
            return cookie["session"].value
        return None

    def _user(self, handler):
        return self.svc.current_user(self._token(handler))

    def _form(self, handler) -> dict[str, str]:
        length = int(handler.headers.get("Content-Length", 0) or 0)
        raw = handler.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _query(self, handler) -> dict[str, str]:
        q = urlparse(handler.path).query
        return {k: v[0] for k, v in parse_qs(q).items()}

    def _redirect(self, handler, location: str, set_cookie: str | None = None) -> None:
        # Location 头只允许 ASCII：对中文等字符做百分号编码（保留 URL 结构字符）
        from urllib.parse import quote
        location = quote(location, safe=":/?#[]@!$&'()*+,;=%")
        handler.send_response(302)
        handler.send_header("Location", location)
        if set_cookie:
            handler.send_header("Set-Cookie", set_cookie)
        handler.end_headers()

    def _html(self, handler, html_text: str, status: int = 200) -> None:
        body = html_text.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _json(self, handler, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _flash_redirect(self, handler, location, err: Exception | None = None):
        from urllib.parse import quote
        if isinstance(err, ServiceError):
            loc = location + ("&" if "?" in location else "?") + f"err={quote(str(err))}"
        else:
            loc = location
        return self._redirect(handler, loc)

    def _dt(self, value: str) -> str:
        # datetime-local -> "YYYY-MM-DD HH:MM"
        return value.replace("T", " ")[:16]

    # -------------------------------------------------------------- 分发

    def handle(self, handler, method: str) -> None:
        path = urlparse(handler.path).path
        user = self._user(handler)
        try:
            if path == "/" and method == "GET":
                return self._redirect(handler, "/teacher" if user and user["role"] == "teacher"
                                      else "/parent" if user else "/login")
            if path == "/login" and method == "GET":
                return self._html(handler, login_page(self._query(handler).get("err", "")))
            if path == "/login" and method == "POST":
                return self.do_login(handler)
            if path == "/logout" and method == "POST":
                tok = self._token(handler)
                if tok:
                    self.svc.logout(tok)
                self._redirect(handler, "/login",
                               "session=; Path=/; Max-Age=0")
                return

            if user is None:
                if path.startswith("/api/"):
                    return self._json(handler, {"error": "未登录", "code": "unauthorized"}, 401)
                return self._redirect(handler, "/login")

            if path == "/teacher" and method == "GET":
                q = self._query(handler)
                rid = q.get("round_id")
                return self._html(handler, teacher_page(self.svc, user,
                                  int(rid) if rid else None,
                                  flash_ok=q.get("ok", ""), flash_err=q.get("err", "")))
            if path == "/parent" and method == "GET":
                q = self._query(handler)
                return self._html(handler, parent_page(self.svc, user,
                                  flash_ok=q.get("ok", ""), flash_err=q.get("err", "")))
            if path == "/logs" and method == "GET":
                if user["role"] not in ("teacher", "admin"):
                    raise ServiceError("无权查看访问记录", "forbidden")
                return self._html(handler, logs_page(self.svc, user))

            if path == "/rounds/publish" and method == "POST":
                return self.do_publish_round(handler, user)
            if path == "/slots/create" and method == "POST":
                return self.do_create_slot(handler, user)
            if path == "/bookings" and method == "POST":
                return self.do_book(handler, user)
            if path.startswith("/bookings/") and path.endswith("/confirm") and method == "POST":
                return self.simple_action(handler, user, "confirm")
            if path.startswith("/bookings/") and path.endswith("/complete") and method == "POST":
                return self.simple_action(handler, user, "complete")
            if path.startswith("/bookings/") and path.endswith("/cancel") and method == "POST":
                return self.simple_action(handler, user, "cancel")
            if path.startswith("/bookings/") and path.endswith("/reschedule") and method == "POST":
                return self.do_guardian_reschedule(handler, user)
            if path.startswith("/bookings/") and path.endswith("/minutes"):
                bid = int(path.split("/")[2])
                if method == "GET":
                    return self.do_minute_page(handler, user, bid)
                if method == "POST":
                    return self.do_save_minutes(handler, user, bid)
            if path.startswith("/reschedule/") and path.endswith("/approve") and method == "POST":
                return self.do_reschedule_decision(handler, user, "approve")
            if path.startswith("/reschedule/") and path.endswith("/reject") and method == "POST":
                return self.do_reschedule_decision(handler, user, "reject")
            if path.startswith("/reschedule/") and path.endswith("/pick") and method == "POST":
                return self.do_reschedule_decision(handler, user, "pick")
            if path == "/leave" and method == "POST":
                return self.do_leave(handler, user)
            if path == "/guardians/revoke" and method == "POST":
                return self.do_revoke(handler, user)
            if path.startswith("/video/") and method == "GET":
                bid = int(path.split("/")[2])
                return self._html(handler, video_page(self.svc, user, bid))
            if path.startswith("/video/") and path.endswith("/join") and method == "POST":
                return self.do_video_join(handler, user)
            if path.startswith("/video/") and path.endswith("/rotate") and method == "POST":
                return self.do_video_rotate(handler, user)
            if path.startswith("/followups/") and path.endswith("/done") and method == "POST":
                fid = int(path.split("/")[2])
                self.svc.complete_follow_up(user, fid)
                return self._redirect_back(handler)
            if path.startswith("/attachments/") and path.endswith("/download") and method == "GET":
                return self.do_attachment_download(handler, user)
            if path == "/notifications/read" and method == "POST":
                self.svc.mark_notifications_read(user)
                return self._redirect(handler, "/parent")

            if path.startswith("/api/"):
                return self.handle_api(handler, method, path, user)

            handler.send_error(404, "Not Found")
        except ServiceError as e:
            if path.startswith("/api/"):
                return self._json(handler, {"error": str(e), "code": e.code}, 400)
            referer = handler.headers.get("Referer", "/")
            sep = "&" if "?" in referer else "?"
            from urllib.parse import quote
            return self._redirect(handler,
                                  f"{referer.split('#')[0]}{sep}err={quote(str(e))}")
        except Exception as e:  # noqa: BLE001 - 页面端兜底
            if path.startswith("/api/"):
                return self._json(handler, {"error": str(e), "code": "server_error"}, 500)
            raise

    def _redirect_back(self, handler):
        back = handler.headers.get("Referer", "/teacher").split("#")[0]
        return self._redirect(handler, back)

    # -------------------------------------------------------------- 页面动作

    def do_login(self, handler) -> None:
        f = self._form(handler)
        try:
            token, _user = self.svc.login(f.get("username", ""), f.get("password", ""))
        except ServiceError as e:
            return self._html(handler, login_page(str(e)), status=401)
        self._redirect(handler, "/", f"session={token}; Path=/; HttpOnly")

    def do_publish_round(self, handler, user) -> None:
        f = self._form(handler)
        rid = self.svc.publish_round(
            user, int(f["class_id"]), f["title"].strip(),
            self._dt(f["open_at"]), self._dt(f["close_at"]))
        self._redirect(handler, f"/teacher?round_id={rid}&ok=窗口已发布")

    def do_create_slot(self, handler, user) -> None:
        f = self._form(handler)
        rid = int(f["round_id"])
        self.svc.create_slot(user, rid, int(f["teacher_id"]),
                             self._dt(f["start_at"]), self._dt(f["end_at"]),
                             f.get("location", "本班教室").strip() or "本班教室")
        self._redirect(handler, f"/teacher?round_id={rid}&ok=时段已添加")

    def do_book(self, handler, user) -> None:
        f = self._form(handler)
        bid = self.svc.book_slot(user, int(f["slot_id"]), int(f["student_id"]),
                                 f.get("mode", "offline"), f.get("topics", ""))
        self._redirect(handler, f"/parent#mine&ok=预约已提交（编号 {bid}），等待教师确认")

    def simple_action(self, handler, user, action: str) -> None:
        path = urlparse(handler.path).path
        bid = int(path.split("/")[2])
        if action == "confirm":
            self.svc.confirm_booking(user, bid)
            msg = "已确认并生成时段；视频预约已创建会议入口"
        elif action == "complete":
            ot = self.svc.complete_booking(user, bid)
            msg = f"会谈已结束" + (f"（超时 {ot} 分钟），请记录纪要" if ot else "，请记录纪要")
        elif action == "cancel":
            f = self._form(handler)
            self.svc.cancel_booking(user, bid, f.get("reason", ""))
            msg = "预约已取消，时段已释放"
        back = handler.headers.get("Referer", "/teacher").split("#")[0]
        sep = "&" if "?" in back else "?"
        self._redirect(handler, f"{back}{sep}ok={msg}")

    def do_guardian_reschedule(self, handler, user) -> None:
        path = urlparse(handler.path).path
        bid = int(path.split("/")[2])
        f = self._form(handler)
        target = int(f["target_slot_id"]) if f.get("target_slot_id") else None
        self.svc.guardian_request_reschedule(user, bid, target, f.get("reason", ""))
        self._redirect(handler, "/parent#mine&ok=改期申请已提交，等待教师处理")

    def do_reschedule_decision(self, handler, user, kind: str) -> None:
        path = urlparse(handler.path).path
        qid = int(path.split("/")[2])
        f = self._form(handler)
        if kind == "approve":
            target = int(f["target_slot_id"]) if f.get("target_slot_id") else None
            self.svc.approve_reschedule(user, qid, target)
            msg = "改期已通过"
        elif kind == "reject":
            self.svc.reject_reschedule(user, qid, f.get("note", ""))
            msg = "已驳回"
        else:
            self.svc.guardian_pick_new_slot(user, qid, int(f["target_slot_id"]))
            msg = "重选成功"
        back = handler.headers.get("Referer", "/teacher").split("#")[0]
        sep = "&" if "?" in back else "?"
        self._redirect(handler, f"{back}{sep}ok={msg}")

    def do_leave(self, handler, user) -> None:
        f = self._form(handler)
        result = self.svc.report_teacher_leave(
            user, int(f["teacher_id"]), self._dt(f["start_at"]),
            self._dt(f["end_at"]), f.get("reason", ""))
        n = len(result["queued_bookings"])
        self._redirect(handler,
                       f"/teacher#queue&ok={result['teacher']} 请假已登记，{n} 个预约进入改期队列")

    def do_revoke(self, handler, user) -> None:
        f = self._form(handler)
        self.svc.revoke_guardianship(user, int(f["student_id"]),
                                     int(f["guardian_user_id"]))
        back = handler.headers.get("Referer", "/teacher").split("#")[0]
        sep = "&" if "?" in back else "?"
        self._redirect(handler, f"{back}{sep}ok=授权已撤销，旧账号立即失去纪要访问权")

    def do_minute_page(self, handler, user, bid: int) -> None:
        b = self.svc.get_booking(bid)
        if not b:
            return self._html(handler, layout("未找到", user,
                              '<section class="card"><p>预约不存在。</p></section>'), 404)
        q = self._query(handler)
        return self._html(handler, minute_editor(
            self.svc, user, b, q.get("ok", ""), q.get("err", "")))

    def do_save_minutes(self, handler, user, bid: int) -> None:
        length = int(handler.headers.get("Content-Length", 0) or 0)
        raw = handler.rfile.read(length).decode("utf-8") if length else ""
        form = parse_qs(raw, keep_blank_values=True)
        follow_ups = []
        for content, owner, due in zip(form.get("content", []),
                                       form.get("owner_label", []),
                                       form.get("due_date", [])):
            if not (content.strip() or owner.strip() or due.strip()):
                continue  # 空行
            follow_ups.append({"content": content, "owner_label": owner,
                               "due_date": due})
        ot_raw = (form.get("overtime_minutes", [""])[0] or "").strip()
        overtime = int(ot_raw) if ot_raw.isdigit() else None
        finalize = form.get("action", ["save"])[0] == "finalize"
        self.svc.save_minutes(user, bid, form.get("conclusions", [""])[0],
                              follow_ups, overtime_minutes=overtime, finalize=finalize)
        self._redirect(handler, f"/bookings/{bid}/minutes?ok=纪要已保存"
                       + ("并锁定" if finalize else ""))

    def do_video_join(self, handler, user) -> None:
        path = urlparse(handler.path).path
        bid = int(path.split("/")[2])
        entry = self.svc.join_video(user, bid)
        self._redirect(handler, f"/video/{bid}?ok=已进入会议 {entry['meeting_code']}")

    def do_video_rotate(self, handler, user) -> None:
        path = urlparse(handler.path).path
        bid = int(path.split("/")[2])
        self.svc.rotate_video(user, bid)
        back = handler.headers.get("Referer", f"/video/{bid}").split("#")[0]
        sep = "&" if "?" in back else "?"
        self._redirect(handler, f"{back}{sep}ok=视频链接已轮换并通知家长")

    def do_attachment_download(self, handler, user) -> None:
        path = urlparse(handler.path).path
        aid = int(path.split("/")[2])
        a = self.svc.view_attachment(user, aid)
        from pathlib import Path
        p = Path(a["local_path"])
        if not p.exists():
            handler.send_error(410, "本地附件文件缺失")
            return
        data = p.read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Content-Disposition",
                            f"attachment; filename*=UTF-8''{quote(a['filename'])}")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    # -------------------------------------------------------------- JSON API

    def handle_api(self, handler, method: str, path: str, user) -> None:
        if path == "/api/slots" and method == "GET":
            q = self._query(handler)
            round_id = int(q["round_id"])
            as_teacher = q.get("view") == "teacher"
            if as_teacher:
                rnd = self.svc.get_round(round_id)
                if not rnd or not self.svc.is_class_teacher(user, rnd["class_id"]):
                    raise ServiceError("无权查看教师日历", "forbidden")
                rows = [dict(r) for r in self.svc.calendar(round_id)]
            else:
                rows = [dict(r) for r in self.svc.open_slots(round_id)]
            return self._json(handler, {"slots": rows})

        if path == "/api/bookings" and method in ("GET", "POST"):
            if method == "GET":
                if user["role"] == "guardian":
                    rows = [dict(r) for r in self.svc.bookings_for_guardian(user)]
                else:
                    q = self._query(handler)
                    cid = int(q["class_id"])
                    self.svc.require_class_teacher(user, cid)
                    rows = [dict(r) for r in self.svc.conn.execute(
                        "SELECT b.*, s.name AS student_name FROM bookings b"
                        " JOIN students s ON s.id=b.student_id WHERE s.class_id=?",
                        (cid,)).fetchall()]
                return self._json(handler, {"bookings": rows})
            length = int(handler.headers.get("Content-Length", 0) or 0)
            data = json.loads(handler.rfile.read(length).decode() or "{}")
            bid = self.svc.book_slot(user, int(data["slot_id"]), int(data["student_id"]),
                                     data.get("mode", "offline"), data.get("topics", ""))
            return self._json(handler, {"booking_id": bid}, 201)

        if path.startswith("/api/reschedule/queue") and method == "GET":
            q = self._query(handler)
            class_id = int(q["class_id"]) if q.get("class_id") else None
            if class_id is not None:
                self.svc.require_class_teacher(user, class_id)
            elif user["role"] != "admin":
                raise ServiceError("需要指定班级", "forbidden")
            return self._json(handler, {"queue": [dict(r) for r in
                                                  self.svc.reschedule_queue(class_id)]})

        if path == "/api/leave" and method == "POST":
            length = int(handler.headers.get("Content-Length", 0) or 0)
            data = json.loads(handler.rfile.read(length).decode() or "{}")
            result = self.svc.report_teacher_leave(
                user, int(data["teacher_id"]), data["start_at"],
                data["end_at"], data.get("reason", ""))
            return self._json(handler, result)

        if path.startswith("/api/video/"):
            parts = path.strip("/").split("/")
            bid = int(parts[2])
            if method == "POST" and len(parts) == 4 and parts[3] == "rotate":
                return self._json(handler, self.svc.rotate_video(user, bid))
            entry = self.svc.join_video(user, bid)
            return self._json(handler, entry)

        if path.startswith("/api/minutes/") and method == "GET":
            mid = int(path.rsplit("/", 1)[1])
            m = self.svc.view_minute(user, mid)
            return self._json(handler, {
                "minute": dict(m),
                "follow_ups": [dict(f) for f in self.svc.follow_ups_for_minute(mid)]})

        if path.startswith("/api/overdue") and method == "GET":
            if user["role"] not in ("teacher", "admin"):
                raise ServiceError("仅教师可查看全部逾期", "forbidden")
            return self._json(handler, {"overdue": [dict(f) for f in
                                                    self.svc.overdue_follow_ups()]})

        handler.send_error(404, "Unknown API")
