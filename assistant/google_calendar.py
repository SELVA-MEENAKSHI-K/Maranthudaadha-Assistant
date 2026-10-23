"""Read-only Google Calendar access and encrypted OAuth token handling."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from assistant.config import Settings
from assistant.store import Store

# Gmail read and confirmed event creation.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


class CalendarNotAuthorized(RuntimeError):
    pass


class GoogleCalendar:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.cipher = Fernet(settings.token_encryption_key.encode("ascii"))

    def save_credentials(self, credentials: Credentials) -> None:
        payload = credentials.to_json().encode("utf-8")
        self.store.save_token(self.cipher.encrypt(payload))

    def credentials(self) -> Credentials:
        encrypted = self.store.get_token()
        if not encrypted:
            raise CalendarNotAuthorized("Google Calendar has not been connected yet. Run scripts/auth_google.py.")
        try:
            payload = self.cipher.decrypt(encrypted).decode("utf-8")
            creds = Credentials.from_authorized_user_info(json.loads(payload), SCOPES)
        except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
            raise CalendarNotAuthorized("Saved Google token cannot be read. Reconnect Google Calendar.") from exc
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self.save_credentials(creds)
        if not creds.valid:
            raise CalendarNotAuthorized("Google authorization expired. Run scripts/auth_google.py again.")
        return creds

    def list_events(self, start: datetime, end: datetime) -> list[dict]:
        service = build("calendar", "v3", credentials=self.credentials(), cache_discovery=False)
        events = []
        page_token = None
        while True:
            response = service.events().list(
                calendarId=self.settings.calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                timeZone=self.settings.timezone_name,
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
                pageToken=page_token,
            ).execute(num_retries=2)
            for item in response.get("items", []):
                if item.get("status") == "cancelled":
                    continue
                raw_start = item.get("start", {})
                if raw_start.get("dateTime"):
                    start_at = datetime.fromisoformat(raw_start["dateTime"].replace("Z", "+00:00"))
                elif raw_start.get("date"):
                    start_at = datetime.fromisoformat(raw_start["date"]).replace(tzinfo=self.settings.timezone)
                else:
                    continue
                raw_end = item.get("end", {})
                if raw_end.get("dateTime"):
                    end_at = datetime.fromisoformat(raw_end["dateTime"].replace("Z", "+00:00"))
                elif raw_end.get("date"):
                    end_at = datetime.fromisoformat(raw_end["date"]).replace(tzinfo=self.settings.timezone)
                else:
                    continue
                events.append({"id": item["id"], "title": item.get("summary", "(Title not set)"), "start": start_at,
                               "end": end_at,
                               "all_day": bool(raw_start.get("date")),
                               "priority": item.get("extendedProperties", {}).get("private", {}).get("agent_priority", "Medium")})
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return events

    def today_events(self, now: datetime) -> list[dict]:
        local_now = now.astimezone(self.settings.timezone)
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        return self.list_events(day_start, day_end)

    def create_event(
        self,
        title: str,
        start_value: str,
        description: str,
        source_message_id: str,
        priority: str = "Medium",
    ) -> dict:
        """Create an event after user confirmation (or explicit direct-add) with an idempotent ID."""
        import hashlib
        from datetime import date

        local_start = start_value.strip()
        if len(local_start) == 10:
            event_date = date.fromisoformat(local_start)
            if event_date < datetime.now(self.settings.timezone).date():
                raise ValueError("That email date has passed; refusing to create a past Calendar event")
            body = {
                "summary": title[:200],
                "description": description[:8000],
                "start": {"date": event_date.isoformat()},
                "end": {"date": (event_date + timedelta(days=1)).isoformat()},
            }
        else:
            start_at = datetime.fromisoformat(local_start.replace("Z", "+00:00"))
            if start_at.tzinfo is None:
                start_at = start_at.replace(tzinfo=self.settings.timezone)
            else:
                start_at = start_at.astimezone(self.settings.timezone)
            if start_at <= datetime.now(self.settings.timezone):
                raise ValueError("That email time has passed; refusing to create a past Calendar event")
            end_at = start_at + timedelta(hours=1)
            body = {
                "summary": title[:200],
                "description": description[:8000],
                "start": {"dateTime": start_at.isoformat(), "timeZone": self.settings.timezone_name},
                "end": {"dateTime": end_at.isoformat(), "timeZone": self.settings.timezone_name},
            }
        stable_id = hashlib.sha256(source_message_id.encode("utf-8")).hexdigest()[:32]
        body["id"] = "mail" + stable_id
        body["extendedProperties"] = {"private": {"agent_priority": priority.title()}}
        service = build("calendar", "v3", credentials=self.credentials(), cache_discovery=False)
        try:
            return service.events().insert(calendarId=self.settings.calendar_id, body=body).execute(num_retries=0)
        except Exception as exc:
            # On a network timeout the create may have succeeded; retrieving the stable ID avoids duplicates.
            try:
                return service.events().get(calendarId=self.settings.calendar_id, eventId=body["id"]).execute()
            except Exception:
                raise exc
