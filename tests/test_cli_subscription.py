"""CLI subscription bridges: official vendor CLIs as plain text models.

Doctrine matches test_chatgpt_zero_native_tools.py: Odysseus is the only agent,
no CLI built-in tool may run, and no token file or API key is ever involved.
"""

import asyncio
import json
import os
import stat
import textwrap
import time

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ModelEndpoint
from src import cli_subscription as cs
from src import llm_core, model_pricing

CLAUDE_URL = "http://cli-bridge.invalid/claude-code"
CODEX_URL = "http://cli-bridge.invalid/codex-cli"
GROK_URL = "http://cli-bridge.invalid/grok-cli"
AGY_URL = "http://cli-bridge.invalid/antigravity"
MESSAGES = [
    {"role": "system", "content": "You are Odysseus. Use <tool>read_file</tool> when needed."},
    {"role": "user", "content": "hi"},
]


# ── URL identity ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,vendor", [
    (CLAUDE_URL, "claude-code"), (CODEX_URL + "/", "codex-cli"), (GROK_URL, "grok-cli"), (AGY_URL, "antigravity"),
])
def test_bridge_urls_are_recognised(url, vendor):
    assert cs.vendor_for_url(url) == vendor
    assert cs.is_cli_subscription_base(url)
    assert llm_core._detect_provider(url) == "cli-subscription"
    assert llm_core._stream_target_url(url) == url


@pytest.mark.parametrize("url", [
    None, "", "https://cli-bridge.invalid/claude-code", "http://cli-bridge.invalid/unknown",
    "http://cli-bridge.invalid:8080/claude-code", "http://evil.com/claude-code",
    "http://cli-bridge.invalid.evil.com/claude-code", "http://x@cli-bridge.invalid/claude-code",
    "http://cli-bridge.invalid/claude-code/extra", "http://cli-bridge.invalid/claude-code?x=1",
])
def test_lookalike_urls_are_rejected(url):
    assert cs.vendor_for_url(url) is None
    assert llm_core._detect_provider(url or "") != "cli-subscription"


def test_provider_labels():
    assert cs.provider_label(CLAUDE_URL) == "Claude (subscription)"
    assert cs.provider_label(AGY_URL) == "Gemini (Antigravity subscription)"
    assert llm_core._provider_label(CLAUDE_URL) == "Claude (subscription)"


def test_bridge_urls_are_not_cost_tracked():
    assert model_pricing.is_subscription_endpoint(CLAUDE_URL)
    assert not model_pricing.is_cost_tracked_endpoint(GROK_URL)
    assert not model_pricing.is_subscription_endpoint("http://cli-bridge.invalid/nope")


# ── argv ────────────────────────────────────────────────────────────────────

def _spec(vendor, tmp_path, system="S" * 300_000, effort="low", model="m1"):
    return cs.build_launch_spec(vendor, "/bin/cli", model, system, "T" * 10, str(tmp_path), effort)


def test_claude_argv_tools_off_and_never_bare(tmp_path):
    spec = _spec("claude-code", tmp_path)
    a = spec.argv
    assert "--bare" not in a
    assert a[a.index("--tools") + 1] == ""
    for flag in ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands", "--include-partial-messages"):
        assert flag in a
    assert a[a.index("--setting-sources") + 1] == ""
    assert a[a.index("--effort") + 1] == "low"
    # Huge system prompt travels in a file, never argv (128 KB per-arg cap).
    assert max(len(x) for x in a) < 1000
    assert open(a[a.index("--system-prompt-file") + 1]).read().startswith("SSS")
    assert spec.stdin_text == "T" * 10


def test_codex_argv_tools_off(tmp_path):
    spec = _spec("codex-cli", tmp_path)
    a = spec.argv
    assert a[1] == "exec" and a[-1] == "-"
    for flag in ("--json", "--ephemeral", "--skip-git-repo-check"):
        assert flag in a
    assert a[a.index("-s") + 1] == "read-only"
    for cfg in ("features.plugins=false", "features.apps=false", "features.shell_tool=false", "model_reasoning_effort=low"):
        assert cfg in a
    assert spec.stdin_text.startswith("SSS") and max(len(x) for x in a) < 1000


