# Ghost Brain — Gemini Web → OpenAI-compatible API Bridge

> ⚠️ **Notice**: This README has been auto-generated from a Persian original and may contain Persian language sections in code examples, comments, or descriptions. A fully refined English version will be published shortly.

A local proxy that converts your Gemini Web account(s) into a fully OpenAI-compatible API
(for Claude Code, Roo/Cline, Open WebUI, AutoClaw/OpenClaw, curl). No Google API Key required —
uses your own browser and account.

## Features

- **Token Pool**: Multiple browser profiles (multiple accounts) with round-robin and automatic failover.
- **Bean's Brain**: System prompt injection, tool-call bridging, session persistence, long message compression.
- **Dynamic Model Discovery**: Models are discovered from the account itself; `/v1/models` and the UI display
  the real list of available models for that account every time (not a static, outdated list).
- **External Model Routing**: Any model (e.g., `gpt-*` or a local LLM) can be forwarded to an external
  OpenAI-compatible API using `ghost_routes.json` — both streaming and non-streaming, with parameter pass-through.
- **Tool Calls Support**: If Gemini generates tool JSON blocks, they are returned as standard
  `message.tool_calls` so Roo/Cline/OpenClaw can execute the tools.
- **OpenAI-Compatible Spec**: `stream` defaults to false, complete chunks (id/object/created/model/index),
  `finish_reason`, `[DONE]`, `usage` (with stream_options), errors in the form
  `{"error":{"message","type","code"}}` with proper HTTP status codes (404 for unknown model, 429 for rate-limit, etc.).
- **Control Window**: Displays the server address; closing the window stops the entire program and closes browsers.
- **Resilience**: Failure of one profile doesn't crash others; single-instance locking; automatic re-login detection
  (after signing in via the Chrome window, the worker becomes green without restart); rate-limit handling.
- **Optional Auth**: Set `GHOST_API_KEY` to accept only clients with `Authorization: Bearer <key>`.

## Quick Start (from source)

```powershell
# Python 3.11+ (requires tkinter; Windows 3.11 build has no issues)
py -3.11 -m pip install -r requirements.txt
py -3.11 -m playwright install chromium

# Run
py -3.11 Gemini_Ghost_Brain.py --workers 2
```

A control window opens and a real Chrome window opens for each profile —
sign in with your Google account (only the first time). Once workers turn green, the API is ready on
`http://127.0.0.1:8000`.

## Building EXE (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
# Output: .\dist\GhostBrain.exe  (~400 MB)
```

Build requirements: Python 3.11 with tkinter (build script checks prerequisites),
Playwright bundles pre-downloaded (`playwright install`). First EXE run takes ~30-60 seconds to extract.

## Usage

```bash
# List all models (external + account + aliases)
curl http://127.0.0.1:8000/v1/models

# Streaming chat (get model ID from /v1/models)
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"3-6-flash","stream":true,
       "messages":[{"role":"user","content":"Hello!"}]}'

# Non-streaming (default when stream is not sent)
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"3-6-flash",
       "messages":[{"role":"user","content":"What is 2+2?"}]}'
```

### Connecting to AutoClaw / OpenClaw (from Control UI or config)

Base: `http://127.0.0.1:8000/v1` — Methods (all three are documented in OpenClaw docs):

1. **Recommended Method — Custom Provider** (add to `~/.openclaw/openclaw.json` from the **Config** tab
   in Control UI at `http://127.0.0.1:18789`, or via `openclaw onboard --non-interactive
   --auth-choice custom-api-key --custom-base-url "http://127.0.0.1:8000/v1"
   --custom-model-id "<id>" --custom-compatibility openai`):

```json5
{
  models: {
    mode: "merge",
    providers: {
      ghost: {
        baseUrl: "http://127.0.0.1:8000/v1",
        apiKey: "sk-local",   // localhost → any non-empty value is sufficient
        api: "openai-completions",
        timeoutSeconds: 300,
        models: [
          { id: "3-6-flash", name: "Ghost Gemini 3.6 Flash",
            reasoning: false, input: ["text"],
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
            contextWindow: 120000, maxTokens: 8192 },
          // ... get remaining IDs from GET /v1/models
        ],
      },
    },
  },
  agents: {
    defaults: {
      model: { primary: "ghost/3-6-flash" },
      models: { "ghost/*": {} },
    },
  },
}
```

