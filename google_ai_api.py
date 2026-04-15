"""
Google AI Mode → OpenAI-compatible API

Usage:
    1. python get_cookies.py        # fetch cookies into config.json (once)
    2. python google_ai_api.py      # start the server

Then query it like any OpenAI API:
    curl http://localhost:8000/v1/chat/completions \\
      -H "Content-Type: application/json" \\
      -d '{"model":"google-ai","messages":[{"role":"user","content":"hello"}]}'
"""

import json
import re
import time
import uuid
import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator

import httpx
from markdownify import markdownify as _md
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.json"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"config.json not found. Run: python get_cookies.py")
    return json.loads(CONFIG_PATH.read_text())


def get_cfg(key: str, default: str = "") -> str:
    return load_config().get(key, default)


CLIENT  = "firefox-b-d"
HS      = "AA2U"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.google.com/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
}

# ---------------------------------------------------------------------------
# Session (per conversation)
# ---------------------------------------------------------------------------

@dataclass
class Session:
    cookies: str
    ei: str
    srtst: str
    stkp: str
    mstk: str
    elrc: str
    fc_elrc: str
    fn_elrc: str
    xsrf_folif: str
    turn: int = 0


def session_from_config() -> Session:
    cfg = load_config()
    missing = [k for k in ("cookies", "ei", "srtst", "xsrf_folif") if not cfg.get(k)]
    if missing:
        raise RuntimeError(
            f"config.json is missing required keys: {missing}. "
            f"Run: python get_cookies.py"
        )
    return Session(
        cookies    = cfg["cookies"],
        ei         = cfg["ei"],
        srtst      = cfg["srtst"],
        stkp       = cfg.get("stkp", ""),
        mstk       = cfg.get("mstk", ""),
        elrc       = cfg.get("elrc", ""),
        fc_elrc    = cfg.get("fc_elrc", ""),
        fn_elrc    = cfg.get("fn_elrc", ""),
        xsrf_folif = cfg["xsrf_folif"],
    )


_sessions: dict[str, Session] = {}

# ---------------------------------------------------------------------------
# folif request
# ---------------------------------------------------------------------------

async def fetch_ai_response(client: httpx.AsyncClient, session: Session, query: str) -> tuple[str, Session]:
    elrc = session.elrc if session.turn == 0 else (session.fc_elrc or session.fn_elrc or session.elrc)

    params = {
        "srtst":  session.srtst,
        "ei":     session.ei,
        "yv":     "3",
        "aep":    "1",
        "sca_esv": get_cfg("sca_esv", "19eacb78983cd92f"),
        "udm":    "50",
        "client": CLIENT,
        "hs":     HS,
        "stkp":   session.stkp,
        "cs":     "1",
        "csuir":  "0",
        "elrc":   elrc,
        "mstk":   session.mstk,
        "csui":   "3",
        "q":      query,
        "async":  f"_fmt:adl,_xsrf:{session.xsrf_folif}",
    }

    log.info("folif  turn=%d  q_len=%d  q=%r", session.turn, len(query), query[:80])
    resp = await client.get("https://www.google.com/async/folif", params=params)

    if resp.status_code == 400:
        raise HTTPException(
            status_code=401,
            detail="Google rejected the request (400). Cookies/tokens may have expired. Run: python get_cookies.py"
        )
    resp.raise_for_status()

    html = resp.text
    text = _extract_text(html)

    m = re.search(r'data-mstk="([^"]+)"', html)
    if m:
        session.mstk = m.group(1)

    session.turn += 1
    return text, session


_SKIP_LINES = {"Learn more", "Copy", "Creating a public link…", "Show all"}
_CUT_MARKERS = [
    "Propose a specific way to proceed:",
    "AI responses may include mistakes.",
    "Good response",
    "Bad response",
]

def _extract_text(html: str) -> str:
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>",   "", html, flags=re.DOTALL)

    # Convert HTML formatting to Markdown
    raw = _md(html, heading_style="ATX", bullets="-", newline_style="backslash")

    # Cut off boilerplate that follows the actual answer
    for marker in _CUT_MARKERS:
        idx = raw.find(marker)
        if idx != -1:
            raw = raw[:idx]

    # Collapse runs of blank lines; drop known UI noise
    lines, out, prev_blank = raw.splitlines(), [], False
    for ln in lines:
        s = ln.strip()
        if not s or s in _SKIP_LINES:
            if not prev_blank:
                out.append("")
            prev_blank = True
        else:
            out.append(s)
            prev_blank = False

    return "\n".join(out).strip()


_QUERY_LIMIT = 3500   # safe ceiling for Google's folif q= parameter

