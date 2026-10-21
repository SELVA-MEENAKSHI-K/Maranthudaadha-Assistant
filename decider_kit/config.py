"""Minimal configuration required by the vendored Strands Decider client."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UGMDU_DECIDER_URL = "https://da25tnxy06gjk.cloudfront.net"
LOCAL_DECIDER_URL = "http://127.0.0.1:8099"


def load_dotenv(path: Path | None = None) -> None:
    """Load simple KEY=VALUE settings without overriding the process environment."""
    env_path = path or ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    decider_mode: str
    decider_url: str
    decider_api_key: str


def settings() -> Settings:
    load_dotenv()
    mode = os.environ.get("DECIDER_MODE", "local")
    default_url = {"local": LOCAL_DECIDER_URL, "ugmdu": UGMDU_DECIDER_URL}.get(mode, LOCAL_DECIDER_URL)
    return Settings(
        decider_mode=mode,
        decider_url=os.environ.get("DECIDER_URL", default_url).rstrip("/"),
        decider_api_key=os.environ.get("DECIDER_API_KEY", ""),
    )
