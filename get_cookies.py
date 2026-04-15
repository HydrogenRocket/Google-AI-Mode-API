"""
Google AI Mode — cookie + token fetcher.
No browser. Uses requests to hit Google directly, handles the consent
redirect automatically, then parses session tokens from the page HTML.
Saves everything to config.json.

Usage:
    python get_cookies.py
"""

import json
import re
import sys
from pathlib import Path
from http.cookiejar import CookieJar
from urllib.request import Request

import requests

CONFIG_PATH = Path(__file__).parent / "config.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:149.0) "
        "Gecko/20100101 Firefox/149.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-GPC": "1",
}

SEARCH_URL = "https://www.google.com/search?q=hello&udm=50&aep=1&hl=en"


def load_existing_cookies() -> str:
    """Return the cookie string stored in config.json, or ''."""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text()).get("cookies", "")
        except Exception:
            pass
    return ""


def inject_cookies(session: requests.Session, cookie_str: str) -> None:
    """Parse a 'name=value; name=value' string into the session jar."""
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            session.cookies.set(name.strip(), value.strip(), domain=".google.com", path="/")


def run():
    session = requests.Session()
    session.headers.update(HEADERS)

    existing_cookies = load_existing_cookies()
    if existing_cookies:
        print("Using existing cookies from config.json")
        inject_cookies(session, existing_cookies)

    print("Fetching Google AI Mode page...")
    resp = session.get(SEARCH_URL, timeout=20)

    # Handle Google's JS-check page (served to clients without JavaScript).
    resp = follow_js_redirect(session, resp)

    # If still getting bot challenge, try the SG_REL fallback URL
    if "data-ei" not in resp.text and "SG_REL" in resp.text:
        resp = follow_sg_rel(session, resp)

    # Handle consent redirect (EU/UK GDPR)
    if "consent.google.com" in resp.url:
        print("Consent page detected — accepting automatically...")
        resp = accept_consent(session, resp)
        if "consent.google.com" in resp.url:
            print("ERROR: Could not auto-accept consent page.")
            print("       Open Firefox, accept Google's cookie prompt, then")
            print("       copy the Cookie header from DevTools and paste below.")
            manual = input("Paste cookie string (or press Enter to abort): ").strip()
            if not manual:
                sys.exit(1)
            save_config(manual, {})
            return

    html = resp.text
    tokens = parse_tokens(html)

    # Build cookie string — prefer jar; fall back to existing if jar is empty
    jar_cookies = "; ".join(f"{k}={v}" for k, v in session.cookies.items())
    cookies = jar_cookies or existing_cookies

    if not cookies:
        print("ERROR: No cookies available.")
        sys.exit(1)

    if not tokens.get("ei"):
        Path("/tmp/google_ai_debug.html").write_text(html)
        print("WARNING: Could not auto-extract tokens (bot detection).")
        print()
        print("  ── Manual setup ──────────────────────────────────────────────")
        print(f"  1. Open this URL in Firefox (while signed in to Google):")
        print(f"     {SEARCH_URL}")
        print()
        print("  2. Wait for the AI response to appear on the page.")
        print()
        print("  3. Open DevTools (F12) → Console tab and paste:")
        print(r"""
  var d=document,f=s=>d.querySelector(`[data-${s}]`)?.dataset[s.replace(/-(.)/g,(_,c)=>c.toUpperCase())]||'';
  var mm=d.body.innerHTML.match(/mstk=([A-Za-z0-9_-]+)/);
  var sca=new URLSearchParams(window.location.search).get('sca_esv')||'';
  copy(JSON.stringify({ei:f('ei'),srtst:f('srtst'),stkp:f('stkp'),mstk:mm?mm[1]:'',elrc:f('elrc'),fc_elrc:f('fc-elrc'),fn_elrc:f('fn-elrc'),xsrf_folif:f('xsrf-folif-token'),sca_esv:sca}));
  console.log('Tokens copied to clipboard');
""")
        print("  4. Open DevTools → Network tab → click any google.com request")
        print("     → Request Headers → copy the full value of the Cookie header.")
        print("  ──────────────────────────────────────────────────────────────")
        print()

        try:
            cookie_raw = input("Paste Cookie header value (IMPORTANT — must match the token session): ").strip()
        except EOFError:
            cookie_raw = ""

        try:
            token_raw = input("Paste token JSON here: ").strip()
        except EOFError:
            token_raw = ""

        if not cookie_raw and not token_raw:
            print("Nothing entered — config unchanged.")
            return

        manual_cookies = cookie_raw or cookies

        if token_raw:
            try:
                manual_tokens = json.loads(token_raw)
                required = ["ei", "srtst", "xsrf_folif"]
                missing = [k for k in required if not manual_tokens.get(k)]
                if missing:
                    print(f"WARNING: Missing required tokens: {missing}")
                    print("         Re-run the DevTools snippet (wait for AI response first).")
                    return
                save_config(manual_cookies, manual_tokens)
                print(f"\nSaved to {CONFIG_PATH}")
                print("Run: python3 google_ai_api.py")
            except json.JSONDecodeError:
                print("Invalid JSON — not saved.")
        else:
            save_config(manual_cookies, {})
            print(f"Saved cookies to {CONFIG_PATH} (tokens still empty).")
        return

    save_config(cookies, tokens)
    names = [p.split("=")[0] for p in cookies.split(";")]
    print(f"Cookies: {', '.join(names)}")
    print(f"Tokens:  ei={tokens.get('ei')}  elrc={tokens.get('elrc','')[:24]}...")
    print(f"\nSaved to {CONFIG_PATH}")
    print("Run: python google_ai_api.py")


