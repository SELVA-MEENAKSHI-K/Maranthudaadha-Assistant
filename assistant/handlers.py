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
from assistant.language import response_language

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


def _calendar_error(exc: Exception, lang: str) -> str:
    if lang == "en":
        return str(exc)
    detail = str(exc).casefold()
    if "not been connected" in detail:
        return "Google Calendar இன்னும் இணைக்கப்படவில்லை. `scripts/auth_google.py` மூலம் connect செய்யுங்கள்."
    if "expired" in detail or "reconnect" in detail:
        return "Google Calendar authorization காலாவதியானது. `scripts/auth_google.py` மூலம் மீண்டும் connect செய்யுங்கள்."
    return "Google Calendar-ஐ அணுக முடியவில்லை. Google connection-ஐ சரிபார்த்து மீண்டும் முயற்சி செய்யுங்கள்."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    store = context.application.bot_data["store"]
    selected = store.get_preference("response_language")
    if selected not in {"ta", "en", "auto"}:
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("தமிழ்", callback_data="lang:ta"),
            InlineKeyboardButton("English", callback_data="lang:en"),
            InlineKeyboardButton("Auto", callback_data="lang:auto"),
        ]])
        await update.effective_message.reply_text(
            "வணக்கம்! Hello! Bot எந்த மொழியில் பேசணும்? Choose a language for the whole bot: தமிழா, English-ah, or Auto?",
            reply_markup=markup,
        )
        return
    keyboard = ReplyKeyboardMarkup(
        [["/add", "/plan"], ["/findtime", "/week"], ["/today", "/tasks"], ["/scanmail", "/status"], ["/notion", "/searchnotes"], ["/language", "/cancel"]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Task / reminder message type pannunga…",
    )
    lang = response_language(context)
    if lang == "en":
        welcome = ("Hello! I'm Maranthudaadha Assistant. I can help with Calendar reminders, daily plans, Gmail summaries, tasks, voice notes and Notion notes.\n\n"
                   "/help — command list\n/add <event details> — add a Calendar event\n/plan — today's events, tasks and email deadlines\n/findtime [minutes] — find free time in the next 7 days\n/week — next 7 days of Calendar events\n/today — today's events\n/tasks — pending tasks\n/note <text> — save a Notion note\n/searchnotes <words> — search Notion\n/notion — Notion connection\n/status — Google connection\n/scanmail — check recent Gmail\n/language — choose Tamil, English or Auto\n/cancel — cancel a pending capture")
    else:
        welcome = ("வணக்கம்! நான் Maranthudaadha Assistant. Calendar reminders, காலை brief, Gmail summary, task capture, voice notes, Notion notes, weekly review செய்ய உதவுவேன்.\n\n"
                   "/help — commands list\n/add <event details> — Calendar event add செய்ய\n/plan — இன்றைய events, pending tasks, email deadlines\n/findtime [minutes] — அடுத்த 7 நாட்களில் free time தேட\n/week — அடுத்த 7 நாட்களின் Calendar events\n/today — இன்றைய events\n/tasks — pending tasks\n/note <text> — Notion note save\n/searchnotes <words> — Notion search\n/notion — Notion connection check\n/status — Google connection status\n/scanmail — Gmail-ஐ இப்போ check பண்ண\n/language — தமிழ், English அல்லது Auto தேர்வு செய்ய\n/cancel — pending task capture-ஐ cancel பண்ண")
    await update.effective_message.reply_text(
        welcome,
        reply_markup=keyboard,
    )


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    selected = context.application.bot_data["store"].get_preference("response_language", "auto")
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("தமிழ்", callback_data="lang:ta"),
        InlineKeyboardButton("English", callback_data="lang:en"),
        InlineKeyboardButton("Auto", callback_data="lang:auto"),
    ]])
    lang = response_language(context)
    prompt = (f"Reply language: {selected.upper()}\nChoose Tamil or English, or let Auto detect your message language." if lang == "en"
              else f"பதில் மொழி: {selected.upper()}\nதமிழ் / English தேர்வு செய்யுங்கள்; Auto உங்கள் message மொழியை கண்டறியும்.")
    await update.effective_message.reply_text(
        prompt,
        reply_markup=markup,
    )


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await _ack_callback(query)
    if not authorized(update, context):
        return
    choice = query.data.split(":", 1)[1]
    context.application.bot_data["store"].set_preference("response_language", choice)
    messages = {
        "ta": "மொழி தமிழாக அமைக்கப்பட்டது. இனிமேல் தமிழில் பதில் தருகிறேன்.",
        "en": "Language set to English. I'll reply in English from now on.",
        "auto": "Auto language enabled. Tamil/Tanglish message-ku Tamil; English message-ku English-la reply pannuren.",
    }
    await query.edit_message_text(messages[choice], reply_markup=None)
    await start(update, context)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply helpfully when the owner sends an unsupported bot command."""
    if not authorized(update, context):
        return
    command = (update.effective_message.text or "").split(maxsplit=1)[0]
    lang = response_language(context)
    await update.effective_message.reply_text(
        (f"I don't know the {command} command. Send /help to see available commands." if lang == "en"
         else f"{command} command theriyala. Available commands paakka /help anuppunga.")
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
    lang = response_language(context)
    await update.effective_message.reply_text(
        ("Send the event details to add to Calendar. Example: `Tomorrow 5 PM project meeting`\n"
         "If the date or time is missing, I'll ask. I'll show a confirmation before adding. Use `/cancel` to stop." if lang == "en"
         else "Calendar-la add panna event details type pannunga. Example: `Tomorrow 5 PM project meeting`\n"
         "Date/time missing-na ketpen; add panna munnaadi confirmation kaatturen. `/cancel`-la stop pannalaam."),
        parse_mode="Markdown",
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    calendar: GoogleCalendar = context.application.bot_data["calendar"]
    lang = response_language(context)
    try:
        events = await asyncio.to_thread(
            calendar.list_events,
            datetime.now(calendar.settings.timezone),
            datetime.now(calendar.settings.timezone).replace(hour=23, minute=59),
        )
        await update.effective_message.reply_text((f"Google Calendar connected ✅\nEvents today/now: {len(events)}" if lang == "en"
                                                   else f"Google Calendar connected ✅\nஇன்று/இப்போது உள்ள events: {len(events)}"))
    except CalendarNotAuthorized as exc:
        await update.effective_message.reply_text(_calendar_error(exc, lang))
    except Exception:
        await update.effective_message.reply_text("I couldn't check Calendar status. Please check the bot logs." if lang == "en" else "Calendar status check முடியவில்லை. Logs-ஐ பாருங்கள்.")


async def notion_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    lang = response_language(context)
    notion = context.application.bot_data.get("notion_notes")
    if notion:
        await update.effective_message.reply_text(
            ("✅ Notion is connected. Save with `/note <text>` or search with `/searchnotes <topic>`." if lang == "en"
             else "✅ Notion connected. `/note <text>`-la save pannunga; `/searchnotes <topic>`-la thedunga.")
        )
        return
    message = ("Notion is not connected. To set it up:\n"
               "1. Create a Notion integration and share your notes database with it.\n"
               "2. Set `NOTION_API_TOKEN` and `NOTION_DATA_SOURCE_ID` in the project `.env`.\n"
               "3. Restart the bot and send `/notion` again.\n\nDo not send the token in Telegram." if lang == "en" else
               "Notion innum connect pannala. Setup panna:\n"
               "1. Notion-la integration create panni, notes database-oda share pannunga.\n"
               "2. Project `.env`-la `NOTION_API_TOKEN` and `NOTION_DATA_SOURCE_ID` set pannunga.\n"
               "3. Bot-ai restart panni `/notion` marubadi anuppunga.\n\nToken-ai Telegram chat-la anuppadheenga.")
    await update.effective_message.reply_text(message)


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    calendar: GoogleCalendar = context.application.bot_data["calendar"]
    lang = response_language(context)
    try:
        events = await asyncio.to_thread(calendar.today_events, datetime.now(ZoneInfo(calendar.settings.timezone_name)))
        if not events:
            return await update.effective_message.reply_text("There are no Calendar events today 🙂" if lang == "en" else "இன்று Calendar-ல் events எதுவும் இல்லை 🙂")
        lines = ["Today's events:" if lang == "en" else "இன்றைய events:"]
        for event in events:
            start_at = ("All day" if lang == "en" else "முழு நாள்") if event.get("all_day") else event["start"].astimezone(calendar.settings.timezone).strftime("%I:%M %p")
            lines.append(f"• {start_at} — {'[HIGH] ' if event.get('priority') == 'High' else ''}{event['title']}")
        await update.effective_message.reply_text("\n".join(lines))
    except CalendarNotAuthorized as exc:
        await update.effective_message.reply_text(_calendar_error(exc, lang))
    except Exception:
        await update.effective_message.reply_text("I couldn't load Calendar events. Please try again shortly." if lang == "en" else "Calendar events எடுக்க முடியவில்லை. கொஞ்ச நேரம் கழித்து முயற்சி செய்யுங்கள்.")


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the next seven days of Calendar events grouped by local date."""
    if not authorized(update, context):
        return
    calendar: GoogleCalendar = context.application.bot_data["calendar"]
    timezone = calendar.settings.timezone
    lang = response_language(context)
    now = datetime.now(timezone)
    horizon = now + timedelta(days=7)
    try:
        events = await asyncio.to_thread(calendar.list_events, now, horizon)
    except CalendarNotAuthorized as exc:
        await update.effective_message.reply_text(_calendar_error(exc, lang))
        return
    except Exception:
        log.exception("Weekly Calendar overview failed")
        await update.effective_message.reply_text("I couldn't load the weekly Calendar overview. Please try again shortly." if lang == "en" else "Weekly Calendar overview load panna mudiyala. Konjam neram kazhichu try pannunga.")
        return

    events.sort(key=lambda event: event["start"].astimezone(timezone))
    grouped: dict[date, list[dict]] = {}
    for event in events:
        grouped.setdefault(event["start"].astimezone(timezone).date(), []).append(event)

    lines = [f"🗓️ Next 7 days — {now.strftime('%d %b')} to {horizon.strftime('%d %b')}" if lang == "en" else f"🗓️ அடுத்த 7 நாட்கள் — {now.strftime('%d %b')} முதல் {horizon.strftime('%d %b')} வரை"]
    if not grouped:
        lines.append("No Calendar events this week 🙂" if lang == "en" else "இந்த வாரம் Calendar events எதுவும் இல்லை 🙂")
    else:
        for day, day_events in grouped.items():
            lines.append(f"\n{day.strftime('%A, %d %b')}")
            for event in day_events:
                start_at = event["start"].astimezone(timezone)
                if event.get("all_day"):
                    when = "All day" if lang == "en" else "முழு நாள்"
                else:
                    end_at = event["end"].astimezone(timezone)
                    when = f"{start_at.strftime('%I:%M %p')}–{end_at.strftime('%I:%M %p')}"
                lines.append(f"• {when} — {event['title']}")
    await update.effective_message.reply_text("\n".join(lines)[:3900])


