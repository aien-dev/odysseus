"""Official vendor CLIs as plain text models (subscription bridges).

Odysseus is the only agent. Each call launches the owner's own, already signed
in vendor CLI (claude, codex, grok) as a subprocess with
every CLI built-in tool switched off. Odysseus' text tool protocol travels
inside the prompt, exactly like the ChatGPT subscription route.

Hard rules enforced here:
* Odysseus never reads, copies, stores or logs a CLI's token files. Login
  state comes from the CLI's own status command .
* Any stream event that shows the CLI calling one of its own tools kills the
  process and yields an error (fail closed).
* API-key environment variables are removed from the child environment so a
  stray key can never silently bill per token instead of the subscription.
* Every call runs from a fresh empty working directory that is deleted after.

Flags marked UNVERIFIED were not confirmed against a real run of that CLI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import weakref
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

BRIDGE_HOST = "cli-bridge.invalid"  # RFC 6761: never resolves
DEFAULT_TIMEOUT = 180
MAX_CONCURRENT_PER_VENDOR = 2
_STREAM_LINE_LIMIT = 16 * 1024 * 1024

# Removed from the child environment so the subscription login is what runs.
SCRUBBED_ENV_VARS: Tuple[str, ...] = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY",
    "XAI_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GEMINI_BASE_URL",
    "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_CLOUD_QUOTA_PROJECT", "GCLOUD_PROJECT", "CLOUDSDK_CORE_PROJECT",
)

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]\-]{0,79}$")
_EFFORTS_CLAUDE = {"low", "medium", "high", "xhigh", "max"}
_EFFORTS_OTHER = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
_EFFORTS_CODEX = {"minimal", "low", "medium", "high", "xhigh"}

GROK_NEUTRAL_SYSTEM = "You are a plain text assistant. Do not use tools. Follow the instructions in the prompt."

# Grok built-in tool ids seen in a real run (grok 1.0.52). --tools "" does NOT
# disable them; --disallowed-tools removes these. MCP tools from the owner's
# own Grok config stay advertised, so permission-mode dontAsk plus the
# fail-closed tool-event check are the real guards for those.
GROK_BUILTIN_TOOLS: Tuple[str, ...] = (
    "run_terminal_command", "read_file", "search_replace", "list_dir", "grep",
    "kill_command_or_subagent", "get_command_or_subagent_output",
    "scheduler_create", "scheduler_delete", "scheduler_list",
    "todo_write", "monitor", "search_tool", "use_tool", "workflow",
    "enter_plan_mode", "exit_plan_mode", "ask_user_question", "send_feedback",
    "image_gen", "image_edit", "image_to_video", "reference_to_video", "write",
)


class CliBridgeError(Exception):
    """A bridge call failed; ``str()`` is safe to show (no env values)."""


# ── URL identity ────────────────────────────────────────────────────────────

def _bridge_url(slug: str) -> str:
    return f"http://{BRIDGE_HOST}/{slug}"


@dataclass(frozen=True)
class Vendor:
    slug: str
    label: str
    binary: str
    env_override: str
    default_models: Tuple[str, ...]
    experimental: bool = False
    # When set, the vendor cannot run as a plain text model and every launch is refused.
    blocked_reason: str = ""

    @property
    def base_url(self) -> str:
        return _bridge_url(self.slug)


VENDORS: Dict[str, Vendor] = {
    "claude-code": Vendor("claude-code", "Claude", "claude", "ODYSSEUS_CLI_CLAUDE_CODE_BIN",
                          ("fable", "opus", "sonnet", "haiku")),
    "codex-cli": Vendor("codex-cli", "Codex", "codex", "ODYSSEUS_CLI_CODEX_CLI_BIN", ()),
    "grok-cli": Vendor("grok-cli", "Grok", "grok", "ODYSSEUS_CLI_GROK_CLI_BIN", ("grok-4.7",)),
    "antigravity": Vendor(
        "antigravity", "Gemini (Antigravity subscription)", "agy", "ODYSSEUS_CLI_ANTIGRAVITY_BIN", (),
        experimental=True,
        blocked_reason=(
            "agy has no switch to turn its tools off. Verified: in print mode it ran run_command and "
            "write_to_file without asking, even with --mode plan. Its ACP server (agy_acp_server) is not "
            "installed and agy has no ACP mode, so Odysseus cannot guarantee plain text use."
        ),
    ),
}


def vendor_for_url(url: Optional[str]) -> Optional[str]:
    """Vendor slug for an exact bridge URL (exact host + known path), else None."""
    if not url:
        return None
    try:
        parts = urlsplit(str(url).strip())
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme != "http" or host != BRIDGE_HOST or parts.port is not None:
        return None
    if parts.query or parts.fragment or parts.username or parts.password:
        return None
    slug = parts.path.strip("/")
    return slug if slug in VENDORS else None


def is_cli_subscription_base(url: Optional[str]) -> bool:
    return vendor_for_url(url) is not None


def provider_label(url: Optional[str]) -> str:
    slug = vendor_for_url(url)
    if not slug:
        return "CLI subscription"
    label = VENDORS[slug].label
    return label if "subscription" in label else f"{label} (subscription)"


def resolve_binary(vendor: str) -> Optional[str]:
    v = VENDORS[vendor]
    override = os.environ.get(v.env_override)
    if override:
        return override if os.path.isfile(override) and os.access(override, os.X_OK) else None
    return shutil.which(v.binary)


# ── Environment ─────────────────────────────────────────────────────────────

def build_child_env(base: Optional[Dict[str, str]] = None, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ if base is None else base)
    for name in SCRUBBED_ENV_VARS:
        env.pop(name, None)
    if extra:
        env.update(extra)
    return env


def _redact(text: str, env: Optional[Dict[str, str]] = None) -> str:
    """Remove values of sensitive env vars from text shown to users."""
    source = os.environ if env is None else env
    for name, value in list(source.items()) + [(n, os.environ.get(n, "")) for n in SCRUBBED_ENV_VARS]:
        if not value or len(value) < 6:
            continue
        upper = name.upper()
        if name in SCRUBBED_ENV_VARS or any(k in upper for k in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            text = text.replace(value, "[redacted]")
    return text


# ── Prompt flattening ───────────────────────────────────────────────────────

def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: List[str] = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                ptype = part.get("type")
                if ptype in ("text", "input_text", "output_text"):
                    out.append(str(part.get("text") or ""))
                elif ptype in ("image_url", "image", "input_image"):
                    out.append("[image omitted: the CLI subscription bridge is text only]")
        return "\n".join(p for p in out if p)
    return str(content)


def flatten_messages(messages: List[Dict]) -> Tuple[str, str]:
    """Return (system_text, transcript_text)."""
    system_parts: List[str] = []
    turns: List[Tuple[str, str]] = []
    for m in messages or []:
        role = (m.get("role") or "user").lower()
        text = _content_text(m.get("content"))
        if role == "system":
            if text:
                system_parts.append(text)
        elif role == "assistant":
            turns.append(("Assistant", text))
        elif role in ("tool", "function"):
            turns.append(("Tool result", text))
        else:
            turns.append(("User", text))
    if len(turns) == 1 and turns[0][0] == "User":
        transcript = turns[0][1]
    else:
        body = "\n\n".join(f"[{label}]\n{text}" for label, text in turns)
        transcript = (
            "Below is the conversation so far. Reply only with the next Assistant message.\n\n"
            + body + "\n\n[Assistant]\n"
        )
    return "\n\n".join(system_parts), transcript


# ── Launch specs ────────────────────────────────────────────────────────────

@dataclass
class LaunchSpec:
    argv: List[str]
    stdin_text: Optional[str] = None
    env_extra: Dict[str, str] = field(default_factory=dict)


def _check_model(model: str) -> str:
    model = (model or "").strip()
    if not _MODEL_RE.match(model):
        raise CliBridgeError(f"Model name {model!r} is not valid for a CLI subscription.")
    return model


def build_launch_spec(vendor: str, binary: str, model: str, system_text: str, transcript: str,
                      io_dir: str, reasoning_effort: Optional[str] = None) -> LaunchSpec:
    """Build argv/stdin for one call. Long text goes through files or stdin, never argv."""
    model = _check_model(model)
    effort = (reasoning_effort or "").strip().lower() or None

    if vendor == "claude-code":
        argv = [binary, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                "--tools", "", "--no-session-persistence", "--setting-sources", "",
                "--strict-mcp-config", "--disable-slash-commands",
                "--permission-mode", "dontAsk", "--model", model]
        if effort in _EFFORTS_CLAUDE:
            argv += ["--effort", effort]
        # --system-prompt-file works (verified by a real run) but is absent from `claude --help`.
        sys_path = os.path.join(io_dir, "system.txt")
        with open(sys_path, "w", encoding="utf-8") as fh:
            fh.write(system_text or "You are a helpful assistant.")
        argv += ["--system-prompt-file", sys_path]
        return LaunchSpec(argv, stdin_text=transcript)

    if vendor == "codex-cli":
        argv = [binary, "exec", "--json", "--ephemeral", "--skip-git-repo-check", "-s", "read-only",
                "--model", model]
        if effort in _EFFORTS_CODEX:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        argv += ["-c", "features.plugins=false", "-c", "features.apps=false",
                 "-c", "features.shell_tool=false", "-"]
        text = (system_text + "\n\n" if system_text else "") + transcript
        return LaunchSpec(argv, stdin_text=text)

    if vendor == "grok-cli":
        # grok -p reads no stdin: use --prompt-file. No system-prompt file flag exists, so
        # the Odysseus system text rides in the prompt file and the override is a short neutral line.
        prompt_path = os.path.join(io_dir, "prompt.txt")
        with open(prompt_path, "w", encoding="utf-8") as fh:
            fh.write((system_text + "\n\n" if system_text else "") + transcript)
        argv = [binary, "--prompt-file", prompt_path, "--output-format", "streaming-json", "-m", model,
                "--max-turns", "1", "--no-subagents", "--no-plan", "--disable-web-search",
                "--permission-mode", "dontAsk",
                "--disallowed-tools", ",".join(GROK_BUILTIN_TOOLS),
                "--system-prompt-override", GROK_NEUTRAL_SYSTEM]
        if effort in _EFFORTS_OTHER:
            argv += ["--reasoning-effort", effort]
        return LaunchSpec(argv, stdin_text=None)

    raise CliBridgeError(f"Unknown CLI vendor: {vendor}")


# ── Stream parsers ──────────────────────────────────────────────────────────
# Each parser maps one decoded JSON line to a list of events:
#   ("delta", text) ("usage", {"input_tokens":n,"output_tokens":n}) ("model", name)
#   ("tool", name) ("error", message) ("done", None)

Event = Tuple[str, Any]


@dataclass
class ParseState:
    streamed_text: bool = False
    saw_result: bool = False
    model_announced: str = ""


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _claude_usage(u: Dict) -> Dict[str, int]:
    inp = _int(u.get("input_tokens")) + _int(u.get("cache_creation_input_tokens")) + _int(u.get("cache_read_input_tokens"))
    return {"input_tokens": inp, "output_tokens": _int(u.get("output_tokens"))}


def parse_claude(obj: Dict, st: ParseState) -> List[Event]:
    ev: List[Event] = []
    t = obj.get("type")
    if t == "system" and obj.get("subtype") == "init":
        if obj.get("tools"):
            ev.append(("tool", "init advertised tools: " + ",".join(map(str, obj["tools"]))[:80]))
        if obj.get("model"):
            ev.append(("model", obj["model"]))
    elif t == "stream_event":
        e = obj.get("event") or {}
        et = e.get("type")
        if et == "content_block_start":
            cb = e.get("content_block") or {}
            if cb.get("type") in ("tool_use", "server_tool_use", "mcp_tool_use"):
                ev.append(("tool", cb.get("name") or cb.get("type")))
        elif et == "content_block_delta":
            d = e.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                st.streamed_text = True
                ev.append(("delta", d["text"]))
    elif t == "assistant":
        msg = obj.get("message") or {}
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("tool_use", "server_tool_use", "mcp_tool_use"):
                ev.append(("tool", block.get("name") or block.get("type")))
            elif block.get("type") == "text" and not st.streamed_text and block.get("text"):
                ev.append(("delta", block["text"]))
        if msg.get("model"):
            ev.append(("model", msg["model"]))
    elif t == "result":
        st.saw_result = True
        if obj.get("is_error") or str(obj.get("subtype", "success")).startswith("error"):
            ev.append(("error", str(obj.get("result") or obj.get("subtype") or "Claude CLI reported an error")))
        u = obj.get("usage")
        if isinstance(u, dict):
            ev.append(("usage", _claude_usage(u)))
        ev.append(("done", None))
    return ev


_CODEX_TEXT_ITEMS = {"agent_message", "reasoning", "error"}


def parse_codex(obj: Dict, st: ParseState) -> List[Event]:
    ev: List[Event] = []
    t = obj.get("type")
    if t in ("item.started", "item.updated", "item.completed"):
        item = obj.get("item") or {}
        itype = item.get("type")
        if itype not in _CODEX_TEXT_ITEMS:
            ev.append(("tool", itype or "unknown item"))
        elif itype == "agent_message" and t == "item.completed" and item.get("text"):
            ev.append(("delta", item["text"]))
    elif t == "turn.completed":
        u = obj.get("usage") or {}
        ev.append(("usage", {"input_tokens": _int(u.get("input_tokens")), "output_tokens": _int(u.get("output_tokens"))}))
        ev.append(("done", None))
    elif t == "turn.failed":
        err = obj.get("error") or {}
        ev.append(("error", str(err.get("message") or "Codex turn failed")))
    return ev


_GROK_TOOL_EVENTS = {"tool_call", "tool_call_update"}


def parse_grok(obj: Dict, st: ParseState) -> List[Event]:
    ev: List[Event] = []
    t = obj.get("type")
    if t in _GROK_TOOL_EVENTS:
        ev.append(("tool", obj.get("toolName") or obj.get("title") or t))
    elif t == "text" and isinstance(obj.get("data"), str) and obj["data"]:
        ev.append(("delta", obj["data"]))
    elif t == "usage":
        u = obj.get("usage") or {}
        inp = _int(u.get("input_tokens")) + _int(u.get("cache_read_input_tokens")) + _int(u.get("cache_creation_input_tokens"))
        ev.append(("usage", {"input_tokens": inp, "output_tokens": _int(u.get("output_tokens"))}))
    elif t == "end":
        ev.append(("done", None))
    elif t == "error":
        ev.append(("error", str(obj.get("message") or obj.get("data") or "Grok CLI reported an error")))
    return ev


PARSERS: Dict[str, Callable[[Dict, ParseState], List[Event]]] = {
    "claude-code": parse_claude, "codex-cli": parse_codex, "grok-cli": parse_grok,
}


# ── Process handling ────────────────────────────────────────────────────────

_SEMAPHORES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[str, asyncio.Semaphore]]" = weakref.WeakKeyDictionary()


def _semaphore(vendor: str) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    per_loop = _SEMAPHORES.setdefault(loop, {})
    if vendor not in per_loop:
        per_loop[vendor] = asyncio.Semaphore(MAX_CONCURRENT_PER_VENDOR)
    return per_loop[vendor]


async def kill_process_group(proc: asyncio.subprocess.Process, grace: float = 1.0) -> None:
    """SIGTERM the whole group, wait up to ``grace`` seconds, then SIGKILL."""
    if proc.returncode is not None:
        # The leader may be gone while children linger in its group.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except asyncio.TimeoutError:
        pass


def _sse(payload: Dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _sse_error(status: int, text: str) -> str:
    return f'event: error\ndata: {json.dumps({"status": status, "text": text})}\n\n'


def _tail(text: str, limit: int = 600) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


async def _pump_stdin(proc: asyncio.subprocess.Process, text: Optional[str]) -> None:
    if proc.stdin is None:
        return
    try:
        if text:
            proc.stdin.write(text.encode("utf-8"))
            await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass


async def _drain(stream: Optional[asyncio.StreamReader], sink: bytearray, cap: int = 64 * 1024) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return
        sink.extend(chunk)
        if len(sink) > cap:
            del sink[: len(sink) - cap]


async def stream_cli_completion(url: str, model: str, messages: List[Dict], *,
                                timeout: Optional[float] = None,
                                reasoning_effort: Optional[str] = None) -> AsyncIterator[str]:
    """Run the vendor CLI once and yield Odysseus SSE chunks (delta/usage/error/[DONE])."""
    vendor = vendor_for_url(url)
    if vendor is None:
        yield _sse_error(400, "Not a CLI subscription endpoint.")
        yield "data: [DONE]\n\n"
        return
    spec_v = VENDORS[vendor]
    if spec_v.blocked_reason:
        yield _sse_error(501, f"{spec_v.label} is blocked: {spec_v.blocked_reason}")
        yield "data: [DONE]\n\n"
        return
    binary = resolve_binary(vendor)
    if not binary:
        yield _sse_error(503, f"{spec_v.label} CLI is not installed on this host "
                              f"(looked for '{spec_v.binary}'; override with {spec_v.env_override}).")
        yield "data: [DONE]\n\n"
        return

    deadline_s = float(timeout) if timeout else float(DEFAULT_TIMEOUT)
    system_text, transcript = flatten_messages(messages)
    root = tempfile.mkdtemp(prefix="odysseus-cli-")
    work_dir = os.path.join(root, "work")
    io_dir = os.path.join(root, "io")
    os.makedirs(work_dir)
    os.makedirs(io_dir)
    proc: Optional[asyncio.subprocess.Process] = None
    tasks: List[asyncio.Task] = []
    try:
        try:
            spec = build_launch_spec(vendor, binary, model, system_text, transcript, io_dir, reasoning_effort)
        except CliBridgeError as exc:
            yield _sse_error(400, str(exc))
            yield "data: [DONE]\n\n"
            return
        env = build_child_env(extra=spec.env_extra)
        parse = PARSERS[vendor]
        state = ParseState()
        stderr_buf = bytearray()
        stdout_other = bytearray()  # non-JSON stdout lines, for error reporting
        got_done = False
        failure: Optional[str] = None
        usage: Optional[Dict[str, int]] = None
        loop = asyncio.get_running_loop()
        end_at = loop.time() + deadline_s

        async with _semaphore(vendor):
            proc = await asyncio.create_subprocess_exec(
                *spec.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=work_dir, env=env, start_new_session=True,
                limit=_STREAM_LINE_LIMIT,
            )
            tasks = [asyncio.create_task(_pump_stdin(proc, spec.stdin_text)),
                     asyncio.create_task(_drain(proc.stderr, stderr_buf))]
            try:
                while True:
                    remaining = end_at - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                    if not line:
                        break
                    raw = line.decode("utf-8", "replace").strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        stdout_other.extend(raw.encode()[:2000] + b"\n")
                        continue
                    if not isinstance(obj, dict):
                        continue
                    for kind, value in parse(obj, state):
                        if kind == "tool":
                            await kill_process_group(proc)
                            yield _sse_error(502, f"{spec_v.label} CLI tried to use its own tool; Odysseus blocks this.")
                            yield "data: [DONE]\n\n"
                            return
                        if kind == "delta":
                            yield _sse({"delta": value})
                        elif kind == "model" and value != state.model_announced:
                            state.model_announced = value
                            yield _sse({"type": "model_actual", "requested_model": model, "model": value})
                        elif kind == "usage":
                            usage = value
                        elif kind == "error":
                            failure = failure or str(value)
                        elif kind == "done":
                            got_done = True
                try:
                    await asyncio.wait_for(proc.wait(), timeout=max(1.0, end_at - loop.time()))
                except asyncio.TimeoutError:
                    raise
            except asyncio.TimeoutError:
                await kill_process_group(proc)
                yield _sse_error(504, f"{spec_v.label} CLI timed out after {int(deadline_s)} seconds.")
                yield "data: [DONE]\n\n"
                return
            finally:
                if proc.returncode is None:
                    await asyncio.shield(kill_process_group(proc))
            # Let the stderr drain finish (pipe is at EOF once the process exits).
            await asyncio.gather(*tasks, return_exceptions=True)

        if failure or (proc.returncode not in (0, None)) or not got_done:
            if failure is None:
                err = stderr_buf.decode("utf-8", "replace").strip() or stdout_other.decode("utf-8", "replace").strip()
                if proc.returncode == 0 and got_done:
                    err = ""
                failure = err or f"{spec_v.label} CLI exited with code {proc.returncode} without a reply."
            yield _sse_error(502, _tail(_redact(failure, env)))
            yield "data: [DONE]\n\n"
            return
        if usage:
            yield _sse({"type": "usage", "data": usage})
        yield "data: [DONE]\n\n"
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        shutil.rmtree(root, ignore_errors=True)


# ── Probing ─────────────────────────────────────────────────────────────────

async def _run_quick(argv: List[str], timeout: float = 4.0) -> Tuple[Optional[int], str]:
    """Run a short status command with a hard cap. Returns (exit code or None, stdout)."""
    root = tempfile.mkdtemp(prefix="odysseus-cli-probe-")
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, cwd=root, env=build_child_env(), start_new_session=True,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace")
    except (asyncio.TimeoutError, OSError):
        if proc is not None:
            await kill_process_group(proc)
        return None, ""
    finally:
        shutil.rmtree(root, ignore_errors=True)


def parse_grok_models(text: str) -> List[str]:
    models = []
    for line in text.splitlines():
        m = re.match(r"^\s*[*-]\s+([A-Za-z0-9][A-Za-z0-9._:/\-]*)", line)
        if m:
            models.append(m.group(1))
    return models


def codex_models_from_cache(path: Optional[str] = None) -> List[str]:
    """Visible model slugs only; reads the public models cache, never auth.json."""
    path = path or os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"), "models_cache.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [m["slug"] for m in data.get("models", [])
                if isinstance(m, dict) and m.get("visibility") == "list" and isinstance(m.get("slug"), str)]
    except (OSError, ValueError, AttributeError, KeyError):
        return []


def parse_agy_models(text: str) -> List[str]:
    """`agy models` prints '<id>\\t<display name>' per model after a progress line."""
    return [line.split("\t", 1)[0].strip() for line in text.splitlines() if "\t" in line and line.split("\t", 1)[0].strip()]


async def probe_vendor(vendor: str) -> Dict[str, Any]:
    v = VENDORS[vendor]
    out: Dict[str, Any] = {
        "vendor": vendor, "label": v.label, "base_url": v.base_url, "experimental": v.experimental,
        "installed": False, "version": None, "blocked_reason": v.blocked_reason, "logged_in": False, "models": list(v.default_models),
        "detail": "",
    }
    binary = resolve_binary(vendor)
    if not binary:
        out["detail"] = f"'{v.binary}' not found"
        return out
    code, text = await _run_quick([binary, "--version"])
    if code != 0:
        out["detail"] = "version check failed or timed out"
        return out
    out["installed"] = True
    out["version"] = _tail(text.splitlines()[0] if text.strip() else "", 80) or None

    if vendor == "claude-code":
        code, text = await _run_quick([binary, "auth", "status"])
        try:
            info = json.loads(text)
            out["logged_in"] = bool(info.get("loggedIn"))
            out["plan"] = info.get("subscriptionType")  # plan name only; email/org are never returned
        except (ValueError, AttributeError):
            pass
    elif vendor == "codex-cli":
        code, _ = await _run_quick([binary, "login", "status"])
        out["logged_in"] = code == 0
        models = codex_models_from_cache()
        if models:
            out["models"] = models
    elif vendor == "grok-cli":
        code, text = await _run_quick([binary, "models"])
        out["logged_in"] = "you are logged in" in text.lower()
        models = parse_grok_models(text)
        if models:
            out["models"] = models
    elif vendor == "antigravity":
        # `agy models` lists models only for a signed-in account (no usage spent). Short cap: it calls the network.
        code, text = await _run_quick([binary, "models"], timeout=15.0)
        models = parse_agy_models(text)
        out["logged_in"] = code == 0 and bool(models)
        if models:
            out["models"] = models
        out["blocked_reason"] = v.blocked_reason
    if not out["logged_in"]:
        out["detail"] = "not signed in; run the CLI's own login on the host"
    return out
