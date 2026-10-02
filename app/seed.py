"""种子数据：重建一个可演示的数据库。

用法：
    python -m app.seed             # 数据库不存在时初始化
    python -m app.seed --reset     # 删除后重建

内含教师请假样例：李华老师请假，她 10/10 的时段全部取消，
小红家已确认的预约自动进入改期队列并收到（模拟）通知。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import db as dbmod
from .adapters import AttachmentStore, NotificationAdapter, VideoAdapter
from .services import ConferenceService

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "conference.db"
FILES_DIR = DATA_DIR / "files"

DEMO_PASSWORD = "123456"  # 演示环境统一密码


class FixedClock:
    def __init__(self, ts: str):
        self.ts = ts

    def set(self, ts: str):
        self.ts = ts

    def __call__(self) -> str:
        return self.ts


def build(conn):
    conn.executescript("""
      INSERT INTO classes(id,name) VALUES
        (1,'三年级1班'),(2,'三年级2班');

      INSERT INTO users(id,login,name,roles) VALUES
        (1,'admin','系统管理员','admin'),
        (2,'wang','王敏','teacher'),      -- 三(1)班班主任/语文
        (3,'li','李华','teacher'),        -- 三(1)班数学（样例中请假）
        (4,'chen','陈静','teacher'),      -- 三(2)班教师
        (5,'zhang','张伟','teacher,guardian'), -- 三(1)任课教师，同时是别班家长
        (6,'ma','刘芳','guardian'),       -- 小明妈妈
        (7,'ba','赵军','guardian'),       -- 小明爸爸
        (8,'hongma','孙丽','guardian'),   -- 小红妈妈
        (9,'old','周伟','guardian'),      -- 小红的旧账号：授权将被撤销
        (10,'qiangma','吴娟','guardian'); -- 小强妈妈

      INSERT INTO teachers(user_id,class_id) VALUES
        (2,1),(3,1),(5,1),(4,2);

      INSERT INTO students(id,name,class_id) VALUES
        (1,'赵小明',1),(2,'孙小红',1),(3,'张小强',2);

      INSERT INTO guardianships(student_id,user_id,relation) VALUES
        (1,6,'妈妈'),(1,7,'爸爸'),
        (2,8,'妈妈'),(2,9,'其他监护人'),
        (3,5,'爸爸'),(3,10,'妈妈');
    """)

    uid = {r["login"]: r["id"] for r in conn.execute(
        "SELECT id,login FROM users")}

    # 上一轮（已结束）与本轮家长会
    conn.executescript("""
      INSERT INTO rounds(id,class_id,title,meeting_date,open_from,open_until)
      VALUES
        (1,1,'三(1)班九月家访家长会','2026-09-25',
             '2026-09-20T08:00','2026-09-25T12:00'),
        (2,1,'三(1)班秋季家长会','2026-10-10',
             '2026-10-01T08:00','2026-10-09T20:00'),
        (3,2,'三(2)班秋季家长会','2026-10-10',
             '2026-10-01T08:00','2026-10-09T20:00');
    """)

    files = AttachmentStore(conn, str(FILES_DIR))
    past = FixedClock("2026-09-25T08:30:00")
    svc_past = ConferenceService(
        conn, VideoAdapter(conn), NotificationAdapter(conn), files, past)

    # ---- 上一轮：王老师发布时段，小明家视频会谈并已完成 ----
    past.set("2026-09-20T09:00:00")
    svc_past.publish_slots(uid["wang"], 1, [
        {"start_ts": "2026-09-25T09:00", "end_ts": "2026-09-25T09:20"},
        {"start_ts": "2026-09-25T09:20", "end_ts": "2026-09-25T09:40"},
    ])
    slots = svc_past.available_slots(1)
    bk = svc_past.request_booking(
        uid["ma"], 1, 1, "video", "近期注意力与作业完成情况",
        slot_id=slots[0]["id"])
    past.set("2026-09-20T09:05:00")
    svc_past.confirm_booking(uid["wang"], bk["booking_id"])
    past.set("2026-09-25T09:22:00")  # 记录时已超时 2 分钟
    svc_past.complete_meeting(
        uid["wang"], bk["booking_id"],
        "小明课堂参与积极，数学计算准确率提升；建议继续保持每日阅读，"
        "家长陪同做口算练习。本次会谈超时约2分钟。",
        overtime=True,
        action_items=[
            {"content": "每天陪读 20 分钟并在联系本签字",
             "owner_id": uid["ma"], "due_date": "2026-10-15"},
            {"content": "准备口算练习单并两周后回访",
             "owner_id": uid["wang"], "due_date": "2026-10-12"},
        ],
        attachments=["小明课堂表现摘录.txt"])

    # ---- 本轮：王老师、李老师发布 10/10 时段 ----
    clock = FixedClock("2026-10-02T10:00:00")
    svc = ConferenceService(
        conn, VideoAdapter(conn), NotificationAdapter(conn), files, clock)
    svc.publish_slots(uid["wang"], 2, [
        {"start_ts": "2026-10-10T09:00", "end_ts": "2026-10-10T09:20"},
        {"start_ts": "2026-10-10T09:20", "end_ts": "2026-10-10T09:40"},
        {"start_ts": "2026-10-10T09:40", "end_ts": "2026-10-10T10:00"},
        {"start_ts": "2026-10-10T10:40", "end_ts": "2026-10-10T11:00"},
    ])
    svc.publish_slots(uid["li"], 2, [
        {"start_ts": "2026-10-10T09:40", "end_ts": "2026-10-10T10:00"},
        {"start_ts": "2026-10-10T10:00", "end_ts": "2026-10-10T10:20"},
        {"start_ts": "2026-10-10T10:20", "end_ts": "2026-10-10T10:40"},
    ])
    svc.publish_slots(uid["chen"], 3, [
        {"start_ts": "2026-10-10T09:00", "end_ts": "2026-10-10T09:20"},
        {"start_ts": "2026-10-10T09:20", "end_ts": "2026-10-10T09:40"},
        {"start_ts": "2026-10-10T09:40", "end_ts": "2026-10-10T10:00"},
    ])

    open_slots = {s["start_ts"]: s["id"]
                  for s in svc.available_slots(2) if s["teacher_name"] == "王敏"}
    # 小明家：视频会谈，王老师 09:00，已确认
    bk_ming = svc.request_booking(
        uid["ma"], 1, 2, "video", "想讨论阅读习惯和数学口算",
        slot_id=open_slots["2026-10-10T09:00"])
    svc.confirm_booking(uid["wang"], bk_ming["booking_id"])

    # 小红家：线下会谈，李老师 10:00，已确认（随后被教师请假打断）
    li_1000 = conn.execute(
        "SELECT id FROM slots WHERE teacher_id=? AND start_ts=?",
        (uid["li"], "2026-10-10T10:00")).fetchone()["id"]
    bk_hong = svc.request_booking(
        uid["hongma"], 2, 2, "offline", "近期作文进步情况与体育课安全",
        slot_id=li_1000)
    svc.confirm_booking(uid["li"], bk_hong["booking_id"])

    # ★ 教师请假样例：李华老师 10/10 临时请假
    svc.teacher_leave(
        uid["admin"], uid["li"], 2, "临时外出教研，10月10日全天请假")

    # 撤销小红旧账号的监护授权（旧账号立即不能再看纪要）
    svc.revoke_guardianship(uid["admin"], 2, uid["old"])


def main(argv):
    reset = "--reset" in argv
    if reset and DB_PATH.exists():
        for p in (DB_PATH, DB_PATH.with_suffix(".db-wal"),
                  DB_PATH.with_suffix(".db-shm")):
            p.unlink(missing_ok=True)
        import shutil
        if FILES_DIR.exists():
            shutil.rmtree(FILES_DIR)
    if DB_PATH.exists() and not reset:
        print(f"数据库已存在：{DB_PATH}（如需重建使用 --reset）")
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = dbmod.connect(str(DB_PATH))
    dbmod.init_db(conn)
    try:
        build(conn)
    finally:
        conn.close()

    print(f"种子数据已写入：{DB_PATH}")
    print(f"虚构附件目录：{FILES_DIR}")
    print("演示账号（密码均为 %s）：" % DEMO_PASSWORD)
    print("  admin   系统管理员（可代登记请假、撤销授权）")
    print("  wang    王敏 三(1)班教师   li  李华（请假样例）")
    print("  ma / ba 小明妈妈/爸爸（多监护人抢约测试）")
    print("  hongma  小红妈妈   old 小红旧账号（授权已撤销）")
    print("  zhang   张伟：三(1)教师 + 三(2)学生家长（跨身份权限）")


if __name__ == "__main__":
    main(sys.argv[1:])
