#!/usr/bin/env python3
"""轻量提醒调度器（单线程、零外部依赖）。

每小时轮询 runtime/data/assistants.db 的 events 表，把到期事件通过
`POST /api/chat` 送进对应助理的会话，从而实现“提前 N 天提醒”。

设计约束（对应 2C2G 服务器）：
- 只用标准库（sqlite3 / urllib / argparse），不引入 APScheduler、Redis、线程池；
- 单线程顺序执行，一轮跑完 sleep；
- 提醒成功才写 last_reminded_on，失败下一轮重试，不会重复轰炸；
- 额外把每次提醒追加到 runtime/data/notifications.jsonl，便于控制台外查看；
- 可选 REMINDER_WEBHOOK_URL（飞书/企业微信机器人）做二次推送。

用法：
  python runtime/scheduler.py --once          # 立刻检查一次（验收用）
  python runtime/scheduler.py                 # 常驻，默认每小时
  python runtime/scheduler.py --interval 600  # 改轮询间隔（秒）

插入测试事件：
  INSERT INTO events(assistant_id, event_type, event_date, remind_days_before, message)
  VALUES('fitness_coach', 'period', '2026-11-06', 3, '预计生理期');
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DEFAULT_TZ = timezone(timedelta(hours=8))
NOTIFY_FILE = "notifications.jsonl"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    assistant_id       TEXT NOT NULL,
    event_type         TEXT NOT NULL,
    event_date         TEXT NOT NULL,
    remind_days_before INTEGER NOT NULL DEFAULT 3,
    message            TEXT,
    last_reminded_on   TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS session_bindings (
    session_id   TEXT PRIMARY KEY,
    assistant_id TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _db_path(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    env = os.environ.get("ASSISTANT_DB")
    if env:
        return Path(env)
    return _repo_root() / "runtime" / "data" / "assistants.db"


def _today() -> date:
    return datetime.now(DEFAULT_TZ).date()


def _ensure(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _due_events(con: sqlite3.Connection, today: date) -> list[sqlite3.Row]:
    rows = con.execute(
        "SELECT * FROM events WHERE last_reminded_on IS NULL OR last_reminded_on != ? "
        "ORDER BY event_date ASC, id ASC",
        (today.isoformat(),),
    ).fetchall()
    due: list[sqlite3.Row] = []
    for row in rows:
        try:
            event_date = date.fromisoformat(str(row["event_date"]))
        except ValueError:
            print(f"[scheduler] skip event {row['id']}: bad date {row['event_date']!r}")
            continue
        days_left = (event_date - today).days
        before = int(row["remind_days_before"] or 3)
        if 0 <= days_left <= before:
            due.append(row)
    return due


def _text(row: sqlite3.Row, days_left: int) -> str:
    when = "今天" if days_left == 0 else f"还有 {days_left} 天"
    detail = str(row["message"] or row["event_type"] or "").strip()
    return (
        f"[系统提醒] {when}是「{detail}」（{row['event_date']}）。"
        "请用一句话提醒用户，并给出一个可执行的小建议。"
    )


def _bind_session(con: sqlite3.Connection, assistant_id: str, session_id: str) -> None:
    con.execute(
        "INSERT INTO session_bindings(session_id, assistant_id, updated_at) "
        "VALUES(?, ?, datetime('now')) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "assistant_id = excluded.assistant_id, updated_at = excluded.updated_at",
        (session_id, assistant_id),
    )
    con.commit()


def _post_json(url: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body[:300]}


def _notify_file(row: sqlite3.Row, text: str, reply: str) -> None:
    path = _repo_root() / "runtime" / "data" / NOTIFY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": datetime.now(DEFAULT_TZ).isoformat(timespec="seconds"),
        "event_id": row["id"],
        "assistant_id": row["assistant_id"],
        "event_type": row["event_type"],
        "event_date": row["event_date"],
        "reminder": text,
        "reply": reply[:500],
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _webhook(text: str) -> None:
    url = os.environ.get("REMINDER_WEBHOOK_URL", "").strip()
    if not url:
        return
    try:
        _post_json(url, {"msg_type": "text", "content": {"text": text}}, 15)
    except Exception as exc:  # noqa: BLE001 - webhook is best-effort
        print(f"[scheduler] webhook failed: {type(exc).__name__}: {exc}")


def run_once(args) -> int:
    db = _db_path(args.db)
    if not db.is_file():
        print(f"[scheduler] no database yet: {db}")
        return 0
    today = _today()
    con = _ensure(db)
    sent = 0
    try:
        for row in _due_events(con, today):
            assistant_id = str(row["assistant_id"])
            session_id = f"reminder-{assistant_id}"
            _bind_session(con, assistant_id, session_id)
            days_left = (date.fromisoformat(str(row["event_date"])) - today).days
            text = _text(row, days_left)
            try:
                body = _post_json(
                    f"{args.base_url.rstrip('/')}/api/chat",
                    {"message": text, "session_id": session_id},
                    args.timeout,
                )
                reply = str(
                    body.get("response") or body.get("reply") or body.get("output") or body.get("raw") or ""
                )
                con.execute(
                    "UPDATE events SET last_reminded_on = ? WHERE id = ?",
                    (today.isoformat(), row["id"]),
                )
                con.commit()
                _notify_file(row, text, reply)
                _webhook(f"[{assistant_id}] {text}\n{reply[:300]}")
                sent += 1
                print(f"[scheduler] sent event={row['id']} assistant={assistant_id} days_left={days_left}")
            except Exception as exc:  # noqa: BLE001 - leave row unmarked, retry next tick
                print(
                    f"[scheduler] failed event={row['id']} assistant={assistant_id}: "
                    f"{type(exc).__name__}: {exc}"
                )
    finally:
        con.close()
    print(f"[scheduler] {datetime.now(DEFAULT_TZ).isoformat(timespec='seconds')} due={sent} db={db}")
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="轻量提醒调度器")
    parser.add_argument("--once", action="store_true", help="只检查一次后退出")
    parser.add_argument("--interval", type=int, default=3600, help="轮询间隔秒数（默认 3600）")
    parser.add_argument("--base-url", default="http://127.0.0.1:7870", help="protoAgent 地址")
    parser.add_argument("--db", default=None, help="assistants.db 路径")
    parser.add_argument("--timeout", type=int, default=120, help="单次 /api/chat 超时（秒）")
    args = parser.parse_args(argv)

    if args.once:
        run_once(args)
        return 0
    print(f"[scheduler] started; interval={args.interval}s base={args.base_url}")
    while True:
        try:
            run_once(args)
        except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the loop
            print(f"[scheduler] tick failed: {type(exc).__name__}: {exc}")
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    sys.exit(main())
