import json
from typing import Dict, Any, Optional

from playwright.sync_api import sync_playwright


DEFAULT_PROFILE = {
    # Generic defaults that work on many login forms
    "username_selector_candidates": [
        'input[name="email"]',
        'input[name="username"]',
        'input[type="email"]',
        'input[type="text"]',
        'input[id*="email" i]',
        'input[id*="user" i]',
    ],
    "password_selector_candidates": [
        'input[name="password"]',
        'input[type="password"]',
        'input[id*="pass" i]',
    ],
    "submit_selector_candidates": [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Login")',
        'button:has-text("Sign in")',
        'button:has-text("Connexion")',
        'button:has-text("Se connecter")',
    ],
    "post_login_wait_ms": 2500,
}


def _first_visible(page, selectors):
    for sel in selectors:
        loc = page.locator(sel)
        try:
            if loc.count() > 0:
                # pick first match
                el = loc.first
                if el.is_visible():
                    return el, sel
        except Exception:
            continue
    return None, None


def login_and_get_cookies(
    service_url: str,
    username: str,
    password: str,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Opens the target login page, fills credentials, submits, returns cookies.
    This does NOT guarantee login will succeed (2FA/CAPTCHA may block).
    """
    prof = dict(DEFAULT_PROFILE)
    if profile:
        # shallow merge
        prof.update(profile)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto(service_url, wait_until="domcontentloaded")

        user_el, user_sel = _first_visible(page, prof["username_selector_candidates"])
        pass_el, pass_sel = _first_visible(page, prof["password_selector_candidates"])

        if not user_el or not pass_el:
            browser.close()
            raise ValueError(
                "Unable to find login inputs. Provide relay_profile selectors for this website."
            )

        user_el.fill(username.strip())
        pass_el.fill(password)

        submit_el, submit_sel = _first_visible(page, prof["submit_selector_candidates"])
        if submit_el:
            submit_el.click()
        else:
            # fallback: press Enter in password field
            pass_el.press("Enter")

        page.wait_for_timeout(int(prof.get("post_login_wait_ms", 2500)))

        cookies = context.cookies()
        current_url = page.url
        title = page.title()

        browser.close()

        return {
            "ok": True,
            "current_url": current_url,
            "title": title,
            "cookies": cookies,
            "used_selectors": {
                "username": user_sel,
                "password": pass_sel,
                "submit": submit_sel,
            },
        }