def test_grok_argv_tools_off(tmp_path):
    spec = _spec("grok-cli", tmp_path)
    a = spec.argv
    assert "--prompt-file" in a and "-p" not in a
    disallowed = a[a.index("--disallowed-tools") + 1].split(",")
    for tool in ("run_terminal_command", "read_file", "search_replace", "grep", "write"):
        assert tool in disallowed
    for flag in ("--no-subagents", "--no-plan", "--disable-web-search"):
        assert flag in a
    assert a[a.index("--max-turns") + 1] == "1"
    assert a[a.index("--permission-mode") + 1] == "dontAsk"
    assert max(len(x) for x in a) < 5000
    assert open(a[a.index("--prompt-file") + 1]).read().startswith("SSS")


def test_antigravity_is_blocked():
    assert cs.VENDORS["antigravity"].blocked_reason
    with pytest.raises(cs.CliBridgeError):
        cs.build_launch_spec("antigravity", "/bin/agy", "m", "", "x", "/tmp")


def test_bad_model_and_effort_cannot_inject_flags(tmp_path):
    with pytest.raises(cs.CliBridgeError):
        _spec("claude-code", tmp_path, model="--dangerously-skip-permissions")
    a = _spec("claude-code", tmp_path, effort="low --bare").argv
    assert "--effort" not in a and "--bare" not in a


# ── env ─────────────────────────────────────────────────────────────────────

def test_child_env_scrubs_api_keys():
    base = {n: "secret-value-123" for n in cs.SCRUBBED_ENV_VARS}
    base.update({"PATH": "/usr/bin", "HOME": "/h"})
    env = cs.build_child_env(base)
    assert env == {"PATH": "/usr/bin", "HOME": "/h"}
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY", "CODEX_API_KEY", "GOOGLE_API_KEY"):
        assert name in cs.SCRUBBED_ENV_VARS


def test_redact_hides_env_values(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-supersecret")
    assert "supersecret" not in cs._redact("failed with sk-ant-supersecret here")


# ── prompt flattening ───────────────────────────────────────────────────────

def test_flatten_messages_labels_turns_and_drops_images():
    sys_t, tr = cs.flatten_messages([
        {"role": "system", "content": "A"}, {"role": "system", "content": "B"},
        {"role": "user", "content": [{"type": "text", "text": "q"}, {"type": "image_url", "image_url": {"url": "data:x"}}]},
        {"role": "assistant", "content": "calls tool"}, {"role": "tool", "content": "result"},
    ])
    assert sys_t == "A\n\nB"
    assert "[User]\nq" in tr and "[Assistant]\ncalls tool" in tr and "[Tool result]\nresult" in tr
    assert "image omitted" in tr and "data:x" not in tr
    assert cs.flatten_messages([{"role": "user", "content": "just this"}])[1] == "just this"


# ── parsers ─────────────────────────────────────────────────────────────────

def _run_parser(vendor, lines):
    st = cs.ParseState()
    out = []
    for obj in lines:
        out.extend(cs.PARSERS[vendor](obj, st))
    return out


def test_claude_parser_real_shapes():
    ev = _run_parser("claude-code", [
        {"type": "system", "subtype": "init", "tools": [], "model": "claude-haiku-5-5"},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}}},
        {"type": "assistant", "message": {"model": "claude-haiku-5-5", "content": [{"type": "text", "text": "ok"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "ok",
         "usage": {"input_tokens": 2, "cache_creation_input_tokens": 564, "output_tokens": 4}},
    ])
    assert [e for e in ev if e[0] == "delta"] == [("delta", "ok")]  # assistant text not duplicated
    assert ("usage", {"input_tokens": 566, "output_tokens": 4}) in ev
    assert ev[-1] == ("done", None)


def test_claude_parser_without_partials_uses_assistant_text_and_errors():
    ev = _run_parser("claude-code", [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
        {"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in"},
    ])
    assert ("delta", "hello") in ev and ("error", "Not logged in") in ev


@pytest.mark.parametrize("vendor,obj", [
    ("claude-code", {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "tool_use", "name": "Bash"}}}),
    ("claude-code", {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}}),
    ("claude-code", {"type": "system", "subtype": "init", "tools": ["Bash"]}),
    ("codex-cli", {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}}),
    ("codex-cli", {"type": "item.completed", "item": {"type": "mcp_tool_call"}}),
    ("grok-cli", {"type": "tool_call", "toolName": "list_dir"}),
    ("grok-cli", {"type": "tool_call_update", "status": "completed"}),
])
def test_parsers_flag_tool_events(vendor, obj):
    assert any(k == "tool" for k, _ in _run_parser(vendor, [obj]))


def test_codex_parser_real_shapes():
    ev = _run_parser("codex-cli", [
        {"type": "thread.started", "thread_id": "x"}, {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 15502, "cached_input_tokens": 0, "output_tokens": 5}},
    ])
    assert ev == [("delta", "ok"), ("usage", {"input_tokens": 15502, "output_tokens": 5}), ("done", None)]


def test_grok_parser_real_shapes():
    ev = _run_parser("grok-cli", [
        {"type": "available_commands", "tools": ["x"]}, {"type": "text", "data": "ok"},
        {"type": "usage", "usage": {"input_tokens": 100, "cache_read_input_tokens": 5, "output_tokens": 27}},
        {"type": "end", "stopReason": "end_turn"},
    ])
    assert ev == [("delta", "ok"), ("usage", {"input_tokens": 105, "output_tokens": 27}), ("done", None)]


def test_grok_model_list_and_agy_model_list_parse():
    text = "You are logged in with grok.com.\n\nAvailable models:\n  * grok-4.7 (default)\n  - grok-4.6\n"
    assert cs.parse_grok_models(text) == ["grok-4.7", "grok-4.6"]
    assert cs.parse_agy_models("Fetching available models...\nid-a\tName A\nid-b\tName B\n") == ["id-a", "id-b"]


def test_codex_models_come_from_cache_slugs_only(tmp_path):
    f = tmp_path / "models_cache.json"
    f.write_text(json.dumps({"models": [{"slug": "a", "visibility": "list"}, {"slug": "b", "visibility": "hide"}]}))
    assert cs.codex_models_from_cache(str(f)) == ["a"]
    assert cs.codex_models_from_cache(str(tmp_path / "missing.json")) == []


# ── process behaviour with fake CLIs ────────────────────────────────────────

def _fake_cli(tmp_path, monkeypatch, vendor_slug, body):
    script = tmp_path / f"fake-{vendor_slug}"
    script.write_text("#!/bin/bash\n" + textwrap.dedent(body))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(cs.VENDORS[vendor_slug].env_override, str(script))
    return script


def _collect(url, **kw):
    async def go():
        return [c async for c in cs.stream_cli_completion(url, "m1", MESSAGES, **kw)]
    return asyncio.run(go())


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_fake_claude_streams_deltas_usage_and_done(tmp_path, monkeypatch):
    _fake_cli(tmp_path, monkeypatch, "claude-code", """
        cat > /dev/null
        echo '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"he"}}}'
        echo '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"llo"}}}'
        echo '{"type":"result","subtype":"success","is_error":false,"usage":{"input_tokens":3,"output_tokens":2}}'
    """)
    chunks = _collect(CLAUDE_URL)
    assert chunks[0] == 'data: {"delta": "he"}\n\n' and chunks[1] == 'data: {"delta": "llo"}\n\n'
    assert 'data: {"type": "usage", "data": {"input_tokens": 3, "output_tokens": 2}}\n\n' in chunks
    assert chunks[-1] == "data: [DONE]\n\n"


def test_fake_cli_runs_in_empty_dir_removed_afterwards(tmp_path, monkeypatch):
    marker = tmp_path / "cwd.txt"
    _fake_cli(tmp_path, monkeypatch, "claude-code", f"""
        cat > /dev/null
        pwd > {marker}; ls -A >> {marker}
        echo '{{"type":"result","subtype":"success","is_error":false}}'
    """)
    _collect(CLAUDE_URL)
    lines = marker.read_text().splitlines()
    assert lines == [lines[0]]  # cwd printed, and nothing listed inside it
    assert "odysseus-cli-" in lines[0] and not os.path.exists(os.path.dirname(lines[0]))


def test_fake_cli_env_has_no_api_keys(tmp_path, monkeypatch):
    out = tmp_path / "env.txt"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("XAI_API_KEY", "xai-should-not-leak")
    _fake_cli(tmp_path, monkeypatch, "grok-cli", f"""
        env > {out}
        echo '{{"type":"text","data":"ok"}}'
        echo '{{"type":"end"}}'
    """)
    _collect(GROK_URL)
    text = out.read_text()
    assert "should-not-leak" not in text and "ANTHROPIC_API_KEY" not in text