async def daily_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Combine today's Calendar events, pending captures and upcoming mail deadlines."""
    if not authorized(update, context):
        return
    settings = context.application.bot_data["settings"]
    calendar: GoogleCalendar = context.application.bot_data["calendar"]
    store = context.application.bot_data["store"]
    now = datetime.now(settings.timezone)
    lang = response_language(context)
    try:
        events = await asyncio.to_thread(calendar.today_events, now)
    except CalendarNotAuthorized as exc:
        await update.effective_message.reply_text(_calendar_error(exc, lang))
        return
    except Exception:
        await update.effective_message.reply_text("I couldn't load today's plan. Please try again shortly." if lang == "en" else "Today's plan load panna mudiyala. Konjam neram kazhichu try pannunga.")
        return

    priority_rank = {"High": 0, "Medium": 1, "Low": 2}
    focus_items: list[tuple[int, str, str]] = []
    for event in events:
        start_at = event["start"].astimezone(settings.timezone)
        when = "All day" if event.get("all_day") else start_at.strftime("%I:%M %p")
        priority = event.get("priority", "Medium")
        focus_items.append((priority_rank.get(priority, 1), start_at.isoformat(),
                            f"{when} — [{priority.upper()}] {event['title']} (Calendar)"))

    status_labels = {
        "awaiting_date": "needs a date",
        "awaiting_time": "needs a time",
        "confirm": "needs confirmation",
        "creating": "being added",
        "created": "task",
    }
    for task in store.pending_captures():
        priority = task["priority"]
        scheduled = " ".join(part for part in (task["event_date"], task["event_time"]) if part)
        when = scheduled or "Unscheduled"
        detail = f"{when} — [{priority.upper()}] {task['title']} ({status_labels.get(task['status'], task['status'])})"
        sort_time = f"{task['event_date'] or '9999-12-31'}T{task['event_time'] or '23:59'}"
        focus_items.append((priority_rank.get(priority, 1), sort_time, detail))
    focus_items.sort(key=lambda item: (item[0], item[1], item[2]))

    since = now - timedelta(days=7)
    deadlines = []
    for mail in store.recent_important_emails(since):
        if not mail["deadline_iso"] or mail["action_status"] in {"added", "declined", "expired"}:
            continue
        raw_deadline = mail["deadline_iso"]
        try:
            if len(raw_deadline) == 10:
                deadline = date.fromisoformat(raw_deadline)
                if deadline < now.date():
                    continue
                deadline_text = deadline.strftime("%d %b")
                sort_value = datetime.combine(deadline, time.min, settings.timezone)
            else:
                deadline = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00"))
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=settings.timezone)
                deadline = deadline.astimezone(settings.timezone)
                if deadline < now:
                    continue
                deadline_text = deadline.strftime("%d %b, %I:%M %p")
                sort_value = deadline
        except ValueError:
            continue
        deadlines.append((sort_value, f"{deadline_text} — {mail['subject']}"))
    deadlines.sort(key=lambda item: item[0])

    lines = [f"🗓️ Today's plan — {now.strftime('%a, %d %b')}" if lang == "en" else f"🗓️ Innaikku plan — {now.strftime('%a, %d %b')}"]
    if focus_items:
        lines.append("\nToday's focus (High → Low):" if lang == "en" else "\nஇன்றைய முக்கிய வேலைகள் (High → Low):")
        lines.extend(f"• {item[2]}" for item in focus_items[:20])
    else:
        lines.append("\nNo Calendar events or pending tasks 🙂" if lang == "en" else "\nCalendar events or pending tasks ethum illa 🙂")
    if deadlines:
        lines.append("\n📧 Upcoming email deadlines:" if lang == "en" else "\n📧 வரவிருக்கும் email deadlines:")
        lines.extend(f"• {item[1]}" for item in deadlines[:10])
    elif not focus_items:
        lines.append("No upcoming email deadlines found." if lang == "en" else "வரவிருக்கும் email deadlines எதுவும் இல்லை.")
    await update.effective_message.reply_text("\n".join(lines)[:3900])


