from __future__ import annotations

import os
import time
from typing import Any, Callable

import requests


def _default_timeout() -> int:
    """Total seconds one send_message() may wait for the agent to finish."""
    try:
        return int(os.getenv("OPENCODE_TIMEOUT", "1800"))
    except ValueError:
        return 1800


def _default_poll_interval() -> float:
    try:
        return max(0.5, float(os.getenv("OPENCODE_POLL_INTERVAL", "5")))
    except ValueError:
        return 5.0


class OpenCodeClient:
    """HTTP client for the OpenCode Server API.

    Verified against OpenCode 1.18.31:
      GET  /global/health
      POST /session                  (params: directory, body: {title})
      GET  /session                  (list all)
      GET  /session/:id
      POST /session/:id/message      (blocking; body: {parts, model?, agent?})
      POST /session/:id/prompt_async (body: {parts, model?, agent?}) -> 204
      GET  /session/:id/message      (list; assistant info.time.completed
                                      is only set once the run finished)
      POST /session/:id/abort
      GET  /session/:id/diff
      DELETE /session/:id

    ``send_message`` prefers the async endpoint: the HTTP call returns
    immediately (204) and the agent runs server-side, so no single
    connection has to survive the whole (potentially hours-long) run.
    Completion is detected by polling the message list until the last
    assistant message carries ``time.completed``.  Servers without the
    async endpoint fall back to the old blocking request.

    ``timeout`` is the total wait budget for one agent run (not an HTTP
    read timeout).  It defaults to the OPENCODE_TIMEOUT env var (1800s).
    """

    POLL_HTTP_TIMEOUT = 30          # per-poll HTTP timeout
    MAX_CONSECUTIVE_POLL_ERRORS = 10  # tolerate transient poll failures

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int | None = None,
        poll_interval: float | None = None,
    ):
        self.base_url = (base_url or os.getenv("OPENCODE_URL", "http://127.0.0.1:4096")).rstrip("/")
        self.timeout = _default_timeout() if timeout is None else int(timeout)
        self.poll_interval = _default_poll_interval() if poll_interval is None else float(poll_interval)

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

    def abort_session(self, session_id: str) -> None:
        """Best-effort abort of a running session (ignored on failure)."""
        try:
            requests.post(
                f"{self.base_url}/session/{session_id}/abort",
                timeout=30,
            )
        except Exception:
            pass

    def send_message(
        self,
        session_id: str,
        prompt: str,
        model: str | None = None,
        agent: str | None = None,
        on_progress: Callable[[float, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Send a message and wait for the response.

        Args:
            model: model spec in ``provider/model`` format (e.g.
                ``opencode/mimo-v2.5-free``).  Parsed into the object form
                ``{"providerID": ..., "modelID": ...}`` required by the API.
                Omit to use the server's default model.
            agent: agent ID (e.g. ``build``).  Omit to use the default.
            on_progress: optional ``callback(elapsed_seconds, message)``
                invoked about once a minute while the agent runs; ``message``
                is the still-streaming assistant message (may be empty).

        Raises:
            TimeoutError: the run exceeded ``self.timeout`` seconds.  The
                session is aborted (best effort) so it stops consuming
                tokens; the caller's own cleanup still applies.
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

        if self._send_async(session_id, payload):
            return self._wait_for_completion(session_id, on_progress=on_progress)

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

    # -- async path -------------------------------------------------------

    def _send_async(self, session_id: str, payload: dict[str, Any]) -> bool:
        """Fire the prompt server-side via /prompt_async (returns fast).

        Returns True when the async endpoint accepted the request.  Any
        non-2xx (unknown route, schema mismatch, server error) falls back
        to the blocking endpoint so old servers keep working.
        """
        try:
            response = requests.post(
                f"{self.base_url}/session/{session_id}/prompt_async",
                json=payload,
                timeout=60,
            )
        except requests.RequestException:
            return False
        return 200 <= response.status_code < 300

    def _wait_for_completion(
        self,
        session_id: str,
        on_progress: Callable[[float, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        deadline = started + self.timeout
        consecutive_errors = 0
        next_progress_at = started + 60.0

        while True:
            try:
                messages = self.list_messages(session_id)
                consecutive_errors = 0
            except requests.RequestException as exc:
                consecutive_errors += 1
                if consecutive_errors >= self.MAX_CONSECUTIVE_POLL_ERRORS:
                    raise RuntimeError(
                        f"OpenCode polling failed {consecutive_errors} times in a row: {exc}"
                    ) from exc
                time.sleep(self.poll_interval)
                continue

            reply = self._last_assistant(messages)
            if reply is not None and (reply.get("info") or {}).get("time", {}).get("completed"):
                return reply

            now = time.monotonic()
            if now >= deadline:
                self.abort_session(session_id)
                raise TimeoutError(
                    f"OpenCode agent did not finish within {self.timeout}s "
                    f"(session {session_id} aborted). Raise opencode.timeout "
                    f"or the OPENCODE_TIMEOUT env to allow longer runs."
                )
            if on_progress and now >= next_progress_at:
                next_progress_at = now + 60.0
                try:
                    on_progress(round(now - started), reply or {})
                except Exception:
                    on_progress = None  # a broken callback must not kill the wait
            time.sleep(self.poll_interval)

    def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        response = requests.get(
            f"{self.base_url}/session/{session_id}/message",
            timeout=self.POLL_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _last_assistant(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        for message in reversed(messages):
            info = message.get("info") or {}
            if info.get("role") == "assistant":
                return message
        return None

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