def follow_js_redirect(session: requests.Session, resp: requests.Response) -> requests.Response:
    """
    Google serves a JS-redirect page to clients it suspects are bots:
      <meta http-equiv="refresh" content="0;url=/httpservice/retry/enablejs?sei=...">
    Follow that URL, which sets a cookie confirming JS support, then
    re-request the actual search URL.
    """
    html = resp.text
    if "enablejs" not in html:
        return resp  # not a JS-check page, nothing to do

    m = re.search(r'url=(/httpservice/retry/enablejs[^"\'>\s]+)', html)
    if not m:
        return resp

    enablejs_url = "https://www.google.com" + m.group(1).replace("&amp;", "&")
    print(f"Following JS-check redirect: {enablejs_url[:80]}...")
    session.get(enablejs_url, timeout=20)   # sets NID / other cookies

    # Retry the actual search page now that we've "confirmed" JS
    print("Retrying AI Mode page with JS cookies...")
    return session.get(SEARCH_URL, timeout=20)


def follow_sg_rel(session: requests.Session, resp: requests.Response) -> requests.Response:
    """
    After the enablejs redirect, Google may still serve a bot-challenge page
    containing an SG_REL fallback link.  Follow it to get the real search page.
    """
    html = resp.text
    m = re.search(r'href="(/search\?[^"]*emsg=SG_REL[^"]*)"', html)
    if not m:
        return resp

    fallback = "https://www.google.com" + m.group(1).replace("&amp;", "&")
    print(f"Following SG_REL fallback: {fallback[:80]}...")
    return session.get(fallback, timeout=20)


def accept_consent(session: requests.Session, resp: requests.Response) -> requests.Response:
    """
    Parse the Google consent form and POST 'Accept all'.
    Google's consent page has a form with hidden fields; the Accept button
    is a submit button — we find its name/value and include it in the POST.
    """
    html = resp.text

    # Find the form action
    m = re.search(r'<form[^>]+action="([^"]+)"', html)
    if not m:
        return resp
    action = m.group(1)
    base_url = "https://consent.google.com"
    post_url = action if action.startswith("http") else base_url + action

    # Collect all hidden inputs
    data = {}
    for inp in re.finditer(r'<input([^>]+)>', html):
        tag = inp.group(1)
        name  = re.search(r'name="([^"]*)"',  tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name:
            data[name.group(1)] = value.group(1) if value else ""

    # Find the "Accept all" submit button and add its name/value
    for btn in re.finditer(r'<button([^>]*)>([^<]*)</button>', html, re.IGNORECASE):
        attrs, label = btn.group(1), btn.group(2)
        if "accept" in label.lower() or "agree" in label.lower():
            name  = re.search(r'name="([^"]*)"',  attrs)
            value = re.search(r'value="([^"]*)"', attrs)
            if name:
                data[name.group(1)] = value.group(1) if value else ""
            break

    post_resp = session.post(post_url, data=data, timeout=20, allow_redirects=True)
    return post_resp


def parse_tokens(html: str) -> dict:
    def attr(name: str) -> str:
        m = re.search(rf'data-{re.escape(name)}="([^"]+)"', html)
        return m.group(1) if m else ""

    m_mstk = re.search(r'[?&]mstk=([A-Za-z0-9_\-]+)', html) or \
             re.search(r'%26mstk%3D([A-Za-z0-9_\-]+)', html)
    m_sca  = re.search(r'[?&]sca_esv=([A-Za-z0-9]+)', html)

    return {
        "ei":         attr("ei"),
        "srtst":      attr("srtst"),
        "stkp":       attr("stkp"),
        "mstk":       m_mstk.group(1) if m_mstk else "",
        "elrc":       attr("elrc"),
        "fc_elrc":    attr("fc-elrc"),
        "fn_elrc":    attr("fn-elrc"),
        "xsrf_folif": attr("xsrf-folif-token"),
        "sca_esv":    m_sca.group(1) if m_sca else "",
    }


def save_config(cookies: str, tokens: dict) -> None:
    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    cfg["cookies"] = cookies
    cfg.update({k: v for k, v in tokens.items() if v})
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


if __name__ == "__main__":
    run()
