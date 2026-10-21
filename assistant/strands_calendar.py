"""Route Calendar writes through Strands Agents and a Strands Decider 2B gate.

The model can only propose the one already-approved event. The intervention checks the
proposal before the tool body runs, and the tool independently compares every argument
with the approved values before calling Google Calendar.
"""
from __future__ import annotations

import json
from typing import Any

from decider_kit import Decider, DeciderError
from strands import tool
from strands.interventions import Confirm, Deny, InterventionHandler, Proceed

from assistant.config import Settings
from assistant.decider_limits import reserve_decider_request
from assistant.strands_runtime import make_strands_agent


QUESTIONS = {
    "grounded": Decider.noul(
        "Is this exact Calendar event grounded in either the user's confirmation or an explicitly cited future date in a classified important email?",
        {
            "true": "The proposed title, date, and priority match the exact event values derived from a user-confirmed request or cited email evidence.",
            "false": "The proposal changes, invents, or is unrelated to the user-confirmed request or cited email evidence.",
        }),
    "appropriate": Decider.noul(
        "Is creating this Calendar event an appropriate action now?",
        {
            "true": "The user explicitly approved this event, OR an important email contains the exact cited future date for a reminder; the date is still in the future.",
            "false": "The event lacks user confirmation and qualifying cited email evidence, is already past, or should not be created.",
        }),
}
class CalendarGuardError(RuntimeError):
    """A Calendar write was blocked or could not be safely completed."""


class _DeciderClient:
    """Adapter from the hackathon kit's typed Decider client to this safety gate."""

    def __init__(self, settings: Settings):
        self.client = Decider(
            url=settings.decider_url,
            api_key=settings.decider_api_key,
            timeout=15,
        )

    def ask(self, state: str) -> dict[str, float]:
        if not self.client.api_key:
            raise CalendarGuardError("Strands Decider is not configured")
        encoded = json.dumps({"state": state, "questions": QUESTIONS}, ensure_ascii=False).encode("utf-8")
        if len(encoded) > 7_500:
            raise CalendarGuardError("Strands Decider request exceeded its safe size limit")
        try:
            reserve_decider_request()
            answers = self.client.ask(state, QUESTIONS)
            probabilities = {name: float(answers[name].p_yes) for name in QUESTIONS}
        except (DeciderError, KeyError, TypeError, ValueError) as exc:
            raise CalendarGuardError("Strands Decider returned an invalid decision") from exc
        if set(probabilities) != set(QUESTIONS) or any(not 0.0 <= p <= 1.0 for p in probabilities.values()):
            raise CalendarGuardError("Strands Decider returned an incomplete decision")
        return probabilities


class _CalendarWriteGuard(InterventionHandler):
    name = "strands-decider-calendar-write-guard"

    def __init__(self, settings: Settings, approved: dict[str, str], human_confirmed: bool,
                 source_grounded: bool = False):
        self.decider = _DeciderClient(settings)
        self.approved = approved
        self.human_confirmed = human_confirmed
        self.source_grounded = source_grounded
        self.calls = 0
        self.blocked_reason: str | None = None
        self.probabilities: dict[str, float] = {}

    @property
    def on_error(self):
        return "deny"

    def before_tool_call(self, event: Any, **kwargs: Any):
        self.calls += 1
        proposed = event.tool_use
        name = proposed.get("name", "")
        args = proposed.get("input") or {}
        if self.calls > 1 or name != "create_calendar_event" or args != self.approved:
            self.blocked_reason = "The proposed Calendar action did not match the approved event."
            return Deny(reason=self.blocked_reason)

        state = json.dumps(
            {"policy": ("Create only this exact event after explicit user approval, or after the important email's cited date evidence is verified and Decider confidently approves the automatic reminder."),
             "approved_event": {key: self.approved[key] for key in ("title", "start_value", "priority")},
             "proposed_tool": name,
             "proposed_arguments": {key: args[key] for key in ("title", "start_value", "priority")},
             "human_confirmed": self.human_confirmed,
             "source_grounded_email_date": self.source_grounded,
             "important_email_classification": self.source_grounded,
             "date_evidence": self.approved.get("date_evidence", "")},
            ensure_ascii=False,
        )
        try:
            self.probabilities = self.decider.ask(state)
        except Exception:
            self.blocked_reason = "Strands Decider did not approve this Calendar action; nothing was added."
            return Deny(reason=self.blocked_reason)

        values = list(self.probabilities.values())
        if any(value <= 0.30 for value in values):
            self.blocked_reason = "Strands Decider rejected this Calendar action; nothing was added."
            return Deny(reason=self.blocked_reason)
        if all(value >= 0.70 for value in values):
            return Proceed()
        if self.human_confirmed:
            # The Telegram Yes button approved the exact event values passed to this run.
            return Confirm(prompt="The Decider is unsure. Use the user's explicit approval of this exact event?",
                           response="yes")
        self.blocked_reason = "Strands Decider is unsure; a human review is required before adding this event."
        return Deny(reason=self.blocked_reason)


def create_calendar_event_with_strands(
    settings: Settings,
    calendar: Any,
    *,
    title: str,
    start_value: str,
    description: str,
    source_message_id: str,
    priority: str = "Medium",
    human_confirmed: bool,
    source_grounded: bool = False,
    date_evidence: str = "",
) -> dict:
    """Create exactly one Calendar event after Strands Decider approves its tool call."""
    if not settings.strands_api_key or not settings.decider_api_key:
        raise CalendarGuardError("Strands API and Decider keys must be configured")

    approved = {
        "title": title,
        "start_value": start_value,
        "description": description,
        "source_message_id": source_message_id,
        "priority": priority,
        "date_evidence": date_evidence,
    }
    execution: dict[str, Any] = {}

    @tool
    def create_calendar_event(title: str, start_value: str, description: str,
                              source_message_id: str, priority: str,
                              date_evidence: str) -> str:
        """Create only the exact event authorized by the user or the email-evidence guardrail."""
        actual = {"title": title, "start_value": start_value, "description": description,
                  "source_message_id": source_message_id, "priority": priority,
                  "date_evidence": date_evidence}
        if actual != approved:
            raise ValueError("Tool arguments differ from the approved Calendar event")
        execution["event"] = calendar.create_event(
            title, start_value, description, source_message_id, priority
        )
        return "Calendar event created."

    guard = _CalendarWriteGuard(settings, approved, human_confirmed, source_grounded)
    agent = make_strands_agent(
        settings,
        system_prompt=("You are a narrow Calendar action agent. You have exactly one tool. "
                       "Call it exactly once using the complete event data in the user request. "
                       "Never alter any field and never claim success unless the tool succeeds. "
                       "The Decider intervention is the authoritative permission check."),
        tools=[create_calendar_event],
        interventions=[guard],
    )
    prompt = "Submit this exact Calendar event to the guarded tool once; do not claim it is approved yourself:\n" + json.dumps(
        approved, ensure_ascii=False
    )
    try:
        agent(prompt)
    except Exception as exc:
        raise CalendarGuardError(
            guard.blocked_reason or "Strands could not safely complete the Calendar action"
        ) from exc
    if "event" not in execution:
        raise CalendarGuardError(guard.blocked_reason or "Calendar action was not executed")
    return execution["event"]
