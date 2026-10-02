"""模拟外部适配器：视频会议、通知、本地虚构附件存储。

真实环境可替换为实际 SDK；这里全部本地实现，行为可观察、可测试。
"""
from __future__ import annotations

import secrets
import sqlite3
from pathlib import Path

from .db import connect


class MockNotificationAdapter:
    """通知落库到 notifications 表；测试可直接读取。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.sent: list[dict] = []  # 进程内镜像，便于断言

    def send(self, user_id: int, kind: str, title: str, body: str = "") -> dict:
        cur = self.conn.execute(
            "INSERT INTO notifications(user_id, kind, title, body) VALUES (?,?,?,?)",
            (user_id, kind, title, body),
        )
        self.conn.commit()
        msg = {"id": cur.lastrowid, "user_id": user_id, "kind": kind,
               "title": title, "body": body}
        self.sent.append(msg)
        return msg


class MockVideoAdapter:
    """模拟视频会议：创建入口、进入、失效、轮换链接。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_entry(self, booking_id: int, code: str | None = None) -> sqlite3.Row:
        # 每次生成唯一会议码（链接失效轮换后必然不同）
        code = code or f"MOCK-{booking_id:04d}-{secrets.token_hex(3).upper()}"
        url = f"https://video.mock.local/j/{code.lower()}"
        cur = self.conn.execute(
            "INSERT INTO meeting_entries(booking_id, meeting_code, join_url, status) "
            "VALUES (?,?,?, 'active')",
            (booking_id, code, url),
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM meeting_entries WHERE id=?", (cur.lastrowid,)
        ).fetchone()

    def expire_entry(self, entry_id: int) -> None:
        self.conn.execute(
            "UPDATE meeting_entries SET status='expired' WHERE id=?", (entry_id,)
        )
        self.conn.commit()

    def rotate_entry(self, booking_id: int) -> sqlite3.Row:
        """链接失效后轮换：旧入口置为 expired，生成新入口。"""
        olds = self.conn.execute(
            "SELECT id FROM meeting_entries WHERE booking_id=? ORDER BY id",
            (booking_id,),
        ).fetchall()
        for row in olds:
            self.expire_entry(row["id"])
        return self.create_entry(booking_id)

    def active_entry(self, booking_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM meeting_entries WHERE booking_id=? AND status='active' "
            "ORDER BY id DESC LIMIT 1",
            (booking_id,),
        ).fetchone()

    def join(self, entry_id: int, now: str) -> sqlite3.Row:
        """进入会议；首次进入记录 started_at（供超时计算）。"""
        self.conn.execute(
            "UPDATE meeting_entries SET started_at=COALESCE(started_at,?) WHERE id=?",
            (now, entry_id),
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM meeting_entries WHERE id=?", (entry_id,)
        ).fetchone()


class LocalAttachmentStore:
    """本地虚构附件：在 data/attachments 下写纯文本样例文件。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, filename: str, content: str) -> Path:
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def open_path(self, filename: str) -> Path | None:
        path = self.root / filename
        return path if path.exists() else None


def get_adapters(conn: sqlite3.Connection | None = None,
                 db_path: str = "data/app.db",
                 attach_root: str = "data/attachments"):
    if conn is None:
        conn = connect(db_path)
    return (conn,
            MockNotificationAdapter(conn),
            MockVideoAdapter(conn),
            LocalAttachmentStore(attach_root))
