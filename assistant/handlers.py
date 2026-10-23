"""Telegram commands and callbacks; every handler checks the configured user ID."""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import ReplyKeyboardMarkup, Update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from assistant.gmail import scan_gmail
from assistant.capture import StrandsCapture, is_direct_add
from assistant.notion_notes import NotionNotes
from assistant.google_calendar import CalendarNotAuthorized, GoogleCalendar
from assistant.strands_calendar import CalendarGuardError, create_calendar_event_with_strands

log = logging.getLogger(__name__)


async def _ack_callback(query) -> None:
    """Acknowledge Telegram button taps before validation or any slow external call."""
    try:
        await query.answer()
    except Exception as exc:
        # Callback acknowledgement is UI-only. Keep processing the already received update.
        log.warning("Telegram callback acknowledgement failed (%s)", type(exc).__name__)


def authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings = context.application.bot_data["settings"]
    return bool(
        update.effective_user
        and update.effective_user.id == settings.telegram_user_id
        and update.effective_chat
        and update.effective_chat.type == "private"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    keyboard = ReplyKeyboardMarkup(
        [["/add", "/today"], ["/tasks", "/scanmail"], ["/status", "/notion"], ["/searchnotes", "/cancel"]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Task / reminder message type pannunga…",
    )
    await update.effective_message.reply_text(
        "வணக்கம்! நான் Maranthudaadha Assistant. Calendar reminders, காலை brief, Gmail summary, task capture, voice notes, Notion notes, weekly review செய்ய உதவுவேன்.\n\n"
        "/help — commands list\n/add <event details> — Calendar event add செய்ய\n/today — இன்றைய events\n/tasks — pending tasks\n/note <text> — Notion note save\n/searchnotes <words> — Notion search\n/notion — Notion connection check\n/status — Google connection status\n/scanmail — Gmail-ஐ இப்போ check பண்ண\n/cancel — pending task capture-ஐ cancel பண்ண",
        reply_markup=keyboard,
    )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply helpfully when the owner sends an unsupported bot command."""
    if not authorized(update, context):
        return
    command = (update.effective_message.text or "").split(maxsplit=1)[0]
    await update.effective_message.reply_text(
        f"{command} command theriyala. Available commands paakka /help anuppunga."
    )


async def add_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a Calendar capture from the explicit /add command."""
    if not authorized(update, context):
        return
    details = " ".join(context.args).strip()
    if details:
        context.user_data.pop("awaiting_calendar_event", None)
        await _capture_text(update, context, details)
        return
    context.user_data["awaiting_calendar_event"] = True
    await update.effective_message.reply_text(
        "Calendar-la add panna event details type pannunga. Example: `Tomorrow 5 PM project meeting`\n"
        "Date/time missing-na ketpen; add panna munnaadi confirmation kaatturen. `/cancel`-la stop pannalaam.",
        parse_mode="Markdown",
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    calendar: GoogleCalendar = context.application.bot_data["calendar"]
    try:
        events = await asyncio.to_thread(
            calendar.list_events,
            datetime.now(calendar.settings.timezone),
            datetime.now(calendar.settings.timezone).replace(hour=23, minute=59),
        )
        await update.effective_message.reply_text(f"Google Calendar connected ✅\nஇன்று/இப்போது உள்ள events: {len(events)}")
    except CalendarNotAuthorized as exc:
        await update.effective_message.reply_text(str(exc))
    except Exception:
        await update.effective_message.reply_text("Calendar status check முடியவில்லை. Logs-ஐ பாருங்கள்.")


async def notion_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    notion = context.application.bot_data.get("notion_notes")
    if notion:
        await update.effective_message.reply_text(
            "✅ Notion connected. `/note <text>`-la save pannunga; `/searchnotes <topic>`-la thedunga."
        )
        return
    await update.effective_message.reply_text(
        "Notion innum connect pannala. Setup panna:\n"
        "1. Notion-la integration create panni, notes database-oda share pannunga.\n"
        "2. Project `.env`-la `NOTION_API_TOKEN` and `NOTION_DATA_SOURCE_ID` set pannunga.\n"
        "3. Bot-ai restart panni `/notion` marubadi anuppunga.\n\n"
        "Token-ai Telegram chat-la anuppadheenga."
    )


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    calendar: GoogleCalendar = context.application.bot_data["calendar"]
    try:
        events = await asyncio.to_thread(calendar.today_events, datetime.now(ZoneInfo(calendar.settings.timezone_name)))
        if not events:
            return await update.effective_message.reply_text("இன்று Calendar-ல் events எதுவும் இல்லை 🙂")
        lines = ["இன்றைய events:"]
        for event in events:
            start_at = "முழு நாள்" if event.get("all_day") else event["start"].astimezone(calendar.settings.timezone).strftime("%I:%M %p")
            lines.append(f"• {start_at} — {'[HIGH] ' if event.get('priority') == 'High' else ''}{event['title']}")
        await update.effective_message.reply_text("\n".join(lines))
    except CalendarNotAuthorized as exc:
        await update.effective_message.reply_text(str(exc))
    except Exception:
        await update.effective_message.reply_text("Calendar events எடுக்க முடியவில்லை. கொஞ்ச நேரம் கழித்து முயற்சி செய்யுங்கள்.")


async def scan_mail_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    await update.effective_message.reply_text("Gmail-la important mails check pannuren…")
    await scan_gmail(context, manual=True)


async def gmail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await _ack_callback(query)
    settings = context.application.bot_data["settings"]
    if (
        query.from_user.id != settings.telegram_user_id
        or not query.message
        or query.message.chat.type != "private"
    ):
        return

    action, message_id = query.data.split(":", maxsplit=2)[1:]
    store = context.application.bot_data["store"]
    row = store.get_email(message_id)
    if not row:
        if query.message:
            await query.message.reply_text("இந்த mail record கிடைக்கவில்லை.")
        return

    if action == "reply":
        context.user_data["awaiting_gmail_reply_to"] = message_id
        await query.message.reply_text(
            f"✉️ {row['subject']} mail-ku anuppa vendiya reply-ai type pannunga.\n"
            "Mudhalil preview kaatturen; Send press pannina mattum mail pogum."
        )
        return

    if not store.claim_email_action(message_id, action):
        current = store.get_email(message_id)
        label = "Calendar-la already add panniyachu." if current and current["action_status"] == "added" else "இந்த choice ஏற்கனவே process ஆயிடுச்சு."
        if query.message:
            await query.message.reply_text(label)
        return

    if action == "no":
        await query.edit_message_text(
            (query.message.text or "").replace("Calendar-la add pannattuma?", "Calendar-la add pannala."),
            reply_markup=None,
        )
        return

    calendar: GoogleCalendar = context.application.bot_data["calendar"]
    mail_url = f"https://mail.google.com/mail/u/0/#all/{message_id}"
    description = (
        f"Added after your Telegram confirmation.\nFrom: {row['sender']}\n"
        f"Summary: {row['summary']}\nDate evidence: {row['date_evidence']}\nSource email: {mail_url}"
    )
    try:
        event = await asyncio.to_thread(
            create_calendar_event_with_strands,
            settings,
            calendar,
            title=row["subject"] or row["summary"],
            start_value=row["deadline_iso"],
            description=description,
            source_message_id=message_id,
            human_confirmed=True,
        )
        store.finish_email_action(message_id, "added", event.get("id"))
        result = (query.message.text or "") + "\n\n✅ Calendar-la add panniten."
        if event.get("htmlLink"):
            result += f"\n{event['htmlLink']}"
        await query.edit_message_text(result[:3900], reply_markup=None, disable_web_page_preview=True)
    except ValueError:
        store.finish_email_action(message_id, "expired")
        await query.edit_message_text(
            (query.message.text or "") + "\n\n⌛ Date/time kadandhadhunaala Calendar-la add pannala.",
            reply_markup=None,
        )
    except CalendarGuardError as exc:
        store.finish_email_action(message_id, "pending")
        retry_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Try again", callback_data=f"mail:yes:{message_id}"),
            InlineKeyboardButton("No", callback_data=f"mail:no:{message_id}"),
        ]])
        await query.edit_message_text(
            (query.message.text or "") + f"\n\n🛡️ Strands/Decider check pass aagala: {str(exc)[:220]}\nCalendar-la add pannala.",
            reply_markup=retry_markup,
        )
    except Exception:
        store.finish_email_action(message_id, "pending")
        retry_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes", callback_data=f"mail:yes:{message_id}"),
            InlineKeyboardButton("No", callback_data=f"mail:no:{message_id}"),
        ]])
        await query.edit_message_reply_markup(reply_markup=retry_markup)


