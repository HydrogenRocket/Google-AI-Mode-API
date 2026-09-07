"""
One-off diagnostic: opens the AI Mode page, lets you solve any CAPTCHA,
then dumps the full page HTML to disk so we can see what actually changed.

Usage:
    python debug_dump_page.py
"""

from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

OUT = Path(__file__).parent / "debug_page_dump.html"
SEARCH_URL = "https://www.google.com/search?q=hello&udm=50&aep=1&hl=en"


def run() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        print(f"Loading {SEARCH_URL} ...")
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)

        print("If a CAPTCHA appears, solve it now — you have up to 2 minutes.")
        try:
            page.wait_for_selector("[data-ei]", state="attached", timeout=120_000)
            print("Found [data-ei] — page loaded.")
        except PWTimeout:
            print("Timed out waiting for [data-ei]. Dumping whatever's there anyway.")

        html = page.content()
        OUT.write_text(html)
        print(f"Dumped {len(html)} chars to {OUT}")
        print(f"Current URL: {page.url}")

        browser.close()


if __name__ == "__main__":
    run()
