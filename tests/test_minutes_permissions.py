"""授权撤销与纪要权限。

覆盖：
* 撤销后旧账号立即不能看纪要/附件/视频入口，且拒绝写入访问记录；
* 监护人不能写/改纪要——包括“同时是教师（别班）的家长”，不能以家长
  身份修改会谈结论；
* 本班任课教师可以读写，其他班教师不行；
* 待办必须有负责人与期限，逾期与完成状态正确。
"""
import unittest

from app.errors import AppError, PermissionError_
from tests.helpers import build_world, temp_paths


class MinutesPermissionTests(unittest.TestCase):
    def setUp(self):
        self.db, self.files, self.tmp = temp_paths()
        self.conn, self.svc, self.ids = build_world(str(self.db), str(self.files))
        i = self.ids
        bk = self.svc.request_booking(i["ma"], i["s1"], 1, "offline",
                                      "数学", slot_id=i["slots"][0])
        self.bid = bk["booking_id"]
        self.svc.confirm_booking(i["wang"], self.bid)
        self.mid = self.svc.complete_meeting(
            i["wang"], self.bid, "结论：保持每日阅读。",
            overtime=True,
            action_items=[{"content": "每日口算 10 分钟",
                           "owner_id": i["ma"], "due_date": "2026-10-20"},
                          {"content": "老师回访",
                           "owner_id": i["wang"], "due_date": "2026-10-01"}],
            attachments=["作业样本.txt"])

    def tearDown(self):
        self.conn.close()

    def test_guardian_reads_own_child_minutes(self):
        m = self.svc.view_minutes(self.ids["ma"], self.mid)
        self.assertEqual(m["summary"], "结论：保持每日阅读。")
        self.assertTrue(m["overtime"])
        states = {a["content"]: a for a in m["action_items"]}
        self.assertTrue(states["老师回访"]["overdue"])   # 10-01 已逾期
        self.assertFalse(states["每日口算 10 分钟"]["overdue"])
        self.assertEqual(len(m["attachments"]), 1)

    def test_other_guardian_denied_and_logged(self):
        # hong 是同班另一名学生 s2 的家长：不能看 s1 的纪要
        with self.assertRaises(PermissionError_):
            self.svc.view_minutes(self.ids["hong"], self.mid)
        denied = self.conn.execute(
            "SELECT COUNT(*) c FROM access_logs WHERE user_id=? AND ok=0",
            (self.ids["hong"],)).fetchone()["c"]
        self.assertGreaterEqual(denied, 1)
        with self.assertRaises(PermissionError_):
            self.svc.view_attachment(self.ids["hong"], 1)

    def test_revoked_guardian_immediately_loses_access(self):
        # ba 原本可以看
        self.svc.view_minutes(self.ids["ba"], self.mid)
        # 管理员/教师撤销 ba 对 s1 的授权
        self.svc.revoke_guardianship(
            self.ids["admin"], self.ids["s1"], self.ids["ba"])
        with self.assertRaises(PermissionError_):
            self.svc.view_minutes(self.ids["ba"], self.mid)
        with self.assertRaises(PermissionError_):
            self.svc.view_attachment(self.ids["ba"], 1)
        # 撤销后也不能再为该学生预约
        from app.errors import PermissionError_ as PE
        with self.assertRaises(PE):
            self.svc.request_booking(
                self.ids["ba"], self.ids["s1"], 1, "offline",
                "尝试", slot_id=self.ids["slots"][1])

    def test_guardian_cannot_write_or_edit_minutes(self):
        with self.assertRaises(PermissionError_):
            self.svc.complete_meeting(
                self.ids["ma"], self.bid, "家长伪造结论")
        with self.assertRaises(PermissionError_):
            self.svc.edit_minutes(self.ids["ma"], self.mid, "家长篡改结论")

    def test_teacher_of_other_class_cannot_edit_as_guardian(self):
        """zhang 是本班(班1)教师同时是班2学生 s3 的家长——
        对班1纪要：以教师身份可以改；
        对班2纪要：只是家长身份，不能改结论。"""
        # 班1：zhang 任教，可以改
        self.svc.edit_minutes(self.ids["zhang"], self.mid, "教师补充意见")
        # zhang 不教班2，只是班2学生 s3 的家长
        i = self.ids
        self.assertFalse(__import__("app.auth", fromlist=["teaches"]).teaches(
            self.conn, i["zhang"], 2))
        # 伪造一条属于班2的纪要记录，验证 edit_minutes 按班级教师判定
        self.conn.execute(
            "INSERT INTO rounds(id,class_id,title,meeting_date,open_from,open_until)"
            " VALUES (2,2,'班2家长会','2026-10-11','2026-10-01T08:00',"
            "'2026-10-10T20:00')")
        self.conn.execute(
            "INSERT INTO bookings(id,slot_id,round_id,student_id,guardian_id,"
            "mode,status,created_at) VALUES (2,NULL,2,3,4,'offline',"
            "'confirmed','2026-10-02T10:00')")
        cur = self.conn.execute(
            "INSERT INTO minutes(booking_id,student_id,class_id,summary,"
            "created_by,created_at) VALUES (2,3,2,'班2结论',3,'2026-10-02T10:00')")
        mid2 = cur.lastrowid
        # zhang 是 s3 的家长但不任教班2 → 不能改
        with self.assertRaises(PermissionError_):
            self.svc.edit_minutes(self.ids["zhang"], mid2, "家长改结论")
        # 班2教师 chen 可以改
        self.svc.edit_minutes(self.ids["chen"], mid2, "班2教师修改")
        # zhang 作为在权家长仍可读自己孩子的纪要
        m = self.svc.view_minutes(self.ids["zhang"], mid2)
        self.assertEqual(m["summary"], "班2教师修改")

    def test_action_item_requires_owner_and_due(self):
        i = self.ids
        bk = self.svc.request_booking(i["hong"], i["s2"], 1, "video",
                                      "", slot_id=i["slots"][1])
        self.svc.confirm_booking(i["wang"], bk["booking_id"])
        with self.assertRaises(AppError):
            self.svc.complete_meeting(
                i["wang"], bk["booking_id"], "缺负责人",
                action_items=[{"content": "x", "owner_id": None,
                               "due_date": "2026-10-20"}])
        with self.assertRaises(AppError):
            self.svc.complete_meeting(
                i["wang"], bk["booking_id"], "缺期限",
                action_items=[{"content": "x", "owner_id": i["wang"],
                               "due_date": ""}])

    def test_action_item_state_permissions(self):
        item = self.conn.execute(
            "SELECT id, owner_id FROM action_items WHERE minutes_id=? "
            "ORDER BY due_date", (self.mid,)).fetchall()
        by_owner = {row["owner_id"]: row["id"] for row in item}
        mine = by_owner[self.ids["ma"]]           # 期限 10-20
        teacher_item = by_owner[self.ids["wang"]]  # 期限 10-01
        # 负责人 ma 可完成自己的待办
        self.svc.mark_action_item(self.ids["ma"], mine, True)
        self.assertEqual(self.conn.execute(
            "SELECT state FROM action_items WHERE id=?", (mine,))
            .fetchone()["state"], "done")
        # 别人（hong）不能动
        with self.assertRaises(PermissionError_):
            self.svc.mark_action_item(self.ids["hong"], mine, False)
        # 教师可以更新任何待办
        self.svc.mark_action_item(self.ids["wang"], teacher_item, True)


if __name__ == "__main__":
    unittest.main()