def _analysis_record(source: str, result, capture_id: str, direct: bool) -> dict:
    return {"capture_id": capture_id, "source_text": source, "title": result.title,
            "event_date": result.event_date, "event_time": result.event_time,
            "priority": result.priority, "direct_requested": direct,
            "status": "awaiting_date" if not result.event_date else "awaiting_time" if not result.event_time else "confirm"}


async def _capture_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, *, forwarded: bool = False) -> None:
    if not authorized(update, context):
        return
    settings = context.application.bot_data["settings"]
    store = context.application.bot_data["store"]
    extractor: StrandsCapture = context.application.bot_data["capture_ai"]
    waiting = store.latest_waiting_capture()
    direct = is_direct_add(text, forwarded=forwarded) if not waiting else False
    capture_id = waiting["capture_id"] if waiting else uuid.uuid4().hex
    source = (waiting["source_text"] + "\nClarification: " + text) if waiting else text
    try:
        result = await asyncio.to_thread(extractor.analyze, source, datetime.now(settings.timezone))
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    except Exception:
        await update.effective_message.reply_text("Task-a purinjukka mudiyala. Konjam clear-ah anuppunga; event create pannala.")
        return
    record = _analysis_record(source, result, capture_id, bool(waiting["direct_requested"]) if waiting else direct)
    store.save_capture(record)
    if not result.event_date:
        await update.effective_message.reply_text("எந்த நாள், என்ன நேரத்துக்கு வைக்கணும்?")
        return
    if not result.event_time:
        await update.effective_message.reply_text("என்ன நேரத்துக்கு Calendar-la add பண்ணட்டும்?")
        return
    local_start = datetime.combine(date.fromisoformat(result.event_date), time.fromisoformat(result.event_time), settings.timezone)
    if local_start <= datetime.now(settings.timezone):
        store.finish_capture(capture_id, "awaiting_time")
        await update.effective_message.reply_text("இந்த நேரம் கடந்துவிட்டது. வேறொரு future time சொல்லுங்க.")
        return
    if record["direct_requested"]:
        await _create_capture_event(update, context, capture_id)
        return
    date_text = local_start.strftime("%d %b %Y, %I:%M %p")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Yes, add", callback_data=f"capture:yes:{capture_id}"),
                                      InlineKeyboardButton("No", callback_data=f"capture:no:{capture_id}")]])
    await update.effective_message.reply_text(
        f"📌 Calendar-la add pannattuma?\n{result.title}\n{date_text} IST\nPriority: {result.priority}", reply_markup=keyboard)


