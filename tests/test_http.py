"""HTTP 集成测试：真实多线程服务器 + Cookie 会话。

覆盖需求点名的四类：时段冲突、多监护人规则、授权撤销、纪要权限。
"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from app import server as webmod
from tests.helpers import build_world, temp_paths


class ApiClient:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def login(self, login, password="123456"):
        return self.call("POST", "/api/login",
                         {"login": login, "password": password})


class HttpIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db, cls.files, cls.tmp = temp_paths()
        cls.conn, cls.svc, cls.ids = build_world(str(cls.db), str(cls.files))
        # 让服务器使用临时数据库
        webmod.DB_PATH = cls.db
        webmod.FILES_DIR = cls.files
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), webmod.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.conn.close()

    def client(self):
        return ApiClient(self.base)

    def test_auth_required_and_wrong_password(self):
        c = self.client()
        status, _ = c.call("GET", "/api/rounds")
        self.assertEqual(status, 401)
        self.assertEqual(c.login("ma", "wrong")[0], 401)
        self.assertEqual(c.login("ma")[0], 200)

    def test_guardian_cannot_publish_slots(self):
        c = self.client(); c.login("ma")
        status, body = c.call("POST", "/api/rounds/1/slots",
                              {"slots": [{"start_ts": "2026-10-10T11:00",
                                          "end_ts": "2026-10-10T11:20"}]})
        self.assertEqual(status, 403)

    def test_slot_conflict_and_multi_guardian_rules(self):
        i = self.ids
        # 1) 两个家庭并发抢同一时段
        slot = i["slots"][2]
        outcomes = []
        barrier = threading.Barrier(2)

        def race(login, student):
            c = self.client(); c.login(login)
            barrier.wait()
            status, _ = c.call("POST", "/api/rounds/1/bookings", {
                "student_id": student, "slot_id": slot,
                "mode": "offline", "topics": ""})
            outcomes.append(status)

        t1 = threading.Thread(target=race, args=("ma", i["s1"]))
        t2 = threading.Thread(target=race, args=("hong", i["s2"]))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(outcomes), [201, 409])

        # 2) 同一学生第二个监护人再约（不同时段）→ 409，仅一份有效预约
        c_ba = self.client(); c_ba.login("ba")
        status, body = c_ba.call("POST", "/api/rounds/1/bookings", {
            "student_id": i["s1"], "slot_id": i["slots"][3],
            "mode": "offline", "topics": ""})
        self.assertEqual(status, 409)
        self.assertIn("已有一份有效预约", body["error"])

        active = self.conn.execute(
            "SELECT COUNT(*) c FROM bookings WHERE student_id=? "
            "AND status NOT IN ('cancelled')", (i["s1"],)).fetchone()["c"]
        self.assertEqual(active, 1)

    def test_revocation_and_minutes_permissions(self):
        i = self.ids
        # 王老师完成一份纪要
        c_w = self.client(); c_w.login("wang")
        # 直接插入一条 s2 的已确认预约，再由教师完成会谈
        self.conn.execute(
            "INSERT INTO bookings(id,slot_id,round_id,student_id,guardian_id,"
            "mode,status,created_at,confirmed_at) VALUES "
            "(100,NULL,1,2,7,'offline','confirmed','2026-10-02T10:00',"
            "'2026-10-02T10:00')")
        mid = self.svc.complete_meeting(
            i["wang"], 100, "s2 的会谈结论。",
            action_items=[{"content": "练习书写", "owner_id": i["hong"],
                           "due_date": "2026-10-30"}])

        # s2 的监护人 hong 可读
        c_h = self.client(); c_h.login("hong")
        st, m = c_h.call("GET", f"/api/minutes/{mid}")
        self.assertEqual(st, 200)
        self.assertEqual(m["summary"], "s2 的会谈结论。")
        # hong 不能改结论（家长身份）
        st, _ = c_h.call("POST", f"/api/minutes/{mid}",
                         {"summary": "篡改"})
        self.assertEqual(st, 403)

        # 王老师可以改
        st, _ = c_w.call("POST", f"/api/minutes/{mid}",
                         {"summary": "教师修订结论"})
        self.assertEqual(st, 200)

        # 其他监护人 ma（s1 家长）不能看 s2 纪要，拒绝被记录
        c_ma = self.client(); c_ma.login("ma")
        st, _ = c_ma.call("GET", f"/api/minutes/{mid}")
        self.assertEqual(st, 403)

        # 管理员撤销 hong 对 s2 的授权，旧会话立即 403
        c_admin = self.client(); c_admin.login("admin")
        st, _ = c_admin.call("POST", "/api/guardianships/revoke", {
            "student_id": i["s2"], "guardian_id": i["hong"]})
        self.assertEqual(st, 200)
        st, _ = c_h.call("GET", f"/api/minutes/{mid}")
        self.assertEqual(st, 403)

        # 访问记录里同时有允许与拒绝
        st, logs = c_admin.call("GET", "/api/access-logs")
        self.assertEqual(st, 200)
        denied = [x for x in logs["items"]
                  if x["user_id"] == i["hong"] or x["ok"] == 0]
        self.assertTrue(any(x["ok"] == 0 for x in denied))
        # 监护人无权访问访问记录
        st, _ = c_ma.call("GET", "/api/access-logs")
        self.assertEqual(st, 403)

        # 恢复授权，避免影响同一世界里按字母序后续执行的并发测试
        self.conn.execute(
            "UPDATE guardianships SET active=1 WHERE student_id=? AND user_id=?",
            (i["s2"], i["hong"]))

    def test_parent_sees_only_open_slots_and_own_minutes(self):
        c = self.client(); c.login("ma")
        st, body = c.call("GET", "/api/rounds/1/slots")
        self.assertEqual(st, 200)
        self.assertTrue(all(s["status"] == "open" for s in body["items"]))
        st, mine = c.call("GET", "/api/rounds/1/bookings")
        self.assertTrue(all(b["student_id"] == self.ids["s1"]
                            for b in mine["items"]))


if __name__ == "__main__":
    unittest.main()
