"""
Google AI Mode — automatic cookie + token fetcher using Playwright.
No Google account required.

Opens a real browser window, loads the AI Mode search page, waits for
the AI response to appear, then extracts cookies and tokens into config.json.

Usage:
    python get_cookies_playwright.py
"""

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

CONFIG_PATH = Path(__file__).parent / "config.json"
SEARCH_URL  = "https://www.google.com/search?q=hello&udm=50&aep=1&hl=en"

_TOKEN_SCRIPT = """() => {
    var d = document;
    var f = s => d.querySelector('[data-' + s + ']')
                  ?.dataset[s.replace(/-(.)/g, (_, c) => c.toUpperCase())] || '';
    var mm  = d.body.innerHTML.match(/mstk=([A-Za-z0-9_-]+)/);
    var sca = new URLSearchParams(window.location.search).get('sca_esv') || '';
    return {
        ei:         f('ei'),
        srtst:      f('srtst'),
        stkp:       f('stkp'),
        mstk:       mm ? mm[1] : '',
        elrc:       f('elrc'),
        fc_elrc:    f('fc-elrc'),
        fn_elrc:    f('fn-elrc'),
        xsrf_folif: f('xsrf-folif-token'),
        sca_esv:    sca,
        // Newer token scheme seen alongside the original one (used by the
        // /async/folwr restore endpoint as of Sept 2026) — captured for
        // future use in case Google migrates /async/folif onto it too.
        garc:           f('garc'),
        lro_token:      f('lro-token'),
        lro_signature:  f('lro-signature'),
    };
}"""


def run() -> None:
    print("Opening browser...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        print(f"Loading AI Mode...")
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)

        # Wait up to 30 s for the AI response to populate [data-ei]
        print("Waiting for AI response...")
        try:
            page.wait_for_selector("[data-ei]", state="attached", timeout=30_000)
        except PWTimeout:
            print("ERROR: [data-ei] not found — AI Mode may not have loaded.")
            print("       Check the browser window for any challenge or error page.")
            browser.close()
            sys.exit(1)

        # srtst can populate a beat after [data-ei] shows up — poll for it
        # specifically rather than assuming it's ready at the same instant.
        try:
            page.wait_for_function(
                "() => !!document.querySelector('[data-srtst]')?.dataset.srtst",
                timeout=20_000,
            )
        except PWTimeout:
            print("WARNING: data-srtst stayed empty after waiting — continuing anyway,")
            print("         but the fetched token set may be incomplete.")

        tokens = page.evaluate(_TOKEN_SCRIPT)
        cookies = context.cookies("https://www.google.com")
        browser.close()

    # ei/xsrf_folif are always required — without them nothing works. srtst
    # is only warned about: Google's page has been inconsistent about
    # populating it lately, and it may no longer be required by /async/folif
    # at all (their own JS has shifted to a garc/lro-token/lro-signature
    # scheme elsewhere on the page). Save whatever we got rather than
    # discarding good tokens over one uncertain one — a stale srtst already
    # in config.json is preserved below since empty values aren't written.
    hard_required = ["ei", "xsrf_folif"]
    missing = [k for k in hard_required if not tokens.get(k)]
    if missing:
        print(f"ERROR: Missing tokens: {missing}")
        sys.exit(1)
    if not tokens.get("srtst"):
        print("WARNING: srtst is empty — saving everything else anyway. "
              "If config.json already has an srtst from a previous fetch, "
              "it's left as-is; try the API and see if it still works.")

    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    cfg["cookies"] = cookie_str
    cfg.update({k: v for k, v in tokens.items() if v})
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

    names = [c["name"] for c in cookies]
    print(f"Cookies: {', '.join(names)}")
    print(f"Tokens:  ei={tokens['ei']}  sca_esv={tokens['sca_esv']}")
    print(f"\nSaved to {CONFIG_PATH}")
    print("Run: python3 google_ai_api.py")


if __name__ == "__main__":
    run()