async def capture_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    text = message.text or ""
    if not authorized(update, context):
        return
    reply_to = context.user_data.get("awaiting_gmail_reply_to")
    if reply_to:
        if not text.strip() or len(text.strip()) > 3000:
            await message.reply_text("Reply 1–3000 characters irukkanum. Short-a type pannunga.")
            return
        context.user_data.pop("awaiting_gmail_reply_to", None)
        context.user_data["pending_gmail_reply"] = {"message_id": reply_to, "body": text.strip()}
        row = context.application.bot_data["store"].get_email(reply_to)
        subject = row["subject"] if row else "(No subject)"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Send reply", callback_data="mailreply:send"),
            InlineKeyboardButton("Cancel", callback_data="mailreply:cancel"),
        ]])
        await message.reply_text(
            f"✉️ Reply preview — {subject}\n\n{text.strip()}",
            reply_markup=keyboard,
        )
        return
    if context.user_data.pop("awaiting_calendar_event", False):
        await _capture_text(update, context, text)
        return
    lower = text.casefold().strip()
    if not message.forward_origin:
        note_match = re.match(r"^(?:note\s+pannu|save\s+note)\s*[:,-]?\s*(.+)$", text, flags=re.I | re.S)
        if note_match:
            await save_note_text(update, context, note_match.group(1))
            return
        if re.search(r"enna\s+sonnanga|enna\s+sonnaanga|what\s+did\s+.+\s+say|search\s+notes|find\s+note", lower):
            question = re.sub(r"(?:enna\s+sonnanga|enna\s+sonnaanga|what\s+did\s+.+\s+say|search\s+notes|find\s+note)", "", text, flags=re.I).strip(" ?!.,") or text
            await search_note_text(update, context, question)
            return
    await _capture_text(update, context, text, forwarded=bool(message.forward_origin))


