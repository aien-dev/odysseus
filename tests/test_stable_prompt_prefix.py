"""Prefix-cache stability of the agent request (local text-tool mode).

A local OpenAI-compatible server reuses its KV cache only up to the first byte
that differs between two requests. The always-available tool catalogue and
rules must therefore come first and be byte-identical across turns, with every
per-request item (retrieved tools, directives, date/time) after them.
"""
import importlib.util
import pathlib
from datetime import datetime, timezone

import pytest

_spec = importlib.util.spec_from_file_location(
    "measure_prompt_prefix",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "measure_prompt_prefix.py",
)
measure = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(measure)

T0 = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 13, 10, 7, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_prompt_cache(monkeypatch):
    import src.agent_loop as agent_loop

    monkeypatch.setattr(agent_loop, "_cached_base_prompt", None)
    monkeypatch.setattr(agent_loop, "_cached_base_prompt_key", None)
    monkeypatch.setattr(agent_loop, "get_builtin_overrides", lambda: {})


def _two_requests(directive=None):
    from src.tool_index import ALWAYS_AVAILABLE

    first = measure.build_request(
        "what is the weather in Austin",
        set(ALWAYS_AVAILABLE) | {"get_weather"},
        now=T0,
    )
    second = measure.build_request(
        "draft an email to Bob about the invoice",
        set(ALWAYS_AVAILABLE) | {"send_email", "draft_email", "manage_calendar"},
        history=[
            {"role": "user", "content": "what is the weather in Austin"},
            {"role": "assistant", "content": "Sunny, 31C."},
        ],
        now=T1,
        directive=directive,
    )
    return measure.flatten(first), measure.flatten(second)


def _stable_block():
    from src.tool_index import ALWAYS_AVAILABLE

    return measure.stable_block_text(ALWAYS_AVAILABLE)


def _prefix_covers_stable_block(a, b):
    return measure.shared_prefix_len(a, b) >= len(_stable_block())


def test_shared_prefix_covers_whole_stable_block():
    a, b = _two_requests()
    assert "Sunny" not in a and "Sunny" in b  # different turn, different clock
    assert _prefix_covers_stable_block(a, b)


def test_per_turn_directive_does_not_break_the_prefix():
    a, b = _two_requests(directive="Prefer draft_email over send_email this turn.")
    assert "Prefer draft_email" in b
    assert _prefix_covers_stable_block(a, b)


def test_request_is_deterministic_for_same_inputs():
    a1, _ = _two_requests()
    a2, _ = _two_requests()
    assert a1 == a2


def test_dynamic_tools_come_after_the_stable_block():
    import src.agent_loop as agent_loop
    from src.tool_index import ALWAYS_AVAILABLE

    stable = _stable_block()
    full = agent_loop._assemble_prompt(set(ALWAYS_AVAILABLE) | {"send_email", "manage_calendar"})
    assert full.startswith(stable)
    assert "## Tools added for this request" in full[len(stable):]


def test_user_message_is_last_and_clock_sits_right_before_it():
    _, b = _two_requests()
    assert b.rstrip().endswith("draft an email to Bob about the invoice")
    clock = b.index("2026-10-13 10:07")
    assert clock > b.index("Sunny")  # after history
    assert clock < b.index("draft an email")


def test_mutant_time_at_top_is_caught(monkeypatch):
    """Putting the clock first (the old #2927 bug) must fail the prefix check."""
    import src.agent_loop as agent_loop

    real = agent_loop._build_base_prompt
    state = {"n": 0}

    def clock_first(*args, **kwargs):
        prompt, index = real(*args, **kwargs)
        state["n"] += 1
        return f"Current time is 10:{state['n']:02d}.\n\n" + prompt, index

    monkeypatch.setattr(agent_loop, "_build_base_prompt", clock_first)
    a, b = _two_requests()
    assert not _prefix_covers_stable_block(a, b)


def test_mutant_directive_at_top_is_caught(monkeypatch):
    """A directive prepended to the head of the prompt must fail the check."""
    import src.agent_loop as agent_loop

    def head_directive(messages, directive):
        for message in messages:
            if message.get("_agent_injected") in {"prompt", "merged_prompt"}:
                message["content"] = directive + "\n\n" + message["content"]
        return messages

    monkeypatch.setattr(agent_loop, "_prepend_agent_directive", head_directive)
    a, b = _two_requests(directive="Prefer draft_email over send_email this turn.")
    assert not _prefix_covers_stable_block(a, b)


def test_directive_order_newest_first_and_inside_system_message():
    import src.agent_loop as agent_loop

    msgs = [{"role": "system", "content": "BASE", "_agent_injected": "prompt"}]
    agent_loop._prepend_agent_directive(msgs, "ONE")
    agent_loop._prepend_agent_directive(msgs, "TWO")
    assert msgs[0]["content"] == "BASE\n\nTWO\n\nONE"
