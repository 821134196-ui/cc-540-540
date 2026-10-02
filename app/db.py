"""SQLite 连接层与表结构。

所有时间以 ``YYYY-MM-DD HH:MM`` 文本存储（零填充，可直接按字典序比较）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 用户：教师 / 监护人 / 管理员（角色严格分离，教师不能以家长身份操作）
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    password    TEXT NOT NULL DEFAULT 'pass1234',
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('teacher','guardian','admin'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS classes (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teachers (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    subject TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS class_teachers (
    id         INTEGER PRIMARY KEY,
    class_id   INTEGER NOT NULL REFERENCES classes(id),
    teacher_id INTEGER NOT NULL REFERENCES teachers(id),
    is_homeroom INTEGER NOT NULL DEFAULT 0,
    UNIQUE(class_id, teacher_id)
);

CREATE TABLE IF NOT EXISTS students (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    class_id INTEGER NOT NULL REFERENCES classes(id)
);

-- 监护授权：可撤销；撤销后旧账号立即失去学生资料访问权
CREATE TABLE IF NOT EXISTS guardianships (
    id               INTEGER PRIMARY KEY,
    student_id       INTEGER NOT NULL REFERENCES students(id),
    guardian_user_id INTEGER NOT NULL REFERENCES users(id),
    relation         TEXT NOT NULL,
    revoked          INTEGER NOT NULL DEFAULT 0,
    revoked_at       TEXT,
    UNIQUE(student_id, guardian_user_id)
);

-- 一轮家长会预约窗口（班主任为班级发布）
CREATE TABLE IF NOT EXISTS rounds (
    id          INTEGER PRIMARY KEY,
    class_id    INTEGER NOT NULL REFERENCES classes(id),
    title       TEXT NOT NULL,
    open_at     TEXT NOT NULL,
    close_at    TEXT NOT NULL,
    created_by  INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 班级时段
CREATE TABLE IF NOT EXISTS time_slots (
    id         INTEGER PRIMARY KEY,
    round_id   INTEGER NOT NULL REFERENCES rounds(id),
    class_id   INTEGER NOT NULL REFERENCES classes(id),
    teacher_id INTEGER NOT NULL REFERENCES teachers(id),
    start_at   TEXT NOT NULL,
    end_at     TEXT NOT NULL,
    location   TEXT NOT NULL DEFAULT '本班教室',
    -- open 可选 / held 已锁定 / completed 已完成 / cancelled 取消 / teacher_leave 教师请假冻结
    status     TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS ix_slots_teacher_time ON time_slots(teacher_id, start_at);
CREATE INDEX IF NOT EXISTS ix_slots_round ON time_slots(round_id);

-- 同一教师不允许重叠时段（应用层校验，此索引辅助查询）

CREATE TABLE IF NOT EXISTS bookings (
    id        INTEGER PRIMARY KEY,
    slot_id   INTEGER NOT NULL REFERENCES time_slots(id),
    round_id  INTEGER NOT NULL REFERENCES rounds(id),
    student_id INTEGER NOT NULL REFERENCES students(id),
    guardian_user_id INTEGER NOT NULL REFERENCES users(id),
    mode      TEXT NOT NULL CHECK (mode IN ('offline','video')),
    topics    TEXT NOT NULL DEFAULT '',
    -- pending 待确认 / confirmed 已确认 / reschedule_requested 监护人申请改期 /
    -- teacher_reschedule 教师请假待重选 / completed 已完成 / cancelled 已取消
    status    TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    decided_at TEXT
);
-- 同一时段只允许一份“有效”预约（pending 也占位，防止多监护人并发抢同一时段）
CREATE UNIQUE INDEX IF NOT EXISTS ux_booking_active_slot
    ON bookings(slot_id)
    WHERE status IN ('pending','confirmed','reschedule_requested','teacher_reschedule');
-- 一个学生同一轮家长会只能保留一份有效预约
CREATE UNIQUE INDEX IF NOT EXISTS ux_booking_student_round
    ON bookings(student_id, round_id)
    WHERE status IN ('pending','confirmed','reschedule_requested','teacher_reschedule');

-- 改期队列
CREATE TABLE IF NOT EXISTS reschedule_queue (
    id             INTEGER PRIMARY KEY,
    booking_id     INTEGER NOT NULL REFERENCES bookings(id),
    source         TEXT NOT NULL CHECK (source IN ('guardian','teacher_leave')),
    requested_by   INTEGER REFERENCES users(id),
    reason         TEXT NOT NULL DEFAULT '',
    target_slot_id INTEGER REFERENCES time_slots(id),
    -- pending / approved / rejected / cancelled
    status         TEXT NOT NULL DEFAULT 'pending',
    note           TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    decided_at     TEXT
);

CREATE TABLE IF NOT EXISTS teacher_leave (
    id         INTEGER PRIMARY KEY,
    teacher_id INTEGER NOT NULL REFERENCES teachers(id),
    start_at   TEXT NOT NULL,
    end_at     TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 模拟视频会议入口（链接可失效 / 轮换）
CREATE TABLE IF NOT EXISTS meeting_entries (
    id          INTEGER PRIMARY KEY,
    booking_id  INTEGER NOT NULL REFERENCES bookings(id),
    provider    TEXT NOT NULL DEFAULT 'mock-video',
    meeting_code TEXT NOT NULL,
    join_url    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',  -- active / expired
    started_at  TEXT,                            -- 首次进入时间，用于计算超时
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_meeting_booking ON meeting_entries(booking_id);

-- 会谈纪要（结论只能由教师写入；finalized 后锁定）
CREATE TABLE IF NOT EXISTS minutes (
    id            INTEGER PRIMARY KEY,
    booking_id    INTEGER NOT NULL UNIQUE REFERENCES bookings(id),
    student_id    INTEGER NOT NULL REFERENCES students(id),
    class_id      INTEGER NOT NULL REFERENCES classes(id),
    conclusions   TEXT NOT NULL DEFAULT '',
    overtime_minutes INTEGER NOT NULL DEFAULT 0,
    finalized     INTEGER NOT NULL DEFAULT 0,
    created_by    INTEGER REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 后续待办：必须明确负责人与期限
CREATE TABLE IF NOT EXISTS follow_ups (
    id           INTEGER PRIMARY KEY,
    minute_id    INTEGER NOT NULL REFERENCES minutes(id),
    content      TEXT NOT NULL,
    owner_label  TEXT NOT NULL,                 -- 负责人姓名（展示用）
    owner_user_id INTEGER REFERENCES users(id),-- 负责人账号（可为空，如“学生本人”）
    due_date     TEXT NOT NULL,                 -- YYYY-MM-DD
    status       TEXT NOT NULL DEFAULT 'open',  -- open / done
    completed_at TEXT
);

-- 本地虚构附件
CREATE TABLE IF NOT EXISTS attachments (
    id           INTEGER PRIMARY KEY,
    student_id   INTEGER NOT NULL REFERENCES students(id),
    minute_id    INTEGER REFERENCES minutes(id),
    filename     TEXT NOT NULL,
    local_path   TEXT NOT NULL,
    uploaded_by  INTEGER REFERENCES users(id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 模拟通知
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    is_read    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_notif_user ON notifications(user_id, is_read);

-- 访问记录（含被拒绝的访问尝试）
CREATE TABLE IF NOT EXISTS access_logs (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    user_id     INTEGER,
    username    TEXT,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT,
    granted     INTEGER NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);
"""


def connect(db_path: str | Path, check_same_thread: bool = True) -> sqlite3.Connection:
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    # Web 服务为多线程模型：check_same_thread=False + WAL + busy_timeout
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
