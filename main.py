"""Start the private Telegram assistant (Phases 1–3)."""
from __future__ import annotations

import logging

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from assistant.config import load_settings
from assistant.email_intelligence import StrandsEmailTriage
from assistant.gmail import GmailReader
from assistant.google_calendar import GoogleCalendar
from assistant.handlers import (add_event_command, cancel_capture, capture_callback, capture_message, gmail_callback, gmail_reply_callback,
                                list_tasks, note_command, notion_status, reminder_action_callback, scan_mail_now,
                                search_notes_command, start, status, task_done_callback, today, unknown_command, voice_message)
from assistant.capture import StrandsCapture
from assistant.jobs import register_jobs
from assistant.store import Store
from assistant.notion_notes import NotionNotes


async def setup_command_menu(application: Application) -> None:
    """Keep bot commands available from Telegram's chat menu across sessions."""
    await application.bot.set_my_commands([
        BotCommand("start", "Open assistant and quick actions"),
        BotCommand("help", "Show commands and how to use them"),
        BotCommand("add", "Add an event to Google Calendar"),
        BotCommand("today", "Show today's Calendar events"),
        BotCommand("tasks", "Show pending captured tasks"),
        BotCommand("scanmail", "Check important Gmail messages"),
        BotCommand("status", "Check Google connection"),
        BotCommand("searchnotes", "List all notes or search by words"),
        BotCommand("notion", "Check or enable Notion notes"),
        BotCommand("cancel", "Cancel pending task capture"),
    ])


def build_application() -> Application:
    settings = load_settings()
    store = Store(settings.database_path)
    store.initialize()
    if settings.google_token_encrypted and not store.get_token():
        store.save_token(settings.google_token_encrypted.encode("ascii"))
    app = Application.builder().token(settings.telegram_bot_token).post_init(setup_command_menu).build()
    calendar = GoogleCalendar(settings, store)
    triage = StrandsEmailTriage(settings)
    capture_ai = StrandsCapture(settings)
    notion_notes = (NotionNotes(settings.notion_api_token, settings.notion_data_source_id)
                    if settings.notion_api_token and settings.notion_data_source_id else None)
    app.bot_data.update(
        settings=settings,
        store=store,
        calendar=calendar,
        gmail_reader=GmailReader(calendar),
        email_triage=triage,
        capture_ai=capture_ai,
        notion_notes=notion_notes,
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("add", add_event_command))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("scanmail", scan_mail_now))
    app.add_handler(CommandHandler("cancel", cancel_capture))
    app.add_handler(CommandHandler("note", note_command))
    app.add_handler(CommandHandler("searchnotes", search_notes_command))
    app.add_handler(CommandHandler("notion", notion_status))
    app.add_handler(CommandHandler("tasks", list_tasks))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_handler(CallbackQueryHandler(gmail_callback, pattern=r"^mail:(yes|no|reply):[A-Za-z0-9_-]{1,45}$"))
    app.add_handler(CallbackQueryHandler(gmail_reply_callback, pattern=r"^mailreply:(send|override|cancel)$"))
    app.add_handler(CallbackQueryHandler(capture_callback, pattern=r"^capture:(yes|no):[0-9a-f]{32}$"))
    app.add_handler(CallbackQueryHandler(reminder_action_callback, pattern=r"^rem:(s1|tom|done):[0-9a-f]{32}$"))
    app.add_handler(CallbackQueryHandler(task_done_callback, pattern=r"^task:done:[0-9a-f]{32}$"))
    app.add_handler(MessageHandler(filters.VOICE, voice_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture_message))
    register_jobs(app)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    application = build_application()
    settings = application.bot_data["settings"]
    if settings.webhook_base_url:
        if not settings.webhook_secret:
            raise RuntimeError("WEBHOOK_SECRET is required when WEBHOOK_BASE_URL is set")
        import os
        import uvicorn

        from assistant.web import create_web_app

        uvicorn.run(create_web_app(application), host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
    else:
        import asyncio

        asyncio.set_event_loop(asyncio.new_event_loop())
        application.run_polling(allowed_updates=["message", "callback_query"])
