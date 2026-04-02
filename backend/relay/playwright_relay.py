from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any, Dict, Optional, Tuple

from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

DEFAULT_TIMEOUT_MS = 30_000

# Requested: always show browser window
HEADLESS = False


def _domain_from_url(url: str) -> str:
    try:
        d = urllib.parse.urlparse(url).netloc
        return d.split(":")[0].lower().strip()
    except Exception:
        return ""


def _origin_from_url(url: str) -> str:
    """
    Return scheme://host[:port] for a given URL.
    Falls back to https://<domain> if scheme missing.
    """
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or parsed.path  # handles malformed inputs like "example.com/login"
    if "/" in netloc:
        netloc = netloc.split("/")[0]
    return f"{scheme}://{netloc}".rstrip("/")


async def _dump_storage(page) -> Tuple[str | None, str | None]:
    """
    Returns (localStorageJSON_or_None, sessionStorageJSON_or_None) for the CURRENT page origin.
    """
    local_storage = await page.evaluate(
        """() => {
          try {
            const o = {};
            for (let i = 0; i < window.localStorage.length; i++) {
              const k = window.localStorage.key(i);
              o[k] = window.localStorage.getItem(k);
            }
            const s = JSON.stringify(o);
            return s === "{}" ? null : s;
          } catch (e) { return null; }
        }"""
    )
    session_storage = await page.evaluate(
        """() => {
          try {
            const o = {};
            for (let i = 0; i < window.sessionStorage.length; i++) {
              const k = window.sessionStorage.key(i);
              o[k] = window.sessionStorage.getItem(k);
            }
            const s = JSON.stringify(o);
            return s === "{}" ? null : s;
          } catch (e) { return null; }
        }"""
    )
    return local_storage, session_storage


def _norm(s: str) -> str:
    return (s or "").strip().lower()


async def _attr(locator, name: str) -> str:
    try:
        v = await locator.get_attribute(name)
        return v or ""
    except Exception:
        return ""


def _score_username(attrs: dict) -> int:
    hay = " ".join([_norm(attrs.get(k, "")) for k in ["name", "id", "placeholder", "autocomplete", "type", "aria"]])
    score = 0
    if "email" in hay:
        score += 6
    if "user" in hay or "username" in hay or "login" in hay:
        score += 5
    if "phone" in hay or "tel" in hay:
        score -= 2
    if attrs.get("type", "") in ["email", "text"]:
        score += 1
    if attrs.get("autocomplete", "") in ["email", "username"]:
        score += 3
    return score


def _score_password(attrs: dict) -> int:
    hay = " ".join([_norm(attrs.get(k, "")) for k in ["name", "id", "placeholder", "autocomplete", "type", "aria"]])
    score = 0
    if attrs.get("type", "") == "password":
        score += 10
    if "password" in hay or "pass" in hay:
        score += 5
    if attrs.get("autocomplete", "") in ["current-password", "new-password"]:
        score += 3
    return score


def _score_submit(attrs: dict, text: str) -> int:
    t = _norm(text)
    hay = " ".join([_norm(attrs.get(k, "")), t])
    score = 0
    if "sign in" in hay or "login" in hay or "connexion" in hay or "se connecter" in hay:
        score += 6
    if "continue" in hay or "next" in hay:
        score += 2
    if "submit" in _norm(attrs.get("type", "")):
        score += 2
    if "cancel" in hay or "register" in hay or "sign up" in hay:
        score -= 4
    return score


