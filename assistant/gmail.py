"""Read filtered Gmail messages and deliver one-time Telegram summaries."""
from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timedelta, timezone
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
from assistant.language import response_language

log = logging.getLogger(__name__)
# Gmail search's relative units are day-based; scan the last day, then enforce
# the exact five-hour window against each message's internalDate below.
GMAIL_QUERY = "in:inbox newer_than:1d"
EMAIL_TRIAGE_TERMS = (
    "exam", "college", "gdg", "internship", "interview", "deadline", "due date",
    "assignment", "placement", "registration",
)


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
            "snippet": message.get("snippet", "")[:500],
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
    lang = response_language(store)
    processed = 0
    scan_ok = False
    sent_count = 0
    failed_count = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=5)
    try:
        # Surface already-stored recent mail once in the new grouped layout.
        store.prepare_recent_mail_grouping(cutoff.isoformat())
        known_ids = store.processed_email_ids()
        candidates = await asyncio.to_thread(reader.list_candidates, settings.gmail_scan_max_results, known_ids)
        scan_ok = True
        for candidate in candidates:
            message_id = candidate.get("id")
            if not message_id or store.email_processed(message_id):
                continue
            try:
                message = await asyncio.to_thread(reader.read_message, message_id)
                received_at = datetime.fromisoformat(message["received_at"])
                if received_at < cutoff:
                    continue
                record = {key: value for key, value in message.items() if key not in {"body", "snippet"}}
                text_for_triage = f"{message['subject']}\n{message['sender']}\n{message['body']}".casefold()
                analysis = None
                if any(term in text_for_triage for term in EMAIL_TRIAGE_TERMS):
                    # Keep the existing hosted-model triage limited to mail that
                    # matches the assistant's academic/career relevance terms.
                    try:
                        analysis = await asyncio.to_thread(
                            triage.analyze,
                            subject=message["subject"],
                            sender=message["sender"],
                            received_at=received_at,
                            body=message["body"],
                            response_language=lang,
                        )
                        record.update(analysis.model_dump())
                    except Exception as exc:
                        # A blocked/unavailable hosted AI endpoint must not hide recent mail.
                        log.warning("Email triage unavailable for %s (%s); storing Gmail preview",
                                    message_id, type(exc).__name__)
                        preview = " ".join((message["snippet"] or message["body"] or "").split())[:700]
                        fallback = (("AI summary unavailable. Gmail preview: " if lang == "en"
                                     else "AI சுருக்கம் கிடைக்கவில்லை. மெயில் முன்னோட்டம்: ") +
                                    (preview or ("No preview available." if lang == "en" else "முன்னோட்டம் இல்லை.")))
                        record.update(important=False, category="other", summary=fallback,
                                      deadline_iso=None, date_evidence="")
                else:
                    record.update(
                        important=False,
                        category="other",
                        summary=message["snippet"] or "(No preview available)",
                        deadline_iso=None,
                        date_evidence="",
                    )
                store.save_email_analysis(record)
                processed += 1
                # Auto-create only for a verified, future explicit date. Any Decider
                # uncertainty/rejection falls back to the normal Yes/No prompt.
                if (analysis and analysis.important and analysis.deadline_iso and analysis.date_evidence
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

        recent_rows = store.unnotified_emails(cutoff.isoformat())
        groups = ((True, "📬 Unseen / Unread mails" if lang == "en" else "📬 பார்க்காத மெயில்கள்"),
                  (False, "📭 Seen / Read mails" if lang == "en" else "📭 பார்த்த மெயில்கள்"),
                  (None, "📨 Mails with unknown read status" if lang == "en" else "📨 படித்த நிலை தெரியாத மெயில்கள்"))
        for unread_value, heading in groups:
            rows = [row for row in recent_rows if row["is_unread"] == unread_value]
            if not rows:
                continue
            await context.bot.send_message(
                chat_id=settings.telegram_user_id,
                text=f"{heading} — last 5 hours: {len(rows)}",
            )
            for row in rows:
                local_received = datetime.fromisoformat(row["received_at"]).astimezone(settings.timezone)
                seen_label = (("Unseen / Unread" if lang == "en" else "பார்க்காதது") if row["is_unread"]
                              else ("Seen / Read" if lang == "en" else "பார்த்தது"))
                text = (f"📩 {row['subject']}\nFrom: {row['sender']}\n"
                        f"Received: {local_received.strftime('%d %b %Y, %I:%M %p')} IST · {seen_label}\n"
                        f"[{row['category']}] {row['summary']}")
                buttons = []
                yes_label, no_label, reply_label = (("Yes", "No", "✉️ Reply to Gmail") if lang == "en"
                                                    else ("ஆம்", "வேண்டாம்", "✉️ Gmail-க்கு பதில்"))
                if row["important"] and row["action_status"] == "pending" and row["deadline_iso"]:
                    text += (f"\nDate/deadline: {row['deadline_iso']}\nAdd this to Calendar?" if lang == "en"
                             else f"\nDate/deadline: {row['deadline_iso']}\nCalendar-la add pannattuma?")
                    buttons.append([
                        InlineKeyboardButton(yes_label, callback_data=f"mail:yes:{row['message_id']}"),
                        InlineKeyboardButton(no_label, callback_data=f"mail:no:{row['message_id']}"),
                    ])
                elif row["action_status"] == "added":
                    text += "\n✅ Automatic Calendar reminder added." if lang == "en" else "\n✅ Calendar-la automatic reminder set panniyachu."
                buttons.append([InlineKeyboardButton(reply_label, callback_data=f"mail:reply:{row['message_id']}")])
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
                text=(("Gmail scan failed. Google access is configured; check the hosted Strands connection or Gmail API. "
                       "See the 'Gmail scan failed' details in the bot's PowerShell window." if lang == "en" else
                       "Gmail scan fail aayiduchu. Google access scope irukku; hosted Strands connection/Gmail API issue irukkalaam. "
                       "Bot run aagura PowerShell-la 'Gmail scan failed' error details paarunga.")),
            )
    if manual and failed_count:
        await context.bot.send_message(
            chat_id=settings.telegram_user_id,
            text=(f"I couldn't summarize {failed_count} email(s). They will be retried on the next scan." if lang == "en"
                  else f"{failed_count} mail-ai summarize panna mudiyala. Thirumba try pannumbodhu retry aagum."),
        )
    elif manual and scan_ok and processed == 0 and sent_count == 0:
        await context.bot.send_message(
            chat_id=settings.telegram_user_id,
            text="No new emails found in the last 5 hours 🙂" if lang == "en" else "Last 5 hours-la pudhu mail edhuvum kandupidikkala 🙂",
        )
    return processed
