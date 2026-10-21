"""Minimal vendored Decider API client from the Strands Decider hackathon kit."""

from .client import Decider, DeciderError, Decision

__all__ = ["Decider", "DeciderError", "Decision"]
__version__ = "0.1.0"