async def find_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Find the earliest free daytime slots in the next seven days."""
    if not authorized(update, context):
        return
    calendar: GoogleCalendar = context.application.bot_data["calendar"]
    lang = response_language(context)
    args = " ".join(context.args).strip().lower()
    duration_minutes = 30
    if args:
        match = re.fullmatch(r"(\d+)\s*(minutes?|mins?|m|hours?|hrs?|h)?", args)
        if not match:
            await update.effective_message.reply_text(
                ("Use `/findtime 45` or `/findtime 1 hour` for example." if lang == "en" else "உதாரணம்: `/findtime 45` அல்லது `/findtime 1 hour`."),
                parse_mode="Markdown",
            )
            return
        duration_minutes = int(match.group(1)) * (60 if (match.group(2) or "").startswith(("h", "hr")) else 1)
    if not 15 <= duration_minutes <= 480:
        await update.effective_message.reply_text("Choose a duration from 15 minutes to 8 hours." if lang == "en" else "15 minutes முதல் 8 hours வரை duration சொல்லுங்க.")
        return

    timezone = calendar.settings.timezone
    now = datetime.now(timezone)
    horizon = now + timedelta(days=7)
    try:
        events = await asyncio.to_thread(calendar.list_events, now, horizon)
    except CalendarNotAuthorized as exc:
        await update.effective_message.reply_text(_calendar_error(exc, lang))
        return
    except Exception:
        log.exception("Free-time search failed")
        await update.effective_message.reply_text("I couldn't check Calendar availability. Please try again shortly." if lang == "en" else "Calendar free time check panna mudiyala. Konjam neram kazhichu try pannunga.")
        return

    busy = sorted(
        (max(now, event["start"].astimezone(timezone)), min(horizon, event["end"].astimezone(timezone)))
        for event in events
        if event["end"].astimezone(timezone) > now and event["start"].astimezone(timezone) < horizon
    )
    slots: list[tuple[datetime, datetime]] = []
    day = now.date()
    while day < horizon.date() or day == horizon.date():
        day_start = datetime.combine(day, time(9), timezone)
        day_end = datetime.combine(day, time(18), timezone)
        cursor = max(day_start, now)
        window_end = min(day_end, horizon)
        if cursor < window_end:
            for busy_start, busy_end in busy:
                if busy_end <= cursor or busy_start >= window_end:
                    continue
                if busy_start - cursor >= timedelta(minutes=duration_minutes):
                    slots.append((cursor, busy_start))
                cursor = max(cursor, busy_end)
                if cursor >= window_end:
                    break
            if window_end - cursor >= timedelta(minutes=duration_minutes):
                slots.append((cursor, window_end))
        day += timedelta(days=1)
    if not slots:
        await update.effective_message.reply_text(
            (f"No free {duration_minutes}-minute slots during 9 AM–6 PM in the next 7 days." if lang == "en"
             else f"அடுத்த 7 நாட்களில் 9 AM–6 PM நேரத்தில் {duration_minutes} நிமிட free slot கிடைக்கலை.")
        )
        return
    lines = [f"🕒 Free slots in the next 7 days for {duration_minutes} minutes (9 AM–6 PM):" if lang == "en" else f"🕒 அடுத்த 7 நாட்களில் {duration_minutes} நிமிட free slots (9 AM–6 PM):"]
    for slot_start, slot_end in slots[:5]:
        lines.append(f"• {slot_start.strftime('%a, %d %b')} — {slot_start.strftime('%I:%M %p')} to {slot_end.strftime('%I:%M %p')}" if lang == "en" else f"• {slot_start.strftime('%a, %d %b')} — {slot_start.strftime('%I:%M %p')} முதல் {slot_end.strftime('%I:%M %p')} வரை")
    await update.effective_message.reply_text("\n".join(lines))


async def scan_mail_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    lang = response_language(context)
    await update.effective_message.reply_text("Checking Gmail for recent mail…" if lang == "en" else "Gmail-la important mails check pannuren…")
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
    lang = response_language(context)
    row = store.get_email(message_id)
    if not row:
        if query.message:
            await query.message.reply_text("I couldn't find this email record." if lang == "en" else "இந்த mail record கிடைக்கவில்லை.")
        return

    if action == "reply":
        context.user_data["awaiting_gmail_reply_to"] = message_id
        await query.message.reply_text(
            (f"✉️ Type the reply you want to send for: {row['subject']}\nI'll show a preview; the email is sent only after you press Send." if lang == "en"
             else f"✉️ {row['subject']} mail-ku anuppa vendiya reply-ai type pannunga.\nMudhalil preview kaatturen; Send press pannina mattum mail pogum.")
        )
        return

    claim_action = "yes" if action == "force" else action
    if not store.claim_email_action(message_id, claim_action):
        current = store.get_email(message_id)
        label = (("Already added to Calendar." if lang == "en" else "Calendar-la already add panniyachu.") if current and current["action_status"] == "added"
                 else ("This choice has already been processed." if lang == "en" else "இந்த choice ஏற்கனவே process ஆயிடுச்சு."))
        if query.message:
            await query.message.reply_text(label)
        return

    if action == "no":
        await query.edit_message_text(
            (query.message.text or "").replace("Add this to Calendar?", "Not added to Calendar.").replace("Calendar-la add pannattuma?", "Calendar-la add pannala."),
            reply_markup=None,
        )
        return

    calendar: GoogleCalendar = context.application.bot_data["calendar"]
    if action != "force":
        try:
            conflicts = await _event_conflicts(calendar, row["deadline_iso"])
        except Exception:
            log.exception("Calendar conflict check failed for email %s", message_id)
            store.finish_email_action(message_id, "pending")
            await query.edit_message_text(
                (query.message.text or "") + ("\n\nI couldn't check for conflicts, so the event was not added. Please retry." if lang == "en"
                                                else "\n\nCalendar conflict check panna mudiyala; event add pannala. Retry pannunga.")
            )
            return
        if conflicts:
            store.finish_email_action(message_id, "pending")
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Add anyway" if lang == "en" else "எப்படியும் சேர்", callback_data=f"mail:force:{message_id}"),
                InlineKeyboardButton("Cancel" if lang == "en" else "ரத்து", callback_data=f"mail:no:{message_id}"),
            ]])
            await query.edit_message_text(_conflict_message(conflicts, response_language(context)), reply_markup=markup)
            return
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
        result = (query.message.text or "") + ("\n\n✅ Added to Calendar." if lang == "en" else "\n\n✅ Calendar-la add panniten.")
        if event.get("htmlLink"):
            result += f"\n{event['htmlLink']}"
        await query.edit_message_text(result[:3900], reply_markup=None, disable_web_page_preview=True)
    except ValueError:
        store.finish_email_action(message_id, "expired")
        await query.edit_message_text(
            (query.message.text or "") + ("\n\n⌛ This date/time has passed, so the event was not added." if lang == "en"
                                           else "\n\n⌛ Date/time kadandhadhunaala Calendar-la add pannala."),
            reply_markup=None,
        )
    except CalendarGuardError as exc:
        store.finish_email_action(message_id, "pending")
        retry_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Try again" if lang == "en" else "மீண்டும் முயற்சி", callback_data=f"mail:yes:{message_id}"),
            InlineKeyboardButton("No" if lang == "en" else "வேண்டாம்", callback_data=f"mail:no:{message_id}"),
        ]])
        await query.edit_message_text(
            (query.message.text or "") + (f"\n\n🛡️ Safety check did not pass: {str(exc)[:220]}\nEvent not added." if lang == "en"
                                           else f"\n\n🛡️ Strands/Decider check pass aagala: {str(exc)[:220]}\nCalendar-la add pannala."),
            reply_markup=retry_markup,
        )
    except Exception:
        store.finish_email_action(message_id, "pending")
        retry_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes" if lang == "en" else "ஆம்", callback_data=f"mail:yes:{message_id}"),
            InlineKeyboardButton("No" if lang == "en" else "வேண்டாம்", callback_data=f"mail:no:{message_id}"),
        ]])
        await query.edit_message_reply_markup(reply_markup=retry_markup)


def _analysis_record(source: str, result, capture_id: str, direct: bool) -> dict:
    return {"capture_id": capture_id, "source_text": source, "title": result.title,
            "event_date": result.event_date, "event_time": result.event_time,
            "priority": result.priority, "direct_requested": direct,
            "status": "awaiting_date" if not result.event_date else "awaiting_time" if not result.event_time else "confirm"}


async def _event_conflicts(calendar: GoogleCalendar, start_value: str) -> list[dict]:
    """Return existing events that overlap the proposed one-hour or all-day event."""
    timezone = calendar.settings.timezone
    if len(start_value.strip()) == 10:
        start = datetime.combine(date.fromisoformat(start_value), time.min, timezone)
        end = start + timedelta(days=1)
    else:
        start = datetime.fromisoformat(start_value.replace("Z", "+00:00"))
        start = start.replace(tzinfo=timezone) if start.tzinfo is None else start.astimezone(timezone)
        end = start + timedelta(hours=1)
    events = await asyncio.to_thread(calendar.list_events, start, end)
    return [
        event for event in events
        if event["start"].astimezone(timezone) < end and event["end"].astimezone(timezone) > start
    ]


def _conflict_message(events: list[dict], lang: str = "ta") -> str:
    lines = ["⚠️ A Calendar event already overlaps this time:" if lang == "en" else "⚠️ Indha time-la already Calendar event irukku:"]
    for event in events[:5]:
        start_at = event["start"]
        end_at = event["end"]
        if event.get("all_day"):
            when = start_at.strftime("%d %b") + (" — all day" if lang == "en" else " — முழு நாள்")
        else:
            when = f"{start_at.strftime('%d %b, %I:%M %p')}–{end_at.strftime('%I:%M %p')}"
        lines.append(f"• {when}: {event['title']}")
    lines.append("\nPress Add anyway to continue, or Cancel to stop." if lang == "en" else "\nபுதிய event add aagala. Add anyway panna button press pannunga.")
    return "\n".join(lines)


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
        lang = response_language(context, text)
        result = await asyncio.to_thread(extractor.analyze, source, datetime.now(settings.timezone), lang)
    except ValueError as exc:
        error = str(exc)
        if lang == "ta":
            error = {"That date has passed. Please send a future date.": "அந்த தேதி கடந்துவிட்டது. எதிர்கால தேதியை அனுப்புங்கள்.",
                     "Could not understand the time. Please send it like 6:30 PM.": "நேரம் புரியவில்லை. 6:30 PM மாதிரி அனுப்புங்கள்."}.get(error, "Task விவரம் புரியவில்லை. தேதி மற்றும் நேரத்தைத் தெளிவாக அனுப்புங்கள்.")
        await update.effective_message.reply_text(error)
        return
    except Exception:
        lang = response_language(context, text)
        await update.effective_message.reply_text(
            "I couldn't understand that task. Please rephrase it; no event was created." if lang == "en" else "Task-a purinjukka mudiyala. Konjam clear-ah anuppunga; event create pannala."
        )
        return
    record = _analysis_record(source, result, capture_id, bool(waiting["direct_requested"]) if waiting else direct)
    store.save_capture(record)
    if not result.event_date:
        await update.effective_message.reply_text("What date and time should I use?" if lang == "en" else "எந்த நாள், என்ன நேரத்துக்கு வைக்கணும்?")
        return
    if not result.event_time:
        await update.effective_message.reply_text("What time should I add to Calendar?" if lang == "en" else "என்ன நேரத்துக்கு Calendar-la add பண்ணட்டும்?")
        return
    local_start = datetime.combine(date.fromisoformat(result.event_date), time.fromisoformat(result.event_time), settings.timezone)
    if local_start <= datetime.now(settings.timezone):
        store.finish_capture(capture_id, "awaiting_time")
        await update.effective_message.reply_text("That time has passed. Please choose a future time." if lang == "en" else "இந்த நேரம் கடந்துவிட்டது. வேறொரு future time சொல்லுங்க.")
        return
    if record["direct_requested"]:
        await _create_capture_event(update, context, capture_id)
        return
    date_text = local_start.strftime("%d %b %Y, %I:%M %p")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Yes, add" if lang == "en" else "சேர்", callback_data=f"capture:yes:{capture_id}"),
                                      InlineKeyboardButton("No" if lang == "en" else "வேண்டாம்", callback_data=f"capture:no:{capture_id}")]])
    prompt = (f"📌 Add this to Calendar?\n{result.title}\n{date_text} IST\nPriority: {result.priority}" if lang == "en"
              else f"📌 Calendar-la add pannattuma?\n{result.title}\n{date_text} IST\nPriority: {result.priority}")
    await update.effective_message.reply_text(prompt, reply_markup=keyboard)


async def capture_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    text = message.text or ""
    if not authorized(update, context):
        return
    lang = response_language(context, text)
    reply_to = context.user_data.get("awaiting_gmail_reply_to")
    if reply_to:
        if not text.strip() or len(text.strip()) > 3000:
            await message.reply_text("Reply must be 1–3000 characters. Please shorten it." if lang == "en" else "Reply 1–3000 characters irukkanum. Short-a type pannunga.")
            return
        context.user_data.pop("awaiting_gmail_reply_to", None)
        context.user_data["pending_gmail_reply"] = {"message_id": reply_to, "body": text.strip()}
        row = context.application.bot_data["store"].get_email(reply_to)
        subject = row["subject"] if row else "(No subject)"
        lang = response_language(context, text)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Send reply" if lang == "en" else "பதிலை அனுப்பு", callback_data="mailreply:send"),
            InlineKeyboardButton("Cancel" if lang == "en" else "ரத்து", callback_data="mailreply:cancel"),
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
    lang = response_language(context)
    if (query.from_user.id != settings.telegram_user_id or not query.message
            or query.message.chat.type != "private"):
        return
    action = query.data.split(":", maxsplit=1)[1]
    draft = context.user_data.get("pending_gmail_reply")
    if not draft:
        await query.message.reply_text("This reply draft expired. Press Reply on the Gmail message and type it again." if lang == "en" else "Reply draft expire aagiduchu; Gmail mail-la Reply button press panni marubadi type pannunga.")
        return
    if action == "cancel":
        context.user_data.pop("pending_gmail_reply", None)
        await query.edit_message_text((query.message.text or "") + ("\n\nReply cancelled." if lang == "en" else "\n\nReply cancel panniten."), reply_markup=None)
        return

    if action == "override" and draft.get("policy_status") not in {"review", "unavailable"}:
        await query.message.reply_text("Send anyway isn't available for this draft. Please review the reply again." if lang == "en" else "Indha draft-ku Send anyway option available illa. Reply-ai marubadi review pannunga.")
        return

    if action == "send" and not draft.get("policy_status"):
        from assistant.reply_policy import review_gmail_reply

        await query.edit_message_text((query.message.text or "") + ("\n\nChecking reply safety…" if lang == "en" else "\n\nReply safety check pannuren…"), reply_markup=None)
        try:
            verdict = await asyncio.to_thread(review_gmail_reply, settings, draft["body"])
        except Exception:
            log.exception("Gmail reply policy check failed")
            draft["policy_status"] = "unavailable"
            context.user_data["pending_gmail_reply"] = draft
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Send anyway" if lang == "en" else "எப்படியும் அனுப்பு", callback_data="mailreply:override"),
                InlineKeyboardButton("Cancel" if lang == "en" else "ரத்து", callback_data="mailreply:cancel"),
            ]])
            await query.message.reply_text(
                ("The safety check is unavailable, so the reply has not been sent. Review the draft, then choose Send anyway or Cancel." if lang == "en"
                 else "Safety check ippo mudiyala; Gmail-ku reply innum pogala. Neenga draft-ai review pannitu Send anyway press pannalaam, illa Cancel."), reply_markup=keyboard
            )
            return

        if verdict.action == "block":
            context.user_data["pending_gmail_reply"] = draft
            await query.message.reply_text(
                (f"⚠️ The reply safety check blocked this ({verdict.category}); the email was not sent. Edit the draft or cancel." if lang == "en"
                 else f"⚠️ Reply safety check block panniduchu ({verdict.category}); mail send aagala. Reply button-ai marubadi press panni draft-ai maathi anuppunga, illa Cancel pannunga.")
            )
            return
        if verdict.action == "review":
            draft["policy_status"] = "review"
            context.user_data["pending_gmail_reply"] = draft
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Send anyway" if lang == "en" else "எப்படியும் அனுப்பு", callback_data="mailreply:override"),
                InlineKeyboardButton("Cancel" if lang == "en" else "ரத்து", callback_data="mailreply:cancel"),
            ]])
            await query.message.reply_text(
                (f"⚠️ This reply needs review ({verdict.category}, {verdict.severity}). Check the draft, then choose Send anyway if intended." if lang == "en"
                 else f"⚠️ Reply safety review thevai ({verdict.category}, {verdict.severity}). Draft-ai check pannitu, intentional-na Send anyway press pannunga."), reply_markup=keyboard
            )
            return
        draft["policy_status"] = "allowed"
        context.user_data["pending_gmail_reply"] = draft

    if action == "override":
        draft["policy_status"] = "overridden"
        context.user_data["pending_gmail_reply"] = draft

    from assistant.gmail import GmailReader

    reader: GmailReader = context.application.bot_data["gmail_reader"]
    await query.edit_message_text((query.message.text or "") + ("\n\nSending…" if lang == "en" else "\n\nAnuppuren…"), reply_markup=None)
    try:
        result = await asyncio.to_thread(reader.send_reply, draft["message_id"], draft["body"])
    except Exception:
        log.exception("Gmail reply send failed for message %s", draft["message_id"])
        # Keep the exact draft. A transport timeout may mean Gmail accepted it;
        # the user should check Sent before trying the Send button again.
        context.user_data["pending_gmail_reply"] = draft
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Retry send (check Sent first)" if lang == "en" else "Sent check செய்து மீண்டும் அனுப்பு", callback_data="mailreply:send"),
            InlineKeyboardButton("Cancel" if lang == "en" else "ரத்து", callback_data="mailreply:cancel"),
        ]])
        await query.message.reply_text(
            ("I couldn't confirm whether the reply was sent. Check Gmail Sent first; retry only if it isn't there." if lang == "en"
             else "Reply send result confirm aagala. Duplicate avoid panna Sent folder check pannunga; send aagala-na mattum Retry press pannunga."), reply_markup=keyboard
        )
        return
    context.user_data.pop("pending_gmail_reply", None)
    await query.message.reply_text(("✅ Gmail reply sent." if lang == "en" else "✅ Gmail reply anuppitten.") + (f"\nThread: {result.get('threadId')}" if result.get("threadId") else ""))


async def save_note_text(update: Update, context: ContextTypes.DEFAULT_TYPE, content: str) -> None:
    if not authorized(update, context):
        return
    lang = response_language(context, content)
    notion: NotionNotes | None = context.application.bot_data.get("notion_notes")
    if not notion:
        await update.effective_message.reply_text("Connect Notion by setting NOTION_API_TOKEN and NOTION_DATA_SOURCE_ID in .env." if lang == "en" else "Notion connect panna .env-la NOTION_API_TOKEN, NOTION_DATA_SOURCE_ID set pannunga.")
        return
    try:
        page = await asyncio.to_thread(notion.save_note, content)
        suffix = f"\n{page.get('url')}" if page.get("url") else ""
        await update.effective_message.reply_text(("📝 Saved the note to Notion." if lang == "en" else "📝 Notion-la note save panniten.") + suffix, disable_web_page_preview=True)
    except Exception as exc:
        await update.effective_message.reply_text((f"I couldn't save the note to Notion: {str(exc)[:250]}" if lang == "en"
                                                   else f"Notion-la save aagala: {str(exc)[:250]}"))


async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_note_text(update, context, " ".join(context.args))


async def search_note_text(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str) -> None:
    if not authorized(update, context):
        return
    lang = response_language(context, question)
    notion: NotionNotes | None = context.application.bot_data.get("notion_notes")
    if not notion:
        await update.effective_message.reply_text("Connect Notion by setting NOTION_API_TOKEN and NOTION_DATA_SOURCE_ID in .env." if lang == "en" else "Notion connect panna .env-la NOTION_API_TOKEN, NOTION_DATA_SOURCE_ID set pannunga.")
        return
    try:
        list_all = not question.strip()
        notes = await asyncio.to_thread(notion.list_notes if list_all else notion.search_notes,
                                        *([] if list_all else [question]))
        if not notes:
            await update.effective_message.reply_text(
                (("There are no notes in the Notion database yet 🙂" if list_all else "I couldn't find a Notion note for that topic.") if lang == "en"
                 else ("Notion database-la notes innum illa 🙂" if list_all else "Andha topic-ku Notion note kidaikkala."))
            )
            return
        heading = ((f"📚 Notion database notes ({len(notes)}):" if list_all else f"🔎 Matching Notion notes ({len(notes)}):") if lang == "en"
                   else (f"📚 Notion தரவுத்தளக் குறிப்புகள் ({len(notes)}):" if list_all else f"🔎 பொருந்தும் குறிப்புகள் ({len(notes)}):"))
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
        await update.effective_message.reply_text((f"I couldn't search Notion: {str(exc)[:250]}" if lang == "en"
                                                   else f"Notion search panna mudiyala: {str(exc)[:250]}"))


async def search_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await search_note_text(update, context, " ".join(context.args))


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    rows = context.application.bot_data["store"].pending_captures()
    lang = response_language(context)
    if not rows:
        await update.effective_message.reply_text("There are no pending tasks 🙂" if lang == "en" else "Pending captured tasks ethum illa 🙂")
        return
    for row in rows[:12]:
        label = f"{row['title']} — {row['event_date'] or ('date missing' if lang == 'en' else 'தேதி இல்லை')} {row['event_time'] or ''} [{row['priority']}]"
        if row["status"] == "created":
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("Done ✅" if lang == "en" else "முடிந்தது ✅", callback_data=f"task:done:{row['capture_id']}")]])
        elif row["status"] == "confirm":
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("Retry Calendar add" if lang == "en" else "Calendar-ல் மீண்டும் சேர்", callback_data=f"capture:yes:{row['capture_id']}"),
                                            InlineKeyboardButton("Cancel" if lang == "en" else "ரத்து", callback_data=f"capture:no:{row['capture_id']}")]])
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
    lang = response_language(context)
    _, _, capture_id = query.data.split(":", 2)
    if context.application.bot_data["store"].mark_capture_done(capture_id):
        await query.edit_message_text((query.message.text or "") + "\n✅ Completed", reply_markup=None)
    else:
        if query.message:
            await query.message.reply_text("This task is already complete or unavailable." if lang == "en" else "Task ஏற்கனவே முடிந்துவிட்டது அல்லது கிடைக்கவில்லை.")


async def reminder_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _ack_callback(query)
    if not authorized(update, context):
        return
    _, action_name, token = query.data.split(":", 2)
    store = context.application.bot_data["store"]
    lang = response_language(context)
    action = store.claim_reminder_action(token, action_name)
    if not action:
        if query.message:
            await query.message.reply_text("This reminder was already processed." if lang == "en" else "இந்த reminder ஏற்கனவே process ஆயிடுச்சு.")
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
            await query.message.reply_text("There isn't enough time before the event to snooze." if lang == "en" else "Event-ku munnaadi snooze time illa; reminder set pannala.")
        return
    store.schedule_snooze(action, due)
    await query.edit_message_text((query.message.text or "") + (f"\n🔔 {('1 hour' if action_name == 's1' else 'Tomorrow')} snooze set." if lang == "en"
                                                       else f"\n🔔 {('1 hour' if action_name == 's1' else 'Tomorrow')} snooze set panniten."), reply_markup=None)


async def _create_capture_event(update: Update, context: ContextTypes.DEFAULT_TYPE, capture_id: str, *, force_conflicts: bool = False) -> None:
    store = context.application.bot_data["store"]
    row = store.get_capture(capture_id)
    lang = response_language(context)
    if not row:
        await update.effective_message.reply_text("I couldn't find this task." if lang == "en" else "இந்த task கிடைக்கவில்லை.")
        return
    calendar: GoogleCalendar = context.application.bot_data["calendar"]
    settings = context.application.bot_data["settings"]
    start = datetime.combine(date.fromisoformat(row["event_date"]), time.fromisoformat(row["event_time"]), settings.timezone)
    if not force_conflicts:
        try:
            conflicts = await _event_conflicts(calendar, start.isoformat())
        except Exception:
            log.exception("Calendar conflict check failed for capture %s", capture_id)
            await update.effective_message.reply_text("I couldn't check for Calendar conflicts, so the event was not added. Please retry shortly." if lang == "en" else "Calendar conflict check panna mudiyala; event add pannala. Konjam neram kazhichu retry pannunga.")
            return
        if conflicts:
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Add anyway" if lang == "en" else "எப்படியும் சேர்", callback_data=f"capture:force:{capture_id}"),
                InlineKeyboardButton("Cancel" if lang == "en" else "ரத்து", callback_data=f"capture:no:{capture_id}"),
            ]])
            await update.effective_message.reply_text(_conflict_message(conflicts, response_language(context)), reply_markup=markup)
            return
    if not store.claim_capture(capture_id):
        await update.effective_message.reply_text("This task was already processed." if lang == "en" else "இந்த task ஏற்கனவே process ஆயிடுச்சு.")
        return
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
            InlineKeyboardButton("Try again" if lang == "en" else "மீண்டும் முயற்சி", callback_data=f"capture:yes:{capture_id}"),
            InlineKeyboardButton("No" if lang == "en" else "வேண்டாம்", callback_data=f"capture:no:{capture_id}"),
        ]])
        await update.effective_message.reply_text(
            ("The event wasn't added. There may be a safety check or Google Calendar issue. Try again; it will be added only if the safety check passes." if lang == "en"
             else "Calendar-la add aagala. Strands/Decider check or Google Calendar issue irukkalaam. Try again press pannunga; safety check pass aana mattum add aagum."),
            reply_markup=markup,
        )
        return

    # Once Google Calendar confirms the write, persist success before sending Telegram output.
    # A Telegram network error must not turn a created event back into a retryable task.
    store.finish_capture(capture_id, "created", event.get("id"))
    store.reconcile_reminders([{"id": event["id"], "title": row["title"], "start": start, "priority": row["priority"]}], datetime.now(settings.timezone))
    try:
        lang = response_language(context)
        confirmation = (f"✅ Added to Calendar: {row['title']}\n{start.strftime('%d %b, %I:%M %p')} IST\nPriority: {row['priority']}" if lang == "en"
                       else f"✅ Calendar-la add panniten: {row['title']}\n{start.strftime('%d %b, %I:%M %p')} IST\nPriority: {row['priority']}")
        await update.effective_message.reply_text(confirmation)
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
    lang = response_language(context)
    if not row or row["status"] != "confirm":
        if query.message:
            await query.message.reply_text("This task was already processed." if lang == "en" else "இந்த task ஏற்கனவே process ஆயிடுச்சு.")
        return
    if action == "no":
        store.finish_capture(capture_id, "cancelled")
        try:
            await query.edit_message_text((query.message.text or "") + ("\n\n❌ Not added." if lang == "en" else "\n\n❌ Add pannala."), reply_markup=None)
        except Exception:
            log.exception("Could not update Telegram after cancelling capture %s", capture_id)
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        log.exception("Could not hide Calendar retry buttons for capture %s", capture_id)
    await _create_capture_event(update, context, capture_id, force_conflicts=(action == "force"))


async def cancel_capture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    lang = response_language(context)
    if context.user_data.pop("awaiting_calendar_event", False):
        await update.effective_message.reply_text("Calendar event flow cancelled." if lang == "en" else "சரி, Calendar event add flow cancel பண்ணிட்டேன்.")
        return
    row = context.application.bot_data["store"].latest_waiting_capture()
    if row:
        context.application.bot_data["store"].finish_capture(row["capture_id"], "cancelled")
        await update.effective_message.reply_text("Pending task cancelled." if lang == "en" else "சரி, அந்த pending capture cancel பண்ணிட்டேன்.")
    else:
        await update.effective_message.reply_text("There is no pending task to cancel." if lang == "en" else "Cancel செய்ய pending capture இல்லை.")


async def voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update, context):
        return
    message = update.effective_message
    voice = message.voice
    settings = context.application.bot_data["settings"]
    extractor: StrandsCapture = context.application.bot_data["capture_ai"]
    lang = response_language(context)
    if (voice.file_size or 0) > settings.voice_max_bytes or voice.duration > settings.voice_max_seconds:
        await message.reply_text("Voice note is too long. Please send one under 5 minutes." if lang == "en" else "Voice note romba perusa/long-ah irukku. 5 minutes-kulla short note anuppunga.")
        return
    status_msg = await message.reply_text("Listening to your voice note…" if lang == "en" else "Voice note-a ketkiren…")
    try:
        telegram_file = await context.bot.get_file(voice.file_id, read_timeout=20, write_timeout=20, connect_timeout=10)
        from io import BytesIO
        audio = BytesIO()
        await telegram_file.download_to_memory(audio, read_timeout=30, write_timeout=30, connect_timeout=10)
        transcript = await asyncio.to_thread(extractor.transcribe, audio.getvalue())
        lang = response_language(context, transcript)
        await status_msg.edit_text("Got it; checking the task details…" if lang == "en" else "Purinjukitten; task details check pannuren…")
        await _capture_text(update, context, transcript)
    except Exception as exc:
        log.exception("Voice note download or transcription failed")
        if not settings.gemini_api_key:
            await status_msg.edit_text("Voice transcription needs a Gemini API key. You can send a text message instead." if lang == "en" else "Voice transcription-ku Gemini API key thevai. Ippo text message anuppalaam; Strands text workflow work aagum.")
        elif isinstance(exc, ValueError) and "No speech was recognized" in str(exc):
            await status_msg.edit_text("I couldn't hear clear speech. Please try a clearer voice note or send text." if lang == "en" else "Audio-la clear speech detect aagala. Konjam clear-ah pesi, voice note marubadi anuppunga; illaina text-ah anuppalaam.")
        elif getattr(exc, "code", None) in {429, 500, 502, 503, 504}:
            await status_msg.edit_text("The Gemini service is busy. Please try the voice note again shortly." if lang == "en" else "Gemini service ippo busy-ah irukku. Konjam neram kazhichu voice note marubadi anuppunga.")
        else:
            await status_msg.edit_text("I couldn't transcribe the voice note. Please try sending text." if lang == "en" else "Voice note transcribe panna mudiyala. Text-ah anuppi try pannunga.")