async def gmail_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await _ack_callback(query)
    settings = context.application.bot_data["settings"]
    if (query.from_user.id != settings.telegram_user_id or not query.message
            or query.message.chat.type != "private"):
        return
    action = query.data.split(":", maxsplit=1)[1]
    draft = context.user_data.get("pending_gmail_reply")
    if not draft:
        await query.message.reply_text("Reply draft expire aagiduchu; Gmail mail-la Reply button press panni marubadi type pannunga.")
        return
    if action == "cancel":
        context.user_data.pop("pending_gmail_reply", None)
        await query.edit_message_text((query.message.text or "") + "\n\nReply cancel panniten.", reply_markup=None)
        return

    if action == "override" and draft.get("policy_status") not in {"review", "unavailable"}:
        await query.message.reply_text("Indha draft-ku Send anyway option available illa. Reply-ai marubadi review pannunga.")
        return

    if action == "send" and not draft.get("policy_status"):
        from assistant.reply_policy import review_gmail_reply

        await query.edit_message_text((query.message.text or "") + "\n\nReply safety check pannuren…", reply_markup=None)
        try:
            verdict = await asyncio.to_thread(review_gmail_reply, settings, draft["body"])
        except Exception:
            log.exception("Gmail reply policy check failed")
            draft["policy_status"] = "unavailable"
            context.user_data["pending_gmail_reply"] = draft
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Send anyway", callback_data="mailreply:override"),
                InlineKeyboardButton("Cancel", callback_data="mailreply:cancel"),
            ]])
            await query.message.reply_text(
                "Safety check ippo mudiyala; Gmail-ku reply innum pogala. Neenga draft-ai review pannitu "
                "Send anyway press pannalaam, illa Cancel.", reply_markup=keyboard
            )
            return

        if verdict.action == "block":
            context.user_data["pending_gmail_reply"] = draft
            await query.message.reply_text(
                f"⚠️ Reply safety check block panniduchu ({verdict.category}); mail send aagala. "
                "Reply button-ai marubadi press panni draft-ai maathi anuppunga, illa Cancel pannunga."
            )
            return
        if verdict.action == "review":
            draft["policy_status"] = "review"
            context.user_data["pending_gmail_reply"] = draft
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Send anyway", callback_data="mailreply:override"),
                InlineKeyboardButton("Cancel", callback_data="mailreply:cancel"),
            ]])
            await query.message.reply_text(
                f"⚠️ Reply safety review thevai ({verdict.category}, {verdict.severity}). "
                "Draft-ai check pannitu, intentional-na Send anyway press pannunga.", reply_markup=keyboard
            )
            return
        draft["policy_status"] = "allowed"
        context.user_data["pending_gmail_reply"] = draft

    if action == "override":
        draft["policy_status"] = "overridden"
        context.user_data["pending_gmail_reply"] = draft

    from assistant.gmail import GmailReader

    reader: GmailReader = context.application.bot_data["gmail_reader"]
    await query.edit_message_text((query.message.text or "") + "\n\nSending…", reply_markup=None)
    try:
        result = await asyncio.to_thread(reader.send_reply, draft["message_id"], draft["body"])
    except Exception:
        log.exception("Gmail reply send failed for message %s", draft["message_id"])
        # Keep the exact draft. A transport timeout may mean Gmail accepted it;
        # the user should check Sent before trying the Send button again.
        context.user_data["pending_gmail_reply"] = draft
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Retry send (after checking Sent)", callback_data="mailreply:send"),
            InlineKeyboardButton("Cancel", callback_data="mailreply:cancel"),
        ]])
        await query.message.reply_text(
            "Reply send result confirm aagala. Duplicate avoid panna Sent folder check pannunga; "
            "send aagala-na mattum Retry press pannunga.", reply_markup=keyboard
        )
        return
    context.user_data.pop("pending_gmail_reply", None)
    await query.message.reply_text("✅ Gmail reply anuppitten." + (f"\nThread: {result.get('threadId')}" if result.get("threadId") else ""))


