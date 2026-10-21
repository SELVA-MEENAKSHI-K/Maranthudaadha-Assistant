"""Notion data-source note storage using the documented REST API."""
from __future__ import annotations

import re

import requests


class NotionNotes:
    API = "https://api.notion.com/v1"
    VERSION = "2026-03-11"

    def __init__(self, token: str, data_source_id: str):
        self.token = token
        self.data_source_id = data_source_id

    def _request(self, endpoint: str, payload: dict) -> dict:
        response = requests.post(
            f"{self.API}/{endpoint.lstrip('/')}", json=payload,
            headers={"Authorization": f"Bearer {self.token}", "Notion-Version": self.VERSION,
                     "Content-Type": "application/json"}, timeout=(5, 15),
        )
        if not response.ok:
            try:
                detail = response.json().get("message", "")
            except ValueError:
                detail = ""
            raise RuntimeError(f"Notion API returned {response.status_code}: {detail[:300]}")
        return response.json()

    def save_note(self, content: str) -> dict:
        content = content.strip()
        if not content:
            raise ValueError("Note content empty-ah irukku")
        if len(content) > 12000:
            raise ValueError("Note 12,000 characters-kulla irukkanum")
        title = content.splitlines()[0].strip()[:100] or "Telegram note"
        chunks = [content[i:i + 1800] for i in range(0, len(content), 1800)]
        return self._request("pages", {
            "parent": {"data_source_id": self.data_source_id},
            "properties": {
                "Name": {"title": [{"text": {"content": title}}]},
                "Content": {"rich_text": [{"text": {"content": chunk}} for chunk in chunks]},
            },
        })

    def search_notes(self, question: str) -> list[dict]:
        terms = [t for t in re.findall(r"[\w-]{3,}", question.casefold())
                 if t not in {"enna", "sonnanga", "what", "did", "say", "note", "meeting", "about"}]
        terms = list(dict.fromkeys(terms))[:4]
        if not terms:
            raise ValueError("Search-ku konjam specific words anuppunga")
        filters = [{"property": "Content", "rich_text": {"contains": term}} for term in terms]
        result = self._request(f"data_sources/{self.data_source_id}/query", {
            "filter": filters[0] if len(filters) == 1 else {"or": filters},
            "page_size": 20,
            "sorts": [{"property": "Name", "direction": "descending"}],
        })
        notes = []
        for page in result.get("results", []):
            properties = page.get("properties", {})
            title_parts = properties.get("Name", {}).get("title", [])
            content_parts = properties.get("Content", {}).get("rich_text", [])
            title = "".join(p.get("plain_text", "") for p in title_parts)
            content = "".join(p.get("plain_text", "") for p in content_parts)
            if title or content:
                notes.append({"title": title or "Note", "content": content[:1000], "url": page.get("url", "")})
        return notes
