"""授权撤销测试：旧账号立即失去所有访问权，且访问被记录。"""
from tests.base import AppCase
from app.services import ServiceError


class RevokeTest(AppCase):
    def setUp(self):
        super().setUp()
        self.s = self.slot(self.t_wang, "2026-10-09 18:00", "2026-10-09 18:20")
        self.bid = self.book(self.granny, self.s)
        self.svc.confirm_booking(self.wang, self.bid)
        self.svc.complete_booking(self.wang, self.bid, now="2026-10-09 18:25")
        self.mid = self.svc.save_minutes(
            self.wang, self.bid, "结论：继续保持",
            [{"content": "每天读一首古诗", "owner_label": "外婆",
              "owner_user_id": self.granny["id"], "due_date": "2026-10-20"}],
            finalize=True)

    def _count_denied(self, action="minutes_denied"):
        return self.conn.execute(
            "SELECT COUNT(*) c FROM access_logs WHERE username=? AND action=?",
            ("granny", action)).fetchone()["c"]

    def test_revoked_account_loses_minutes_list(self):
        self.assertTrue(self.svc.minutes_visible_to(self.granny))
        self.svc.revoke_guardianship(self.wang, self.sid, self.granny["id"])
        self.assertEqual(self.svc.minutes_visible_to(self.granny), [])

    def test_revoked_account_cannot_view_minute(self):
        before = self._count_denied()
        self.svc.revoke_guardianship(self.wang, self.sid, self.granny["id"])
        with self.assertRaises(ServiceError) as cm:
            self.svc.view_minute(self.granny, self.mid)
        self.assertEqual(cm.exception.code, "forbidden")
        after = self._count_denied()
        self.assertGreater(after, before)  # 拒绝访问已落日志

    def test_revoked_account_cannot_book_or_cancel(self):
        self.svc.revoke_guardianship(self.wang, self.sid, self.granny["id"])
        with self.assertRaises(ServiceError):
            self.book(self.granny, self.s)
        with self.assertRaises(ServiceError) as cm:
            self.svc.cancel_booking(self.granny, self.bid)
        # 取消权限也随授权失效
        self.assertEqual(cm.exception.code, "forbidden")

    def test_revoked_account_cannot_enter_video(self):
        # 先给该线下预约改视频不现实；单独造一个视频预约
        s2 = self.slot(self.t_wang, "2026-10-09 18:20", "2026-10-09 18:40")
        # granny 的 bid 已完成且占唯一名额，先取消旧的再新约视频
        self.conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (self.bid,))
        bid2 = self.book(self.granny, s2, mode="video")
        self.svc.confirm_booking(self.wang, bid2)
        self.svc.join_video(self.granny, bid2)  # 撤销前可以
        self.svc.revoke_guardianship(self.wang, self.sid, self.granny["id"])
        with self.assertRaises(ServiceError) as cm:
            self.svc.join_video(self.granny, bid2)
        self.assertEqual(cm.exception.code, "forbidden")

    def test_other_guardians_unaffected(self):
        self.svc.revoke_guardianship(self.wang, self.sid, self.granny["id"])
        # 父母授权仍然有效
        self.assertTrue(self.svc.active_guardianship(self.dad["id"], self.sid))
        self.assertTrue(self.svc.minutes_visible_to(self.dad))

    def test_double_revoke_is_idempotent(self):
        self.svc.revoke_guardianship(self.wang, self.sid, self.granny["id"])
        self.svc.revoke_guardianship(self.wang, self.sid, self.granny["id"])  # 不报错

    def test_non_teacher_cannot_revoke(self):
        with self.assertRaises(ServiceError) as cm:
            self.svc.revoke_guardianship(self.dad, self.sid, self.granny["id"])
        self.assertEqual(cm.exception.code, "forbidden")