async def save_note_text(update: Update, context: ContextTypes.DEFAULT_TYPE, content: str) -> None:
    if not authorized(update, context):
        return
    notion: NotionNotes | None = context.application.bot_data.get("notion_notes")
    if not notion:
        await update.effective_message.reply_text("Notion connect panna .env-la NOTION_API_TOKEN, NOTION_DATA_SOURCE_ID set pannunga.")
        return
    try:
        page = await asyncio.to_thread(notion.save_note, content)
        suffix = f"\n{page.get('url')}" if page.get("url") else ""
        await update.effective_message.reply_text("📝 Notion-la note save panniten." + suffix, disable_web_page_preview=True)
    except Exception as exc:
        await update.effective_message.reply_text(f"Notion-la save aagala: {str(exc)[:250]}")


async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_note_text(update, context, " ".join(context.args))


async def search_note_text(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str) -> None:
    if not authorized(update, context):
        return
    notion: NotionNotes | None = context.application.bot_data.get("notion_notes")
    if not notion:
        await update.effective_message.reply_text("Notion connect panna .env-la NOTION_API_TOKEN, NOTION_DATA_SOURCE_ID set pannunga.")
        return
    try:
        list_all = not question.strip()
        notes = await asyncio.to_thread(notion.list_notes if list_all else notion.search_notes,
                                        *([] if list_all else [question]))
        if not notes:
            await update.effective_message.reply_text(
                "Notion database-la notes innum illa 🙂" if list_all else "Andha topic-ku Notion note kidaikkala."
            )
            return
        heading = (f"📚 Notion database notes ({len(notes)}):" if list_all
                   else f"🔎 Matching Notion notes ({len(notes)}):")
        text = heading + "\n\n" + "\n\n".join(
            f"📝 {note['title']}\n{note['content']}" + (f"\n{note['url']}" if note["url"] else "")
            for note in notes
        )
        # Telegram limits each text message to 4096 characters. Split into
        # readable chunks so all notes or all search matches can be returned.
        while text:
            if len(text) <= 3800:
                chunk, text = text, ""
            else:
                split_at = text.rfind("\n", 0, 3800)
                if split_at < 1000:
                    split_at = 3800
                chunk, text = text[:split_at], text[split_at:].lstrip("\n")
            await update.effective_message.reply_text(chunk, disable_web_page_preview=True)
    except Exception as exc:
        await update.effective_message.reply_text(f"Notion search panna mudiyala: {str(exc)[:250]}")


