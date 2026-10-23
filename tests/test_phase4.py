"""Offline phase 4 tests. External services are mocked; no credentials/network are used."""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from google.genai.errors import ServerError

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    sys.modules["requests"] = types.SimpleNamespace(post=None)

from assistant import notion_notes
from assistant.capture import GeminiCapture
from assistant.store import Store


class StorePhase4Tests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.store = Store(Path(self.tempdir.name) / "assistant.sqlite3")
        self.store.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_recent_gmail_grouping_only_re_notifies_messages_after_cutoff(self):
        now = datetime.now(timezone.utc)
        for message_id, received_at in (
            ("recent-mail", now - timedelta(hours=2)),
            ("old-mail", now - timedelta(hours=6)),
        ):
            self.store.save_email_analysis({
                "message_id": message_id,
                "thread_id": f"thread-{message_id}",
                "subject": message_id,
                "sender": "sender@example.com",
                "received_at": received_at.isoformat(),
                "is_unread": True,
                "important": False,
                "category": "other",
                "summary": "Preview",
            })
            self.store.mark_email_notified(message_id)

        cutoff = (now - timedelta(hours=5)).isoformat()
        self.store.prepare_recent_mail_grouping(cutoff)
        recent_rows = self.store.unnotified_emails(cutoff)
        self.assertEqual([row["message_id"] for row in recent_rows], ["recent-mail"])

        # The one-time migration must not make the same mail notify again.
        self.store.prepare_recent_mail_grouping(cutoff)
        self.assertEqual(self.store.unnotified_emails(cutoff), recent_rows)

    def test_priority_reminder_schedule_and_done(self):
        now = datetime.now(timezone.utc)
        start = now + timedelta(days=3)
        event = {"id": "evt-high", "title": "Exam", "start": start, "priority": "High"}
        self.store.reconcile_reminders([event], now)
        with self.store.connect() as db:
            leads = {row[0] for row in db.execute("SELECT lead_minutes FROM reminders WHERE event_id='evt-high'")}
        self.assertEqual(leads, {1440, 720, 180, 60, 30, 15})
        action = self.store.create_reminder_action({"event_id": "evt-high", "event_title": "Exam",
            "event_start": start.isoformat(), "priority": "High"})
        claimed = self.store.claim_reminder_action(action, "done")
        self.assertEqual(claimed["event_id"], "evt-high")
        self.assertIsNone(self.store.claim_reminder_action(action, "s1"))
        self.store.mark_event_done("evt-high")
        self.assertEqual(self.store.completed_event_ids(["evt-high"]), {"evt-high"})
        self.assertEqual(self.store.due_reminders(start), [])

    def test_snooze_is_persisted_and_due(self):
        now = datetime.now(timezone.utc)
        event_start = now + timedelta(days=2)
        action = {"event_id": "evt-snooze", "event_title": "Submit report",
                  "event_start": event_start.isoformat(), "priority": "Medium"}
        self.store.schedule_snooze(action, now - timedelta(seconds=1))
        due = self.store.due_snoozes(now)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["event_title"], "Submit report")
        self.store.mark_snooze_sent(due[0]["snooze_id"])
        self.assertEqual(self.store.due_snoozes(now + timedelta(days=1)), [])

    def test_captured_task_can_be_completed_and_in_weekly_review(self):
        capture_id = "a" * 32
        self.store.save_capture({"capture_id": capture_id, "source_text": "finish assignment",
            "title": "Finish assignment", "event_date": "2026-10-25", "event_time": "10:00",
            "priority": "High", "direct_requested": False, "status": "confirm"})
        self.store.finish_capture(capture_id, "created", "evt-task")
        self.assertTrue(self.store.mark_capture_done(capture_id))
        self.assertFalse(self.store.mark_capture_done(capture_id))
        self.assertEqual(self.store.completed_event_ids(["evt-task"]), {"evt-task"})
        now = datetime.now(timezone.utc)
        completed, pending = self.store.weekly_tasks(now - timedelta(days=1), now + timedelta(seconds=1))
        self.assertEqual([row["title"] for row in completed], ["Finish assignment"])
        self.assertEqual(pending, [])

    def test_migrates_phase3_reminder_table(self):
        legacy_path = Path(self.tempdir.name) / "legacy.sqlite3"
        import sqlite3
        db = sqlite3.connect(legacy_path)
        try:
            db.execute("CREATE TABLE reminders(event_id TEXT NOT NULL,event_title TEXT NOT NULL,event_start TEXT NOT NULL,lead_minutes INTEGER NOT NULL,due_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',sent_at TEXT,PRIMARY KEY(event_id,lead_minutes))")
        finally:
            db.close()
        Store(legacy_path).initialize()
        db = sqlite3.connect(legacy_path)
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(reminders)")}
        finally:
            db.close()
        self.assertIn("priority", columns)


