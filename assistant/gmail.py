"""Read filtered Gmail messages and deliver one-time Telegram summaries."""
from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from assistant.email_intelligence import StrandsEmailTriage
from assistant.google_calendar import GoogleCalendar
from assistant.store import Store

log = logging.getLogger(__name__)
GMAIL_QUERY = 'in:inbox newer_than:2d {exam college GDG internship interview deadline "due date" assignment placement registration}'


def _quoted_absolute_date_matches(evidence: str, deadline_iso: str, timezone) -> bool:
    """Only auto-add when the quoted evidence contains the same explicit full date."""
    try:
        expected = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=timezone)
    expected_date = expected.astimezone(timezone).date()
    candidates = re.findall(
        r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|"
        r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b",
        evidence,
    )
    formats = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y",
               "%d %B %Y", "%d %b %Y", "%B %d %Y", "%B %d, %Y", "%b %d %Y", "%b %d, %Y")
    for candidate in candidates:
        numeric = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", candidate)
        if numeric and int(numeric.group(1)) <= 12 and int(numeric.group(2)) <= 12 \
                and numeric.group(1) != numeric.group(2):
            # 03/04 can mean March 4 or April 3; keep it for user confirmation.
            continue
        for pattern in formats:
            try:
                if datetime.strptime(candidate, pattern).date() == expected_date:
                    return True
            except ValueError:
                continue
    return False


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


