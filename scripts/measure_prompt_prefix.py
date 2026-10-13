"""Report how much of an Odysseus agent request is byte-stable between two turns.

Local OpenAI-compatible servers (llama.cpp, the own-30B server, ...) reuse their
KV cache only up to the first byte that differs between two requests. This
script builds two consecutive requests the way the agent loop does for a local
model that uses text tools (fenced blocks in the prompt), with different
queries, different retrieved tools, a different clock and a per-turn directive,
and prints the length of the shared prefix. Run it on any checkout:

    uv run --no-project --with-requirements requirements.txt \
        python scripts/measure_prompt_prefix.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def flatten(messages) -> str:
    """Serialise messages the way a chat template would lay them out."""
    return "".join(
        f"<|{m.get('role')}|>\n{m.get('content') or ''}\n" for m in messages
    )


def shared_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def build_request(query, tools, history=(), now=None, directive=None):
    """Build the provider-bound messages for one local text-tool request."""
    import src.agent_loop as agent_loop
    import src.user_time as user_time

    now = now or datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
    real = user_time.current_datetime_context_message
    user_time.current_datetime_context_message = lambda *a, **k: {
        "role": "user",
        "content": "[Context — current date/time]\n" + now.strftime("%A %Y-%m-%d %H:%M UTC"),
    }
    try:
        messages = [dict(m) for m in history] + [{"role": "user", "content": query}]
        built, _ = agent_loop._build_system_prompt(
            messages,
            "own-30b",
            None,
            None,
            relevant_tools=set(tools),
        )
    finally:
        user_time.current_datetime_context_message = real
    if directive:
        agent_loop._prepend_agent_directive(built, directive)
    return built


def stable_block_text(core_tools) -> str:
    """The system prompt for the always-available tools alone."""
    import src.agent_loop as agent_loop

    return agent_loop._assemble_prompt(set(core_tools))


def estimate(text: str) -> int:
    from src.model_context import estimate_tokens

    return estimate_tokens([{"role": "user", "content": text}])


def main() -> None:
    from src.tool_index import ALWAYS_AVAILABLE

    first = build_request(
        "what is the weather in Austin",
        set(ALWAYS_AVAILABLE) | {"get_weather"},
        now=datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc),
    )
    second = build_request(
        "draft an email to Bob about the invoice",
        set(ALWAYS_AVAILABLE) | {"send_email", "draft_email", "manage_calendar"},
        history=[
            {"role": "user", "content": "what is the weather in Austin"},
            {"role": "assistant", "content": "Sunny, 31C."},
        ],
        now=datetime(2026, 10, 13, 10, 7, tzinfo=timezone.utc),
        directive="Prefer draft_email over send_email this turn.",
    )
    a, b = flatten(first), flatten(second)
    shared = shared_prefix_len(a, b)
    stable_chars = len(flatten([{"role": "system", "content": stable_block_text(ALWAYS_AVAILABLE)}]))
    print(f"request 1: {len(a)} chars, ~{estimate(a)} tokens")
    print(f"request 2: {len(b)} chars, ~{estimate(b)} tokens")
    print(f"shared prefix: {shared} chars, ~{estimate(a[:shared])} tokens "
          f"({100 * shared / len(a):.0f}% of request 1)")
    print(f"stable block (always-available tools + rules): {stable_chars} chars")


if __name__ == "__main__":
    main()
