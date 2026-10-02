"""SQLite 连接与建表脚本。

保存：班级/用户/关系、轮次、时段、预约、改期队列、视频会议、纪要、
后续待办、附件与访问记录。所有写操作通过事务完成，关键唯一性约束
（一个学生同一轮只能有一份有效预约、一个时段最多一份已确认预约）
在数据库层用部分唯一索引兜底。
"""
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS classes (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE
);

-- 用户同时可能是教师和家长（用 roles 标记），业务上禁止以家长身份
-- 改会谈结论。teachers.class_id 表示该教师任教/负责的班级。
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    login       TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    roles       TEXT NOT NULL DEFAULT 'guardian'  -- 逗号分隔：teacher,guardian,admin
);

CREATE TABLE IF NOT EXISTS teachers (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id),
    class_id    INTEGER NOT NULL REFERENCES classes(id)
);

CREATE TABLE IF NOT EXISTS students (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    class_id    INTEGER NOT NULL REFERENCES classes(id)
);

-- 监护关系：active=0 表示授权已撤销，旧账号立即失去资料/纪要访问权。
CREATE TABLE IF NOT EXISTS guardianships (
    id          INTEGER PRIMARY KEY,
    student_id  INTEGER NOT NULL REFERENCES students(id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    relation    TEXT NOT NULL DEFAULT '监护人',
    active      INTEGER NOT NULL DEFAULT 1,
    UNIQUE(student_id, user_id)
);

-- 一轮家长会（某班在某个日期范围内开放预约窗口）。
CREATE TABLE IF NOT EXISTS rounds (
    id          INTEGER PRIMARY KEY,
    class_id    INTEGER NOT NULL REFERENCES classes(id),
    title       TEXT NOT NULL,
    meeting_date TEXT NOT NULL,          -- 家长会谈日期 YYYY-MM-DD
    open_from   TEXT NOT NULL,
    open_until  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'  -- open/closed
);

-- 教师发布的时段。status: open/booked/cancelled
-- 教师请假时整段 cancelled；被影响的预约进入 reschedules 队列。
CREATE TABLE IF NOT EXISTS slots (
    id          INTEGER PRIMARY KEY,
    teacher_id  INTEGER NOT NULL REFERENCES users(id),
    round_id    INTEGER NOT NULL REFERENCES rounds(id),
    class_id    INTEGER NOT NULL REFERENCES classes(id),
    start_ts    TEXT NOT NULL,           -- ISO，本地时间
    end_ts      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    note        TEXT NOT NULL DEFAULT '',
    UNIQUE(teacher_id, start_ts)
);

-- mode: offline/video；status: requested/confirmed/reschedule_pending/
-- completed/cancelled。一个学生同一轮只能有一条“有效”预约
-- （非 cancelled/completed 的历史记录允许多条，例如改期旧记录保留审计）。
CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY,
    slot_id      INTEGER REFERENCES slots(id),
    round_id     INTEGER NOT NULL REFERENCES rounds(id),
    student_id   INTEGER NOT NULL REFERENCES students(id),
    guardian_id  INTEGER NOT NULL REFERENCES users(id),
    mode         TEXT NOT NULL CHECK(mode IN ('offline','video')),
    topics       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'requested',
    created_at   TEXT NOT NULL,
    confirmed_at TEXT,
    cancelled_at TEXT
    -- 一个时段至多一条“有效”预约由下方部分唯一索引保证；
    -- 取消/改期后的历史记录 slot_id 置空以便时段复用。
);

-- 改期队列：家长临时改期请求 或 教师请假导致的被动改期。
-- state: pending/approved/rejected/cancelled；kind: guardian/teacher_leave
CREATE TABLE IF NOT EXISTS reschedules (
    id           INTEGER PRIMARY KEY,
    booking_id   INTEGER NOT NULL REFERENCES bookings(id),
    kind         TEXT NOT NULL CHECK(kind IN ('guardian','teacher_leave')),
    reason       TEXT NOT NULL DEFAULT '',
    old_slot_id  INTEGER REFERENCES slots(id),
    new_slot_id  INTEGER REFERENCES slots(id),
    requested_by INTEGER NOT NULL REFERENCES users(id),
    state        TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    decided_at   TEXT
);

-- 模拟视频会议：链接可能失效（active=0），系统可重新生成并推送通知。
CREATE TABLE IF NOT EXISTS video_meetings (
    id          INTEGER PRIMARY KEY,
    booking_id  INTEGER NOT NULL REFERENCES bookings(id),
    url         TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    refreshed_at TEXT
);

-- 会谈纪要：只有本班任课教师与该生在权监护人可读，教师才可写。
CREATE TABLE IF NOT EXISTS minutes (
    id          INTEGER PRIMARY KEY,
    booking_id  INTEGER NOT NULL UNIQUE REFERENCES bookings(id),
    student_id  INTEGER NOT NULL REFERENCES students(id),
    class_id    INTEGER NOT NULL REFERENCES classes(id),
    summary     TEXT NOT NULL DEFAULT '',
    overtime    INTEGER NOT NULL DEFAULT 0,   -- 是否超时
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL,
    updated_at  TEXT
);

-- 后续待办：必须写明负责人与期限。state: open/done。
CREATE TABLE IF NOT EXISTS action_items (
    id          INTEGER PRIMARY KEY,
    minutes_id  INTEGER NOT NULL REFERENCES minutes(id),
    content     TEXT NOT NULL,
    owner_id    INTEGER NOT NULL REFERENCES users(id),
    owner_label TEXT NOT NULL,             -- 角色/姓名冗余，便于展示
    due_date    TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'open',
    completed_at TEXT
);

-- 本地虚构附件（路径在本地 files 目录，内容为占位文件）。
CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY,
    minutes_id  INTEGER NOT NULL REFERENCES minutes(id),
    filename    TEXT NOT NULL,
    path        TEXT NOT NULL
);

-- 访问记录：谁在什么时候读了哪份纪要（含拒绝尝试）。
CREATE TABLE IF NOT EXISTS access_logs (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    minutes_id  INTEGER REFERENCES minutes(id),
    action      TEXT NOT NULL,             -- view_minutes/view_attachment/denied
    ok          INTEGER NOT NULL DEFAULT 1,
    at          TEXT NOT NULL
);

-- 通知发送记录（模拟适配器）。
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    channel     TEXT NOT NULL,             -- sms/email/inapp
    kind        TEXT NOT NULL,
    body        TEXT NOT NULL,
    at          TEXT NOT NULL
);

-- 一个时段至多一条“有效”预约：已取消/已完成的不占位。
CREATE UNIQUE INDEX IF NOT EXISTS idx_slot_active_booking
    ON bookings(slot_id) WHERE status NOT IN ('cancelled');

-- 一个学生同一轮家长会只能保留一份有效预约。
CREATE UNIQUE INDEX IF NOT EXISTS idx_student_round_active
    ON bookings(student_id, round_id) WHERE status NOT IN ('cancelled');

-- 一份预约至多一场有效视频会议。
CREATE UNIQUE INDEX IF NOT EXISTS idx_booking_active_video
    ON video_meetings(booking_id) WHERE active = 1;
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False：每个工作线程仍使用各自的连接；
    # 测试中的并发线程也在线程内新建连接，这里只是放开同一连接跨线程限制。
    conn = sqlite3.connect(db_path, timeout=15, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