class GmailReader:
    def __init__(self, calendar: GoogleCalendar):
        self.calendar = calendar

    @staticmethod
    def _decode_header(value: str) -> str:
        try:
            return str(make_header(decode_header(value)))
        except (LookupError, UnicodeDecodeError, ValueError):
            return value

    @staticmethod
    def _decode_part(part: dict) -> str:
        if part.get("filename"):
            return ""  # Phase 2 does not download or inspect attachments.
        body = part.get("body", {}).get("data")
        mime = part.get("mimeType", "")
        if body and mime in {"text/plain", "text/html"}:
            padded = body + "=" * (-len(body) % 4)
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
            if mime == "text/html":
                parser = _TextOnly()
                parser.feed(decoded)
                return " ".join(parser.parts)
            return decoded
        fragments = []
        for child in part.get("parts", []):
            fragments.append(GmailReader._decode_part(child))
        return "\n".join(fragment for fragment in fragments if fragment)

    def read_message(self, message_id: str) -> dict:
        service = build("gmail", "v1", credentials=self.calendar.credentials(), cache_discovery=False)
        message = service.users().messages().get(userId="me", id=message_id, format="full").execute(num_retries=2)
        payload = message.get("payload", {})
        headers = {item.get("name", "").lower(): item.get("value", "") for item in payload.get("headers", [])}
        sender_name, sender_address = parseaddr(self._decode_header(headers.get("from", "")))
        sender = f"{sender_name} <{sender_address}>" if sender_name else sender_address or "Unknown sender"
        received_ms = int(message.get("internalDate", "0"))
        received_at = datetime.fromtimestamp(received_ms / 1000, tz=ZoneInfo("UTC"))
        body = self._decode_part(payload) or message.get("snippet", "")
        return {
            "message_id": message_id,
            "thread_id": message.get("threadId", ""),
            "subject": self._decode_header(headers.get("subject", "(No subject)"))[:300],
            "sender": sender[:240],
            "received_at": received_at.isoformat(),
            "is_unread": "UNREAD" in message.get("labelIds", []),
            "body": body[:12000],
        }

    def send_reply(self, message_id: str, body: str) -> dict:
        """Reply in the original Gmail thread; caller must require an explicit Send tap."""
        clean_body = body.strip()
        if not clean_body or len(clean_body) > 3000:
            raise ValueError("Reply must contain 1 to 3000 characters")
        service = build("gmail", "v1", credentials=self.calendar.credentials(), cache_discovery=False)
        original = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute(num_retries=2)
        headers = {item.get("name", "").lower(): item.get("value", "")
                   for item in original.get("payload", {}).get("headers", [])}
        _, recipient = parseaddr(self._decode_header(headers.get("reply-to") or headers.get("from", "")))
        if not recipient or "@" not in recipient:
            raise ValueError("Original sender address is unavailable")
        parent_id = headers.get("message-id", "")
        subject = self._decode_header(headers.get("subject", "(No subject)"))
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        message = EmailMessage()
        message["To"] = recipient
        message["Subject"] = subject
        if parent_id:
            message["In-Reply-To"] = parent_id
            refs = headers.get("references", "").strip()
            message["References"] = f"{refs} {parent_id}".strip()
        message.set_content(clean_body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        return service.users().messages().send(
            userId="me", body={"raw": raw, "threadId": original.get("threadId", "")}
        ).execute(num_retries=0)

    def list_candidates(self, max_results: int, processed_ids: set[str]) -> list[dict]:
        service = build("gmail", "v1", credentials=self.calendar.credentials(), cache_discovery=False)
        candidates: list[dict] = []
        page_token = None
        # Walk past already handled messages so a busy inbox cannot starve older candidates.
        for _ in range(5):
            response = service.users().messages().list(
                userId="me", q=GMAIL_QUERY, maxResults=100,
                includeSpamTrash=False, pageToken=page_token,
            ).execute(num_retries=2)
            for item in response.get("messages", []):
                if item.get("id") not in processed_ids:
                    candidates.append(item)
                    if len(candidates) >= max_results:
                        return candidates
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return candidates


async def scan_gmail(context, manual: bool = False) -> int:
    """Scan no more than the configured cap; notify important messages once."""
    import asyncio

    settings = context.application.bot_data["settings"]
    triage: StrandsEmailTriage | None = context.application.bot_data.get("email_triage")
    if triage is None:
        if manual:
            await context.bot.send_message(
                settings.telegram_user_id,
                "Strands API key .env-la set pannitu bot restart pannunga; appo Gmail summary start aagum.",
            )
        return 0
    reader: GmailReader = context.application.bot_data["gmail_reader"]
    store: Store = context.application.bot_data["store"]
    processed = 0
    scan_ok = False
    sent_count = 0
    failed_count = 0
    try:
        known_ids = store.processed_email_ids()
        candidates = await asyncio.to_thread(reader.list_candidates, settings.gmail_scan_max_results, known_ids)
        scan_ok = True
        for candidate in candidates:
            message_id = candidate.get("id")
            if not message_id or store.email_processed(message_id):
                continue
            try:
                message = await asyncio.to_thread(reader.read_message, message_id)
                analysis = await asyncio.to_thread(
                    triage.analyze,
                    subject=message["subject"],
                    sender=message["sender"],
                    received_at=datetime.fromisoformat(message["received_at"]),
                    body=message["body"],
                )
                record = {key: value for key, value in message.items() if key != "body"}
                record.update(analysis.model_dump())
                store.save_email_analysis(record)
                processed += 1
                # Auto-create only for a verified, future explicit date. Any Decider
                # uncertainty/rejection falls back to the normal Yes/No prompt.
                if (analysis.important and analysis.deadline_iso and analysis.date_evidence
                        and _quoted_absolute_date_matches(analysis.date_evidence, analysis.deadline_iso, settings.timezone)):
                    try:
                        from assistant.strands_calendar import CalendarGuardError, create_calendar_event_with_strands

                        settings = context.application.bot_data["settings"]
                        calendar = context.application.bot_data["calendar"]
                        event = await asyncio.to_thread(
                            create_calendar_event_with_strands,
                            settings, calendar,
                            title=message["subject"] or analysis.summary,
                            start_value=analysis.deadline_iso,
                            description=(f"Automatic reminder from important email.\nFrom: {message['sender']}\n"
                                         f"Summary: {analysis.summary}\nDate evidence: {analysis.date_evidence}\n"
                                         f"Source email: https://mail.google.com/mail/u/0/#all/{message_id}"),
                            source_message_id=message_id,
                            priority="High",
                            human_confirmed=False,
                            source_grounded=True,
                            date_evidence=analysis.date_evidence,
                        )
                        store.finish_email_action(message_id, "added", event.get("id"))
                        now = datetime.now(settings.timezone)
                        # Reconcile the whole calendar; reconciling only this new event
                        # would incorrectly remove reminders for the user's other events.
                        upcoming = await asyncio.to_thread(
                            calendar.list_events, now,
                            now + timedelta(days=settings.calendar_lookahead_days),
                        )
                        store.reconcile_reminders(upcoming, now)
                        log.info("Automatic email reminder created for Gmail message %s", message_id)
                    except (CalendarGuardError, ValueError):
                        log.info("Mail date did not pass automatic Calendar safety; keeping manual confirmation")
                    except Exception:
                        log.exception("Automatic mail reminder failed; keeping manual confirmation for %s", message_id)
            except Exception:
                log.exception("Could not process Gmail message %s", message_id)
                failed_count += 1

        for row in store.unnotified_emails():
            local_received = datetime.fromisoformat(row["received_at"]).astimezone(settings.timezone)
            seen_label = ("Unread" if row["is_unread"] else "Read") if row["is_unread"] is not None else "status unknown"
            text = (f"📩 {row['subject']}\nFrom: {row['sender']}\n"
                    f"Received: {local_received.strftime('%d %b %Y, %I:%M %p')} IST · {seen_label}\n"
                    f"[{row['category']}] {row['summary']}")
            buttons = []
            if row["action_status"] == "pending" and row["deadline_iso"]:
                text += f"\nDate/deadline: {row['deadline_iso']}\nCalendar-la add pannattuma?"
                buttons.append([
                    InlineKeyboardButton("Yes", callback_data=f"mail:yes:{row['message_id']}"),
                    InlineKeyboardButton("No", callback_data=f"mail:no:{row['message_id']}"),
                ])
            elif row["action_status"] == "added":
                text += "\n✅ Calendar-la automatic reminder set panniyachu."
            buttons.append([InlineKeyboardButton("✉️ Reply to Gmail", callback_data=f"mail:reply:{row['message_id']}")])
            keyboard = InlineKeyboardMarkup(buttons)
            await context.bot.send_message(
                chat_id=settings.telegram_user_id,
                text=text[:3900],
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            store.mark_email_notified(row["message_id"])
            sent_count += 1
    except Exception:
        log.exception("Gmail scan failed")
        if manual:
            await context.bot.send_message(
                chat_id=settings.telegram_user_id,
                text=("Gmail scan fail aayiduchu. Google access scope irukku; hosted Strands connection/Gmail API issue irukkalaam. "
                      "Bot run aagura PowerShell-la 'Gmail scan failed' error details paarunga."),
            )
    if manual and failed_count:
        await context.bot.send_message(
            chat_id=settings.telegram_user_id,
            text=f"{failed_count} mail-ai summarize panna mudiyala. Thirumba try pannumbodhu retry aagum.",
        )
    elif manual and scan_ok and processed == 0 and sent_count == 0:
        await context.bot.send_message(
            chat_id=settings.telegram_user_id,
            text="Pudhu important mail edhuvum kandupidikkala 🙂",
        )
    return processed
