"""端到端业务流：教师请假改期、视频失效轮换、会谈超时、逾期提醒。"""
from tests.base import AppCase
from app.services import ServiceError


class FlowTest(AppCase):
    def test_teacher_leave_queues_bookings_and_frees_slots(self):
        s1 = self.slot(self.t_wang, "2026-10-09 18:00", "2026-10-09 18:20")
        s2 = self.slot(self.t_wang, "2026-10-09 18:20", "2026-10-09 18:40")  # 空闲
        s3 = self.slot(self.t_wang, "2026-10-09 19:00", "2026-10-09 19:20")  # 不在假期
        bid = self.book(self.dad, s1)
        self.svc.confirm_booking(self.wang, bid)

        result = self.svc.report_teacher_leave(
            self.wang, self.t_wang, "2026-10-09 18:00", "2026-10-09 19:00",
            "临时教研")
        self.assertIn(bid, result["queued_bookings"])
        self.assertIn(s2, result["cancelled_slots"])
        self.assertNotIn(s3, result["cancelled_slots"])
        self.assertEqual(self.svc.get_booking(bid)["status"], "teacher_reschedule")
        self.assertEqual(
            self.conn.execute("SELECT status FROM time_slots WHERE id=?",
                              (s1,)).fetchone()["status"], "teacher_leave")
        # 家长收到通知
        notes = [n for n in self.notifier.sent if n["user_id"] == self.dad["id"]]
        self.assertTrue(any(n["kind"] == "teacher_leave" for n in notes))

        # 家长自助重选到不受影响的 s3
        qid = result["queued_bookings"][0]
        qitem = self.conn.execute("SELECT id FROM reschedule_queue WHERE booking_id=?",
                                  (bid,)).fetchone()
        self.svc.guardian_pick_new_slot(self.dad, qitem["id"], s3)
        self.assertEqual(self.svc.get_booking(bid)["status"], "confirmed")
        self.assertEqual(self.svc.get_booking(bid)["slot_id"], s3)

    def test_guardian_pick_taken_slot_rejected(self):
        s1 = self.slot(self.t_wang, "2026-10-09 18:00", "2026-10-09 18:20")
        s3 = self.slot(self.t_wang, "2026-10-09 18:40", "2026-10-09 19:00")
        s4 = self.slot(self.t_wang, "2026-10-09 19:00", "2026-10-09 19:20")
        bid = self.book(self.dad, s1)
        self.svc.confirm_booking(self.wang, bid)
        # 乙占用 s3（不在请假区间，不会被取消）
        b2 = self.book(self.other_parent, s3, student_id=self.sid2)
        self.svc.confirm_booking(self.wang, b2)
        result = self.svc.report_teacher_leave(
            self.wang, self.t_wang, "2026-10-09 18:00", "2026-10-09 18:30")
        qitem = self.conn.execute("SELECT id FROM reschedule_queue WHERE booking_id=?",
                                  (bid,)).fetchone()
        with self.assertRaises(ServiceError) as cm:
            self.svc.guardian_pick_new_slot(self.dad, qitem["id"], s3)
        self.assertEqual(cm.exception.code, "slot_taken")
        # s4 空闲，可以重选
        self.svc.guardian_pick_new_slot(self.dad, qitem["id"], s4)

    def test_video_link_expiry_and_rotation(self):
        s = self.slot(self.t_li, "2026-10-09 18:00", "2026-10-09 18:25", "线上视频")
        bid = self.book(self.dad, s, mode="video")
        self.svc.confirm_booking(self.wang, bid)  # 自动创建视频入口
        entry1 = self.video.active_entry(bid)
        self.assertIsNotNone(entry1)
        joined = self.svc.join_video(self.dad, bid)
        self.assertEqual(joined["status"], "active")

        # 链接失效后进入被拒
        self.video.expire_entry(entry1["id"])
        with self.assertRaises(ServiceError) as cm:
            self.svc.join_video(self.dad, bid)
        self.assertEqual(cm.exception.code, "link_expired")

        # 教师轮换后新链接可用，旧链接仍失效
        entry2 = self.svc.rotate_video(self.wang, bid)
        self.assertNotEqual(entry1["join_url"], entry2["join_url"])
        self.svc.join_video(self.dad, bid)  # 不报错
        # 家长收到新链接通知
        self.assertTrue(any(n["kind"] == "video_new_link"
                            and n["user_id"] == self.dad["id"]
                            for n in self.notifier.sent))

    def test_overtime_recorded_at_completion(self):
        s = self.slot(self.t_wang, "2026-10-09 18:00", "2026-10-09 18:20")
        bid = self.book(self.dad, s)
        self.svc.confirm_booking(self.wang, bid)
        overtime = self.svc.complete_booking(self.wang, bid, now="2026-10-09 18:32")
        self.assertEqual(overtime, 12)
        # 提前/准时结束为 0
        s2 = self.slot(self.t_wang, "2026-10-09 18:20", "2026-10-09 18:40")
        self.conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
        bid2 = self.book(self.mom, s2)
        self.svc.confirm_booking(self.wang, bid2)
        self.assertEqual(
            self.svc.complete_booking(self.wang, bid2, now="2026-10-09 18:35"), 0)

    def test_reschedule_video_regenerates_link(self):
        s1 = self.slot(self.t_li, "2026-10-09 18:00", "2026-10-09 18:25", "线上视频")
        s2 = self.slot(self.t_li, "2026-10-09 18:25", "2026-10-09 18:50", "线上视频")
        bid = self.book(self.dad, s1, mode="video")
        self.svc.confirm_booking(self.wang, bid)
        url1 = self.video.active_entry(bid)["join_url"]
        qid = self.svc.guardian_request_reschedule(self.dad, bid, s2, "网络不方便")
        self.svc.approve_reschedule(self.wang, qid)
        url2 = self.video.active_entry(bid)["join_url"]
        self.assertNotEqual(url1, url2)
        self.assertEqual(self.svc.get_booking(bid)["slot_id"], s2)

    def test_overdue_notification_sent_to_owner(self):
        s = self.slot(self.t_wang, "2026-10-09 18:00", "2026-10-09 18:20")
        bid = self.book(self.dad, s)
        self.svc.confirm_booking(self.wang, bid)
        self.svc.complete_booking(self.wang, bid, now="2026-10-09 18:25")
        self.svc.save_minutes(
            self.wang, bid, "结论",
            [{"content": "补交练习册", "owner_label": "爸爸",
              "owner_user_id": self.dad["id"], "due_date": "2026-09-30"}])
        n = self.svc.notify_overdue_follow_ups("2026-10-02")
        self.assertEqual(n, 1)
        self.assertTrue(any(x["kind"] == "followup_overdue" and x["user_id"] == self.dad["id"]
                            for x in self.notifier.sent))
