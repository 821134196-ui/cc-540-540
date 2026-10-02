"""测试共用夹具：内存 SQLite + 最小数据集。"""
from __future__ import annotations

import unittest

from app.adapters import MockNotificationAdapter, MockVideoAdapter
from app.db import connect, init_db
from app.services import Service


class AppCase(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        init_db(self.conn)
        self.notifier = MockNotificationAdapter(self.conn)
        self.video = MockVideoAdapter(self.conn)
        self.svc = Service(self.conn, self.notifier, self.video)

        # 用户先于教师（teachers.user_id 外键）
        self.wang = self.add_user("wang", "王老师", "teacher")
        self.li = self.add_user("li", "李老师", "teacher")
        self.admin = self.add_user("admin", "管理员", "admin")
        self.dad = self.add_user("dad", "爸爸", "guardian")
        self.mom = self.add_user("mom", "妈妈", "guardian")
        self.granny = self.add_user("granny", "外婆", "guardian")
        self.other_parent = self.add_user("other", "乙妈妈", "guardian")
        # 非本班教师，用于跨班权限测试
        self.outsider_t = self.add_user("out_t", "外班老师", "teacher")

        self.cid = self.add_class("测试一班")
        self.cid2 = self.add_class("测试二班")
        self.t_wang = self.add_teacher(self.wang["id"], "王老师", "班主任", self.cid, hr=True)
        self.t_li = self.add_teacher(self.li["id"], "李老师", "数学", self.cid)
        self.t_out = self.add_teacher(self.outsider_t["id"], "外班老师", "语文", self.cid2, hr=True)

        self.sid = self.add_student("学生甲", self.cid)
        self.sid2 = self.add_student("学生乙", self.cid)
        self.conn.executemany(
            "INSERT INTO guardianships(student_id, guardian_user_id, relation) VALUES (?,?,?)",
            [(self.sid, self.dad["id"], "父亲"),
             (self.sid, self.mom["id"], "母亲"),
             (self.sid, self.granny["id"], "外婆"),
             (self.sid2, self.other_parent["id"], "母亲")])
        self.conn.commit()

        self.rid = self.svc.publish_round(
            self.wang, self.cid, "测试窗口", "2026-09-01 08:00", "2026-10-31 20:00")

    # ------------------------------------------------------------ helpers

    def add_user(self, username, display, role):
        cur = self.conn.execute(
            "INSERT INTO users(username, display_name, role) VALUES (?,?,?)",
            (username, display, role))
        return self.conn.execute("SELECT * FROM users WHERE id=?",
                                 (cur.lastrowid,)).fetchone()

    def add_class(self, name):
        return self.conn.execute("INSERT INTO classes(name) VALUES (?)", (name,)).lastrowid

    def add_teacher(self, user_id, name, subject, class_id, hr=False):
        tid = self.conn.execute(
            "INSERT INTO teachers(name, subject, user_id) VALUES (?,?,?)",
            (name, subject, user_id)).lastrowid
        self.conn.execute(
            "INSERT INTO class_teachers(class_id, teacher_id, is_homeroom) VALUES (?,?,?)",
            (class_id, tid, 1 if hr else 0))
        return tid

    def add_student(self, name, class_id):
        return self.conn.execute(
            "INSERT INTO students(name, class_id) VALUES (?,?)", (name, class_id)).lastrowid

    def slot(self, teacher_id, start, end, location="本班教室"):
        return self.svc.create_slot(self.wang, self.rid, teacher_id, start, end, location)

    def book(self, user, slot_id, student_id=None, mode="offline", topics="t",
             now="2026-09-20 09:00"):
        return self.svc.book_slot(user, slot_id, student_id or self.sid,
                                  mode, topics, now=now)
