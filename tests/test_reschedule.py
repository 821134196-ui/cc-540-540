"""改期与教师请假：临时改期队列、批准原子换时段、请假批量入队。"""
import unittest

from app.errors import AppError, ConflictError
from tests.helpers import build_world, temp_paths


class RescheduleTests(unittest.TestCase):
    def setUp(self):
        self.db, self.files, self.tmp = temp_paths()
        self.conn, self.svc, self.ids = build_world(str(self.db), str(self.files))
        i = self.ids
        self.slot_a, self.slot_b, self.slot_c = i["slots"][:3]
        bk = self.svc.request_booking(i["ma"], i["s1"], 1, "video",
                                      "阅读习惯", slot_id=self.slot_a)
        self.bid = bk["booking_id"]
        self.svc.confirm_booking(i["wang"], self.bid)
        self.old_video = bk["video_url"]

    def tearDown(self):
        self.conn.close()

    def test_guardian_reschedule_approve_swaps_slot_and_video(self):
        rid = self.svc.request_reschedule(
            self.ids["ma"], self.bid, "临时有事", self.slot_b)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM bookings WHERE id=?", (self.bid,))
            .fetchone()["status"], "reschedule_pending")
        # 队列里能看到（教师视角）
        pending = self.svc.list_reschedules(self.ids["wang"], 1)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], rid)

        res = self.svc.approve_reschedule(self.ids["wang"], rid)
        self.assertEqual(res["new_slot_id"], self.slot_b)
        bk = self.conn.execute(
            "SELECT * FROM bookings WHERE id=?", (self.bid,)).fetchone()
        self.assertEqual(bk["status"], "confirmed")
        self.assertEqual(bk["slot_id"], self.slot_b)
        # 旧时段释放、新时段占用
        self.assertEqual(self.conn.execute(
            "SELECT status FROM slots WHERE id=?", (self.slot_a,))
            .fetchone()["status"], "open")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM slots WHERE id=?", (self.slot_b,))
            .fetchone()["status"], "booked")
        # 视频会谈换房间：新链接有效且与旧链接不同
        self.assertIsNotNone(res["video_url"])
        self.assertNotEqual(res["video_url"], self.old_video)

    def test_approve_to_taken_slot_conflict(self):
        # slot_b 先被另一家约掉
        other = self.svc.request_booking(
            self.ids["hong"], self.ids["s2"], 1, "offline",
            "作文", slot_id=self.slot_b)
        self.svc.confirm_booking(self.ids["wang"], other["booking_id"])
        # 申请时不指定时段，批准时指向已被占用的 slot_b
        rid = self.svc.request_reschedule(self.ids["ma"], self.bid, "改期")
        with self.assertRaises(ConflictError):
            self.svc.approve_reschedule(self.ids["wang"], rid, self.slot_b)

    def test_reject_reschedule_restores_original(self):
        rid = self.svc.request_reschedule(
            self.ids["ma"], self.bid, "想改时间", self.slot_b)
        self.svc.reject_reschedule(self.ids["wang"], rid, "时间不合适")
        bk = self.conn.execute(
            "SELECT * FROM bookings WHERE id=?", (self.bid,)).fetchone()
        self.assertEqual(bk["status"], "confirmed")
        self.assertEqual(bk["slot_id"], self.slot_a)

    def test_propose_then_approve(self):
        rid = self.svc.request_reschedule(
            self.ids["ma"], self.bid, "先不定新时间")
        self.svc.propose_new_slot(self.ids["ma"], rid, self.slot_c)
        res = self.svc.approve_reschedule(self.ids["wang"], rid)
        self.assertEqual(res["new_slot_id"], self.slot_c)

    def test_unrelated_guardian_cannot_touch_queue(self):
        rid = self.svc.request_reschedule(
            self.ids["ma"], self.bid, "改期", self.slot_b)
        # hong 是 s2 的监护人，不能替 s1 选时段
        from app.errors import PermissionError_
        with self.assertRaises(PermissionError_):
            self.svc.propose_new_slot(self.ids["hong"], rid, self.slot_c)
        # 家长视角队列里看不到别人家的请求
        self.assertEqual(
            self.svc.list_reschedules(self.ids["hong"], 1), [])


class TeacherLeaveTests(unittest.TestCase):
    def setUp(self):
        self.db, self.files, self.tmp = temp_paths()
        self.conn, self.svc, self.ids = build_world(str(self.db), str(self.files))
        i = self.ids
        # 王老师 09:00 视频（s1），09:20 线下（s2）
        b1 = self.svc.request_booking(i["ma"], i["s1"], 1, "video", "",
                                      slot_id=i["slots"][0])
        b2 = self.svc.request_booking(i["hong"], i["s2"], 1, "offline", "",
                                      slot_id=i["slots"][1])
        self.b1, self.b2 = b1["booking_id"], b2["booking_id"]
        self.svc.confirm_booking(i["wang"], self.b1)
        self.svc.confirm_booking(i["wang"], self.b2)

    def tearDown(self):
        self.conn.close()

    def test_leave_cancels_future_slots_and_queues_bookings(self):
        res = self.svc.teacher_leave(
            self.ids["admin"], self.ids["wang"], 1, "外出教研")
        self.assertEqual(res["cancelled_slots"], 4)
        self.assertEqual(len(res["affected_bookings"]), 2)
        # 所有未来时段取消
        cancelled = self.conn.execute(
            "SELECT COUNT(*) c FROM slots WHERE teacher_id=? AND status='cancelled'",
            (self.ids["wang"],)).fetchone()["c"]
        self.assertEqual(cancelled, 4)
        # 预约进入改期队列，状态为 reschedule_pending
        for bid in (self.b1, self.b2):
            self.assertEqual(self.conn.execute(
                "SELECT status FROM bookings WHERE id=?", (bid,))
                .fetchone()["status"], "reschedule_pending")
        rq = self.conn.execute(
            "SELECT * FROM reschedules WHERE booking_id=?", (self.b1,)).fetchone()
        self.assertEqual(rq["kind"], "teacher_leave")
        # 视频链接随请假失效
        self.assertIsNone(self.svc.video.current_url(self.b1))
        # 教师请假型改期不能驳回（原时段已取消）
        with self.assertRaises(AppError):
            self.svc.reject_reschedule(self.ids["wang"], rq["id"])
        # 监护人选择新时段——但王老师时段全取消，没有可选时段
        self.assertEqual(self.svc.available_slots(1), [])


if __name__ == "__main__":
    unittest.main()
