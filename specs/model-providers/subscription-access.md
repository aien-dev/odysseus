# Subscription Access Research

Last updated: dev@a8c147b2 | 2026-10-13

## Scope

How Odysseus could power chats from a consumer subscription (flat monthly plan) instead of per-token
API keys: OpenAI (ChatGPT Plus/Pro), Anthropic (Claude Pro/Max), xAI (SuperGrok / X Premium+), Google
(Google AI Pro/Ultra). Research only, no code changed. Primary sources only. Tags: `[L]` ran locally
2026-10-13, `[D]` vendor doc, `[S]` vendor source code, `[X]` secondary or search-tool summary (weak),
UNVERIFIED = not confirmed. Exercised locally: `claude` 2.1.296, `codex` 0.162.1, `grok` 1.0.52.
`gemini` is not installed, so every Gemini item is doc or source only.

Today's Odysseus path: `src/chatgpt_subscription.py` runs the Codex device flow with Codex's own client
ID and calls `https://chatgpt.com/backend-api/codex` (see [chatgpt-subscription.md](chatgpt-subscription.md)).
Odysseus is AGPL-3.0 and self-hosted (`LICENSE`).

## Summary

| Vendor | Official path for a third-party program | Terms verdict | Headless interface | Host tools via MCP | ACP | Auth check (no spend) | API-key override risk |
|---|---|---|---|---|---|---|---|
| OpenAI | "Sign in with ChatGPT" (SIWC) open-source flow: own OAuth, `https://api.openai.com/v1/responses` [D1] | (a) ALLOWED for open-source / locally hosted apps via SIWC [D1]. Codex client ID + chatgpt.com backend (today) is covered by no OpenAI text, UNVERIFIED. (b) ALLOWED [D18] | `codex exec --json` | `-c mcp_servers.<id>.*`; app-server `dynamicTools` (experimental) | Community `codex-acp` adapter only | `codex login status` (exit 0/1) | `CODEX_API_KEY` beats login [S] |
| Anthropic | None for third parties; official `claude` binary only [D7] | (a) PROHIBITED [D7][D8]. (b) end user signing in to the unmodified binary not blocked [D7]; hosted / on-behalf-of use is | `claude -p --output-format stream-json --verbose` | `--mcp-config` + `--strict-mcp-config` | Community `claude-agent-acp` adapter (Agent SDK) only | `claude auth status` (exit 0/1, JSON) | `ANTHROPIC_API_KEY` always wins in `-p` [D9]; `--bare` ignores login |
| xAI | None documented; `grok` CLI is offered to SuperGrok / X Premium+ [D14] | (a) UNVERIFIED (no program found; ToS pages blocked, HTTP 403). (b) vendor-endorsed: headless `-p` and "full ACP support to build your own bots" [D14] | `grok -p --output-format streaming-json` | `.mcp.json` / `grok mcp add`; ACP `session/new.mcpServers` | Native: `grok agent stdio` | `grok models` prints a login line (exit 0 either way) | Stored login beats `XAI_API_KEY` [D13] |
| Google | None for third parties [D10] | (a) PROHIBITED, with suspension language [D10]. (b) not addressed; third-party ACP hosts (Zed, JetBrains) documented [D11]; UNVERIFIED for Odysseus | `gemini -p -o stream-json` | `settings.json` `mcpServers`; ACP | Native: `gemini --acp` | No status command found | Saved login beats `GEMINI_API_KEY` in headless [S] |

## OpenAI (ChatGPT Plus/Pro)

### Terms

- OpenAI runs an official program for this case. [D1]: "ChatGPT plan usage is an optional capability
  within Sign in with ChatGPT ... your open-source app can request permission to use the user's ChatGPT
  plan for eligible Responses API requests." Also: "These docs explain ChatGPT plan usage for open-source
  and locally hosted apps. If you're interested in offering it in a paid or remotely hosted app, complete
  the interest form."
- [D2]: "ChatGPT plan usage is available to all open-source partners and selected private clients"; users
  are "Eligible ChatGPT Plus and Pro users". The OSS flow "needs neither a client secret nor a partner API
  key" and registers with `client_id=dynamic_agent_client` plus an `ext_agent_host_id` [D3].
