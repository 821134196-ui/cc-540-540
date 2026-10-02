"""python -m app [--host 127.0.0.1] [--port 8000] [--reset]

一条命令完成：初始化/种子数据（含教师请假样例）并启动 Web 服务。
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .seed import seed
from .server import make_service, App


class Handler(BaseHTTPRequestHandler):
    app: App | None = None

    def do_GET(self):  # noqa: N802
        self.app.handle(self, "GET")

    def do_POST(self):  # noqa: N802
        self.app.handle(self, "POST")

    def log_message(self, fmt, *args):  # 简洁日志
        print(f"[http] {self.address_string()} - {fmt % args}")


def main():
    ap = argparse.ArgumentParser(description="家长会预约系统")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="data/app.db")
    ap.add_argument("--attach", default="data/attachments")
    ap.add_argument("--reset", action="store_true", help="重建数据库与种子")
    args = ap.parse_args()

    seed(args.db, args.attach, force=args.reset)
    svc = make_service(args.db, args.attach)
    Handler.app = App(svc)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"家长会预约系统已启动：http://{args.host}:{args.port}")
    print("演示账号：wangzl / zhang_wei / zhang_min / lin_mu / zhou_lan（密码 pass1234）")
    print("种子已内置教师请假样例（王正梁 10/09 晚，赵北辰的预约进入改期队列）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
