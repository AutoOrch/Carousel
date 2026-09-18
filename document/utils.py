"""Shared helpers for the document pipeline (p11)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def response_text(response: Any) -> str:
    """Extract the assistant's text from an OpenCode message response.

    Same contract as ``task_graph._response_text`` — duplicated here so the
    document pipeline does not pull in the whole task-graph import chain.
    """
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        parts = response.get("parts")
        if isinstance(parts, list):
            texts = [
                p.get("text", "")
                for p in parts
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
        for key in ("message", "text", "content", "output"):
            if key in response:
                return str(response[key])
    return str(response)


def extract_json_object(text: str) -> dict | None:
    """Parse the first JSON object from an LLM reply (fenced or bare)."""
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match is None:
        match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def id_in_text(text: str, needle: str) -> bool:
    """Word-boundary substring match.

    ``demo`` must not match inside ``demo2`` (p11 §5 project scoring),
    while Chinese keywords still match inside Chinese text.
    """
    if not needle:
        return False
    pattern = rf"(?<![0-9A-Za-z]){re.escape(needle.lower())}(?![0-9A-Za-z])"
    return re.search(pattern, text.lower()) is not None
