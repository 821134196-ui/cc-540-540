"""测试夹具：在临时目录构建最小可演示世界。"""
import tempfile
from pathlib import Path

from app import db as dbmod
from app.adapters import AttachmentStore, NotificationAdapter, VideoAdapter
from app.services import ConferenceService

CLOCK = "2026-10-02T10:00:00"


class FixedClock:
    def __init__(self, ts=CLOCK):
        self.ts = ts

    def __call__(self):
        return self.ts


def service_for(conn, files_dir, clock=FixedClock()):
    return ConferenceService(
        conn, VideoAdapter(conn), NotificationAdapter(conn),
        AttachmentStore(conn, str(files_dir)), clock)


def build_world(db_path: str, files_dir: str):
    """返回 id 映射。结构：
    班级1：教师 wang、zhang（zhang 同时是班级2学生 s3 的家长）
    班级2：教师 chen
    s1（班1）监护人 ma、ba；s2（班1）监护人 hong；s3（班2）监护人 zhang、qiang
    """
    conn = dbmod.connect(db_path)
    dbmod.init_db(conn)
    conn.executescript("""
      INSERT INTO classes(id,name) VALUES (1,'一班'),(2,'二班');
      INSERT INTO users(id,login,name,roles) VALUES
        (1,'admin','管理员','admin'),
        (2,'wang','王老师','teacher'),
        (3,'chen','陈老师','teacher'),
        (4,'zhang','张老师','teacher,guardian'),
        (5,'ma','马妈妈','guardian'),
        (6,'ba','巴爸爸','guardian'),
        (7,'hong','洪妈妈','guardian'),
        (8,'qiang','强爸爸','guardian');
      INSERT INTO teachers(user_id,class_id) VALUES (2,1),(4,1),(3,2);
      INSERT INTO students(id,name,class_id) VALUES
        (1,'学生甲',1),(2,'学生乙',1),(3,'学生丙',2);
      INSERT INTO guardianships(student_id,user_id,relation) VALUES
        (1,5,'妈妈'),(1,6,'爸爸'),
        (2,7,'妈妈'),
        (3,4,'爸爸'),(3,8,'妈妈');
      INSERT INTO rounds(id,class_id,title,meeting_date,open_from,open_until)
      VALUES (1,1,'十月家长会','2026-10-10',
              '2026-10-01T08:00','2026-10-09T20:00');
    """)
    svc = service_for(conn, files_dir)
    slot_ids = svc.publish_slots(2, 1, [
        {"start_ts": "2026-10-10T09:00", "end_ts": "2026-10-10T09:20"},
        {"start_ts": "2026-10-10T09:20", "end_ts": "2026-10-10T09:40"},
        {"start_ts": "2026-10-10T09:40", "end_ts": "2026-10-10T10:00"},
        {"start_ts": "2026-10-10T10:00", "end_ts": "2026-10-10T10:20"},
    ])
    ids = {"admin": 1, "wang": 2, "chen": 3, "zhang": 4, "ma": 5, "ba": 6,
           "hong": 7, "qiang": 8, "s1": 1, "s2": 2, "s3": 3,
           "round1": 1, "slots": slot_ids}
    return conn, svc, ids


def temp_paths():
    tmp = Path(tempfile.mkdtemp())
    return tmp / "t.db", tmp / "files", tmp
