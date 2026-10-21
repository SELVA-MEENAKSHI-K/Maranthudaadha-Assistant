"""FastAPI webhook adapter for Render. Local development can use Telegram polling."""
from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request

from assistant.config import Settings
from telegram import Update
from telegram.ext import Application


def create_web_app(application: Application) -> FastAPI:
    settings: Settings = application.bot_data["settings"]
    webhook_path = "/telegram/webhook"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await application.initialize()
        await application.start()
        await application.bot.set_webhook(
            url=f"{settings.webhook_base_url}{webhook_path}",
            secret_token=settings.webhook_secret,
            allowed_updates=["message", "callback_query"],
        )
        try:
            yield
        finally:
            await application.bot.delete_webhook(drop_pending_updates=False)
            await application.stop()
            await application.shutdown()

    web_app = FastAPI(title="Maranthudaadha Assistant", lifespan=lifespan)

    @web_app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @web_app.post(webhook_path)
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> dict[str, bool]:
        if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
            x_telegram_bot_api_secret_token, settings.webhook_secret
        ):
            raise HTTPException(status_code=403, detail="Invalid webhook secret")
        payload = await request.json()
        update = Update.de_json(payload, application.bot)
        await application.update_queue.put(update)
        return {"ok": True}

    return web_app
