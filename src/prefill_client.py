"""Ahead-of-Enter prefill client (T2, client side).

While the user is still composing a message, or while a tool is running, the
backend can send the request it is about to make to the model server's
``POST /v1/aien/prefill`` endpoint. The server extends its session-keyed KV
cache in the background, so when the real chat request arrives only the last few
tokens are left to compute.

Wire format (aien-serve-http, per the T2 server report)::

    POST {server}/v1/aien/prefill
    x-aien-session: <session id>          (the body field ``session`` also works)
    {"session": "...", "messages": [...], "hold_back": <optional int>}

The reply is advisory (``cached_tokens``, ``queued_tokens``, ``target_tokens``,
``status``). Everything here is best effort: the feature is off by default
(``aien_prefill_enabled``), only local endpoints are contacted, and a 404
(server without the endpoint), a refusal or any network error is swallowed so a
chat request can never fail because of it.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import httpx

from src.model_context import is_local_endpoint
from src.settings import get_setting

logger = logging.getLogger(__name__)

SETTING_KEY = "aien_prefill_enabled"
PREFILL_PATH = "/v1/aien/prefill"
SESSION_HEADER = "x-aien-session"
# Servers that answered 404 are not asked again for this long.
UNSUPPORTED_TTL_SECONDS = 15 * 60
_DEFAULT_TIMEOUT_SECONDS = 2.0

_UNSUPPORTED: Dict[str, float] = {}


def prefill_enabled() -> bool:
    """True when the user turned ahead-of-Enter prefill on (default: off)."""
    try:
        return bool(get_setting(SETTING_KEY, False))
    except Exception:
        return False


def prefill_url(endpoint_url: str) -> str:
    """Map a chat endpoint URL (``.../v1`` or ``.../v1/chat/completions``) to the prefill URL."""
    parsed = urlparse((endpoint_url or "").strip())
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/v1"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            if suffix == "/chat/completions" and path.endswith("/v1"):
                path = path[: -len("/v1")]
            break
    return urlunparse(parsed._replace(path=path + PREFILL_PATH, params="", query="", fragment=""))


def _server_key(endpoint_url: str) -> str:
    parsed = urlparse((endpoint_url or "").strip())
    return f"{parsed.scheme}://{parsed.netloc}"


def _marked_unsupported(endpoint_url: str) -> bool:
    key = _server_key(endpoint_url)
    marked_at = _UNSUPPORTED.get(key)
    if marked_at is None:
        return False
    if time.monotonic() - marked_at <= UNSUPPORTED_TTL_SECONDS:
        return True
    _UNSUPPORTED.pop(key, None)
    return False


def reset_unsupported_cache() -> None:
    _UNSUPPORTED.clear()


async def send_prefill(
    endpoint_url: str,
    session_id: str,
    messages: List[Dict[str, Any]],
    *,
    headers: Optional[Dict[str, str]] = None,
    hold_back: Optional[int] = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """Ask the model server to prefill ``messages`` for ``session_id``.

    Returns the server's JSON reply, or ``None`` when nothing was sent or the
    server did not take it (setting off, non-local endpoint, empty input,
    missing endpoint, error). Never raises.

    ``messages`` must be the exact provider-bound messages the real request
    will start with (same system prompt, same history), ending with the draft
    user text or the latest tool output; the server only reuses a byte-identical
    prefix. ``force`` bypasses the setting (tests, explicit callers).
    """
    if not (force or prefill_enabled()):
        return None
    if not endpoint_url or not session_id or not messages:
        return None
    if not is_local_endpoint(endpoint_url) or _marked_unsupported(endpoint_url):
        return None
    try:
        from src.llm_core import _sanitize_llm_messages

        clean = _sanitize_llm_messages(messages)
    except Exception:
        logger.debug("prefill: message sanitising failed", exc_info=True)
        return None
    if not clean:
        return None
    body: Dict[str, Any] = {"session": session_id, "messages": clean}
    if hold_back is not None:
        body["hold_back"] = int(hold_back)
    request_headers = dict(headers or {})
    request_headers[SESSION_HEADER] = session_id
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(
                prefill_url(endpoint_url), json=body, headers=request_headers
            )
        if response.status_code == 404:
            _UNSUPPORTED[_server_key(endpoint_url)] = time.monotonic()
            return None
        if response.status_code >= 400:
            logger.debug("prefill: server answered %s", response.status_code)
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:
        logger.debug("prefill: request failed", exc_info=True)
        return None
