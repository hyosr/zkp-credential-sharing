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

async def login_and_get_cookies(
    service_url: str,
    username: str,
    password: str,
    profile: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

    if profile is None:
        profile = {}

    username_selector = profile.get("username_selector", "input[type='email'], input[name='email']")
    password_selector = profile.get("password_selector", "input[type='password']")
    submit_selector = profile.get("submit_selector", "button[type='submit']")
    # Augmenter le délai d'attente après soumission
    post_login_wait = profile.get("post_login_wait", 5000)  # 5 secondes par défaut

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(service_url, wait_until="networkidle")

            # Attendre que les champs soient présents
            await page.wait_for_selector(username_selector, timeout=10000)
            await page.fill(username_selector, username)

            await page.wait_for_selector(password_selector, timeout=5000)
            await page.fill(password_selector, password)

            await page.wait_for_selector(submit_selector, timeout=5000)
            await page.click(submit_selector)

            # Attendre après soumission
            # Soit un temps fixe, soit attendre que l'URL change
            try:
                # Attendre que l'URL ne soit plus l'URL de login (optionnel)
                await page.wait_for_function(
                    f"window.location.href !== '{service_url}'",
                    timeout=post_login_wait
                )
            except PlaywrightTimeoutError:
                # Si l'URL ne change pas, on attend juste un peu
                await page.wait_for_timeout(post_login_wait)

            # Attendre un état stable
            await page.wait_for_load_state("networkidle")

            # Récupérer les cookies
            cookies = await page.context.cookies()

            # Optionnel : récupérer le localStorage si la session y est stockée
            localStorage = await page.evaluate("() => JSON.stringify(window.localStorage)")

            return {
                "cookies": cookies,
                "localStorage": localStorage if localStorage != "{}" else None,
                "current_url": page.url,
                "title": await page.title(),
                "used_selectors": {
                    "username": username_selector,
                    "password": password_selector,
                    "submit": submit_selector,
                }
            }
        except Exception as e:
            # En cas d'erreur, on peut prendre une capture d'écran pour debug
            await page.screenshot(path="debug_login_failure.png")
            raise Exception(f"Playwright login failed: {str(e)}")
        finally:
            await browser.close()





# async def login_and_get_cookies(
#     service_url: str,
#     username: str,
#     password: str,
#     profile: Optional[Dict[str, Any]] = None
# ) -> Dict[str, Any]:
#     from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

#     if profile is None:
#         profile = {}

#     username_selector = profile.get("username_selector", "input[type='email'], input[name='email']")
#     password_selector = profile.get("password_selector", "input[type='password']")
#     submit_selector = profile.get("submit_selector", "button[type='submit']")
#     post_login_wait = profile.get("post_login_wait", 0)

#     async with async_playwright() as p:
#         browser = await p.chromium.launch(headless=True)
#         page = await browser.new_page()
#         try:
#             await page.goto(service_url, wait_until="networkidle")

#             # Attendre que les champs soient présents avant de remplir
#             await page.wait_for_selector(username_selector, timeout=10000)
#             await page.fill(username_selector, username)

#             await page.wait_for_selector(password_selector, timeout=5000)
#             await page.fill(password_selector, password)

#             await page.wait_for_selector(submit_selector, timeout=5000)
#             await page.click(submit_selector)

#             if post_login_wait > 0:
#                 await page.wait_for_timeout(post_login_wait)
#             else:
#                 await page.wait_for_load_state("networkidle")

#             cookies = await page.context.cookies()
#             return {
#                 "cookies": cookies,
#                 "current_url": page.url,
#                 "title": await page.title(),
#                 "used_selectors": {
#                     "username": username_selector,
#                     "password": password_selector,
#                     "submit": submit_selector,
#                 }
#             }
#         except PlaywrightTimeoutError as e:
#             raise Exception(f"Timeout en attendant les sélecteurs: {e}")
#         except Exception as e:
#             raise Exception(f"Playwright login failed: {str(e)}")
#         finally:
#             await browser.close()