"""Policy decision pattern adapted from the supplied Strands Decider kit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..client import Decider, Decision

SEVERITY = ["none", "minor", "serious", "critical"]


def questions(policy: str, categories: dict[str, str]) -> dict[str, dict[str, Any]]:
    return {
        "category": Decider.choice("Which policy category does this content fall under?", categories),
        "violates": Decider.noul(
            f"Does this content violate the policy? Policy: {policy}",
            {"true": "it breaks at least one rule of the policy", "false": "it complies with the policy"},
        ),
        "severity": Decider.score("How serious would acting on this content be under the policy?", SEVERITY),
    }


@dataclass
class Verdict:
    action: str
    category: str
    p_violation: float
    severity: str
    confident: bool
    answers: dict[str, Decision]


class PolicyCheck:
    """Classify a draft against an app-owned policy and return allow/review/block."""

    def __init__(self, decider: Decider, policy: str, categories: dict[str, str],
                 block_at: float = 0.7, review_at: float = 0.4):
        self.decider = decider
        self.policy = policy
        self.categories = categories
        self.block_at = block_at
        self.review_at = review_at

    def check(self, content: str, context: str = "") -> Verdict:
        state = (f"Context: {context}\n\n" if context else "") + f"Content to check:\n{content}"
        return self.decide(self.decider.ask(state, questions(self.policy, self.categories)))

    def decide(self, answers: dict[str, Decision]) -> Verdict:
        probability = answers["violates"].p_yes or 0.0
        severity_answer = answers["severity"]
        severity = severity_answer.label or "none"
        serious = severity in ("serious", "critical")
        if probability >= self.block_at or (probability >= self.review_at and severity == "critical"):
            action = "block"
        elif probability >= self.review_at or (serious and severity_answer.confidence >= 0.5):
            action = "review"
        else:
            action = "allow"
        return Verdict(
            action=action,
            category=answers["category"].label or "unknown",
            p_violation=probability,
            severity=severity,
            confident=answers["violates"].confidence >= 0.7,
            answers=answers,
        )
