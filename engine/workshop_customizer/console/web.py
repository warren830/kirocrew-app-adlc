"""What a console route may return besides ``(status, payload)``."""
from __future__ import annotations

import json
from typing import Any, Iterable


class Stream:
    """A streamed response: ``chunks`` (bytes) under ``content_type`` (``text/event-stream`` for the chat)."""

    def __init__(self, chunks: Iterable[bytes], content_type: str = "text/event-stream"):
        self.chunks, self.content_type = chunks, content_type


def sse(events: Iterable[Any]) -> Stream:
    """Server-sent events, one ``data:`` line of JSON per event."""
    return Stream(f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode("utf-8") for e in events)
