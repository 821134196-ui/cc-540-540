"""视频链接：创建、失效、刷新、入口鉴权。"""
import unittest

from app.errors import ConflictError, PermissionError_
from tests.helpers import build_world, temp_paths


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.db, self.files, self.tmp = temp_paths()
        self.conn, self.svc, self.ids = build_world(str(self.db), str(self.files))
        i = self.ids
        self.video_bid = self.svc.request_booking(
            i["ma"], i["s1"], 1, "video", "视频会谈",
            slot_id=i["slots"][0])["booking_id"]
        self.svc.confirm_booking(i["wang"], self.video_bid)
        self.off_bid = self.svc.request_booking(
            i["hong"], i["s2"], 1, "offline", "线下",
            slot_id=i["slots"][1])["booking_id"]
        self.svc.confirm_booking(i["wang"], self.off_bid)

    def tearDown(self):
        self.conn.close()

    def test_entry_visible_to_parties_only(self):
        entry = self.svc.video_entry(self.ids["ma"], self.video_bid)
        self.assertTrue(entry["active"])
        self.assertIn("https://video.example.test/mtg/", entry["url"])
        # 本班教师可进入
        self.assertTrue(self.svc.video_entry(
            self.ids["wang"], self.video_bid)["active"])
        # 无关监护人不行
        with self.assertRaises(PermissionError_):
            self.svc.video_entry(self.ids["hong"], self.video_bid)

    def test_offline_has_no_video_entry(self):
        from app.errors import AppError
        with self.assertRaises(AppError):
            self.svc.video_entry(self.ids["hong"], self.off_bid)

    def test_invalidate_then_refresh(self):
        self.assertTrue(self.svc.video.invalidate(self.video_bid))
        entry = self.svc.video_entry(self.ids["ma"], self.video_bid)
        self.assertFalse(entry["active"])
        self.assertIsNone(entry["url"])
        # 链接有效时刷新应被拒绝
        self.svc.video.refresh(self.video_bid, "2026-10-02T11:00:00")
        with self.assertRaises(ConflictError):
            self.svc.refresh_video(self.ids["ma"], self.video_bid)
        # 再次失效后，监护人刷新并通知教师
        self.svc.video.invalidate(self.video_bid)
        url = self.svc.refresh_video(self.ids["ma"], self.video_bid)
        self.assertTrue(self.svc.video_entry(
            self.ids["ma"], self.video_bid)["active"])
        note = self.conn.execute(
            "SELECT body FROM notifications WHERE user_id=? AND kind="
            "'video_refreshed' ORDER BY id DESC LIMIT 1",
            (self.ids["wang"],)).fetchone()
        self.assertIn(url, note["body"])
        # 旧链接记录失效，全场只有一条有效会议
        active = self.conn.execute(
            "SELECT COUNT(*) c FROM video_meetings WHERE booking_id=? "
            "AND active=1", (self.video_bid,)).fetchone()["c"]
        self.assertEqual(active, 1)

    def test_revoked_guardian_cannot_refresh(self):
        self.svc.revoke_guardianship(
            self.ids["admin"], self.ids["s1"], self.ids["ba"])
        # ba 从未是会谈参与方，且授权撤销后更无权
        with self.assertRaises(PermissionError_):
            self.svc.refresh_video(self.ids["ba"], self.video_bid)


if __name__ == "__main__":
    unittest.main()