2. **vLLM Shortcut** (automatic model discovery): Since port 8000 is vLLM's default port,
   use `env: { VLLM_API_KEY: "any" }` + `agents.defaults.models: { "vllm/*": {} }` + `primary: "vllm/<id>"`.

3. **env-var**: `env: { OPENAI_API_KEY: "sk-local", OPENAI_BASE_URL: "http://127.0.0.1:8000/v1" }`
   + `primary: "openai/<id>"` (using the bundled `openai` provider).

After changes, run `openclaw config validate` and `openclaw infer model run --local --model ghost/<id>
--prompt "Reply with exactly: pong" --json` to test. Changes to the `models` section hot-apply
(restart not required).

### Claude Code / Roo / Cline

- **Roo / Cline / Open WebUI**: `Base URL = http://127.0.0.1:8000/v1` and model = any ID from `/v1/models`.
- **Claude Code**: Claude Code only accepts Anthropic endpoints; to connect, use
  `claude-code-router` or register Ghost Brain models in AutoClaw/OpenClaw (above)
  and connect Claude Code to OpenClaw.

## External Model Routing — `ghost_routes.json`

Place the file next to the program (or set `GHOST_ROUTES=<path>`). Template: `ghost_routes.example.json`.

```json
{
  "routes": [
    { "match": "gpt-*", "base_url": "https://api.openai.com/v1",
      "api_key": "<key>", "models": ["gpt-4o"], "label": "OpenAI" },
    { "match": "my-local", "base_url": "http://127.0.0.1:9999/v1",
      "api_key": "", "models": ["my-local"], "label": "Local LLM" }
  ]
}
```

Requests whose `model` matches the `match` pattern (fnmatch) are forwarded to that endpoint
(streaming and non-streaming, with temperature/max_tokens/tools/...). This file may contain API keys —
don't commit it to git (`.gitignore` handles this).

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GHOST_HOST` | `127.0.0.1` | Server bind address; **for security, keep it as localhost only** |
| `GHOST_PORT` | `8000` | Server port |
| `GHOST_WORKERS` | `2` | Number of profiles/workers |
| `GHOST_CHANNEL` | `chrome` | Real browser for Google login (prevents "unsafe browser" warnings). Value `chromium` = bundled browser |
| `GHOST_AUTO_OPEN` | `1` | Auto-open dashboard in default browser when worker is healthy |
| `GHOST_API_KEY` | *(empty)* | If set, requests require `Authorization: Bearer <key>` |
| `GHOST_ROUTES` | `ghost_routes.json` | Path to external routing file |
| `GHOST_IDLE_TIMEOUT` | `120` | Seconds without progress → forced generation stop |
| `GHOST_COOLDOWN` | `60` | Cooldown seconds after rate-limit |
| `GHOST_MAX_PROMPT` | `60000` | Maximum character length of payload sent to Gemini |

## Structure

```
Gemini_Ghost_Brain.py   Main single-file code (server + workers + embedded UI)
ui.html                 Web interface (loaded if beside program; otherwise embedded version used)
build_exe.ps1           PyInstaller build script
requirements.txt        Python dependencies
ghost_routes.example.json  Example external routing
Gemini_Profiles/        (auto-created) Browser profiles for each account — don't commit to git
```

## Security & Disclaimer ⚠️

- The API is **unauthenticated** (unless you set `GHOST_API_KEY`) — only run on `127.0.0.1`;
  never expose it to the internet/network.
- Automated use of your personal Google account (under Google's ToS) carries risk of rate-limiting/account issues;
  use with caution and low volume.
- Profiles in `Gemini_Profiles/` contain cookies and login info — never publish them.
- Google UI selectors may break with Gemini updates; if issues occur, please report them.

## License

MIT — see `LICENSE`.
