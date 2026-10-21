"""One-time local OAuth setup; stores the encrypted refresh token in SQLite."""
from __future__ import annotations

import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from google_auth_oauthlib.flow import InstalledAppFlow

from assistant.config import load_settings
from assistant.google_calendar import GoogleCalendar, SCOPES
from assistant.store import Store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-env-token", action="store_true",
                        help="also print encrypted token for copying into a hosting secret variable")
    args = parser.parse_args()
    settings = load_settings()
    store = Store(settings.database_path)
    store.initialize()
    flow = InstalledAppFlow.from_client_config(settings.google_client_config, SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    calendar = GoogleCalendar(settings, store)
    calendar.save_credentials(credentials)
    print("Google Calendar connected. OAuth token saved encrypted in the SQLite database.")
    if args.export_env_token:
        encrypted = store.get_token()
        print("GOOGLE_TOKEN_ENCRYPTED=" + encrypted.decode("ascii"))


if __name__ == "__main__":
    main()