async def search_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await search_note_text(update, context, " ".join(context.args))


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    rows = context.application.bot_data["store"].pending_captures()
    if not rows:
        await update.effective_message.reply_text("Pending captured tasks ethum illa 🙂")
        return
    for row in rows[:12]:
        label = f"{row['title']} — {row['event_date'] or 'date missing'} {row['event_time'] or ''} [{row['priority']}]"
        if row["status"] == "created":
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("Done ✅", callback_data=f"task:done:{row['capture_id']}")]])
        elif row["status"] == "confirm":
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("Retry Calendar add", callback_data=f"capture:yes:{row['capture_id']}"),
                                            InlineKeyboardButton("Cancel", callback_data=f"capture:no:{row['capture_id']}")]])
        else:
            markup = None
        await update.effective_message.reply_text(label, reply_markup=markup)


async def task_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _ack_callback(query)
    if not authorized(update, context):
        return
    _, _, capture_id = query.data.split(":", 2)
    if context.application.bot_data["store"].mark_capture_done(capture_id):
        await query.edit_message_text((query.message.text or "") + "\n✅ Completed", reply_markup=None)
    else:
        if query.message:
            await query.message.reply_text("Task already completed or unavailable.")


async def reminder_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _ack_callback(query)
    if not authorized(update, context):
        return
    _, action_name, token = query.data.split(":", 2)
    store = context.application.bot_data["store"]
    action = store.claim_reminder_action(token, action_name)
    if not action:
        if query.message:
            await query.message.reply_text("இந்த reminder ஏற்கனவே process ஆயிடுச்சு.")
        return
    if action_name == "done":
        store.mark_event_done(action["event_id"])
        await query.edit_message_text((query.message.text or "") + "\n✅ Done", reply_markup=None)
        return
    delta = timedelta(hours=1) if action_name == "s1" else timedelta(days=1)
    due = datetime.now(context.application.bot_data["settings"].timezone) + delta
    event_start = datetime.fromisoformat(action["event_start"]).astimezone(context.application.bot_data["settings"].timezone)
    if due >= event_start:
        if query.message:
            await query.message.reply_text("Event-ku munnaadi snooze time illa; reminder set pannala.")
        return
    store.schedule_snooze(action, due)
    await query.edit_message_text((query.message.text or "") + f"\n🔔 {('1 hour' if action_name == 's1' else 'Tomorrow')} snooze set panniten.", reply_markup=None)


async def _create_capture_event(update: Update, context: ContextTypes.DEFAULT_TYPE, capture_id: str) -> None:
    store = context.application.bot_data["store"]
    if not store.claim_capture(capture_id):
        await update.effective_message.reply_text("இந்த task ஏற்கனவே process ஆயிடுச்சு.")
        return
    row = store.get_capture(capture_id)
    calendar: GoogleCalendar = context.application.bot_data["calendar"]
    settings = context.application.bot_data["settings"]
    start = datetime.combine(date.fromisoformat(row["event_date"]), time.fromisoformat(row["event_time"]), settings.timezone)
    try:
        event = await asyncio.to_thread(
            create_calendar_event_with_strands,
            settings,
            calendar,
            title=row["title"],
            start_value=start.isoformat(),
            description="Created from a confirmed Telegram inbox capture.",
            source_message_id=capture_id,
            priority=row["priority"],
            # This helper is reached only after the Yes button, or an explicit direct-add command.
            human_confirmed=True,
        )
    except Exception:
        log.exception("Calendar event creation failed for capture %s", capture_id)
        store.finish_capture(capture_id, "confirm")
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Try again", callback_data=f"capture:yes:{capture_id}"),
            InlineKeyboardButton("No", callback_data=f"capture:no:{capture_id}"),
        ]])
        await update.effective_message.reply_text(
            "Calendar-la add aagala. Strands/Decider check or Google Calendar issue irukkalaam. "
            "Try again press pannunga; safety check pass aana mattum add aagum.",
            reply_markup=markup,
        )
        return

    # Once Google Calendar confirms the write, persist success before sending Telegram output.
    # A Telegram network error must not turn a created event back into a retryable task.
    store.finish_capture(capture_id, "created", event.get("id"))
    store.reconcile_reminders([{"id": event["id"], "title": row["title"], "start": start, "priority": row["priority"]}], datetime.now(settings.timezone))
    try:
        await update.effective_message.reply_text(f"✅ Calendar-la add panniten: {row['title']}\n{start.strftime('%d %b, %I:%M %p')} IST\nPriority: {row['priority']}")
    except Exception:
        log.exception("Calendar event was created but Telegram confirmation could not be sent for capture %s", capture_id)