- The same docs contradict Odysseus's current endpoint [D4]: "do not point it at ChatGPT's `backend-api`
  endpoints."
- App-server docs [D5]: "If you've built a local or open-source application using Codex app-server
  authentication, you can continue using it, though we recommend migrating to Sign in with ChatGPT ...
  App-server authentication has never been permitted for commercial or hosted services."
- No OpenAI source found authorizes a third party to reuse the Codex client ID
  (`app_EMoamEEZ73f0CkXaXp7hrann`, `openai/codex` `codex-rs/login/src/auth/manager.rs` [S]) against
  `chatgpt.com/backend-api/codex`. Gray, UNVERIFIED (about 60 percent tolerated for local use, 90 percent
  not blessed).
- Enforcement: no ban policy stated in the pages read. The Help Center page (HTTP 403 to fetch; search
  summary only [X]) says each app gets a weekly cap under Settings > Usage and mentions the Go plan: UNVERIFIED.

### (a) Direct OAuth, documented way [D3][D4][D6][D12]

- Authorization code + PKCE at `https://auth.openai.com/api/accounts/authorize`, loopback redirect
  `http://127.0.0.1:<port>/auth/callback`, scopes `openid profile email offline_access resource.invoke
  chatgpt.tokens.use.direct`, `resource=https://api.openai.com/v1`; token endpoint
  `https://auth.openai.com/api/accounts/oauth/token`. Access token 1 hour; refresh token 30 days, rotating.
- Inference: `POST https://api.openai.com/v1/responses`, bearer token, `store:false`, `stream:true`; models via
  `GET https://api.openai.com/v1/models`. Needs a "Continue with ChatGPT" button with approved branding.
- Limits [D6]: no `previous_response_id` over HTTP (send full `input`); omit `max_output_tokens`, `temperature`,
  `top_p`, `metadata`, `user` and others; system-role items rejected (use `instructions` or developer
  messages); image generation, file search, Code Interpreter, hosted MCP, `tool_search` unsupported; function
  tools and images supported. Quota errors arrive as `response.failed` with
  `subscription_sharing_usage_limit_exceeded`.
- Self-hosted VM: finish OAuth on the user's machine, copy the credential file; host-level revocation is not
  yet available ([D3] `/self-hosted-vms`).

### (b) CLI subprocess

- One shot: `codex exec --json --ephemeral --skip-git-repo-check -s read-only -C <dir> "<prompt>"` [L]. If stdin
  is piped and a prompt is given, stdin is appended as a `<stdin>` block, so run with `</dev/null`.
- Model `-m`; effort `-c model_reasoning_effort="low"`; images `-i FILE`; system prompt `-c
  model_instructions_file="<path>"` (replaces built-in instructions); isolation `--ignore-user-config
  --ignore-rules`; `--output-schema` ([D15], `codex exec --help` [L]).
- Tools: no single "no tools" flag. `-c features.shell_tool=false` and `-c web_search="disabled"` were accepted
  [L]. `-c tools.view_image=false` is documented [D15] but 0.162.1 printed "`tools.view_image` is ignored";
  `features.view_image` exists in `codex features list` (untested).
- Not a plain model [L]: "Reply with the word ok" cost 9415 input tokens with shell off and a one-line
  instructions file; `codex debug prompt-input` shows extra developer-role blocks (skills, permissions,
  collaboration mode).
- History/persistence/cancel: `codex exec resume <id>|--last`; app-server `thread/inject_items` appends Responses
  items; `--ephemeral` skips disk; app-server `turn/interrupt`, or kill the process ([D5]).
- Host tools: `-c 'mcp_servers.odysseus.command="..."'` (or `.url`, `.enabled_tools`) [D15]; app-server
  `thread/start.dynamicTools` + `item/tool/call` (experimental) [D5]. `codex mcp-server` is gone: "have been
  removed. Use the Codex app server" [D16]. App-server listens on `stdio://` (default), `unix://`, `ws://`;
  WebSocket is "experimental and unsupported" [D5]. Its `account/login/start` types: `chatgpt`,
  `chatgptDeviceCode`, `apiKey`, experimental `chatgptAuthTokens` (host supplies the token).