async def _auto_find_login_controls(page):
    """
    Returns (username_locator, password_locator, submit_locator, debug_dict)
    best-effort for generic login forms.
    """
    debug = {"candidates": {"username": [], "password": [], "submit": []}}

    pw = page.locator("input[type='password']")
    pw_count = await pw.count()
    if pw_count == 0:
        return None, None, None, {"reason": "no password inputs found"}

    password_locator = None
    best_pw_score = -999
    for i in range(min(pw_count, 25)):
        loc = pw.nth(i)
        try:
            await loc.wait_for(state="visible", timeout=2000)
            attrs = {
                "type": await _attr(loc, "type"),
                "name": await _attr(loc, "name"),
                "id": await _attr(loc, "id"),
                "placeholder": await _attr(loc, "placeholder"),
                "autocomplete": await _attr(loc, "autocomplete"),
                "aria": await _attr(loc, "aria-label"),
            }
            sc = _score_password(attrs)
            debug["candidates"]["password"].append({"attrs": attrs, "score": sc})
            if sc > best_pw_score:
                best_pw_score = sc
                password_locator = loc
        except Exception:
            continue

    if not password_locator:
        return None, None, None, {"reason": "no visible password input"}

    user_inputs = page.locator("input:not([type='hidden']):not([type='password'])")
    user_count = await user_inputs.count()

    username_locator = None
    best_user_score = -999
    for i in range(min(user_count, 60)):
        loc = user_inputs.nth(i)
        try:
            await loc.wait_for(state="visible", timeout=1200)
            attrs = {
                "type": await _attr(loc, "type"),
                "name": await _attr(loc, "name"),
                "id": await _attr(loc, "id"),
                "placeholder": await _attr(loc, "placeholder"),
                "autocomplete": await _attr(loc, "autocomplete"),
                "aria": await _attr(loc, "aria-label"),
            }
            sc = _score_username(attrs)
            debug["candidates"]["username"].append({"attrs": attrs, "score": sc})
            if sc > best_user_score:
                best_user_score = sc
                username_locator = loc
        except Exception:
            continue

    submit_locator = None
    best_submit_score = -999
    buttons = page.locator("button, input[type='submit'], button[type='submit']")
    btn_count = await buttons.count()
    for i in range(min(btn_count, 60)):
        b = buttons.nth(i)
        try:
            await b.wait_for(state="visible", timeout=1200)
            text = ""
            try:
                text = await b.inner_text()
            except Exception:
                pass
            attrs = {
                "type": await _attr(b, "type"),
                "name": await _attr(b, "name"),
                "id": await _attr(b, "id"),
                "aria": await _attr(b, "aria-label"),
            }
            sc = _score_submit(attrs, text)
            debug["candidates"]["submit"].append({"attrs": attrs, "text": text, "score": sc})
            if sc > best_submit_score:
                best_submit_score = sc
                submit_locator = b
        except Exception:
            continue

    if not submit_locator:
        return username_locator, password_locator, None, {"reason": "no visible submit button", **debug}

    return username_locator, password_locator, submit_locator, debug


async def _wait_for_cookie_readiness(
    context,
    service_url: str,
    cookie_wait_name: Optional[str],
    timeout_ms: int,
) -> list[dict]:
    """
    Wait until cookies exist for target domain and (optionally) a cookie name exists.
    Prevents extracting too early.
    """
    domain = _domain_from_url(service_url)
    poll_ms = 250
    elapsed = 0

    while elapsed < timeout_ms:
        cookies = await context.cookies()
        has_domain_cookie = any((c.get("domain") or "").lstrip(".").endswith(domain) for c in cookies)

        has_named_cookie = True
        if cookie_wait_name:
            has_named_cookie = any(c.get("name") == cookie_wait_name for c in cookies)

        if has_domain_cookie and has_named_cookie:
            return cookies

        await context.pages[0].wait_for_timeout(poll_ms) if getattr(context, "pages", None) else None
        elapsed += poll_ms

    # last attempt
    return await context.cookies()





