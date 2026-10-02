"""纪要权限测试：教师/监护人角色边界、锁定、待办负责人。"""
from tests.base import AppCase
from app.services import ServiceError


class MinutesPermissionTest(AppCase):
    def setUp(self):
        super().setUp()
        self.s = self.slot(self.t_wang, "2026-10-09 18:00", "2026-10-09 18:20")
        self.bid = self.book(self.dad, self.s, topics="成绩与作业")
        self.svc.confirm_booking(self.wang, self.bid)
        self.fus = [
            {"content": "检查每日作业签字", "owner_label": "爸爸",
             "owner_user_id": self.dad["id"], "due_date": "2026-10-15"},
            {"content": "调整座位并关注两周", "owner_label": "王老师",
             "owner_user_id": self.wang["id"], "due_date": "2026-10-12"},
        ]

    def test_teacher_writes_minutes(self):
        mid = self.svc.save_minutes(self.wang, self.bid, "状态稳定，继续鼓励", self.fus)
        m = self.svc.view_minute(self.wang, mid)
        self.assertEqual(m["conclusions"], "状态稳定，继续鼓励")
        self.assertEqual(len(self.svc.follow_ups_for_minute(mid)), 2)

    def test_guardian_cannot_write_minutes(self):
        # 教师不能以普通家长身份修改结论：监护人身份直接被拒
        with self.assertRaises(ServiceError) as cm:
            self.svc.save_minutes(self.dad, self.bid, "家长写的结论", self.fus)
        self.assertEqual(cm.exception.code, "forbidden")
        denied = self.conn.execute(
            "SELECT COUNT(*) c FROM access_logs WHERE username='dad' AND action='minutes_denied'"
        ).fetchone()["c"]
        self.assertEqual(denied, 1)

    def test_outside_teacher_cannot_write_or_view(self):
        with self.assertRaises(ServiceError):
            self.svc.save_minutes(self.outsider_t, self.bid, "外班教师写入", self.fus)
        mid = self.svc.save_minutes(self.wang, self.bid, "结论", self.fus)
        with self.assertRaises(ServiceError):
            self.svc.view_minute(self.outsider_t, mid)

    def test_other_guardian_cannot_view(self):
        mid = self.svc.save_minutes(self.wang, self.bid, "结论", self.fus)
        with self.assertRaises(ServiceError) as cm:
            self.svc.view_minute(self.other_parent, mid)
        self.assertEqual(cm.exception.code, "forbidden")

    def test_guardian_can_view_only_own(self):
        mid = self.svc.save_minutes(self.wang, self.bid, "结论", self.fus)
        m = self.svc.view_minute(self.dad, mid)  # 本人监护人可以
        self.assertEqual(m["id"], mid)
        # 同学生另一位监护人也可以
        self.svc.view_minute(self.mom, mid)

    def test_finalized_minutes_locked(self):
        mid = self.svc.save_minutes(self.wang, self.bid, "初版", self.fus, finalize=True)
        with self.assertRaises(ServiceError) as cm:
            self.svc.save_minutes(self.wang, self.bid, "想改结论", self.fus)
        self.assertEqual(cm.exception.code, "locked")

    def test_follow_up_requires_owner_and_due(self):
        bad = [{"content": "没有负责人", "owner_label": "", "due_date": "2026-10-15"}]
        with self.assertRaises(ServiceError):
            self.svc.save_minutes(self.wang, self.bid, "结论", bad)
        bad2 = [{"content": "没有期限", "owner_label": "爸爸", "due_date": ""}]
        with self.assertRaises(ServiceError):
            self.svc.save_minutes(self.wang, self.bid, "结论", bad2)

    def test_follow_up_completion_and_overdue(self):
        mid = self.svc.save_minutes(self.wang, self.bid, "结论", self.fus)
        fus = {f["owner_label"]: f for f in self.svc.follow_ups_for_minute(mid)}
        fu = fus["爸爸"]
        # 非负责人、非本班教师不能标记完成
        with self.assertRaises(ServiceError):
            self.svc.complete_follow_up(self.other_parent, fu["id"])
        self.svc.complete_follow_up(self.dad, fu["id"])
        after = {f["owner_label"]: f for f in self.svc.follow_ups_for_minute(mid)}
        self.assertEqual(after["爸爸"]["status"], "done")

        overdue = self.svc.overdue_follow_ups("2026-10-20")
        owners = {(f["content"], f["owner_label"]) for f in overdue}
        self.assertIn(("调整座位并关注两周", "王老师"), owners)
        self.assertNotIn(("检查每日作业签字", "爸爸"), owners)  # 已完成不再逾期

    def test_minutes_list_scoped_to_class(self):
        self.svc.save_minutes(self.wang, self.bid, "甲的结论", self.fus)
        teacher_list = self.svc.minutes_visible_to(self.li)  # 本班数学老师可见
        self.assertEqual(len(teacher_list), 1)
        self.assertEqual(teacher_list[0]["student_name"], "学生甲")
        self.assertEqual(self.svc.minutes_visible_to(self.outsider_t), [])

    def test_guardian_sees_notifications_on_minutes(self):
        self.svc.save_minutes(self.wang, self.bid, "结论", self.fus)
        titles = [n["title"] for n in self.notifier.sent if n["user_id"] == self.dad["id"]]
        self.assertIn("会谈纪要已更新", titles)
