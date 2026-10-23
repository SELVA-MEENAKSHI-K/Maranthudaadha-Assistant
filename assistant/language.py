"""User language preference and lightweight Tamil/Tanglish auto-detection."""
from __future__ import annotations

import re

_TANGLISH_MARKERS = {
    "aachu", "ah", "ama", "anna", "apram", "epdi", "enna", "iruka", "iruku", "irukku", "illa", "illai",
    "pannunga", "panna", "pannanum", "pannu", "pannitu", "panunga", "podu", "seri", "sollu", "sollunga",
    "venam", "venum", "varum", "varanum", "varanumm", "kuudu", "kudu", "mudiyala", "mudiya", "ungalukku",
    "enakku", "unakku", "intha", "indha", "naalaikku", "naalaiku", "naaliku", "inniku", "innaiku", "seekiram",
    "konjam", "paakka", "paathukuren", "pathukuren", "poga", "ponga", "theriyala", "eppo", "apdi", "panni",
}


def detect_language(text: str | None) -> str | None:
    """Return ta for Tamil script/Tanglish, en for clear English, else None."""
    if not text:
        return None
    if re.search(r"[\u0B80-\u0BFF]", text):
        return "ta"
    words = set(re.findall(r"[a-z]+", text.casefold()))
    if words & {marker.strip() for marker in _TANGLISH_MARKERS}:
        return "ta"
    if words:
        return "en"
    return None


def response_language(store, text: str | None = None) -> str:
    """Resolve explicit Tamil/English preference or update the auto-detected choice."""
    # Handlers pass Telegram's CallbackContext; background jobs pass Store directly.
    if not hasattr(store, "get_preference"):
        store = store.application.bot_data["store"]
    preference = store.get_preference("response_language", "auto")
    if preference in {"ta", "en"}:
        return preference
    detected = detect_language(text)
    if detected:
        store.set_preference("last_response_language", detected)
        return detected
    return store.get_preference("last_response_language", "ta") or "ta"
