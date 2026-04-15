# Google AI Mode Proxy

An OpenAI-compatible API server that routes requests through Google's AI Mode search (`/async/folif`). Lets you point any OpenAI-compatible client (AnythingLLM, Open WebUI, etc.) at it and get responses from Google's AI.

## How it works

Google's AI Mode search returns an AI-generated answer for any query. This proxy:

1. Accepts requests in OpenAI chat completions format
2. Packs the conversation history (system prompt + messages) into a single query string
3. Sends it to Google's internal `/async/folif` endpoint using your browser cookies
4. Parses the HTML response and returns it as an OpenAI-formatted reply

No official API key is needed — it rides on your existing Google session cookies.

## Setup

### 1. Get cookies and tokens

**Option A — Playwright (recommended):**
```bash
pip install playwright
playwright install chromium
python get_cookies_playwright.py
```
A browser window opens, loads Google AI Mode, then saves cookies and tokens to `config.json` automatically.

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

## Known issues and limitations

### Token expiry
Session tokens (`ei`, `srtst`, `xsrf_folif`, etc.) are tied to a specific browser session and expire after a few hours to a day or two. When they expire, Google returns a 400 and the server responds with a 401. Re-run `get_cookies_playwright.py` (or `get_cookies.py`) to refresh them.

### GET-only endpoint — no POST
The `/async/folif` endpoint only accepts GET requests. A POST returns `405 Method Not Allowed`. This means the entire payload — system prompt, conversation history, and the user message — has to be encoded into a single URL query string. There is no workaround for this; it is a hard constraint of the endpoint.

### URL length limit and MCP tools
Because everything goes in a GET query string, there is a practical limit of around **3500 characters** for the `q=` parameter before Google starts returning errors.

This becomes a problem when using agent systems like AnythingLLM with MCP servers configured. These systems inject all tool definitions (function names, descriptions, full JSON parameter schemas, error response lists) into the system prompt automatically — a single agent session can easily produce a 14,000+ character system prompt.

To work around this, the server applies a three-level condensing strategy before sending:

1. **Strip JSON schemas** — removes parameter blocks and error lists, keeps function names and descriptions
2. **Names only** — if still too long, keeps only the instruction preamble and a list of function names
3. **Hard truncate** — last resort, cuts to 3500 chars with a `[truncated]` marker

The model can usually still pick the right tool at level 1 or 2, but the more MCP servers you have configured, the more likely it is to lose context about what arguments each tool expects. Level 3 will likely cause tool calls to fail or produce wrong arguments.

There is no clean fix for this without a different backend. The URL length constraint is fundamental.

### Tool calling reliability
OpenAI-native agent systems use a dedicated `tools` array in the API request, with structured function calling baked into the model. This endpoint does not support that. Instead, the tool definitions arrive as plain text in the system prompt, and the model has to decide to output a JSON function call on its own.

This works, but it is less reliable — the model occasionally falls back to a conversational response or web search instead of calling a tool, especially on the first turn of an agent session. A follow-up message usually recovers it.

### No images or document attachments
Only text content is handled. Images, PDFs, or file attachments passed by the client are silently ignored.

### Stateless per request
Each request creates a fresh session. There is no persistent conversation state stored on Google's side — prior turns are reconstructed by replaying the message history in the query string. This also means the history itself counts against the URL length limit.

### Not an official API
This relies on an undocumented internal Google endpoint and personal session cookies, It can break without warning if Google changes the endpoint.
## Files

| File | Purpose |
|------|---------|
| `google_ai_api.py` | Main FastAPI server |
| `get_cookies_playwright.py` | Automated cookie/token fetcher (Playwright) |
| `get_cookies.py` | Cookie fetcher with manual DevTools fallback |
| `config.json` | Cookies, tokens, and port (auto-generated) |