async def capture_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _ack_callback(query)
    if not authorized(update, context):
        return
    _, action, capture_id = query.data.split(":", 2)
    store = context.application.bot_data["store"]
    row = store.get_capture(capture_id)
    if not row or row["status"] != "confirm":
        if query.message:
            await query.message.reply_text("இந்த task ஏற்கனவே process ஆயிடுச்சு.")
        return
    if action == "no":
        store.finish_capture(capture_id, "cancelled")
        try:
            await query.edit_message_text((query.message.text or "") + "\n\n❌ Add pannala.", reply_markup=None)
        except Exception:
            log.exception("Could not update Telegram after cancelling capture %s", capture_id)
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        log.exception("Could not hide Calendar retry buttons for capture %s", capture_id)
    await _create_capture_event(update, context, capture_id)


async def cancel_capture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    if context.user_data.pop("awaiting_calendar_event", False):
        await update.effective_message.reply_text("சரி, Calendar event add flow cancel பண்ணிட்டேன்.")
        return
    row = context.application.bot_data["store"].latest_waiting_capture()
    if row:
        context.application.bot_data["store"].finish_capture(row["capture_id"], "cancelled")
        await update.effective_message.reply_text("சரி, அந்த pending capture cancel பண்ணிட்டேன்.")
    else:
        await update.effective_message.reply_text("Cancel செய்ய pending capture இல்லை.")


async def voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    message = update.effective_message
    voice = message.voice
    settings = context.application.bot_data["settings"]
    extractor: StrandsCapture = context.application.bot_data["capture_ai"]
    if (voice.file_size or 0) > settings.voice_max_bytes or voice.duration > settings.voice_max_seconds:
        await message.reply_text("Voice note romba perusa/long-ah irukku. 5 minutes-kulla short note anuppunga.")
        return
    status_msg = await message.reply_text("Voice note-a ketkiren…")
    try:
        telegram_file = await context.bot.get_file(voice.file_id, read_timeout=20, write_timeout=20, connect_timeout=10)
        from io import BytesIO
        audio = BytesIO()
        await telegram_file.download_to_memory(audio, read_timeout=30, write_timeout=30, connect_timeout=10)
        transcript = await asyncio.to_thread(extractor.transcribe, audio.getvalue())
        await status_msg.edit_text("Purinjukitten; task details check pannuren…")
        await _capture_text(update, context, transcript)
    except Exception as exc:
        log.exception("Voice note download or transcription failed")
        if not settings.gemini_api_key:
            await status_msg.edit_text("Voice transcription-ku Gemini API key thevai. Ippo text message anuppalaam; Strands text workflow work aagum.")
        elif isinstance(exc, ValueError) and "No speech was recognized" in str(exc):
            await status_msg.edit_text("Audio-la clear speech detect aagala. Konjam clear-ah pesi, voice note marubadi anuppunga; illaina text-ah anuppalaam.")
        elif getattr(exc, "code", None) in {429, 500, 502, 503, 504}:
            await status_msg.edit_text("Gemini service ippo busy-ah irukku. Konjam neram kazhichu voice note marubadi anuppunga.")
        else:
            await status_msg.edit_text("Voice note transcribe panna mudiyala. Text-ah anuppi try pannunga.")