async def login_and_get_cookies(
    service_url: str,
    username: str,
    password: str,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Generic relay login with robust waits to avoid extracting cookies too early.
    headless=False (browser visible).

    profile options (all optional):
      - pre_fill_wait_ms: int (default 1200)          # wait after goto before finding fields
      - between_actions_wait_ms: int (default 250)    # small pauses between fill/click
      - after_submit_wait_ms: int (default 2500)      # wait right after submit click
      - post_login_timeout_ms: int (default 20000)    # total time to wait for "connected" state
      - post_login_url_contains: str                  # best generic signal (e.g. "inventory.html")
      - post_login_selector: str                      # selector visible only when logged in
      - post_login_goto: str                          # explicit connected page to open after login
      - stay_connected_ms: int (default 4000)         # keep page open a bit while cookies settle
      - cookie_wait_name: str                         # wait for a specific cookie name
      - cookie_min_count: int (default 1)             # require at least N cookies for the domain
      - cookie_wait_timeout_ms: int (default 15000)   # polling time for cookie readiness
      - username_selector/password_selector/submit_selector/open_login_selector/goto_wait_until: same as before
    """
    profile = profile or {}

    def _as_list(x, default_list):
        if not x:
            return default_list
        if isinstance(x, str):
            return [x]
        if isinstance(x, list):
            return x
        return default_list

    username_selectors = _as_list(
        profile.get("username_selector"),
        ["input[type='email']", "input[name='email']", "input#email", "input[name='username']", "input[type='text']"],
    )
    password_selectors = _as_list(
        profile.get("password_selector"),
        ["input[type='password']", "input[name='password']", "input#password"],
    )
    submit_selectors = _as_list(
        profile.get("submit_selector"),
        ["button[type='submit']", "input[type='submit']", "button:has-text('Login')", "button:has-text('Sign in')"],
    )

    open_login_selector = profile.get("open_login_selector")
    goto_wait_until = profile.get("goto_wait_until", "domcontentloaded")

    pre_fill_wait_ms = int(profile.get("pre_fill_wait_ms", 1200))
    between_actions_wait_ms = int(profile.get("between_actions_wait_ms", 250))
    after_submit_wait_ms = int(profile.get("after_submit_wait_ms", 2500))

    post_login_timeout_ms = int(profile.get("post_login_timeout_ms", 20_000))
    post_login_url_contains = profile.get("post_login_url_contains")
    post_login_selector = profile.get("post_login_selector")
    post_login_goto = profile.get("post_login_goto")

    stay_connected_ms = int(profile.get("stay_connected_ms", 4000))

    cookie_wait_name = profile.get("cookie_wait_name")
    cookie_min_count = int(profile.get("cookie_min_count", 1))
    cookie_wait_timeout_ms = int(profile.get("cookie_wait_timeout_ms", 15_000))

    used = {"username": None, "password": None, "submit": None}
    browser = None
    page = None

    try:
        service_url = (service_url or "").strip()
        if not service_url:
            raise Exception("service_url is empty")

        if service_url.startswith("http://"):
            service_url = "https://" + service_url[len("http://") :]

        if not service_url.startswith(("http://", "https://")):
            service_url = "https://" + service_url

        origin = _origin_from_url(service_url)
        domain = _domain_from_url(service_url)

        async with async_playwright() as p:
            # Always visible
            browser = await p.chromium.launch(
                headless=False,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(DEFAULT_TIMEOUT_MS)

            # 1) Go to login page and let it settle
            await page.goto(service_url, wait_until=goto_wait_until)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass

            if pre_fill_wait_ms > 0:
                await page.wait_for_timeout(pre_fill_wait_ms)

            # Optional open login modal
            if open_login_selector:
                try:
                    await page.locator(open_login_selector).first.click()
                    await page.wait_for_timeout(between_actions_wait_ms)
                except Exception:
                    pass

            before_url = page.url

            # 2) Auto-detect once (if your helper exists), otherwise fallback selectors
            used_auto = False
            try:
                auto_user, auto_pw, auto_submit, auto_debug = await _auto_find_login_controls(page)
            except Exception:
                auto_user = auto_pw = auto_submit = None
                auto_debug = {"reason": "auto detection failed"}

            if auto_pw is not None and auto_submit is not None:
                used_auto = True

                if auto_user is not None:
                    try:
                        await auto_user.wait_for(state="visible", timeout=8000)
                        await auto_user.click(timeout=2000)
                        await auto_user.fill("")
                        await page.wait_for_timeout(between_actions_wait_ms)
                        await auto_user.type(username, delay=35)
                        used["username"] = "auto-detect"
                    except Exception:
                        pass

                await auto_pw.wait_for(state="visible", timeout=8000)
                await auto_pw.click(timeout=2000)
                await auto_pw.fill("")
                await page.wait_for_timeout(between_actions_wait_ms)
                await auto_pw.type(password, delay=35)
                used["password"] = "auto-detect"

                try:
                    await page.wait_for_timeout(between_actions_wait_ms)
                    await auto_submit.click(timeout=8000)
                    used["submit"] = "auto-detect"
                except Exception:
                    await auto_pw.press("Enter")
                    used["submit"] = "auto-detect:press-enter"

            else:
                # Fallback: explicit selectors
                username_locator = None
                for sel in username_selectors:
                    try:
                        loc = page.locator(sel).first
                        await loc.wait_for(state="visible", timeout=10_000)
                        await loc.scroll_into_view_if_needed()
                        await loc.click(timeout=2000)
                        await loc.fill("")
                        await page.wait_for_timeout(between_actions_wait_ms)
                        await loc.type(username, delay=35)
                        used["username"] = sel
                        username_locator = loc
                        break
                    except Exception:
                        continue

                if not username_locator:
                    raise Exception("Cannot find/fill username field. Tried: " + ", ".join(username_selectors))

                password_locator = None
                for sel in password_selectors:
                    try:
                        loc = page.locator(sel).first
                        await loc.wait_for(state="visible", timeout=10_000)
                        await loc.scroll_into_view_if_needed()
                        await loc.click(timeout=2000)
                        await loc.fill("")
                        await page.wait_for_timeout(between_actions_wait_ms)
                        await loc.type(password, delay=35)
                        used["password"] = sel
                        password_locator = loc
                        break
                    except Exception:
                        continue

                if not password_locator:
                    raise Exception("Cannot find/fill password field. Tried: " + ", ".join(password_selectors))

                clicked = False
                for sel in submit_selectors:
                    try:
                        btn = page.locator(sel).first
                        await btn.wait_for(state="visible", timeout=8_000)
                        await page.wait_for_timeout(between_actions_wait_ms)
                        await btn.click()
                        used["submit"] = sel
                        clicked = True
                        break
                    except Exception:
                        continue

                if not clicked:
                    await password_locator.press("Enter")
                    used["submit"] = "press:Enter"

            # 3) After submit: give time for redirect + SPA routing
            if after_submit_wait_ms > 0:
                await page.wait_for_timeout(after_submit_wait_ms)

            # Try to settle network (don't fail hard if SPA keeps sockets open)
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass

            # 4) Wait for "connected" proof (URL contains or selector), best-effort
            login_detected = False

            if post_login_url_contains:
                try:
                    await page.wait_for_url(f"**{post_login_url_contains}**", timeout=post_login_timeout_ms)
                    login_detected = True
                except Exception:
                    pass

            if (not login_detected) and post_login_selector:
                try:
                    await page.locator(post_login_selector).first.wait_for(state="visible", timeout=post_login_timeout_ms)
                    login_detected = True
                except Exception:
                    pass

            # 5) Optional explicit connected page navigation (VERY effective for sites like saucedemo)
            if post_login_goto:
                try:
                    await page.goto(post_login_goto, wait_until="domcontentloaded")
                    await page.wait_for_timeout(800)
                    login_detected = True
                except Exception:
                    pass

            # 6) Stay connected a bit so cookies/storage fully settle BEFORE extraction
            if stay_connected_ms > 0:
                await page.wait_for_timeout(stay_connected_ms)

            # 7) Cookie readiness polling loop (avoid extracting too early)
            cookies = []
            poll_ms = 250
            elapsed = 0
            while elapsed < cookie_wait_timeout_ms:
                cookies = await context.cookies()

                domain_cookies = [
                    c for c in cookies
                    if (c.get("domain") or "").lstrip(".").endswith(domain)
                ]
                ok_count = len(domain_cookies) >= cookie_min_count

                ok_name = True
                if cookie_wait_name:
                    ok_name = any(c.get("name") == cookie_wait_name for c in domain_cookies)

                if ok_count and ok_name:
                    break

                await page.wait_for_timeout(poll_ms)
                elapsed += poll_ms

            # One last tiny settle
            await page.wait_for_timeout(300)

            # 8) Dump storage AFTER cookies are ready
            local_storage, session_storage = await _dump_storage(page)

            return {
                "cookies": cookies,
                "localStorage": local_storage,
                "sessionStorage": session_storage,
                "current_url": page.url,
                "title": await page.title(),
                "used_selectors": used,
                "login_detected": login_detected,
                "domain": domain,
                "origin": origin,
                "debug": {
                    "before_url": before_url,
                    "after_url": page.url,
                    "used_auto_detect": used_auto,
                    "auto_debug": auto_debug if used_auto else None,
                    "cookie_wait_elapsed_ms": elapsed,
                    "domain_cookie_count": len([c for c in cookies if (c.get("domain") or "").lstrip(".").endswith(domain)]),
                },
            }

    except Exception as e:
        raise Exception(f"Playwright login failed: {str(e)}")

    finally:
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass



