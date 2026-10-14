import json

import httpx
import pytest

from src import prefill_client
from src.prefill_client import prefill_url, send_prefill

LOCAL = "http://127.0.0.1:18091/v1"
MESSAGES = [
    {"role": "system", "content": "SYS", "_agent_injected": "prompt"},
    {"role": "user", "content": "draft so far", "metadata": {"x": 1}},
]


@pytest.fixture(autouse=True)
def _reset():
    prefill_client.reset_unsupported_cache()
    yield
    prefill_client.reset_unsupported_cache()


def _transport(status=200, body=None, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body if body is not None else {"status": "queued"})

    return httpx.MockTransport(handler)


def test_setting_defaults_off():
    from src.settings import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["aien_prefill_enabled"] is False
    assert prefill_client.prefill_enabled() is False


@pytest.mark.parametrize("given", [
    "http://h:1/v1", "http://h:1/v1/", "http://h:1/v1/chat/completions", "http://h:1",
])
def test_prefill_url(given):
    assert prefill_url(given) == "http://h:1/v1/aien/prefill"


@pytest.mark.asyncio
async def test_disabled_sends_nothing():
    seen = []
    assert await send_prefill(LOCAL, "s1", MESSAGES, transport=_transport(seen=seen)) is None
    assert seen == []


@pytest.mark.asyncio
async def test_posts_session_and_clean_messages():
    seen = []
    reply = await send_prefill(
        LOCAL, "s1", MESSAGES, force=True, hold_back=2,
        transport=_transport(body={"status": "queued", "cached_tokens": 2300}, seen=seen),
    )
    assert reply["cached_tokens"] == 2300
    request = seen[0]
    assert request.url.path == "/v1/aien/prefill"
    assert request.headers["x-aien-session"] == "s1"
    body = json.loads(request.content)
    assert body["session"] == "s1" and body["hold_back"] == 2
    assert body["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "draft so far"},
    ]


@pytest.mark.asyncio
async def test_404_is_silent_and_remembered():
    seen = []
    assert await send_prefill(LOCAL, "s1", MESSAGES, force=True,
                              transport=_transport(404, seen=seen)) is None
    assert len(seen) == 1
    # Second call does not even hit the network.
    assert await send_prefill(LOCAL, "s1", MESSAGES, force=True,
                              transport=_transport(seen=seen)) is None
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 413, 503])
async def test_refusals_are_silent(status):
    assert await send_prefill(LOCAL, "s1", MESSAGES, force=True,
                              transport=_transport(status)) is None


@pytest.mark.asyncio
async def test_network_error_is_silent():
    def boom(request):
        raise httpx.ConnectError("down")

    assert await send_prefill(LOCAL, "s1", MESSAGES, force=True,
                              transport=httpx.MockTransport(boom)) is None


@pytest.mark.asyncio
async def test_non_local_endpoint_and_empty_input_are_skipped():
    seen = []
    t = _transport(seen=seen)
    assert await send_prefill("https://api.openai.com/v1", "s", MESSAGES, force=True, transport=t) is None
    assert await send_prefill(LOCAL, "", MESSAGES, force=True, transport=t) is None
    assert await send_prefill(LOCAL, "s", [], force=True, transport=t) is None
    assert seen == []