def test_tool_event_kills_process_and_yields_error(tmp_path, monkeypatch):
    pidfile = tmp_path / "pid"
    _fake_cli(tmp_path, monkeypatch, "grok-cli", f"""
        echo $$ > {pidfile}
        echo '{{"type":"tool_call","toolName":"run_terminal_command"}}'
        sleep 60
    """)
    chunks = _collect(GROK_URL, timeout=30)
    joined = "".join(chunks)
    assert "tried to use its own tool; Odysseus blocks this" in joined
    assert chunks[-1] == "data: [DONE]\n\n" and '"delta"' not in joined
    time.sleep(0.2)
    assert not _pid_alive(int(pidfile.read_text()))


def test_codex_command_item_is_blocked(tmp_path, monkeypatch):
    _fake_cli(tmp_path, monkeypatch, "codex-cli", """
        cat > /dev/null
        echo '{"type":"item.started","item":{"type":"command_execution","command":"rm -rf x"}}'
        sleep 60
    """)
    joined = "".join(_collect(CODEX_URL, timeout=30))
    assert "tried to use its own tool" in joined


def test_timeout_kills_whole_process_group(tmp_path, monkeypatch):
    childfile = tmp_path / "child"
    _fake_cli(tmp_path, monkeypatch, "claude-code", f"""
        sleep 120 &
        echo $! > {childfile}
        wait
    """)
    start = time.time()
    joined = "".join(_collect(CLAUDE_URL, timeout=1))
    assert time.time() - start < 15
    assert "timed out" in joined
    time.sleep(0.3)
    assert not _pid_alive(int(childfile.read_text()))


def test_nonzero_exit_reports_stderr_first_without_env_values(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-leakme-123")
    _fake_cli(tmp_path, monkeypatch, "codex-cli", """
        cat > /dev/null
        echo "boom: key sk-openai-leakme-123 rejected" >&2
        echo "stdout detail"
        exit 3
    """)
    joined = "".join(_collect(CODEX_URL))
    assert "event: error" in joined and "boom" in joined
    assert "leakme" not in joined and "stdout detail" not in joined


def test_missing_binary_and_blocked_vendor(monkeypatch, tmp_path):
    monkeypatch.setenv("ODYSSEUS_CLI_CLAUDE_CODE_BIN", str(tmp_path / "nope"))
    assert "not installed" in "".join(_collect(CLAUDE_URL))
    assert "blocked" in "".join(_collect(AGY_URL))


# ── llm_core dispatch ───────────────────────────────────────────────────────

def test_stream_llm_delegates_and_never_sends_tools(monkeypatch):
    seen = {}

    async def fake(url, model, messages, *, timeout=None, reasoning_effort=None, **extra):
        seen.update(url=url, model=model, extra=extra, effort=reasoning_effort)
        yield 'data: {"delta": "x"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(cs, "stream_cli_completion", fake)
    tools = [{"type": "function", "function": {"name": "run_shell"}}]

    async def go():
        return [c async for c in llm_core.stream_llm(CLAUDE_URL, "sonnet", MESSAGES, tools=tools, reasoning_effort="low")]

    out = asyncio.run(go())
    assert out[0] == 'data: {"delta": "x"}\n\n'
    assert seen["url"] == CLAUDE_URL and seen["effort"] == "low"
    assert seen["extra"] == {}  # tools never forwarded


def test_llm_call_async_collects_stream(monkeypatch):
    async def fake(url, model, messages, **kw):
        yield 'data: {"delta": "o"}\n\n'
        yield 'data: {"delta": "k"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(cs, "stream_cli_completion", fake)
    llm_core._response_cache.clear() if hasattr(llm_core, "_response_cache") else None
    assert asyncio.run(llm_core.llm_call_async(CLAUDE_URL, "sonnet", [{"role": "user", "content": "unique-async-1"}])) == "ok"


def test_llm_call_async_surfaces_bridge_error(monkeypatch):
    async def fake(url, model, messages, **kw):
        yield 'event: error\ndata: {"status": 502, "text": "nope"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(cs, "stream_cli_completion", fake)
    with pytest.raises(HTTPException):
        asyncio.run(llm_core.llm_call_async(CLAUDE_URL, "sonnet", [{"role": "user", "content": "unique-async-2"}]))


def test_sync_llm_call_runs_bridge_inside_running_loop(monkeypatch):
    async def fake(url, model, messages, **kw):
        yield 'data: {"delta": "sync-ok"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(cs, "stream_cli_completion", fake)

    async def inside_loop():
        return llm_core.llm_call(CLAUDE_URL, "sonnet", [{"role": "user", "content": "unique-sync-1"}])

    assert asyncio.run(inside_loop()) == "sync-ok"


# ── routes ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    import routes.cli_subscription_routes as csr

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]).StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(csr, "SessionLocal", Session)
    monkeypatch.setattr(csr, "_invalidate_models_cache", lambda: None)
    monkeypatch.setattr(csr, "_owner", lambda request: "alice")
    state = {"admin": True}

    def gate(request: Request):
        if not state["admin"]:
            raise HTTPException(403, "Admin only")

    monkeypatch.setattr(csr, "require_admin", gate)

    async def fake_probe(vendor):
        v = cs.VENDORS[vendor]
        return {"vendor": vendor, "label": v.label, "base_url": v.base_url, "experimental": v.experimental,
                "installed": True, "version": "1", "logged_in": True, "models": ["m1", "m2"],
                "blocked_reason": v.blocked_reason, "detail": ""}

    monkeypatch.setattr(cs, "probe_vendor", fake_probe)
    app = FastAPI()
    app.include_router(csr.setup_cli_subscription_routes())
    c = TestClient(app, raise_server_exceptions=False)
    c.state, c.Session = state, Session
    return c


