"""时段冲突：多个家庭 / 多个监护人并发抢同一时段。"""
import threading
import unittest

from app import db as dbmod
from app.errors import ConflictError
from tests.helpers import build_world, service_for, temp_paths


class SlotConflictTests(unittest.TestCase):
    def setUp(self):
        self.db, self.files, self.tmp = temp_paths()
        self.conn, self.svc, self.ids = build_world(str(self.db), str(self.files))

    def tearDown(self):
        self.conn.close()

    def _fresh_service(self):
        c = dbmod.connect(str(self.db))
        self.addCleanup(c.close)
        return service_for(c, str(self.files)), c

    def test_two_families_race_same_slot_only_one_wins(self):
        """不同学生的两个家庭抢同一时段：恰好一份成功。"""
        slot = self.ids["slots"][0]
        results = []
        barrier = threading.Barrier(2)

        def book(guardian, student):
            svc, _ = self._fresh_service()
            barrier.wait()
            try:
                svc.request_booking(guardian, student, 1, "offline",
                                    "面谈", slot_id=slot)
                results.append("ok")
            except ConflictError:
                results.append("conflict")

        t1 = threading.Thread(target=book, args=(self.ids["ma"], self.ids["s1"]))
        t2 = threading.Thread(target=book, args=(self.ids["hong"], self.ids["s2"]))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(sorted(results), ["conflict", "ok"])
        row = self.conn.execute(
            "SELECT status FROM slots WHERE id=?", (slot,)).fetchone()
        self.assertEqual(row["status"], "booked")
        active = self.conn.execute(
            "SELECT COUNT(*) c FROM bookings WHERE slot_id=? "
            "AND status NOT IN ('cancelled')", (slot,)).fetchone()
        self.assertEqual(active["c"], 1)

    def test_two_guardians_same_student_race_only_one_booking(self):
        """同一学生的两个监护人并发抢约：一个学生一轮只留一份有效预约。"""
        slot_a, slot_b = self.ids["slots"][0], self.ids["slots"][1]
        results = []
        barrier = threading.Barrier(2)

        def book(guardian, slot):
            svc, _ = self._fresh_service()
            barrier.wait()
            try:
                svc.request_booking(guardian, self.ids["s1"], 1, "video",
                                    "讨论", slot_id=slot)
                results.append("ok")
            except ConflictError:
                results.append("conflict")

        t1 = threading.Thread(target=book, args=(self.ids["ma"], slot_a))
        t2 = threading.Thread(target=book, args=(self.ids["ba"], slot_b))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(sorted(results), ["conflict", "ok"])
        active = self.conn.execute(
            "SELECT COUNT(*) c FROM bookings WHERE student_id=? "
            "AND status NOT IN ('cancelled')", (self.ids["s1"],)).fetchone()
        self.assertEqual(active["c"], 1)
        # 抢到的家庭锁定的是其中一个时段，另一个必须仍可被其他家庭预约
        open_slot = self.conn.execute(
            "SELECT COUNT(*) c FROM slots WHERE status='open' "
            "AND id IN (?,?)", (slot_a, slot_b)).fetchone()
        self.assertGreaterEqual(open_slot["c"], 1)

    def test_sequential_second_guardian_rejected(self):
        slot = self.ids["slots"][0]
        self.svc.request_booking(self.ids["ma"], self.ids["s1"], 1,
                                 "offline", "", slot_id=slot)
        with self.assertRaises(ConflictError):
            self.svc.request_booking(self.ids["ba"], self.ids["s1"], 1,
                                     "offline", "",
                                     slot_id=self.ids["slots"][1])

    def test_cancelled_then_slot_reusable_and_rebook_allowed(self):
        slot = self.ids["slots"][0]
        bk = self.svc.request_booking(self.ids["ma"], self.ids["s1"], 1,
                                      "offline", "", slot_id=slot)
        self.svc.cancel_by_guardian(self.ids["ma"], bk["booking_id"])
        row = self.conn.execute(
            "SELECT status FROM slots WHERE id=?", (slot,)).fetchone()
        self.assertEqual(row["status"], "open")
        # 另一个家庭可以重新约这个时段
        self.svc.request_booking(self.ids["hong"], self.ids["s2"], 1,
                                 "offline", "", slot_id=slot)


if __name__ == "__main__":
    unittest.main()