def _strip_schemas(text: str) -> str:
    """Level 1: remove JSON parameter blocks and error lists, keep descriptions."""
    out, depth, skip = [], 0, False
    for line in text.splitlines():
        s = line.strip()
        if "parameters in JSON format" in s or "Error Responses:" in s:
            skip = True
        if skip:
            depth += s.count("{") - s.count("}")
            if depth <= 0:
                skip = False
                depth = 0
            continue
        out.append(line)
    return "\n".join(out)


def _condense_system(text: str) -> str:
    """Progressively condense a system prompt to fit within the URL query limit.

    Level 1 — strip JSON schemas + error lists, keep function descriptions.
    Level 2 — keep only function names + instruction preamble (no descriptions).
    Level 3 — hard truncate as last resort.
    """
    if len(text) <= _QUERY_LIMIT:
        return text

    # Level 1: strip schemas
    condensed = _strip_schemas(text)
    if len(condensed) <= _QUERY_LIMIT:
        log.debug("condense L1: %d→%d chars", len(text), len(condensed))
        return condensed

    # Level 2: keep only preamble + function names
    # Preamble = everything before the first "Function name:" line
    first = text.find("Function name:")
    preamble = text[:first].strip() if first != -1 else ""
    names = re.findall(r"Function name:\s*(.+)", text)
    if names:
        condensed = preamble + "\n\nAvailable functions:\n" + "\n".join(f"- {n.strip()}" for n in names)
        # Append the last instruction if present ("Now pick a function...")
        last_inst = re.search(r"Now pick a function.+", text)
        if last_inst:
            condensed += "\n\n" + last_inst.group(0)
        if len(condensed) <= _QUERY_LIMIT:
            log.debug("condense L2: %d→%d chars", len(text), len(condensed))
            return condensed

    # Level 3: hard truncate
    log.debug("condense L3: hard truncate %d→%d chars", len(text), _QUERY_LIMIT)
    return condensed[:_QUERY_LIMIT] + "\n[truncated]"


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Validate config at startup
    try:
        session_from_config()
        log.info("Config loaded from %s", CONFIG_PATH)
    except RuntimeError as e:
        log.error("%s", e)
    yield

app = FastAPI(title="Google AI Mode Proxy", version="0.3.0", lifespan=lifespan)


class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    model: str = "google-ai"
    messages: list[Message]
    stream: bool = False


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    messages  = [m.model_dump() for m in req.messages]
    user_msgs = [m for m in messages if m["role"] == "user"]
    if not user_msgs:
        raise HTTPException(status_code=400, detail="No user messages.")

    last = user_msgs[-1]["content"]
    conv_id = str(uuid.uuid4())   # new session per request for now

    # Fresh session from config for each request
    # (tokens from config are reusable — only q changes)
    try:
        session = session_from_config()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # Pull out system messages and build conversation history
    system_msgs = [m for m in messages if m["role"] == "system"]
    chat_msgs   = [m for m in messages if m["role"] != "system"]

    parts = []
    if system_msgs:
        system_text = "\n".join(m["content"] for m in system_msgs)
        system_text = _condense_system(system_text)
        parts.append(f"[System instructions]\n{system_text}\n[End system instructions]")

    if len(chat_msgs) > 1:
        for m in chat_msgs[:-1]:
            role = "User" if m["role"] == "user" else "Assistant"
            parts.append(f"{role}: {m['content']}")

    parts.append(f"User: {last}")
    query = "\n".join(parts)

    async with httpx.AsyncClient(
        headers={**HEADERS, "Cookie": session.cookies},
        follow_redirects=True,
        timeout=30,
        transport=httpx.AsyncHTTPTransport(retries=1),
    ) as client:
        try:
            text, session = await fetch_ai_response(client, session, query)
        except HTTPException:
            raise
        except Exception as e:
            log.exception("Error")
            raise HTTPException(status_code=500, detail=str(e))

    if not text:
        raise HTTPException(status_code=502, detail="Empty response from Google.")

    if req.stream:
        return StreamingResponse(_stream(text, conv_id, req.model), media_type="text/event-stream")
    return _response(conv_id, text, req.model)


def _response(conv_id: str, text: str, model: str) -> dict:
    return {
        "id": f"chatcmpl-{conv_id}", "object": "chat.completion",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": -1, "completion_tokens": -1, "total_tokens": -1},
    }


async def _stream(text: str, conv_id: str, model: str) -> AsyncGenerator[str, None]:
    for i, word in enumerate(text.split(" ")):
        chunk = {
            "id": f"chatcmpl-{conv_id}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "delta": {"content": word + (" " if i < len(text.split())-1 else "")}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        await asyncio.sleep(0.02)
    yield f"data: {json.dumps({'id':f'chatcmpl-{conv_id}','object':'chat.completion.chunk','created':int(time.time()),'model':model,'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})}\n\n"
    yield "data: [DONE]\n\n"


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "google-ai", "object": "model", "owned_by": "google"}]}

@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    port = load_config().get("port", 8000)
    uvicorn.run(app, host="0.0.0.0", port=port)
