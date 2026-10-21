"""Shared Strands Agents model setup using the provided OpenAI-compatible hosted endpoint."""
from strands import Agent
from strands.models.openai import OpenAIModel

from assistant.config import Settings


def make_strands_agent(settings: Settings, *, system_prompt: str = "", tools=None, interventions=None) -> Agent:
    if not settings.strands_api_key:
        raise RuntimeError("Strands API key is missing. Set STRANDS_API_KEY in .env.")
    model = OpenAIModel(
        client_args={"base_url": settings.strands_base_url, "api_key": settings.strands_api_key},
        model_id=settings.strands_model,
        params={"temperature": 0, "max_tokens": 1200},
    )
    return Agent(
        model=model,
        system_prompt=system_prompt or None,
        tools=tools or [],
        interventions=interventions or [],
        callback_handler=None,
    )
