"""种子数据：三年二班、教师、学生、多监护人、时段与一轮预约，
并预置一条教师请假样例（自动触发改期队列）。

可重复执行：检测到数据库已有数据则跳过（--force 可重建）。
"""
from __future__ import annotations

import sys
from pathlib import Path

from .adapters import get_adapters, LocalAttachmentStore
from .db import connect, init_db
from .services import Service


def seed(db_path: str = "data/app.db", attach_root: str = "data/attachments",
         force: bool = False) -> Service:
    if force and Path(db_path).exists():
        Path(db_path).unlink()
    conn = connect(db_path)
    init_db(conn)
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]:
        return Service(conn)

    conn, notifier, video, _ = get_adapters(conn, db_path, attach_root)
    svc = Service(conn, notifier, video)

    # ---------- 用户
    def add_user(username, display, role):
        cur = conn.execute(
            "INSERT INTO users(username, display_name, role) VALUES (?,?,?)",
            (username, display, role))
        return cur.lastrowid

    uid_wang = add_user("wangzl", "王正梁", "teacher")
    uid_li = add_user("lijing", "李静", "teacher")
    uid_chen = add_user("chenyue", "陈悦", "teacher")

    uid_zw = add_user("zhang_wei", "张伟（父亲）", "guardian")
    uid_zm = add_user("zhang_min", "张敏（母亲）", "guardian")
    uid_lm = add_user("lin_mu", "林木（母亲）", "guardian")
    uid_zl = add_user("zhou_lan", "周兰（外婆，授权已撤销）", "guardian")
    uid_admin = add_user("admin", "教务管理员", "admin")

    # ---------- 班级 / 教师
    cid = conn.execute("INSERT INTO classes(name) VALUES ('三年二班')").lastrowid
    tid_wang = conn.execute(
        "INSERT INTO teachers(name, subject, user_id) VALUES ('王正梁','班主任·语文',?)",
        (uid_wang,)).lastrowid
    tid_li = conn.execute(
        "INSERT INTO teachers(name, subject, user_id) VALUES ('李静','数学',?)",
        (uid_li,)).lastrowid
    tid_chen = conn.execute(
        "INSERT INTO teachers(name, subject, user_id) VALUES ('陈悦','英语',?)",
        (uid_chen,)).lastrowid
    conn.executemany(
        "INSERT INTO class_teachers(class_id, teacher_id, is_homeroom) VALUES (?,?,?)",
        [(cid, tid_wang, 1), (cid, tid_li, 0), (cid, tid_chen, 0)])

    # ---------- 学生
    sid_zhang = conn.execute(
        "INSERT INTO students(name, class_id) VALUES ('张一诺',?)", (cid,)).lastrowid
    sid_lin = conn.execute(
        "INSERT INTO students(name, class_id) VALUES ('林小夏',?)", (cid,)).lastrowid
    sid_zhao = conn.execute(
        "INSERT INTO students(name, class_id) VALUES ('赵北辰',?)", (cid,)).lastrowid

    # ---------- 监护授权（同一学生多个监护人；一条已撤销）
    conn.executemany(
        "INSERT INTO guardianships(student_id, guardian_user_id, relation, revoked, revoked_at)"
        " VALUES (?,?,?,?,?)",
        [(sid_zhang, uid_zw, "父亲", 0, None),
         (sid_zhang, uid_zm, "母亲", 0, None),
         (sid_zhang, uid_zl, "外婆", 1, "2026-09-20 10:00"),
         (sid_lin, uid_lm, "母亲", 0, None)])

    # ---------- 预约窗口（当前时间落在窗口内）
    rid = conn.execute(
        "INSERT INTO rounds(class_id, title, open_at, close_at, created_by) VALUES (?,?,?,?,?)",
        (cid, "2026 秋季家长会", "2026-09-25 08:00", "2026-10-10 20:00", uid_wang)).lastrowid

    def slot(teacher_id, start, end, location="本班教室", status="open"):
        cur = conn.execute(
            "INSERT INTO time_slots(round_id, class_id, teacher_id, start_at, end_at, location, status)"
            " VALUES (?,?,?,?,?,?,?)",
            (rid, cid, teacher_id, start, end, location, status))
        return cur.lastrowid

    # 王正梁（班主任）10/09 周五时段
    s1 = slot(tid_wang, "2026-10-09 18:00", "2026-10-09 18:20")
    s2 = slot(tid_wang, "2026-10-09 18:20", "2026-10-09 18:40")
    s3 = slot(tid_wang, "2026-10-09 18:40", "2026-10-09 19:00")
    s4 = slot(tid_wang, "2026-10-09 19:00", "2026-10-09 19:20")
    s5 = slot(tid_wang, "2026-10-09 19:20", "2026-10-09 19:40")
    # 李静（数学）10/09 视频时段
    s6 = slot(tid_li, "2026-10-09 18:00", "2026-10-09 18:25", "线上视频")
    s7 = slot(tid_li, "2026-10-09 18:25", "2026-10-09 18:50", "线上视频")
    s8 = slot(tid_li, "2026-10-09 18:50", "2026-10-09 19:15", "线上视频")
    # 陈悦（英语）10/10
    s9 = slot(tid_chen, "2026-10-10 09:00", "2026-10-10 09:20")
    s10 = slot(tid_chen, "2026-10-10 09:20", "2026-10-10 09:40")

    # ---------- 样例预约
    wang = svc.get_user(uid_wang)
    zw = svc.get_user(uid_zw)
    zm = svc.get_user(uid_zm)
    lm = svc.get_user(uid_lm)

    # 张伟为张一诺预约 s1 线下，已确认；稍后教师写入纪要与后续待办
    b1 = svc.book_slot(zw, s1, sid_zhang, "offline", "近期课堂专注情况与阅读计划", now="2026-09-28 09:00")
    svc.confirm_booking(wang, b1)
    # 同轮第二份有效预约应被拒绝（留给测试覆盖；种子不制造失败数据）

    # 张敏（第二位监护人）为林小夏？不行——她只监护张一诺。改由林木为林小夏预约视频
    b2 = svc.book_slot(lm, s6, sid_lin, "video", "数学计算粗心，想了解练习方法", now="2026-09-28 09:05")
    svc.confirm_booking(wang, b2)

    # 张敏也想为张一诺抢 s3（不同时段）——同一学生同一轮只能一份有效预约，
    # 种子里直接以“待确认但稍后取消”的路径不合适；改为保留一条张敏视角的被拒日志，
    # 由应用层异常演示。这里只写一条访问记录说明规则。
    conn.execute(
        "INSERT INTO access_logs(user_id, username, action, entity_type, entity_id, granted, detail)"
        " VALUES (?,?,?,?,?,0,?)",
        (uid_zm, "zhang_min", "book_blocked", "student", sid_zhang,
         "同一轮家长会已有有效预约（booking=%d），第二位监护人不能再约" % b1))
    conn.commit()

    # ---------- 把 b1 标记完成并写入纪要 + 后续待办（含一条已逾期样例）
    svc.complete_booking(wang, b1, now="2026-10-09 18:33")  # 超时 13 分钟
    minute_id = svc.save_minutes(
        wang, b1,
        "孩子课堂参与积极，阅读理解有进步；约定每天亲子共读 20 分钟，"
        "两周后带读书卡回校交流。数学作业建议固定时间完成并自查。",
        [
            {"content": "每天陪孩子亲子共读 20 分钟，填写读书卡",
             "owner_label": "张伟（父亲）", "owner_user_id": uid_zw,
             "due_date": "2026-10-23"},
            {"content": "整理数学错题本，每周三晚间复盘",
             "owner_label": "张敏（母亲）", "owner_user_id": uid_zm,
             "due_date": "2026-10-14"},
            {"content": "在班级图书角补充两本桥梁书",
             "owner_label": "王正梁（班主任）", "owner_user_id": uid_wang,
             "due_date": "2026-09-30"},  # 已逾期，用于“后续事项未完成”提醒样例
        ],
        overtime_minutes=13, finalize=True)

    # ---------- 本地虚构附件
    store = LocalAttachmentStore(attach_root)
    files = {
        f"student_{sid_zhang}_reading_note.txt": (
            "张一诺 阅读观察记录（虚构样例）\n\n"
            "9 月在校阅读量明显上升，能主动复述故事大意；建议继续桥梁书阅读。\n"
            "—— 王正梁 2026-09-24\n"),
        f"student_{sid_lin}_math_snapshot.txt": (
            "林小夏 数学课堂小测情况（虚构样例）\n\n"
            "计算速度快但检查习惯待加强；近三次小测因粗心失分 2~4 分。\n"
            "—— 李静 2026-09-23\n"),
    }
    for name, content in files.items():
        path = store.put(name, content)
        student_id = sid_zhang if str(sid_zhang) in name else sid_lin
        conn.execute(
            "INSERT INTO attachments(student_id, minute_id, filename, local_path, uploaded_by)"
            " VALUES (?,?,?,?,?)",
            (student_id, minute_id if student_id == sid_zhang else None,
             name, str(path), uid_wang))
    conn.commit()

    # ---------- 教师请假样例：赵北辰的预约 + 王正梁 10/09 傍晚临时请假
    # 第三个家庭：赵北辰与父亲赵建国
    uid_zhao_dad = conn.execute(
        "INSERT INTO users(username, display_name, role) VALUES ('zhao_dad','赵建国（父亲）','guardian')"
    ).lastrowid
    conn.execute(
        "INSERT INTO guardianships(student_id, guardian_user_id, relation) VALUES (?,?,?)",
        (sid_zhao, uid_zhao_dad, "父亲"))
    conn.commit()
    b3 = svc.book_slot(svc.get_user(uid_zhao_dad), s3, sid_zhao, "offline",
                       "想了解新学期的同伴相处情况", now="2026-09-29 10:00")
    svc.confirm_booking(wang, b3)

    # s2(18:20-18:40) 与假期重叠、空闲 -> 自动取消；
    # s3(18:40-19:00，赵北辰已确认) 重叠 -> 进入教师请假改期队列；
    # s4(19:00-19:20)/s5(19:20-19:40) 在假期之后、首尾不相撞 -> 保留，家长可自助重选。
    result = svc.report_teacher_leave(
        wang, tid_wang, "2026-10-09 18:30", "2026-10-09 19:00",
        "临时参加区教研会议")

    # 给林小夏的视频样例补一条“链接失效”记录，供页面演示轮换：
    # 将 b2 的活动链接置为失效（教师可在页面一键轮换并通知家长）
    entry = video.active_entry(b2)
    if entry:
        video.expire_entry(entry["id"])
        conn.execute(
            "INSERT INTO access_logs(user_id, username, action, entity_type, entity_id, granted, detail)"
            " VALUES (?,?,?,?,?,0,'视频链接已失效，等待教师轮换')",
            (uid_lm, "lin_mu", "video_expired", "booking", b2))
        conn.commit()

    print(f"种子完成：班级 {cid}，窗口 {rid}，教师请假改期队列 {result['queued_bookings']}，"
          f"取消空闲时段 {result['cancelled_slots']}")
    return svc


if __name__ == "__main__":
    seed(force="--force" in sys.argv)
