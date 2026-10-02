"""模拟适配器：视频会议、短信/邮件通知、本地附件存储。

真实部署时替换这些类即可——领域服务只依赖它们的接口。
"""
import sqlite3
import uuid
from pathlib import Path

VIDEO_BASE = "https://video.example.test/mtg/"


class VideoAdapter:
    """模拟视频平台：生成/失效/刷新会议链接。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_meeting(self, booking_id: int, now: str) -> str:
        url = VIDEO_BASE + uuid.uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO video_meetings(booking_id, url, active, created_at) "
            "VALUES (?,?,1,?)",
            (booking_id, url, now),
        )
        return url

    def invalidate(self, booking_id: int) -> bool:
        """链接失效（模拟过期/房间关闭）。返回是否找到有效链接。"""
        cur = self.conn.execute(
            "UPDATE video_meetings SET active=0 WHERE booking_id=? AND active=1",
            (booking_id,),
        )
        return cur.rowcount > 0

    def refresh(self, booking_id: int, now: str) -> str:
        """失效后重新生成链接，旧链接作废。"""
        self.invalidate(booking_id)
        return self.create_meeting(booking_id, now)

    def current_url(self, booking_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT url FROM video_meetings WHERE booking_id=? AND active=1",
            (booking_id,),
        ).fetchone()
        return row["url"] if row else None


class NotificationAdapter:
    """模拟通知：写 notifications 表，不真正发送。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def send(self, user_id: int, kind: str, body: str, now: str,
             channel: str = "inapp") -> None:
        self.conn.execute(
            "INSERT INTO notifications(user_id, channel, kind, body, at) "
            "VALUES (?,?,?,?,?)",
            (user_id, channel, kind, body, now),
        )

    def send_many(self, user_ids, kind: str, body: str, now: str) -> None:
        for uid in set(user_ids):
            self.send(uid, kind, body, now)


class AttachmentStore:
    """本地虚构附件：在 files_dir 下写占位文件。"""

    def __init__(self, conn: sqlite3.Connection, files_dir: str):
        self.conn = conn
        self.files_dir = Path(files_dir)
        self.files_dir.mkdir(parents=True, exist_ok=True)

    def add(self, minutes_id: int, filename: str, content: bytes | None = None) -> str:
        safe = Path(filename).name  # 防目录穿越
        path = self.files_dir / f"m{minutes_id}_{uuid.uuid4().hex[:8]}_{safe}"
        body = content if content is not None else f"（虚构附件）{safe}\n".encode()
        path.write_bytes(body)
        self.conn.execute(
            "INSERT INTO attachments(minutes_id, filename, path) VALUES (?,?,?)",
            (minutes_id, safe, str(path)),
        )
        return str(path)

    def read(self, attachment_id: int):
        return self.conn.execute(
            "SELECT * FROM attachments WHERE id=?", (attachment_id,)
        ).fetchone()
