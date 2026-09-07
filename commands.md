# Commands

## 1. Install dependencies

```bash
pip install fastapi uvicorn httpx markdownify playwright
playwright install chromium
```

## 2. Get cookies (run once, re-run when you get 401 errors)

**Option A — Automated (recommended):**
```bash
python get_cookies_playwright.py
```

**Option B — Manual fallback:**
```bash
python get_cookies.py
```

## 3. Start the server

```bash
python google_ai_api.py
```

Server runs at `http://localhost:8000` (change port in `config.json`).

## 4. Query the API

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"google-ai","messages":[{"role":"user","content":"hello"}]}'
```

**With streaming:**
```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"google-ai","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

**List models:**
```bash
curl http://localhost:8000/v1/models
```

**Health check:**
```bash
curl http://localhost:8000/health
```
