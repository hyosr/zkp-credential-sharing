import json
from playwright.sync_api import sync_playwright

# Paste the cookies JSON list you got from /sharing/relay-login here:
COOKIES = [
    {
        "name": "token",
        "value": "PASTE_VALUE_HERE",
        "domain": "recolyse.com",
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,   # <-- set True for https sites
        "sameSite": "Lax",
    }
]

START_URL = "https://recolyse.com/"

def normalize_cookies(cookies):
    """
    Playwright expects:
    - expires: either -1 for session cookies OR a unix timestamp in seconds
    - secure: should be True for https
    """
    out = []
    for c in cookies:
        c = dict(c)
        # If expires missing, make it session cookie
        if "expires" not in c:
            c["expires"] = -1
        # Some APIs return null -> Playwright doesn't like it
        if c["expires"] is None:
            c["expires"] = -1
        # Strongly recommended
        if START_URL.startswith("https://"):
            c["secure"] = True
        out.append(c)
    return out

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    context.add_cookies(normalize_cookies(COOKIES))

    page = context.new_page()
    page.goto(START_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(5000)

    print("Browser opened. If cookies are correct, you should be logged in.")
    page.wait_for_timeout(600000)  # keep open 10 min