Real smoke output [L] (`--json`, empty temp dir, no API-key env, 5 of 6 lines):

```
{"type":"thread.started","thread_id":"<uuid>"}
{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Codex is ignoring 1 unrecognized configuration setting ... `tools.view_image` is ignored."}}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"ok"}}
{"type":"turn.completed","usage":{"input_tokens":9415,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0}}
```

`item.type:"error"` items can be config warnings only. The stream does not report the auth mode.

### Auth state and quota

- Login: `codex login` (browser), `--device-auth`, `--with-api-key`, `--with-access-token` (stdin).
- Store: `~/.codex/auth.json` (`CODEX_HOME` relocates) or OS keyring via `cli_auth_credentials_store =
  file|keyring|auto|ephemeral` [D17]. JSON keys [L]: `OPENAI_API_KEY, auth_mode, last_refresh,
  tokens{access_token, account_id, id_token, refresh_token}`; `auth_mode` is `chatgpt` here.
- Check: `codex login status` -> "Logged in using ChatGPT", exit 0; with an empty `CODEX_HOME` -> "Not logged
  in", exit 1 [L]. App-server: `account/read`.
- Override: `CODEX_API_KEY` is checked first and "takes precedence over any other auth method" (`load_auth`
  in `manager.rs` [S]); documented for exec, review, TS SDK [D18]. `OPENAI_API_KEY` only prefills onboarding and
  feeds a few side features in the files found [S].
- Quota: app-server `account/rateLimits/read` and `account/rateLimits/updated` give `usedPercent`,
  `windowDurationMins`, `resetsAt` per bucket (`limitId` e.g. `codex`) [D5]. `exec --json` has token counts only.
  Odysseus already reads unofficial `chatgpt.com/backend-api/wham/usage`.

## Anthropic (Claude Pro/Max)

### Terms

- [D7] (Legal and compliance, "Authentication and credential use"): "OAuth authentication is intended
  exclusively for purchasers of Claude Free, Pro, Max, Team, and Enterprise subscription plans and is designed
  to support ordinary use of Claude Code and other native Anthropic applications."
- [D7]: "Anthropic does not permit third-party developers to offer Claude.ai login into their own applications,
  or to route requests through Free, Pro, or Max plan credentials on behalf of their users. Moreover,
  developers may not collect, store, or intermediate Claude.ai credentials or session tokens."
- [D8] (Agent SDK overview): "Unless previously approved, Anthropic does not allow third party developers to
  offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK."
- (b) [D7]: "Nor does it prevent an end user from signing in to the unmodified Claude Code binary with their own
  Claude subscription, including where a platform hosts Claude Code". Also "Advertised usage limits for Pro and
  Max plans assume ordinary, individual usage". Consumer Terms [D19] (copy fetched shows "Effective October 8,
  2025") bar "automated or non-human means, whether through a bot, script, or otherwise" except via an API key
  "or where we otherwise explicitly permit it", and bar sharing accounts. So (b) fits only a human owner
  running their own binary; one login serving many Odysseus users does not.
- Enforcement: [D7] "Anthropic reserves the right to take measures to enforce these restrictions and may do so
  without prior notice." Reports of 2026 bans or a February 2026 policy change are secondary [X]: UNVERIFIED.

### Headless interface (verified [L])

```
claude -p "<prompt>" --output-format stream-json --verbose --tools "" --no-session-persistence \
  --system-prompt "<text>" --model sonnet --setting-sources "" --strict-mcp-config --disable-slash-commands
```

- Streaming: `--include-partial-messages` (token deltas). Streaming input: `--input-format stream-json` (queued
  messages, interrupts, base64 image blocks [D20]). Model `--model <alias|id>`; effort `--effort
  low|medium|high|xhigh|max`; system prompt `--system-prompt[-file]` (replace) or `--append-system-prompt[-file]`;
  `--json-schema`, `--max-turns`, `--fallback-model`.
- All tools off: `--tools ""` [D21]; init showed `"tools":[]`, `"mcp_servers":[]` [L].
- Sessions: `--no-session-persistence`; `--resume <id>`, `--continue`, `--session-id <uuid>`, `--fork-session`.
  History: resume, stream-json input, or replay as text.
