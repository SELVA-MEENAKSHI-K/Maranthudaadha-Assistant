"""Environment-based configuration. Secrets are never hard-coded."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_user_id: int
    google_client_config: dict
    token_encryption_key: str
    google_token_encrypted: str
    database_path: Path
    timezone_name: str
    calendar_id: str
    calendar_sync_minutes: int
    calendar_lookahead_days: int
    reminder_check_seconds: int
    webhook_base_url: str
    webhook_secret: str
    gemini_api_key: str
    gemini_model: str
    gmail_scan_minutes: int
    gmail_scan_max_results: int
    voice_max_bytes: int
    voice_max_seconds: int
    notion_api_token: str
    notion_data_source_id: str
    strands_api_key: str
    strands_base_url: str
    strands_model: str
    decider_api_key: str
    decider_url: str

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)


def load_settings() -> Settings:
    """Load and validate required environment variables at startup."""
    try:
        user_id = int(os.environ["TELEGRAM_USER_ID"])
        client_config = json.loads(os.environ["GOOGLE_CLIENT_CONFIG_JSON"])
        tz = os.getenv("TIMEZONE", "Asia/Kolkata")
        ZoneInfo(tz)
    except KeyError as exc:
        raise RuntimeError(f"Missing required environment variable: {exc.args[0]}") from exc
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_USER_ID must be numeric and Google client config must be valid JSON") from exc
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError("TIMEZONE must be a valid IANA timezone, for example Asia/Kolkata") from exc

    if user_id <= 0:
        raise RuntimeError("TELEGRAM_USER_ID must be a positive Telegram user ID")
    if not isinstance(client_config, dict) or "installed" not in client_config:
        raise RuntimeError("GOOGLE_CLIENT_CONFIG_JSON must contain the downloaded OAuth desktop client JSON")
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        raise RuntimeError("Missing required environment variable: TELEGRAM_BOT_TOKEN")
    if not os.getenv("TOKEN_ENCRYPTION_KEY"):
        raise RuntimeError("Missing required environment variable: TOKEN_ENCRYPTION_KEY")

    return Settings(
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_user_id=user_id,
        google_client_config=client_config,
        token_encryption_key=os.environ["TOKEN_ENCRYPTION_KEY"],
        google_token_encrypted=os.getenv("GOOGLE_TOKEN_ENCRYPTED", ""),
        database_path=Path(os.getenv("DATABASE_PATH", "data/assistant.sqlite3")),
        timezone_name=tz,
        calendar_id=os.getenv("CALENDAR_ID", "primary"),
        calendar_sync_minutes=max(1, int(os.getenv("CALENDAR_SYNC_MINUTES", "5"))),
        calendar_lookahead_days=max(1, int(os.getenv("CALENDAR_LOOKAHEAD_DAYS", "45"))),
        reminder_check_seconds=max(10, int(os.getenv("REMINDER_CHECK_SECONDS", "30"))),
        webhook_base_url=os.getenv("WEBHOOK_BASE_URL", "").rstrip("/"),
        webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        gmail_scan_minutes=max(5, int(os.getenv("GMAIL_SCAN_MINUTES", "30"))),
        gmail_scan_max_results=min(100, max(1, int(os.getenv("GMAIL_SCAN_MAX_RESULTS", "30")))),
        voice_max_bytes=max(100_000, int(os.getenv("VOICE_MAX_MB", "20")) * 1024 * 1024),
        voice_max_seconds=max(30, min(600, int(os.getenv("VOICE_MAX_SECONDS", "300")))),
        notion_api_token=os.getenv("NOTION_API_TOKEN", ""),
        notion_data_source_id=os.getenv("NOTION_DATA_SOURCE_ID", ""),
        strands_api_key=os.getenv("STRANDS_API_KEY") or os.getenv("DECIDER_API_KEY", ""),
        strands_base_url=os.getenv("STRANDS_BASE_URL", "https://da25tnxy06gjk.cloudfront.net/v1").rstrip("/"),
        strands_model=os.getenv("STRANDS_MODEL", "qwen3.5-4b"),
        decider_api_key=os.getenv("DECIDER_API_KEY") or os.getenv("STRANDS_API_KEY", ""),
        decider_url=os.getenv("DECIDER_URL", "https://da25tnxy06gjk.cloudfront.net").rstrip("/"),
    )
