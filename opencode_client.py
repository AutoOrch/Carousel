from __future__ import annotations

import os
from typing import Any

import requests


class OpenCodeClient:
    """HTTP client for the OpenCode Server API.

    Verified against OpenCode 1.18.30:
      GET  /global/health
      POST /session              (params: directory, body: {title})
      GET  /session              (list all)
      GET  /session/:id
      POST /session/:id/message  (body: {parts, model?, agent?})
      GET  /session/:id/diff
      DELETE /session/:id
    """

    def __init__(self, base_url: str | None = None, timeout: int = 1800):
        self.base_url = (base_url or os.getenv("OPENCODE_URL", "http://127.0.0.1:4096")).rstrip("/")
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        response = requests.get(f"{self.base_url}/global/health", timeout=10)
        response.raise_for_status()
        return response.json()

    def create_session(self, title: str, directory: str | None = None) -> dict[str, Any]:
        response = requests.post(
            f"{self.base_url}/session",
            params={"directory": directory} if directory else None,
            json={"title": title},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def get_session(self, session_id: str) -> dict[str, Any]:
        response = requests.get(
            f"{self.base_url}/session/{session_id}",
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def list_sessions(self) -> list[dict[str, Any]]:
        response = requests.get(f"{self.base_url}/session", timeout=30)
        response.raise_for_status()
        return response.json()

    def send_message(
        self,
        session_id: str,
        prompt: str,
        model: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Send a message and wait for the response.

        Args:
            model: model spec in ``provider/model`` format (e.g.
                ``opencode/mimo-v2.5-free``).  Parsed into the object form
                ``{"providerID": ..., "modelID": ...}`` required by the API.
                Omit to use the server's default model.
            agent: agent ID (e.g. ``build``).  Omit to use the default.
        """
        payload: dict[str, Any] = {
            "parts": [{"type": "text", "text": prompt}],
        }
        if model and "/" in model:
            provider_id, model_id = model.split("/", 1)
            payload["model"] = {"providerID": provider_id, "modelID": model_id}
        elif model:
            payload["model"] = {"modelID": model}
        if agent:
            payload["agent"] = agent

        response = requests.post(
            f"{self.base_url}/session/{session_id}/message",
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code == 500:
            raise RuntimeError(
                f"OpenCode message failed (500). "
                f"This usually means no AI model is configured on the server. "
                f"Check `opencode auth` or OPENCODE_API_KEY. "
                f"Response: {response.text[:200]}"
            )
        response.raise_for_status()
        return response.json()

    def get_diff(self, session_id: str) -> Any:
        response = requests.get(
            f"{self.base_url}/session/{session_id}/diff",
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def delete_session(self, session_id: str) -> None:
        """Delete a session (cleanup). Best-effort — ignores errors."""
        try:
            requests.delete(
                f"{self.base_url}/session/{session_id}",
                timeout=10,
            )
        except Exception:
            pass