- Do not use `--bare`: help says "Anthropic auth is strictly ANTHROPIC_API_KEY or apiKeyHelper ... (OAuth and
  keychain are never read)" [L][D9].
- Timeout/cancel: no timeout flag; kill the process or send an interrupt on stream-json input (init lists
  `interrupt_receipt_v1` [L]).
- Host tools: `--mcp-config <json|file>` + `--strict-mcp-config`, allow with `--allowedTools "mcp__<server>__*"`;
  `--permission-prompts none` auto-denies prompts.

Real smoke output [L] (4 lines, IDs and long fields shortened; Max plan, no API-key env):

```
{"type":"system","subtype":"init","session_id":"<uuid>","tools":[],"mcp_servers":[],"model":"claude-sonnet-5-5","apiKeySource":"none","claude_code_version":"2.1.296",...}
{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","rateLimitType":"five_hour","overageStatus":"rejected","isUsingOverage":false,"unifiedWindows":{"five_hour":{"utilization":0.1,"resetsAt":1791897600},"seven_day":{"utilization":0.58,"resetsAt":1792252800}}},...}
{"type":"assistant","message":{"model":"claude-sonnet-5-5","role":"assistant","content":[{"type":"text","text":"ok"}],"usage":{"input_tokens":2,"cache_creation_input_tokens":564,"output_tokens":4}},"session_id":"<uuid>",...}
{"type":"result","subtype":"success","is_error":false,"result":"ok","num_turns":1,"terminal_reason":"completed","total_cost_usd":0.0024,"usage":{...},"modelUsage":{"claude-haiku-5-5":{...},"claude-sonnet-5-5":{...}},...}
```

`apiKeySource:"none"` plus `claude auth status` (`authMethod:"claude.ai"`, `subscriptionType:"max"`) show the
subscription was used. `total_cost_usd` is a list-price estimate, not a bill. A side call to a Haiku model also ran.
`rate_limit_event` is typed `SDKRateLimitEvent` in the SDK reference [D22].

### Server modes, auth state, quota