def test_routes_are_admin_only(client):
    client.state["admin"] = False
    assert client.get("/api/cli-subscription/status").status_code == 403
    assert client.post("/api/cli-subscription/claude-code/connect").status_code == 403
    assert client.delete("/api/cli-subscription/claude-code").status_code == 403
    assert client.Session().query(ModelEndpoint).count() == 0


def test_connect_is_idempotent_and_plain_text_only(client):
    first = client.post("/api/cli-subscription/claude-code/connect")
    second = client.post("/api/cli-subscription/claude-code/connect")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"] and first.json()["created"] and not second.json()["created"]
    db = client.Session()
    rows = db.query(ModelEndpoint).all()
    assert len(rows) == 1
    ep = rows[0]
    assert ep.base_url == CLAUDE_URL and ep.owner == "alice" and ep.supports_tools is False
    assert ep.model_refresh_mode == "manual" and ep.endpoint_kind == "api" and ep.api_key is None
    assert json.loads(ep.cached_models) == ["m1", "m2"]
    status = client.get("/api/cli-subscription/status").json()["vendors"]
    assert {v["vendor"]: v["connected"] for v in status}["claude-code"] is True
    assert client.delete("/api/cli-subscription/claude-code").json() == {"removed": 1}
    assert db.query(ModelEndpoint).count() == 0


def test_connect_refuses_blocked_unknown_and_signed_out(client, monkeypatch):
    assert client.post("/api/cli-subscription/antigravity/connect").status_code == 409
    assert client.post("/api/cli-subscription/nope/connect").status_code == 404

    async def signed_out(vendor):
        return {"installed": True, "logged_in": False, "models": ["m"], "base_url": "x"}

    monkeypatch.setattr(cs, "probe_vendor", signed_out)
    assert client.post("/api/cli-subscription/codex-cli/connect").status_code == 409
    assert client.Session().query(ModelEndpoint).count() == 0


def test_bridge_url_is_recognised_with_chat_route_appended():
    """Regression: the chat path appends /chat/completions to the base URL.

    Before the fix the bridge was not recognised on that URL, so chats fell
    through to a real HTTP request to the reserved host and failed with
    "Upstream http://cli-bridge.invalid unreachable".
    """
    from src.cli_subscription import vendor_for_url
    from src.llm_core import _detect_provider

    assert vendor_for_url("http://cli-bridge.invalid/claude-code/chat/completions") == "claude-code"
    assert vendor_for_url("http://cli-bridge.invalid/grok-cli/models") == "grok-cli"
    assert _detect_provider("http://cli-bridge.invalid/codex-cli/chat/completions") == "cli-subscription"
    assert vendor_for_url("http://cli-bridge.invalid/claude-code/other") is None
    assert vendor_for_url("http://cli-bridge.invalid/chat/completions") is None


def test_bridge_routes_are_not_cost_tracked():
    """Subscription use must not show per-token dollar costs in chat metrics."""
    from src.endpoint_resolver import endpoint_cost_tracked
    from src.model_pricing import estimate_cost_usd

    url = "http://cli-bridge.invalid/grok-cli/chat/completions"
    assert endpoint_cost_tracked(url) is False
    assert estimate_cost_usd("grok-4.7", 3000, 10, url) is None
    assert endpoint_cost_tracked("https://api.x.ai/v1") is True
