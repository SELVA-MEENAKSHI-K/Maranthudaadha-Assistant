"""Calendar synchronization, pre-event notifications, and the 7 AM daily brief."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram.ext import Application, ContextTypes
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from assistant.google_calendar import CalendarNotAuthorized, GoogleCalendar
from assistant.gmail import scan_gmail
from assistant.store import Store

log = logging.getLogger(__name__)


def reminder_keyboard(store: Store, row) -> InlineKeyboardMarkup:
    token = store.create_reminder_action(row)
    return InlineKeyboardMarkup([[InlineKeyboardButton("Snooze 1h", callback_data=f"rem:s1:{token}"),
                                  InlineKeyboardButton("Tomorrow", callback_data=f"rem:tom:{token}"),
                                  InlineKeyboardButton("Done", callback_data=f"rem:done:{token}")]])


async def sync_calendar(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    calendar: GoogleCalendar = context.application.bot_data["calendar"]
    store: Store = context.application.bot_data["store"]
    now = datetime.now(settings.timezone)
    try:
        events = await asyncio.to_thread(
            calendar.list_events, now, now + timedelta(days=settings.calendar_lookahead_days)
        )
        store.reconcile_reminders(events, now)
        log.info("Calendar sync complete: %d upcoming events", len(events))
    except CalendarNotAuthorized as exc:
        log.warning("Calendar not connected: %s", exc)
    except Exception:
        log.exception("Calendar sync failed")


async def send_due_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    store: Store = context.application.bot_data["store"]
    now = datetime.now(settings.timezone)
    for row in store.due_reminders(now):
        event_start = datetime.fromisoformat(row["event_start"]).astimezone(settings.timezone)
        when = event_start.strftime("%a, %d %b at %I:%M %p")
        lead_text = {1440: "1 நாள்", 720: "12 மணி நேரம்", 180: "3 மணி நேரம்",
                     60: "1 மணி நேரம்", 30: "30 நிமிடம்", 15: "15 நிமிடம்"}[row["lead_minutes"]]
        try:
            priority_label = f"[{row['priority'].upper()}] "
            await context.bot.send_message(
                chat_id=settings.telegram_user_id,
                text=f"⏰ இன்னும் {lead_text}ல் event\n{priority_label}{row['event_title']}\n{when}",
                reply_markup=reminder_keyboard(store, row),
            )
            store.mark_sent(row["event_id"], row["lead_minutes"], now)
        except Exception:
            log.exception("Could not send reminder for event %s", row["event_id"])
    for row in store.due_snoozes(now):
        try:
            action_row = {"event_id": row["event_id"], "event_title": row["event_title"],
                          "event_start": row["event_start"], "priority": row["priority"]}
            await context.bot.send_message(chat_id=settings.telegram_user_id,
                text=f"🔔 Snoozed reminder\n[{row['priority'].upper()}] {row['event_title']}\n"
                     f"{datetime.fromisoformat(row['event_start']).astimezone(settings.timezone).strftime('%a, %d %b at %I:%M %p')}",
                reply_markup=reminder_keyboard(store, action_row))
            store.mark_snooze_sent(row["snooze_id"])
        except Exception:
            log.exception("Could not send snoozed reminder for event %s", row["event_id"])


async def morning_brief(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    calendar: GoogleCalendar = context.application.bot_data["calendar"]
    now = datetime.now(settings.timezone)
    try:
        events = await asyncio.to_thread(calendar.today_events, now)
        recent_mail = context.application.bot_data["store"].recent_important_emails(now - timedelta(days=1))
        if events or recent_mail:
            lines = ["🌅 காலை வணக்கம்!"]
            if events:
                lines.append("இன்றைய Calendar:")
            for event in events:
                time_text = "முழு நாள்" if event.get("all_day") else event["start"].astimezone(settings.timezone).strftime("%I:%M %p")
                lines.append(f"• {time_text} — {'[HIGH] ' if event.get('priority') == 'High' else ''}{event['title']}")
            if recent_mail:
                lines.append("\nமுக்கியமான recent emails:")
                for mail in recent_mail:
                    lines.append(f"• [{mail['category']}] {mail['subject']}: {mail['summary']}")
            message = "\n".join(lines)
        else:
            message = "🌅 காலை வணக்கம்! இன்று Calendar events அல்லது முக்கிய email இல்லை. நல்ல நாளாகட்டும் 🙂"
        await context.bot.send_message(chat_id=settings.telegram_user_id, text=message)
    except CalendarNotAuthorized:
        log.warning("Skipping morning brief because Google Calendar is not connected")
    except Exception:
        log.exception("Morning brief failed")


async def weekly_review(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    store: Store = context.application.bot_data["store"]
    calendar: GoogleCalendar = context.application.bot_data["calendar"]
    now = datetime.now(settings.timezone)
    completed, pending = store.weekly_tasks(now - timedelta(days=7), now)
    next_monday = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=(7 - now.weekday()) % 7 or 7)
    next_week_end = next_monday + timedelta(days=7)
    try:
        next_events = await asyncio.to_thread(calendar.list_events, next_monday, next_week_end)
    except Exception:
        log.exception("Weekly review could not fetch next week's events")
        next_events = []
    lines = ["🗓️ Weekly review"]
    lines.append(f"\n✅ Completed this week ({len(completed)}):")
    lines.extend(f"• {row['title']}" for row in completed[:12])
    if not completed:
        lines.append("• Completed tasks were not marked this week.")
    lines.append(f"\n📌 Pending ({len(pending)}):")
    lines.extend(f"• {row['title']} — {row['event_date'] or 'date missing'} {row['event_time'] or ''} [{row['priority']}]" for row in pending[:15])
    if not pending:
        lines.append("• No pending captured tasks.")
    lines.append("\n📅 Next week:")
    lines.extend(f"• {event['start'].astimezone(settings.timezone).strftime('%a %d %b %I:%M %p') if not event.get('all_day') else event['start'].strftime('%a %d %b')} — {event['title']}" for event in next_events[:15])
    if not next_events:
        lines.append("• No Calendar events found.")
    await context.bot.send_message(chat_id=settings.telegram_user_id, text="\n".join(lines)[:3900])


def register_jobs(app: Application) -> None:
    settings = app.bot_data["settings"]
    queue = app.job_queue
    if queue is None:
        raise RuntimeError('Install python-telegram-bot with the "job-queue" extra')
    queue.run_repeating(sync_calendar, interval=settings.calendar_sync_minutes * 60, first=2, name="calendar-sync")
    queue.run_repeating(send_due_reminders, interval=settings.reminder_check_seconds, first=10, name="due-reminders")
    queue.run_repeating(scan_gmail, interval=settings.gmail_scan_minutes * 60,
                        first=60, name="gmail-scan")
    brief_time = datetime.strptime("07:00", "%H:%M").time().replace(tzinfo=ZoneInfo(settings.timezone_name))
    queue.run_daily(
        morning_brief,
        time=brief_time,
        days=(0, 1, 2, 3, 4, 5, 6),
        name="morning-brief",
    )
    review_time = datetime.strptime("20:00", "%H:%M").time().replace(tzinfo=ZoneInfo(settings.timezone_name))
    queue.run_daily(weekly_review, time=review_time, days=(6,), name="weekly-review")
