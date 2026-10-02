"""HTTP 端到端测试：真实端口、cookie 会话、种子数据。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.parse
import http.cookiejar
from http.server import ThreadingHTTPServer
from pathlib import Path

from app.__main__ import Handler
from app.seed import seed
from app.server import make_service, App


class HttpCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.db = str(root / "app.db")
        cls.attach = str(root / "attachments")
        seed(cls.db, cls.attach, force=True)
        svc = make_service(cls.db, cls.attach)
        Handler.app = App(svc)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def client(self):
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        return opener

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def login(self, opener, username):
        data = urllib.parse.urlencode({"username": username, "password": "pass1234"}).encode()
        opener.open(self.url("/login"), data=data)
        return opener

    def get(self, opener, path):
        return opener.open(self.url(path))

    def post(self, opener, path, fields: dict):
        data = urllib.parse.urlencode(fields).encode()
        return opener.open(self.url(path), data=data)

    # -------------------------------------------------------------- tests

    def test_pages_render(self):
        op = self.login(self.client(), "wangzl")
        html_text = self.get(op, "/teacher").read().decode()
        self.assertIn("班级日历", html_text)
        self.assertIn("改期队列", html_text)
        # 种子中的教师请假样例：赵北辰在队列中
        self.assertIn("赵北辰", html_text)

        op2 = self.login(self.client(), "zhang_wei")
        p = self.get(op2, "/parent").read().decode()
        self.assertIn("我的预约", p)
        self.assertIn("张一诺", p)

    def test_narrow_screen_time_not_wrapped(self):
        op = self.login(self.client(), "wangzl")
        css = self.get(op, "/teacher").read().decode()
        self.assertIn("white-space:nowrap", css)
        self.assertIn("overflow-x:auto", css)

    def test_duplicate_booking_rejected_via_api(self):
        # 父亲已有一份有效预约（种子中 s1，已完成？b1 已 completed -> 不占名额）。
        # 用母亲（同为张一诺监护人）预约一个开放时段成功后，父亲再约被拒。
        mom = self.login(self.client(), "zhang_min")
        slots = json.loads(self.get(mom, "/api/slots?round_id=1").read())["slots"]
        open_ids = [s["id"] for s in slots]
        # 张一诺的已完成预约不占名额；母亲新约
        import sqlite3
        conn = sqlite3.connect(self.db)
        sid = conn.execute("SELECT id FROM students WHERE name='张一诺'").fetchone()[0]
        target = slots[0]["id"]
        self.assertIsNotNone(target)
        req = urllib.request.Request(
            self.url("/api/bookings"),
            data=json.dumps({"slot_id": target, "student_id": sid,
                             "mode": "offline", "topics": "api 预约"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        resp = json.loads(mom.open(req).read())
        self.assertIn("booking_id", resp)
        # 父亲立刻再约同一轮 -> 400 一份有效预约
        dad = self.login(self.client(), "zhang_wei")
        other_slot = next(s["id"] for s in slots if s["id"] != target)
        req2 = urllib.request.Request(
            self.url("/api/bookings"),
            data=json.dumps({"slot_id": other_slot, "student_id": sid,
                              "mode": "offline", "topics": "第二份"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            dad.open(req2)
        self.assertEqual(cm.exception.code, 400)
        body = json.loads(cm.exception.read())
        self.assertEqual(body["code"], "duplicate_booking")

    def test_revoked_account_blocked_via_api(self):
        # zhou_lan 在种子里已是被撤销状态
        op = self.login(self.client(), "zhou_lan")
        page = self.get(op, "/parent").read().decode()
        self.assertIn("没有有效的监护授权", page)

        # 直接访问纪要 API 应被拒（找到张一诺纪要 id）
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        mid = conn.execute(
            "SELECT m.id FROM minutes m JOIN students s ON s.id=m.student_id"
            " WHERE s.name='张一诺'").fetchone()[0]
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(op, f"/api/minutes/{mid}")
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual(json.loads(cm.exception.read())["code"], "forbidden")

        # 有效监护人可正常读取
        dad = self.login(self.client(), "zhang_wei")
        resp = json.loads(self.get(dad, f"/api/minutes/{mid}").read())
        self.assertEqual(len(resp["follow_ups"]), 3)

    def test_expired_video_link_blocks_then_rotate(self):
        # 种子中林小夏视频链接已失效
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        bid = conn.execute(
            "SELECT b.id FROM bookings b JOIN students s ON s.id=b.student_id"
            " WHERE s.name='林小夏'").fetchone()[0]
        mom = self.login(self.client(), "lin_mu")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(mom, f"/api/video/{bid}")
        self.assertEqual(json.loads(cm.exception.read())["code"], "link_expired")
        # 教师轮换
        t = self.login(self.client(), "wangzl")
        req = urllib.request.Request(self.url(f"/api/video/{bid}/rotate"),
                                     data=b"", method="POST")
        entry = json.loads(t.open(req).read())
        self.assertEqual(entry["status"], "active")
        # 家长可进入
        ok = json.loads(self.get(mom, f"/api/video/{bid}").read())
        self.assertEqual(ok["meeting_code"], entry["meeting_code"])

    def test_teacher_leave_api_queues(self):
        t = self.login(self.client(), "lijing")
        # 李静名下目前没有已确认预约；登记一个未来窗口的请假应成功且队列为空列表
        req = urllib.request.Request(
            self.url("/api/leave"),
            data=json.dumps({"teacher_id": _teacher_id(self.db, "李静"),
                             "start_at": "2026-10-12 14:00",
                             "end_at": "2026-10-12 17:00",
                             "reason": "外出培训"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        resp = json.loads(t.open(req).read())
        self.assertEqual(resp["teacher"], "李静")
        self.assertIsInstance(resp["queued_bookings"], list)


def _teacher_id(db_path, name):
    import sqlite3
    conn = sqlite3.connect(db_path)
    return conn.execute("SELECT id FROM teachers WHERE name=?", (name,)).fetchone()[0]


if __name__ == "__main__":
    unittest.main()
