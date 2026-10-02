"""HTTP 服务（仅标准库）：JSON API + 静态前端。

会话令牌保存在内存字典中；本地演示环境所有账号密码统一（见 seed 输出）。
每个请求使用独立的 SQLite 连接，写事务用 BEGIN IMMEDIATE，
配合部分唯一索引即可在多线程并发抢约时保持一致。
"""
import json
import os
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import db as dbmod
from .adapters import AttachmentStore, NotificationAdapter, VideoAdapter
from .errors import AppError, NotFoundError
from .seed import DB_PATH, FILES_DIR, DEMO_PASSWORD
from .services import ConferenceService
from . import auth

STATIC_DIR = Path(__file__).resolve().parent / "static"

SESSIONS: dict[str, int] = {}   # token -> user_id
SESSION_LOCK = threading.Lock()


def make_service(conn):
    return ConferenceService(
        conn, VideoAdapter(conn), NotificationAdapter(conn),
        AttachmentStore(conn, str(FILES_DIR)))


class Handler(BaseHTTPRequestHandler):
    server_version = "ConferenceBooking/1.0"

    # ---------- 基础工具 ----------

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode())
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, UnicodeDecodeError):
            raise AppError("请求体必须是 JSON 对象", status=400)

    def _current_user(self, conn):
        cookie = self.headers.get("Cookie", "")
        token = None
        for part in cookie.split(";"):
            if part.strip().startswith("token="):
                token = part.strip().split("=", 1)[1]
        if not token:
            return None
        with SESSION_LOCK:
            uid = SESSIONS.get(token)
        if uid is None:
            return None
        return auth.get_user(conn, uid)

    def _require_user(self, conn):
        user = self._current_user(conn)
        if user is None:
            raise AppError("请先登录", status=401)
        return user

    def log_message(self, fmt, *args):
        pass  # 静默；测试中也不污染输出

    # ---------- 路由 ----------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        conn = dbmod.connect(str(DB_PATH))
        try:
            self._route(conn, method, path)
        except AppError as e:
            self._send_json({"error": e.message, "code": e.code}, e.status)
        except Exception as e:  # noqa: BLE001 - 本地演示兜底
            self._send_json({"error": f"服务器内部错误：{e}",
                             "code": "InternalError"}, 500)
        finally:
            conn.close()

    def _route(self, conn, method, path):
        # 静态资源
        if method == "GET" and (path == "/" or path == "/index.html"):
            self._serve_static("index.html")
            return
        if method == "GET" and path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return

        if path == "/api/login" and method == "POST":
            self._login(conn); return
        if path == "/api/logout" and method == "POST":
            self._logout(); return
        if path == "/api/me" and method == "GET":
            user = self._current_user(conn)
            self._send_json({"user": self._user_payload(user)}); return

        user = self._require_user(conn)
        svc = make_service(conn)
        uid = user["id"]

        if path == "/api/notifications" and method == "GET":
            rows = conn.execute(
                "SELECT * FROM notifications WHERE user_id=? "
                "ORDER BY id DESC LIMIT 50", (uid,)).fetchall()
            self._send_json({"items": [dict(r) for r in rows]}); return

        if path == "/api/rounds" and method == "GET":
            rows = conn.execute(
                "SELECT * FROM rounds ORDER BY meeting_date").fetchall()
            self._send_json({"items": [dict(r) for r in rows]}); return

        if path == "/api/access-logs" and method == "GET":
            self._send_json({"items": svc.access_logs(uid)}); return

        if path.startswith("/api/rounds/") and path.endswith("/people") \
                and method == "GET":
            # 本班教师或管理员：记录后续待办/登记请假时选择人员
            rid = int(path.split("/")[3])
            rnd = svc._round(rid)
            if "admin" not in auth.roles(user):
                auth.require_teacher_of_class(conn, user, rnd["class_id"])
            teachers = conn.execute(
                """SELECT u.id,u.name,'教师' AS kind FROM users u
                   JOIN teachers t ON t.user_id=u.id
                   WHERE t.class_id=? ORDER BY u.id""",
                (rnd["class_id"],)).fetchall()
            guardians = conn.execute(
                """SELECT DISTINCT u.id,u.name,g.relation||'（'||s.name||'）'
                   AS kind FROM users u
                   JOIN guardianships g ON g.user_id=u.id AND g.active=1
                   JOIN students s ON s.id=g.student_id
                   WHERE s.class_id=? ORDER BY u.id""",
                (rnd["class_id"],)).fetchall()
            self._send_json(
                {"items": [dict(r) for r in list(teachers) + list(guardians)]})
            return

        # /api/rounds/{rid}/...
        parts = [p for p in path.split("/") if p]
        if len(parts) == 4 and parts[:2] == ["api", "rounds"]:
            rid = int(parts[2]); resource = parts[3]
            if method == "GET" and resource == "slots":
                self._send_json({"items": svc.available_slots(rid)}); return
            if method == "GET" and resource == "calendar":
                self._send_json({"items": svc.teacher_calendar(uid, rid)}); return
            if method == "GET" and resource == "bookings":
                self._send_json({"items": svc.my_bookings(uid, rid)}); return
            if resource == "reschedules":
                if method == "GET":
                    state = self._query_state(parsed_query=self.path)
                    self._send_json(
                        {"items": svc.list_reschedules(uid, rid, state)}); return
            if method == "POST" and resource == "slots":
                data = self._read_json()
                ids = svc.publish_slots(uid, rid, data.get("slots", []))
                self._send_json({"slot_ids": ids}, 201); return
            if method == "POST" and resource == "bookings":
                d = self._read_json()
                res = svc.request_booking(
                    uid, int(d["student_id"]), rid, d["mode"],
                    d.get("topics", ""),
                    int(d["slot_id"]) if d.get("slot_id") else None)
                self._send_json(res, 201); return
            if method == "POST" and resource == "teacher-leave":
                d = self._read_json()
                res = svc.teacher_leave(
                    uid, int(d["teacher_id"]), rid, d.get("reason", ""))
                self._send_json(res); return

        # /api/bookings/{id}/...
        if len(parts) >= 3 and parts[:2] == ["api", "bookings"]:
            bid = int(parts[2])
            action = parts[3] if len(parts) > 3 else ""
            d = self._read_json() if method == "POST" else {}
            if method == "POST" and action == "confirm":
                slot = int(d["slot_id"]) if d.get("slot_id") else None
                self._send_json(svc.confirm_booking(uid, bid, slot)); return
            if method == "POST" and action == "reject":
                svc.reject_request(uid, bid, d.get("reason", ""))
                self._send_json({"ok": True}); return
            if method == "POST" and action == "cancel":
                svc.cancel_by_guardian(uid, bid, d.get("reason", ""))
                self._send_json({"ok": True}); return
            if method == "POST" and action == "complete":
                minutes_id = svc.complete_meeting(
                    uid, bid, d.get("summary", ""),
                    bool(d.get("overtime")),
                    d.get("action_items", []),
                    d.get("attachments", []))
                self._send_json({"minutes_id": minutes_id}, 201); return
            if method == "GET" and action == "video-entry":
                self._send_json(svc.video_entry(uid, bid)); return
            if method == "POST" and action == "video-refresh":
                self._send_json({"url": svc.refresh_video(uid, bid)}); return
            if method == "POST" and action == "reschedule":
                new_slot = int(d["new_slot_id"]) if d.get("new_slot_id") else None
                rid = svc.request_reschedule(
                    uid, bid, d.get("reason", ""), new_slot)
                self._send_json({"reschedule_id": rid}, 201); return

        # /api/reschedules/{id}/...
        if len(parts) >= 3 and parts[:2] == ["api", "reschedules"]:
            rqid = int(parts[2]); action = parts[3] if len(parts) > 3 else ""
            d = self._read_json()
            if method == "POST" and action == "approve":
                slot = int(d["slot_id"]) if d.get("slot_id") else None
                self._send_json(svc.approve_reschedule(uid, rqid, slot)); return
            if method == "POST" and action == "reject":
                svc.reject_reschedule(uid, rqid, d.get("reason", ""))
                self._send_json({"ok": True}); return
            if method == "POST" and action == "propose-slot":
                svc.propose_new_slot(uid, rqid, int(d["slot_id"]))
                self._send_json({"ok": True}); return

        # /api/minutes/{id}
        if len(parts) == 3 and parts[:2] == ["api", "minutes"]:
            mid = int(parts[2])
            if method == "GET":
                self._send_json(svc.view_minutes(uid, mid)); return
            if method == "POST":
                d = self._read_json()
                svc.edit_minutes(uid, mid, d.get("summary", ""))
                self._send_json({"ok": True}); return

        # /api/action-items/{id}/state
        if len(parts) == 4 and parts[:2] == ["api", "action-items"] \
                and parts[3] == "state" and method == "POST":
            d = self._read_json()
            svc.mark_action_item(uid, int(parts[2]), bool(d.get("done")))
            self._send_json({"ok": True}); return

        # /api/guardianships/revoke
        if path == "/api/guardianships/revoke" and method == "POST":
            d = self._read_json()
            svc.revoke_guardianship(
                uid, int(d["student_id"]), int(d["guardian_id"]))
            self._send_json({"ok": True}); return

        # /api/students （下拉选择用：教师看本班全部；监护人看自己孩子）
        if path == "/api/students" and method == "GET":
            if "teacher" in auth.roles(user):
                rows = conn.execute(
                    """SELECT s.*, c.name AS class_name FROM students s
                       JOIN classes c ON c.id=s.class_id
                       WHERE s.class_id IN
                         (SELECT class_id FROM teachers WHERE user_id=?)
                       ORDER BY s.id""", (uid,)).fetchall()
            else:
                rows = conn.execute(
                    """SELECT s.*, c.name AS class_name FROM students s
                       JOIN classes c ON c.id=s.class_id
                       JOIN guardianships g ON g.student_id=s.id
                       WHERE g.user_id=? AND g.active=1 ORDER BY s.id""",
                    (uid,)).fetchall()
            self._send_json({"items": [dict(r) for r in rows]}); return

        if len(parts) == 4 and parts[:2] == ["api", "students"] \
                and parts[3] == "guardians" and method == "GET":
            sid = int(parts[2])
            stu = auth.student_class(conn, sid)
            if stu is None:
                raise NotFoundError("学生不存在")
            if "admin" not in auth.roles(user):
                auth.require_teacher_of_class(conn, user, stu["class_id"])
            rows = conn.execute(
                """SELECT g.user_id, u.name, g.relation, g.active
                   FROM guardianships g JOIN users u ON u.id=g.user_id
                   WHERE g.student_id=? ORDER BY g.id""", (sid,)).fetchall()
            self._send_json({"items": [dict(r) for r in rows]}); return

        self._send_json({"error": f"未知接口：{method} {path}",
                         "code": "NotFound"}, 404)

    # ---------- 登录/静态文件 ----------

    def _query_state(self, parsed_query):
        from urllib.parse import parse_qs
        qs = parse_qs(urlparse(self.path).query)
        return qs.get("state", ["pending"])[0]

    def _user_payload(self, user):
        if user is None:
            return None
        return {"id": user["id"], "login": user["login"],
                "name": user["name"], "roles": user["roles"].split(",")}

    def _login(self, conn):
        d = self._read_json()
        user = auth.get_user_by_login(conn, d.get("login", "").strip())
        if user is None or d.get("password") != DEMO_PASSWORD:
            raise AppError("账号或密码错误（演示密码：%s）" % DEMO_PASSWORD,
                            status=401)
        token = secrets.token_hex(16)
        with SESSION_LOCK:
            SESSIONS[token] = user["id"]
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header(
            "Set-Cookie",
            f"token={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=28800")
        body = json.dumps({"user": self._user_payload(user)},
                          ensure_ascii=False).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _logout(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            if part.strip().startswith("token="):
                token = part.strip().split("=", 1)[1]
                with SESSION_LOCK:
                    SESSIONS.pop(token, None)
        self._send_json({"ok": True})

    def _serve_static(self, rel: str):
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) \
                or not target.is_file():
            self._send_json({"error": "文件不存在", "code": "NotFound"}, 404)
            return
        ctype = {".html": "text/html; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8"}.get(
                     target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="家长会预约系统")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", 8000)))
    args = parser.parse_args()

    dbmod.init_db(dbmod.connect(str(DB_PATH)))
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"家长会预约系统已启动：http://{args.host}:{args.port}")
    print(f"数据库：{DB_PATH}（演示密码：{DEMO_PASSWORD}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
