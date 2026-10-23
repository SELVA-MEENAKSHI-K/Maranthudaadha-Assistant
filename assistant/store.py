"""Small SQLite store for encrypted OAuth credentials and reminder deduplication."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


class _ClosingConnection(sqlite3.Connection):
    """Make `with Store.connect()` commit/rollback and always release its file handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, factory=_ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tokens (
                    name TEXT PRIMARY KEY,
                    encrypted_value BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_metadata (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    event_id TEXT NOT NULL,
                    event_title TEXT NOT NULL,
                    event_start TEXT NOT NULL,
                    lead_minutes INTEGER NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    sent_at TEXT,
                    priority TEXT NOT NULL DEFAULT 'Medium',
                    PRIMARY KEY(event_id, lead_minutes)
                );
                CREATE INDEX IF NOT EXISTS idx_reminders_due
                    ON reminders(status, due_at);
                CREATE TABLE IF NOT EXISTS gmail_messages (
                    message_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    is_unread INTEGER,
                    important INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    deadline_iso TEXT,
                    date_evidence TEXT,
                    action_status TEXT NOT NULL DEFAULT 'none',
                    notified INTEGER NOT NULL DEFAULT 0,
                    calendar_event_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_gmail_notification
                    ON gmail_messages(important, notified, created_at);
                CREATE TABLE IF NOT EXISTS captures (
                    capture_id TEXT PRIMARY KEY,
                    source_text TEXT NOT NULL,
                    title TEXT NOT NULL,
                    event_date TEXT,
                    event_time TEXT,
                    priority TEXT NOT NULL DEFAULT 'Medium',
                    direct_requested INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    event_id TEXT,
                    created_at TEXT NOT NULL,
                    done_at TEXT
                );
                CREATE TABLE IF NOT EXISTS reminder_actions (
                    token TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    event_title TEXT NOT NULL,
                    event_start TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE IF NOT EXISTS snoozes (
                    snooze_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    event_title TEXT NOT NULL,
                    event_start TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                );
            """)
            reminder_columns = {row[1] for row in db.execute("PRAGMA table_info(reminders)")}
            if "priority" not in reminder_columns:
                db.execute("ALTER TABLE reminders ADD COLUMN priority TEXT NOT NULL DEFAULT 'Medium'")
            capture_columns = {row[1] for row in db.execute("PRAGMA table_info(captures)")}
            if "done_at" not in capture_columns:
                db.execute("ALTER TABLE captures ADD COLUMN done_at TEXT")
            gmail_columns = {row[1] for row in db.execute("PRAGMA table_info(gmail_messages)")}
            if "is_unread" not in gmail_columns:
                db.execute("ALTER TABLE gmail_messages ADD COLUMN is_unread INTEGER")
                # Show the upgraded mail cards once so the new date/status/reply
                # controls are available for messages already stored locally.
                db.execute("UPDATE gmail_messages SET notified=0 WHERE important=1")

    def save_token(self, encrypted_token: bytes) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO tokens(name, encrypted_value, updated_at) VALUES('google', ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET encrypted_value=excluded.encrypted_value, "
                "updated_at=excluded.updated_at",
                (encrypted_token, datetime.now().astimezone().isoformat()),
            )

    def get_token(self) -> bytes | None:
        with self.connect() as db:
            row = db.execute("SELECT encrypted_value FROM tokens WHERE name='google'").fetchone()
            return bytes(row[0]) if row else None

    def get_preference(self, name: str, default: str | None = None) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM app_metadata WHERE name=?", (name,)).fetchone()
            return row["value"] if row else default

    def set_preference(self, name: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO app_metadata(name, value) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, value),
            )

    def reconcile_reminders(self, events: list[dict], now: datetime) -> None:
        """Upsert reminders for current events; preserve already delivered reminders."""
        active_ids = [event["id"] for event in events]
        with self.connect() as db:
            for event in events:
                start: datetime = event["start"]
                priority = event.get("priority", "Medium")
                leads = (1440, 720, 180, 60, 30, 15) if priority == "High" else (1440, 180, 30)
                for lead in leads:
                    due = start.timestamp() - lead * 60
                    due_at = datetime.fromtimestamp(due, start.tzinfo)
                    if due_at <= now:
                        continue
                    db.execute(
                        "INSERT INTO reminders(event_id, event_title, event_start, lead_minutes, due_at, priority) "
                        "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(event_id, lead_minutes) DO UPDATE SET "
                        "event_title=excluded.event_title, event_start=excluded.event_start, due_at=excluded.due_at, "
                        "priority=excluded.priority, "
                        "status=CASE WHEN reminders.event_start<>excluded.event_start THEN 'pending' ELSE reminders.status END, "
                        "sent_at=CASE WHEN reminders.event_start<>excluded.event_start THEN NULL ELSE reminders.sent_at END",
                        (event["id"], event["title"], start.isoformat(), lead, due_at.isoformat(), priority),
                    )
                db.execute(
                    "DELETE FROM reminders WHERE event_id=? AND status='pending' AND lead_minutes NOT IN (" +
                    ",".join("?" for _ in leads) + ")", (event["id"], *leads)
                )
            if active_ids:
                placeholders = ",".join("?" for _ in active_ids)
                db.execute(
                    f"DELETE FROM reminders WHERE status='pending' AND event_id NOT IN ({placeholders})",
                    active_ids,
                )
                db.execute(f"UPDATE snoozes SET status='done' WHERE status='pending' AND event_id NOT IN ({placeholders})", active_ids)
            else:
                db.execute("DELETE FROM reminders WHERE status='pending'")
                db.execute("UPDATE snoozes SET status='done' WHERE status='pending'")

    def due_reminders(self, now: datetime) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM reminders WHERE status='pending' AND due_at<=? ORDER BY due_at",
                (now.isoformat(),),
            ).fetchall()

    def mark_sent(self, event_id: str, lead_minutes: int, now: datetime) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE reminders SET status='sent', sent_at=? WHERE event_id=? AND lead_minutes=? AND status='pending'",
                (now.isoformat(), event_id, lead_minutes),
            )

    def email_processed(self, message_id: str) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM gmail_messages WHERE message_id=?", (message_id,)).fetchone() is not None

    def processed_email_ids(self) -> set[str]:
        with self.connect() as db:
            return {row[0] for row in db.execute("SELECT message_id FROM gmail_messages")}

    def prepare_recent_mail_grouping(self, since: str) -> None:
        """Re-notify recent stored mail once after enabling Seen/Unseen grouping."""
        with self.connect() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO app_metadata(name, value) VALUES(?, ?)",
                ("gmail_recent_grouping_v1", "done"),
            )
            if inserted.rowcount:
                db.execute(
                    "UPDATE gmail_messages SET notified=0 WHERE received_at>=?",
                    (since,),
                )

    def save_email_analysis(self, record: dict) -> None:
        """Persist minimal metadata/summary only; the raw email body is not stored."""
        status = "pending" if record["important"] and record.get("deadline_iso") else "none"
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO gmail_messages(
                    message_id, thread_id, subject, sender, received_at, is_unread, important, category,
                    summary, deadline_iso, date_evidence, action_status, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record["message_id"], record["thread_id"], record["subject"], record["sender"],
                 record["received_at"], int(record.get("is_unread", False)), int(record["important"]), record["category"], record["summary"],
                 record.get("deadline_iso"), record.get("date_evidence"), status,
                 datetime.now().astimezone().isoformat()),
            )

    def unnotified_emails(self, since: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as db:
            if since is None:
                return db.execute(
                    "SELECT * FROM gmail_messages WHERE notified=0 ORDER BY received_at"
                ).fetchall()
            return db.execute(
                "SELECT * FROM gmail_messages WHERE notified=0 AND received_at>=? ORDER BY received_at",
                (since,),
            ).fetchall()

    def mark_email_notified(self, message_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE gmail_messages SET notified=1 WHERE message_id=? AND notified=0", (message_id,))

    def claim_email_action(self, message_id: str, action: str) -> bool:
        with self.connect() as db:
            result = db.execute(
                "UPDATE gmail_messages SET action_status=? WHERE message_id=? AND action_status='pending'",
                ("adding" if action == "yes" else "declined", message_id),
            )
            return result.rowcount == 1

    def finish_email_action(self, message_id: str, status: str, event_id: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE gmail_messages SET action_status=?, calendar_event_id=? WHERE message_id=?",
                (status, event_id, message_id),
            )

    def get_email(self, message_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM gmail_messages WHERE message_id=?", (message_id,)).fetchone()

    def recent_important_emails(self, since: datetime) -> list[sqlite3.Row]:
        since_utc = since.astimezone(timezone.utc).isoformat()
        with self.connect() as db:
            return db.execute(
                "SELECT subject, summary, category, deadline_iso, action_status FROM gmail_messages "
                "WHERE important=1 AND received_at>=? ORDER BY received_at DESC LIMIT 10",
                (since_utc,),
            ).fetchall()

    def save_capture(self, record: dict) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO captures(capture_id, source_text, title, event_date, event_time,
                   priority, direct_requested, status, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(capture_id) DO UPDATE SET source_text=excluded.source_text,
                   title=excluded.title, event_date=excluded.event_date, event_time=excluded.event_time,
                   priority=excluded.priority, direct_requested=excluded.direct_requested, status=excluded.status""",
                (record["capture_id"], record["source_text"], record["title"], record.get("event_date"),
                 record.get("event_time"), record.get("priority", "Medium"), int(record.get("direct_requested", False)),
                 record["status"], datetime.now().astimezone().isoformat()),
            )

    def latest_waiting_capture(self):
        with self.connect() as db:
            return db.execute("SELECT * FROM captures WHERE status IN ('awaiting_date','awaiting_time') ORDER BY created_at DESC LIMIT 1").fetchone()

    def get_capture(self, capture_id: str):
        with self.connect() as db:
            return db.execute("SELECT * FROM captures WHERE capture_id=?", (capture_id,)).fetchone()

    def claim_capture(self, capture_id: str) -> bool:
        with self.connect() as db:
            return db.execute("UPDATE captures SET status='creating' WHERE capture_id=? AND status='confirm'", (capture_id,)).rowcount == 1

    def finish_capture(self, capture_id: str, status: str, event_id: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE captures SET status=?, event_id=?, source_text=CASE WHEN ? IN ('created','cancelled') THEN '' ELSE source_text END WHERE capture_id=?",
                       (status, event_id, status, capture_id))

    def task_captures(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT * FROM captures WHERE status IN ('created','done','awaiting_date','awaiting_time') ORDER BY created_at DESC LIMIT 30").fetchall()

    def pending_captures(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT * FROM captures WHERE status IN ('created','confirm','creating','awaiting_date','awaiting_time') ORDER BY created_at DESC LIMIT 30").fetchall()

    def mark_capture_done(self, capture_id: str) -> bool:
        with self.connect() as db:
            result = db.execute("UPDATE captures SET status='done', done_at=? WHERE capture_id=? AND status='created'",
                                (datetime.now(timezone.utc).isoformat(), capture_id))
            if result.rowcount:
                db.execute("UPDATE reminders SET status='done' WHERE event_id=(SELECT event_id FROM captures WHERE capture_id=?) AND status='pending'", (capture_id,))
                db.execute("UPDATE snoozes SET status='done' WHERE event_id=(SELECT event_id FROM captures WHERE capture_id=?) AND status='pending'", (capture_id,))
                db.execute("UPDATE reminder_actions SET status='done' WHERE event_id=(SELECT event_id FROM captures WHERE capture_id=?) AND status='pending'", (capture_id,))
            return result.rowcount == 1

    def weekly_tasks(self, start: datetime, end: datetime) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        start_utc, end_utc = start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()
        with self.connect() as db:
            completed = db.execute("SELECT title, priority, done_at FROM captures WHERE status='done' AND done_at>=? AND done_at<? ORDER BY done_at",
                                   (start_utc, end_utc)).fetchall()
            pending = db.execute("SELECT title, event_date, event_time, priority, status FROM captures WHERE status IN ('created','awaiting_date','awaiting_time') ORDER BY event_date LIMIT 25").fetchall()
            return completed, pending

    def create_reminder_action(self, row) -> str:
        token = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO reminder_actions(token,event_id,event_title,event_start,priority) VALUES(?,?,?,?,?)",
                       (token, row["event_id"], row["event_title"], row["event_start"], row["priority"]))
        return token

    def claim_reminder_action(self, token: str, action: str):
        with self.connect() as db:
            result = db.execute("UPDATE reminder_actions SET status=? WHERE token=? AND status='pending'",
                                (action, token))
            if result.rowcount != 1:
                return None
            return db.execute("SELECT * FROM reminder_actions WHERE token=?", (token,)).fetchone()

    def schedule_snooze(self, action, due_at: datetime) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO snoozes(snooze_id,event_id,event_title,event_start,priority,due_at) VALUES(?,?,?,?,?,?)",
                       (uuid.uuid4().hex, action["event_id"], action["event_title"], action["event_start"], action["priority"], due_at.isoformat()))

    def due_snoozes(self, now: datetime) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT * FROM snoozes WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT 25", (now.isoformat(),)).fetchall()

    def mark_snooze_sent(self, snooze_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE snoozes SET status='sent' WHERE snooze_id=? AND status='pending'", (snooze_id,))

    def mark_event_done(self, event_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE reminders SET status='done' WHERE event_id=? AND status='pending'", (event_id,))
            db.execute("UPDATE snoozes SET status='done' WHERE event_id=? AND status='pending'", (event_id,))
            db.execute("UPDATE reminder_actions SET status='done' WHERE event_id=? AND status='pending'", (event_id,))
            db.execute("UPDATE captures SET status='done', done_at=? WHERE event_id=? AND status='created'",
                       (datetime.now(timezone.utc).isoformat(), event_id))