- Agent SDK (TS/Python) spawns the same binary; the rule in [D8] covers products built on it. Anthropic docs never
  mention ACP (index grep: 0 hits [L]). `agentclientprotocol/claude-agent-acp` (Apache-2.0, "Use Claude Agent SDK
  from any ACP client", images and client MCP supported) is a community adapter.
- Login: `claude auth login [--claudeai|--console]`; `claude setup-token` for a long-lived token. Store: Linux
  `~/.claude/.credentials.json` (0600), macOS Keychain; `CLAUDE_CONFIG_DIR` relocates [D9]. JSON keys [L]:
  `claudeAiOauth{accessToken, expiresAt, rateLimitTier, refreshToken, refreshTokenExpiresAt, scopes,
  subscriptionType}, mcpOAuth`.
- Check: `claude auth status --json` -> `loggedIn, authMethod, apiProvider, subscriptionType`; "Exits with code 0 if
  logged in, 1 if not" [D21], confirmed with an empty config dir [L]. Output also holds email and org id: never log it.
- Override [D9], precedence: cloud-provider vars, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY` ("In non-interactive
  mode (-p), the key is always used when present"), `apiKeyHelper`, `CLAUDE_CODE_OAUTH_TOKEN`, then the subscription
  login. A stray API key silently turns subscription chat into per-token billing.
- Quota: `rate_limit_event.unifiedWindows.{five_hour,seven_day}.{utilization,resetsAt}` in the stream [L]; `/usage`
  interactively [D23]. No standalone command found.

## xAI (SuperGrok / X Premium+)

### Terms

- [D14] (Grok Build launch post): "Now in early beta for all SuperGrok and X Premium Plus subscribers." "Headless
  mode (-p) allows easily running agents inside scripts and automations. Grok Build also provides full ACP support
  to build your own bots and agent orchestration apps."
- No xAI text found on third-party use of the subscription login or its OAuth tokens. `x.ai/legal/*` returned HTTP 403
  (Cloudflare) to every fetch, so the Consumer Terms and Acceptable Use Policy were NOT read; a search summary says the
  AUP lists using bots to access the service [X], UNVERIFIED. (a): no program found, treat as not allowed. (b):
  endorsed by [D14]. Enforcement: nothing found.
- Usage: "one shared weekly usage pool" across Chat, Imagine, Voice, Build and API, viewed at Settings > Usage [D13].

### Headless interface (bundled docs `~/.grok/docs/user-guide/`, flags from `grok --help` [L])

```
grok -p "<prompt>" --output-format streaming-json -m grok-4.7 --effort low --system-prompt-override "<text>" \
  --max-turns 1 --no-subagents --no-plan --disable-web-search
```

- Formats: `plain | json | streaming-json | streaming-messages-json`. The last is "NDJSON in the Anthropic Messages API
  wire format" (`system/init`, `assistant`, `user`, `result`; `--include-partial-messages` adds `content_block_delta`
  framing), so one parser can serve Claude and Grok [D24].
- Tools: `--tools` (allowlist), `--disallowed-tools` (denylist, internal ids like `run_terminal_cmd`). **`--tools ""` did
  NOT disable tools** [L]: the stream listed 24+ built-ins (`run_terminal_command, read_file, search_replace, grep`...)
  plus the user's own MCP tools, and the prompt cost 17915 input tokens. Try an explicit `--disallowed-tools` list (untested).
- Inputs: `-p`, `--prompt-file`, `--prompt-json` (content blocks); stdin is NOT read in headless [D24]. Sessions: `-r`,
  `-c`, `-s <new uuid>`, `--fork-session`. Effort: `none|minimal|low|medium|high|xhigh|max`.
- Host tools: no per-run `--mcp-config`; use `.mcp.json` in the cwd, `grok mcp add`, or ACP `session/new.mcpServers` [D25].
- ACP: `grok agent stdio`; `grok agent serve --bind 127.0.0.1:2419 --secret <t>` (WebSocket). `session/new` `_meta`:
  `systemPromptOverride`, `rules`, `yoloMode`; `session/set_config_option` for `model`, `reasoning_effort` [D25]. Grok is
  absent from the agentclientprotocol.com agent list at fetch time, but xAI's docs state ACP support.

Real smoke output [L] (12 lines; the first line repeated 9 times; tool list cut):

```
{"type":"available_commands","tools":["run_terminal_command","read_file","search_replace","list_dir","grep",...],"commands":[...]}
{"type":"text","data":"ok"}
{"type":"usage","usage":{"input_tokens":17915,"output_tokens":27,"cache_read_input_tokens":1152,"reasoning_tokens":26},"signature":"..."}
{"type":"end","stopReason":"end_turn","sessionId":"<uuid>","cwd":"/tmp/...","num_turns":1,"total_cost_usd":0.0124,"modelUsage":{"grok-4.7-build":{...}}}
```

`grok models` reported "You are logged in with grok.com" (OAuth). `total_cost_usd` appeared on the OAuth login, so it does
not reveal API-key billing.

### Auth state

- Login: `grok login [--oauth | --device-auth]`, `grok logout`. Store `~/.grok/auth.json` (0600; `GROK_HOME` relocates
  [L]); JSON keyed by `https://auth.x.ai::<id>` with `auth_mode` (`oidc`), `key`, `refresh_token`, `expires_at`, `email`,
  `team_id`, `user_id` and more [L].
- Check: `grok models` prints "You are logged in with grok.com." or "You are not authenticated." but exits 0 either way
  [L]; parse the text, or test `expires_at` (read that field only).
- Override [D13]: "Grok uses the API key as a fallback when no session token is active. If you have already signed in
  interactively, the stored session token takes precedence." Risk exists only when logged out. In
  `streaming-messages-json`, `init.apiKeySource` is `user` for API key, `oauth` otherwise [D24].
- Quota: Settings > Usage on the web; `grok usage <session>` is per-session tokens and cost. No remaining-quota read found.

## Google (Google AI Pro/Ultra via Gemini CLI)

### Terms

- [D10] (`docs/resources/tos-privacy.md` in `google-gemini/gemini-cli`, mirrored at geminicli.com): "Directly accessing
  the services powering Gemini CLI (for example, the Gemini Code Assist service) using third-party software, tools, or
  services (for example, using OpenClaw with Gemini CLI OAuth) is a violation of applicable terms and policies. Such
  actions may be grounds for suspension or termination of your account."
- (a) is explicitly prohibited, with vendor-stated enforcement. (b) is not addressed: launching the unmodified official
  CLI is not "directly accessing the services", and Google documents Zed and JetBrains hosting `gemini --acp` [D11].
  For Odysseus: plausible, UNVERIFIED (about 60 percent). AI Pro/Ultra access runs through "Gemini Code Assist with
  Google AI Pro or Ultra subscription" [D10]. Daily caps in the repo's table: Pro 1,500, Ultra 2,000 requests [D26] (may be stale).

### Headless interface (docs and source only; not run)

- `gemini -p "<prompt>" -o stream-json -m <auto|pro|flash|flash-lite|id>` [D27]. Events ([S]
  `packages/core/src/output/types.ts`): `init{session_id,model}`, `message{role,content,delta}`,
  `tool_use{tool_name,tool_id,parameters}`, `tool_result{tool_id,status,output|error}`, `error{severity,message}`,
  `result{status,stats{total_tokens,input_tokens,output_tokens,cached,input,duration_ms,tool_calls,models}}`, each with
  `timestamp`. Exit codes 0, 1, 42 (input), 53 (turn limit).
- System prompt: env `GEMINI_SYSTEM_MD=/abs/path.md` is "a full replacement, not a merge" [D28]. Tools off: no CLI flag;
  `tools.core` (allowlist) and `tools.exclude` in `settings.json` [D29], empty-array behavior UNVERIFIED; isolate config
  with `GEMINI_CLI_HOME`. Resume `-r latest|<index>`. Persistence-off, image input, effort flag: UNVERIFIED. Host tools:
  `settings.json` `mcpServers` (no `--mcp-config` found) or ACP.
- ACP: `gemini --acp` (`--experimental-acp` is deprecated, `packages/cli/src/config/config.ts` [S]; `cli-reference.md` still
  lists only the old flag). Methods `initialize, authenticate, newSession, loadSession, prompt, cancel, setSessionMode,
  unstable_setSessionModel`; client MCP servers become model tools [D11].

### Auth state

- Login: run `gemini`, pick "Sign in with Google"; "Your credentials will be cached locally" [D30]. File
  `~/.gemini/oauth_creds.json` (`OAUTH_FILE` [S]); `GEMINI_CLI_HOME` relocates. Headless reuses the cached login [D30].
  Keychain storage: UNVERIFIED. Check: no status command found (UNVERIFIED); only a real prompt proves it.
- Override [S]: headless uses `configuredAuthType || getAuthTypeFromEnv()` (`validateNonInterActiveAuth.ts`); env order
  `GOOGLE_GENAI_USE_GCA=true` (login), `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_GEMINI_BASE_URL`, `GEMINI_API_KEY`
  (`contentGenerator.ts`). A saved `security.auth.selectedType` wins, so an API key bites only when none is saved. Scrub the
  key vars and set `GOOGLE_GENAI_USE_GCA=true`.
- Quota: `/stats` interactively; headless `result.stats` has tokens only. No remaining-quota API found.

## ACP as one integration

Agent Client Protocol (JSON-RPC 2.0 over stdio, agentclientprotocol.com): `initialize`, `session/new` (cwd, `mcpServers`),
`session/prompt`, streamed `session/update` (`agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update`,
`plan`), `session/cancel`, permission requests, config options.

| Vendor | ACP command | Who ships it |
|---|---|---|
| Google | `gemini --acp` | Vendor, native |
| xAI | `grok agent stdio` | Vendor, native |
| OpenAI | `npx @agentclientprotocol/codex-acp` (wraps `codex app-server`) | Community adapter; OpenAI docs never mention ACP [L] |
| Anthropic | `npx @agentclientprotocol/claude-agent-acp` (wraps Claude Agent SDK) | Community adapter; Anthropic docs never mention ACP [L] |

Assessment: one client would give one transport, sessions, host MCP injection and cancel for all four, and Gemini and Grok
need no extra software. Against: ACP targets editors (agent expects fs/terminal capabilities and approval prompts), every
agent is a full coding agent, two of four need Node packages outside vendor control, and the Claude adapter sits on the Agent
SDK, the product Anthropic's rule targets [D8]. For OpenAI, `codex app-server` is the vendor-owned interface and is richer.

## Recommended integration for Odysseus

| Option | OpenAI | Anthropic | xAI | Google | Build cost |
|---|---|---|---|---|---|
| Direct OAuth | Allowed via SIWC | Prohibited | No program | Prohibited | Medium, reuses Responses client |
| CLI subprocess (headless stream) | OK | Conditional (single owner) | Endorsed | Likely, UNVERIFIED | Low per CLI; agent, not plain model |
| ACP client | Via adapter | Via adapter, risky | Native | Native | Medium once, cheap after |

1. OpenAI: replace the Codex-client-ID flow with the documented Sign in with ChatGPT open-source flow (own dynamic client,
   `api.openai.com/v1/responses`). It is the only direct-OAuth route any of the four vendors blesses, it acts as a plain model,
   and Odysseus keeps its own tool loop. Keep the old path behind a warning until the new one ships.
2. Anthropic, Google, xAI: one "agent CLI" provider kind that runs the user's own official CLI, one process per turn, headless
   JSON stream, host tools through Odysseus MCP servers. Claude and Grok can share a Messages-style parser. Label it as an agent
   (own tool loop, large injected prompt: 564 to 17915 input tokens for "ok" [L]) and limit it to single-owner installs.
3. ACP later, for Gemini and Grok (native, vendor-owned), once the headless bridge exists. Skip the community Claude adapter.

Guardrails for item 2: never read, copy or log token files; per-user config dirs (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
`GROK_HOME`, `GEMINI_CLI_HOME`); scrub `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_*`, `CODEX_API_KEY`,
`XAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY` from the child env; check login first (`claude auth status`, `codex login
status`); never pass `--bare` to Claude; close stdin for Codex; kill the process on cancel.

## Open questions / UNVERIFIED

1. Does OpenAI accept Odysseus (AGPL, self-hosted web server, possibly multi-user) as an "open-source and locally hosted"
   SIWC app without the partner form? [D31] says SIWC is "currently offered to a select group of commercial partners"; the OSS
   pages describe no waitlist. Needs one real dynamic registration.
2. Is today's Codex-client-ID + `chatgpt.com/backend-api/codex` path (and `wham/usage`) tolerated? No OpenAI text covers it.
3. Anthropic: is an Odysseus-launched `claude -p` "ordinary, individual usage" or "routing on behalf of users"? Only [D7]
   sentences exist; ask through the sales contact that page names.
4. xAI Consumer Terms and AUP unread (403); unknown whether the subscription may back a third-party harness; whether
   `--disallowed-tools` fully removes built-ins (`--tools ""` did not).
5. Gemini: nothing run. Unknown: tool-off recipe, image input, effort, persistence-off, keychain storage, login check, (b).
6. Codex: which tools stay in the request after `features.shell_tool=false` (9415 input tokens unexplained).
7. Secondary-only claims not relied on: February 2026 enforcement waves; OpenAI Help Center details (403).

## Sources

- [D1] https://developers.openai.com/siwc/token-sharing-open-source ; [D2] https://developers.openai.com/siwc/quickstart
- [D3] https://developers.openai.com/siwc/token-sharing-open-source/sign-in (and `/self-hosted-vms`) ; [D4] `/models-and-inference`
- [D5] https://learn.chatgpt.com/docs/app-server (section "Auth endpoints") ; [D6] `/preview-limitations` ; [D12] `/token-reference`
- [D31] https://developers.openai.com/siwc/request-client-id
- [D7] https://code.claude.com/docs/en/legal-and-compliance ; [D8] https://code.claude.com/docs/en/agent-sdk/overview
- [D9] https://code.claude.com/docs/en/authentication and `/headless` ; [D21] `/cli-reference` ; [D22] `/agent-sdk/typescript`
  ; [D20] `/agent-sdk/streaming-vs-single-mode` ; [D23] `/costs` (all under code.claude.com/docs/en)
- [D19] https://www.anthropic.com/legal/consumer-terms
- [D10] https://github.com/google-gemini/gemini-cli/blob/main/docs/resources/tos-privacy.md and https://geminicli.com/docs/resources/tos-privacy/
- [D11] gemini-cli `docs/cli/acp-mode.md` ; [D26] `docs/resources/quota-and-pricing.md` ; [D27] `docs/cli/cli-reference.md` ;
  [D28] `docs/cli/system-prompt.md` ; [D29] `docs/reference/configuration.md` ; [D30] `docs/get-started/authentication.mdx`
- [D13] https://docs.x.ai/grok/faq and `~/.grok/docs/user-guide/02-authentication.md` ; [D14] https://x.ai/news/grok-build-cli ;
  [D24] `14-headless-mode.md` ; [D25] `15-agent-mode.md`
- [D15] https://learn.chatgpt.com/docs/config-file/config-reference ; [D16] https://learn.chatgpt.com/docs/codex-sdk ;
  [D17] https://learn.chatgpt.com/docs/auth ; [D18] https://learn.chatgpt.com/docs/non-interactive-mode
- ACP: https://agentclientprotocol.com/overview/agents ; https://github.com/agentclientprotocol/codex-acp ;
  https://github.com/agentclientprotocol/claude-agent-acp
- [S] `openai/codex` `codex-rs/login/src/auth/manager.rs`; `google-gemini/gemini-cli` `packages/cli/src/validateNonInterActiveAuth.ts`,
  `packages/core/src/core/contentGenerator.ts`, `packages/cli/src/config/config.ts`, `packages/core/src/output/types.ts`
- [X] Search-tool summaries, not relied on: help.openai.com article 20001542 (403), press coverage.

## Implementation (dev branch feat/cli-subscription-bridges)

Built 2026-10-13. Files: `src/cli_subscription.py`, `routes/cli_subscription_routes.py` (admin only, `/api/cli-subscription`: `GET /status`, `POST /{vendor}/connect`, `DELETE /{vendor}`), hooks in `src/llm_core.py` (provider `cli-subscription`, streaming, async collect, sync worker-thread path) and `src/model_pricing.py` (not cost tracked), friendly labels in `static/js/providers.js`, tests in `tests/test_cli_subscription.py`. The settings page has no "Connect via local CLI" buttons yet; the API is ready for them.

Design: reserved URLs `http://cli-bridge.invalid/<claude-code|codex-cli|grok-cli|antigravity>`. Each call runs the owner's signed-in CLI as a plain text model from an empty temp directory, long text via files or stdin, API-key variables scrubbed from the child environment, process group killed on timeout or cancel, two concurrent calls per vendor. Any stream event showing the CLI using its own tool kills the process and returns an error. Odysseus never reads a token file. Login is read from `claude auth status`, `codex login status`, `grok models`, `agy models`.

Verified by real runs ("Reply with the word ok", tools off):
- Claude (haiku): reply `ok`, 564 input tokens (cache-counted), init showed `tools:[]`. `--system-prompt-file` works although `claude --help` does not list it.
- Codex (gpt-6-luna): reply `ok`, 14899 input tokens (Codex's own base prompt). The stream has no token deltas, one `agent_message` item per turn.
- Grok (grok-4.7): reply `ok`, 11021 input tokens. `--tools ""` does not disable tools. `--disallowed-tools <ids>` removed all built-in tools (input tokens 19458 down to 9842 with a short system override), but MCP tools from the owner's own Grok config stay advertised, so `--permission-mode dontAsk` and the fail-closed `tool_call` check are the guards. A real tool run showed event type `tool_call`.

Antigravity (`agy` 1.3.4, replaces the Gemini CLI vendor): BLOCKED. `agy` print mode has no flag to turn tools off and ran `run_command` and `write_to_file` unprompted, even with `--mode plan`. It has no ACP mode and the ACP server T3 Code uses is not installed. The vendor is registered (probe lists models) but connect and launch are refused until a tools-off route exists. Prompt input via `--input-format stream-json` is UNVERIFIED.

UNVERIFIED: Codex and Grok behaviour on non-success exits beyond the fake-CLI tests; Claude `--permission-mode dontAsk` effect (accepted by the CLI, not exercised). Gemini CLI is not built.
