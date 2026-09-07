# Google AI Mode To OpenAI

An OpenAI-compatible API server that routes requests through Google's AI Mode search (`/async/folif`). Lets you point any OpenAI-compatible client (AnythingLLM, Open WebUI, etc.) at it and get responses from Google's AI.

## How it works

Google's AI Mode search returns an AI-generated answer for any query. This proxy:

1. Accepts requests in OpenAI chat completions format
2. On the first turn of a new conversation, sends the user's message (plus a condensed system prompt, if any) to Google's internal `/async/folif` endpoint using your browser cookies
3. On follow-up turns, sends only the newest message — conversation continuity is carried server-side by Google via session tokens, the same way the real browser client works (see [What we've learned](#what-weve-learned-about-googles-ai-mode-requests) below)
4. Parses the HTML response, strips Google's UI chrome, and returns it as an OpenAI-formatted reply

No official API key is needed — it rides on your existing Google session cookies.

## Setup

### 1. Get cookies and tokens

**Option A — Playwright (recommended):**
```bash
pip install playwright
playwright install chromium
python get_cookies_playwright.py
```
A browser window opens, loads Google AI Mode, then saves cookies and tokens to `config.json` automatically. If Google shows a CAPTCHA, solve it in the browser window — the script waits for the page to finish loading.

**Option B — Manual:**
```bash
python get_cookies.py
```
If bot detection blocks auto-extraction, it prints DevTools instructions to copy cookies and tokens manually from your browser.

Tokens expire periodically (hours to days). Re-run the cookie fetcher when you get 400/401 errors.

### 2. Start the server

```bash
pip install fastapi uvicorn httpx markdownify
python google_ai_api.py
```

Server starts on `http://localhost:8000` by default. Change the port in `config.json`:
```json
{ "port": 8001 }
```

### 3. Query it

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"google-ai","messages":[{"role":"user","content":"hello"}]}'
```

Streaming is supported (`"stream": true`).

Point any OpenAI-compatible client at `http://localhost:8000` with model name `google-ai`.

## Feature support

What a typical model API offers, and where this proxy stands — it's a wrapper around a search-grounded web UI, not a raw model endpoint, so a lot of standard API surface doesn't map cleanly.

| Feature | Status | Notes |
|---|---|---|
| Chat completions (`/v1/chat/completions`) | ✅ Working | Core functionality. |
| System prompts | ✅ Working | Sent only on a conversation's first turn (condensed if long); continuations rely on Google's own session memory instead of resending it. |
| Multi-turn conversation memory | ✅ Working | Reconstructed server-side by hashing the message history to key a session cache — see below. Not full-history replay. |
| Web search grounding | ✅ Working | This *is* the underlying engine (Google AI Mode) — arguably the main value-add over a raw LLM API. |
| Streaming (`stream: true`) | ⚠️ Partial | Simulated — Google's response is fully generated first, then chunked word-by-word as SSE. Not true incremental generation. |
| Tool/function calling (`tools` API param) | ❌ Not supported | No structured function-calling support in the underlying endpoint. |
| MCP tool definitions (via system prompt) | ⚠️ Partial | Tool defs arrive as plain text, not structured calls. Condensed to fit the URL length limit; reliability drops as more tools are configured — see [Known issues](#known-issues-and-limitations). |
| Vision / image input | ❌ Not supported | Image content parts are silently dropped; text-only. |
| Image / file generation output | ❌ Not supported | Images in Google's response are stripped, not returned. |
| JSON mode / structured output | ❌ Not supported | No constrained decoding available. |
| Embeddings endpoint | ❌ Not supported | Not part of this API. |
| `/v1/models` | ✅ Working | Static stub listing a single `google-ai` model. |
| Sampling params (`temperature`, `top_p`, etc.) | ❌ Not supported | Silently ignored — no control surface over Google's backend. |
| Multiple choices (`n > 1`) | ❌ Not supported | Always returns one choice. |
| Stop sequences | ❌ Not supported | Not implemented. |
| Token usage reporting | ❌ Not supported | `usage` fields are placeholder `-1`s. |
| Seed / determinism | ❌ Not supported | Responses are not reproducible. |

## What we've learned about Google's AI Mode requests

Notes from reverse-engineering the real browser client's behavior (via captured HAR traffic) that shaped how this proxy works:

- **Conversation state lives server-side, not in the request.** The real AI Mode client never resends prior turns. `ei` (a per-conversation id) and `elrc`/`stkp` stay constant for the whole conversation; `mstk` rotates every turn as a continuation token scraped from each response's `data-mstk` attribute. This proxy mirrors that: a session cache (keyed by a hash of the message history) lets follow-up turns send only the newest message instead of replaying the whole conversation.
- **`/async/folif` is GET-only**, so only a *brand-new* conversation's first message (plus a condensed system prompt, if present) has to fit in the URL — roughly a 3500-character practical ceiling on `q=`. Continuations aren't subject to this since they only carry the latest message.
- **Message ordering affects topic extraction.** Putting a long system-prompt block *before* a short/vague user message (e.g. "hi") causes Google to treat the instructional text itself as the query topic and fall back to a raw web-results listing instead of answering. Putting the user's actual message first avoids this.
- **Every response is wrapped in UI chrome** — a heading like `### AI Mode reply for <the full input query>`, then `Shared`, then `N files` — which has to be stripped before returning the answer. The heading echoes the entire input verbatim, so it can be several thousand characters long when a system prompt is attached.
- **A second, newer token scheme exists** (`data-garc`, `data-lro-token`, `data-lro-signature`), used by Google's own JS for the `/async/folwr` "restore" call (fired on back/forward navigation). `/async/folif` still runs on the older `srtst`/`stkp`/`elrc`/`mstk` scheme as far as we've confirmed, but `data-srtst` has recently become unreliable/empty on fresh page loads — possibly an early sign Google is migrating this endpoint too.
- **Automated traffic gets soft-blocked** with a `/sorry/index` CAPTCHA challenge, and it isn't purely IP-based — switching VPN exit nodes didn't clear it. Solving the CAPTCHA once (via `get_cookies_playwright.py`, which opens a real visible browser) earns a `GOOGLE_ABUSE_EXEMPTION` cookie that appears to clear it afterward.
- **AI Mode replies in the language it infers from your IP/locale**, not the `hl=` query param — a non-English VPN exit can get you a non-English answer even with `hl=en` set.

## Known issues and limitations

### Token expiry
Session tokens (`ei`, `srtst`, `xsrf_folif`, etc.) are tied to a specific browser session and expire after a few hours to a day or two. When they expire, Google returns a 400 and the server responds with a 401. Re-run `get_cookies_playwright.py` (or `get_cookies.py`) to refresh them.

### URL length limit and MCP tools
A brand-new conversation's first message (system prompt + first user message) has to fit in a GET query string — roughly **3500 characters** for `q=` before Google starts returning errors. Follow-up turns aren't affected, since they only send the newest message (see [What we've learned](#what-weve-learned-about-googles-ai-mode-requests)).

This still bites on that first turn with agent systems like AnythingLLM that have MCP servers configured — they inject all tool definitions (names, descriptions, full JSON parameter schemas, error lists) into the system prompt automatically, and a single agent session can easily produce a 14,000+ character system prompt before it's ever sent.

To work around this, the server applies a three-level condensing strategy before sending:

1. **Strip JSON schemas** — removes parameter blocks and error lists, keeps function names and descriptions
2. **Names only** — if still too long, keeps only the instruction preamble and a list of function names
3. **Hard truncate** — last resort, cuts to 3500 chars with a `[truncated]` marker

The model can usually still pick the right tool at level 1 or 2, but the more MCP servers you have configured, the more likely it is to lose context about what arguments each tool expects. Level 3 will likely cause tool calls to fail or produce wrong arguments.

### Tool calling reliability
OpenAI-native agent systems use a dedicated `tools` array in the API request, with structured function calling baked into the model. This endpoint does not support that. Instead, the tool definitions arrive as plain text in the system prompt, and the model has to decide to output a JSON function call on its own.

This works, but it is less reliable — the model occasionally falls back to a conversational response or web search instead of calling a tool, especially on the first turn of an agent session. A follow-up message usually recovers it.

### No images or document attachments
Only text content is handled. Images, PDFs, or file attachments passed by the client are silently ignored.

### Conversation memory is a best-effort reconstruction
There's no OpenAI-style conversation id, so the server fingerprints a conversation by hashing its message history (minus the newest message) and uses that to look up a cached session. This works as long as the client only *appends* to history — if a client rewrites or summarizes earlier turns instead, the hash won't match and the server silently falls back to treating it as a new conversation (losing continuity for that turn, but not erroring). Sessions are held in memory only and evicted after 30 minutes of inactivity or on server restart.

### Bot detection and soft blocks
Automated requests can trigger Google's `/sorry/index` CAPTCHA challenge. This isn't strictly IP-based — it appears tied to request volume/pattern more than network origin. If you hit it: solve the CAPTCHA via `python get_cookies_playwright.py` (it opens a real, visible browser window), then re-fetch cookies. Avoid retrying the cookie fetcher back-to-back, since that itself adds to the automated-traffic signal.

### Not an official API
This relies on an undocumented internal Google endpoint and personal session cookies. It can break without warning if Google changes the endpoint.

## Files

| File | Purpose |
|------|---------|
| `google_ai_api.py` | Main FastAPI server |
| `get_cookies_playwright.py` | Automated cookie/token fetcher (Playwright) |
| `get_cookies.py` | Cookie fetcher with manual DevTools fallback |
| `debug_dump_page.py` | Diagnostic: dumps the raw AI Mode page HTML for inspecting Google's current token/HTML structure |
| `config.json` | Cookies, tokens, and port (auto-generated) |
