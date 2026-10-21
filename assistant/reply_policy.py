"""Decider policy review for Gmail replies before their external send."""
from __future__ import annotations

from decider_kit import Decider
from decider_kit.patterns.policy import PolicyCheck, Verdict

from assistant.config import Settings
from assistant.decider_limits import reserve_decider_request

REPLY_POLICY = (
    "Before an email reply is sent, prevent disclosure of passwords, API keys, access tokens, or one-time security codes; "
    "prevent threats, harassment, impersonation, or instructions facilitating fraud. A normal professional reply, "
    "including ordinary contact details or a respectful disagreement, complies."
)
REPLY_CATEGORIES = {
    "credentials": "Passwords, API keys, access tokens, or one-time security codes",
    "abuse_or_fraud": "Threats, harassment, impersonation, or assistance with fraud",
    "ordinary_reply": "A normal professional response that complies with the policy",
}


def review_gmail_reply(settings: Settings, body: str) -> Verdict:
    """Review only the outgoing draft text; raise on service/configuration failure."""
    if not settings.decider_api_key:
        raise RuntimeError("Strands Decider is not configured")
    decider = Decider(url=settings.decider_url, api_key=settings.decider_api_key, timeout=15)
    check = PolicyCheck(decider, REPLY_POLICY, REPLY_CATEGORIES)
    reserve_decider_request()
    return check.check(body[:3000], context="Owner-approved draft for an existing Gmail reply thread")