class VoiceTranscriptionTests(unittest.TestCase):
    def test_empty_transcriber_response_falls_back_to_configured_gemini_model(self):
        capture = GeminiCapture.__new__(GeminiCapture)
        uploaded = types.SimpleNamespace(name="files/voice", uri="https://example.test/voice")
        capture.client = Mock()
        capture.client.files.upload.return_value = uploaded
        capture.client.models.generate_content.side_effect = [
            types.SimpleNamespace(text=""),
            types.SimpleNamespace(text="வணக்கம்"),
        ]
        capture.model = "gemini-3.8-flash"

        transcript = capture.transcribe(b"test audio bytes")

        self.assertEqual(transcript, "வணக்கம்")
        self.assertEqual(capture.client.models.generate_content.call_count, 2)
        self.assertEqual(capture.client.files.delete.call_args.kwargs["name"], uploaded.name)

    def test_temporary_gemini_unavailable_error_retries_once(self):
        from unittest.mock import patch

        capture = GeminiCapture.__new__(GeminiCapture)
        uploaded = types.SimpleNamespace(name="files/voice", uri="https://example.test/voice")
        capture.client = Mock()
        capture.client.files.upload.return_value = uploaded
        capture.client.models.generate_content.side_effect = [
            types.SimpleNamespace(text=""),
            ServerError(503, {"error": {"message": "temporarily unavailable"}}),
            types.SimpleNamespace(text="வணக்கம்"),
        ]
        capture.model = "gemini-3.8-flash"

        with patch("assistant.capture.time.sleep"):
            transcript = capture.transcribe(b"test audio bytes")

        self.assertEqual(transcript, "வணக்கம்")
        self.assertEqual(capture.client.models.generate_content.call_count, 3)


class NotionPhase4Tests(unittest.TestCase):
    def setUp(self):
        self.client = notion_notes.NotionNotes("test-token", "data-source-id")
        self.post = Mock()
        self.original_post = notion_notes.requests.post
        notion_notes.requests.post = self.post

    def tearDown(self):
        notion_notes.requests.post = self.original_post

    def test_save_note_uses_data_source_and_required_properties(self):
        response = types.SimpleNamespace(ok=True, json=lambda: {"id": "page-1", "url": "https://notion.test/page-1"})
        self.post.return_value = response
        result = self.client.save_note("GDG Meeting\nDecisions: build the bot")
        self.assertEqual(result["id"], "page-1")
        args, kwargs = self.post.call_args
        self.assertEqual(args[0], "https://api.notion.com/v1/pages")
        self.assertEqual(kwargs["json"]["parent"], {"data_source_id": "data-source-id"})
        self.assertEqual(kwargs["json"]["properties"]["Name"]["title"][0]["text"]["content"], "GDG Meeting")
        self.assertEqual(kwargs["headers"]["Notion-Version"], "2026-03-11")
        self.assertEqual(kwargs["timeout"], (5, 15))

    def test_search_returns_note_excerpt_and_link(self):
        page = {"properties": {"Name": {"title": [{"plain_text": "GDG meeting"}]},
                    "Content": {"rich_text": [{"plain_text": "We discussed the workshop date."}]}},
                "url": "https://notion.test/gdg"}
        self.post.return_value = types.SimpleNamespace(ok=True, json=lambda: {"results": [page]})
        notes = self.client.search_notes("GDG meeting la enna sonnanga?")
        self.assertEqual(notes[0]["title"], "GDG meeting")
        self.assertIn("workshop date", notes[0]["content"])
        self.assertEqual(notes[0]["url"], "https://notion.test/gdg")
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["filter"]["property"], "Content")
        self.assertEqual(payload["filter"]["rich_text"]["contains"], "gdg")


if __name__ == "__main__":
    unittest.main()
