# from __future__ import annotations

# from typing import Any, Dict, Optional
# import urllib.parse

# from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# DEFAULT_TIMEOUT_MS = 30_000


# def _domain_from_url(url: str) -> str:
#     try:
#         d = urllib.parse.urlparse(url).netloc
#         return d.split(":")[0].lower().strip()
#     except Exception:
#         return ""


# async def login_and_get_cookies(
#     service_url: str,
#     username: str,
#     password: str,
#     profile: Optional[Dict[str, Any]] = None,
# ) -> Dict[str, Any]:
#     """
#     Headless login via Playwright. Returns:
#     - cookies
#     - current_url
#     - title
#     - used_selectors
#     """
#     profile = profile or {}

#     # Allow profile values to be str or list[str]
#     def _as_list(x, default_list):
#         if not x:
#             return default_list
#         if isinstance(x, str):
#             return [x]
#         if isinstance(x, list):
#             return x
#         return default_list

#     username_selectors = _as_list(
#         profile.get("username_selector"),
#         [
#             "input[type='email']",
#             "input[name='email']",
#             "input#email",
#             "input[name='username']",
#             "input[type='text']",
#         ],
#     )
#     password_selectors = _as_list(
#         profile.get("password_selector"),
#         [
#             "input[type='password']",
#             "input[name='password']",
#             "input#password",
#         ],
#     )
#     submit_selectors = _as_list(
#         profile.get("submit_selector"),
#         [
#             "button[type='submit']",
#             "input[type='submit']",
#             "button:has-text('Login')",
#             "button:has-text('Sign in')",
#             "button:has-text('Se connecter')",
#             "button:has-text('Connexion')",
#         ],
#     )

#     # optional: click a "open login modal" button first
#     open_login_selector = profile.get("open_login_selector")  # optional
#     post_login_wait_ms = int(profile.get("post_login_wait", 2000))
#     goto_wait_until = profile.get("goto_wait_until", "domcontentloaded")


#     login_detected = False

#     post_login_selector = profile.get("post_login_selector")
#     post_login_timeout_ms = int(profile.get("post_login_timeout_ms", 10_000))
#     post_login_url_contains = profile.get("post_login_url_contains")

#     # 1) If we know a post-login selector, wait for it
#     if post_login_selector:
#         try:
#             await page.locator(post_login_selector).first.wait_for(
#                         state="visible", timeout=post_login_timeout_ms
#                     )
#             login_detected = True
#         except PlaywrightTimeoutError:
#                     login_detected = False

#             # 2) If we know a URL pattern, wait for it (soft)
#     if (not login_detected) and post_login_url_contains:
#         try:
#             await page.wait_for_url(f"**{post_login_url_contains}**", timeout=post_login_timeout_ms)
#             login_detected = True
#         except PlaywrightTimeoutError:
#             pass

#             # 3) Small extra settle time for SPA redirects
#     if not login_detected:
#         await page.wait_for_timeout(800)








#     async with async_playwright() as p:
#         browser = await p.chromium.launch(headless=False)
#         context = await browser.new_context()
#         page = await context.new_page()
#         page.set_default_timeout(DEFAULT_TIMEOUT_MS)

#         used = {"username": None, "password": None, "submit": None}

#         try:
#             await page.goto(service_url, wait_until=goto_wait_until)

#             # If site needs an extra click to show login form
#             if open_login_selector:
#                 try:
#                     await page.locator(open_login_selector).first.click()
#                 except Exception:
#                     # don't fail hard here
#                     pass

#             # 1) find username field
#             username_locator = None
#             for sel in username_selectors:
#                 try:
#                     loc = page.locator(sel).first
#                     await loc.wait_for(state="visible", timeout=10_000)
#                     username_locator = loc
#                     used["username"] = sel
#                     break
#                 except PlaywrightTimeoutError:
#                     continue

#             if not username_locator:
#                 raise Exception(
#                     "Cannot find username/email input. Tried: " + ", ".join(username_selectors)
#                 )

#             # 2) find password field
#             password_locator = None
#             for sel in password_selectors:
#                 try:
#                     loc = page.locator(sel).first
#                     await loc.wait_for(state="visible", timeout=10_000)
#                     password_locator = loc
#                     used["password"] = sel
#                     break
#                 except PlaywrightTimeoutError:
#                     continue

#             if not password_locator:
#                 raise Exception(
#                     "Cannot find password input. Tried: " + ", ".join(password_selectors)
#                 )

#             await username_locator.fill(username)
#             await password_locator.fill(password)

#             # 3) click submit
#             clicked = False
#             for sel in submit_selectors:
#                 try:
#                     btn = page.locator(sel).first
#                     await btn.wait_for(state="visible", timeout=5_000)
#                     await btn.click()
#                     used["submit"] = sel
#                     clicked = True
#                     break
#                 except PlaywrightTimeoutError:
#                     continue
#                 except Exception:
#                     continue

#             if not clicked:
#                 # fallback: press Enter on password field
#                 await password_locator.press("Enter")
#                 used["submit"] = "press:Enter"

#             # 4) wait for navigation / settle
#             try:
#                 await page.wait_for_load_state("networkidle", timeout=5_000)
#             except PlaywrightTimeoutError:
#                 # some SPAs never become networkidle; accept
#                 pass

#             if post_login_wait_ms > 0:
#                 await page.wait_for_timeout(post_login_wait_ms)

#             cookies = await context.cookies()

#             return {
#                 "cookies": cookies,
#                 "current_url": page.url,
#                 "title": await page.title(),
#                 "used_selectors": used,
#                 "domain": _domain_from_url(service_url),
#                 "login_detected": login_detected,  # ✅ NEW

#             }

#         except Exception as e:
#             raise Exception(f"Playwright login failed: {str(e)}")
#         finally:
#             await browser.close()



from __future__ import annotations

from typing import Any, Dict, Optional
import urllib.parse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


DEFAULT_TIMEOUT_MS = 30_000


def _domain_from_url(url: str) -> str:
    try:
        d = urllib.parse.urlparse(url).netloc
        return d.split(":")[0].lower().strip()
    except Exception:
        return ""


async def login_and_get_cookies(
    service_url: str,
    username: str,
    password: str,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Headless login via Playwright. Returns:
    - cookies
    - current_url
    - title
    - used_selectors
    - login_detected
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
        [
            "input[type='email']",
            "input[name='email']",
            "input#email",
            "input[name='username']",
            "input[type='text']",
        ],
    )
    password_selectors = _as_list(
        profile.get("password_selector"),
        [
            "input[type='password']",
            "input[name='password']",
            "input#password",
        ],
    )
    submit_selectors = _as_list(
        profile.get("submit_selector"),
        [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Login')",
            "button:has-text('Sign in')",
            "button:has-text('Se connecter')",
            "button:has-text('Connexion')",
        ],
    )

    open_login_selector = profile.get("open_login_selector")
    post_login_wait_ms = int(profile.get("post_login_wait", 1500))
    goto_wait_until = profile.get("goto_wait_until", "domcontentloaded")

    post_login_selector = profile.get("post_login_selector")
    post_login_timeout_ms = int(profile.get("post_login_timeout_ms", 10_000))
    post_login_url_contains = profile.get("post_login_url_contains")

    used = {"username": None, "password": None, "submit": None}

    browser = None
    context = None
    page = None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(DEFAULT_TIMEOUT_MS)

            await page.goto(service_url, wait_until=goto_wait_until)

            # Optional click to open login modal/page
            if open_login_selector:
                try:
                    await page.locator(open_login_selector).first.click()
                except Exception:
                    pass

            # Find username/email input
            username_locator = None
            for sel in username_selectors:
                try:
                    loc = page.locator(sel).first
                    await loc.wait_for(state="visible", timeout=10_000)
                    username_locator = loc
                    used["username"] = sel
                    break
                except PlaywrightTimeoutError:
                    continue

            if not username_locator:
                raise Exception("Cannot find username/email input. Tried: " + ", ".join(username_selectors))

            # Find password input
            password_locator = None
            for sel in password_selectors:
                try:
                    loc = page.locator(sel).first
                    await loc.wait_for(state="visible", timeout=10_000)
                    password_locator = loc
                    used["password"] = sel
                    break
                except PlaywrightTimeoutError:
                    continue

            if not password_locator:
                raise Exception("Cannot find password input. Tried: " + ", ".join(password_selectors))

            await username_locator.fill(username)
            await password_locator.fill(password)

            # Submit
            clicked = False
            for sel in submit_selectors:
                try:
                    btn = page.locator(sel).first
                    await btn.wait_for(state="visible", timeout=5_000)
                    await btn.click()
                    used["submit"] = sel
                    clicked = True
                    break
                except PlaywrightTimeoutError:
                    continue
                except Exception:
                    continue

            if not clicked:
                await password_locator.press("Enter")
                used["submit"] = "press:Enter"

            # SPA-friendly settle (avoid networkidle)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5_000)
            except PlaywrightTimeoutError:
                pass

            if post_login_wait_ms > 0:
                await page.wait_for_timeout(post_login_wait_ms)

            # Detect login (best effort)
            login_detected = False

            if post_login_selector:
                try:
                    await page.locator(post_login_selector).first.wait_for(
                        state="visible", timeout=post_login_timeout_ms
                    )
                    login_detected = True
                except PlaywrightTimeoutError:
                    login_detected = False

            if (not login_detected) and post_login_url_contains:
                try:
                    await page.wait_for_url(f"**{post_login_url_contains}**", timeout=post_login_timeout_ms)
                    login_detected = True
                except PlaywrightTimeoutError:
                    pass

            cookies = await context.cookies()

            return {
                "cookies": cookies,
                "current_url": page.url if page else service_url,
                "title": (await page.title()) if page else "",
                "used_selectors": used,
                "domain": _domain_from_url(service_url),
                "login_detected": login_detected,
            }

    except Exception as e:
        # NEVER reference page/context/browser if they were not created
        raise Exception(f"Playwright login failed: {str(e)}")

    finally:
        # Close safely
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass






















# import json
# from typing import Dict, Any, Optional

# from playwright.sync_api import sync_playwright


# DEFAULT_PROFILE = {
#     # Generic defaults that work on many login forms
#     "username_selector_candidates": [
#         'input[name="email"]',
#         'input[name="username"]',
#         'input[type="email"]',
#         'input[type="text"]',
#         'input[id*="email" i]',
#         'input[id*="user" i]',
#     ],
#     "password_selector_candidates": [
#         'input[name="password"]',
#         'input[type="password"]',
#         'input[id*="pass" i]',
#     ],
#     "submit_selector_candidates": [
#         'button[type="submit"]',
#         'input[type="submit"]',
#         'button:has-text("Login")',
#         'button:has-text("Sign in")',
#         'button:has-text("Connexion")',
#         'button:has-text("Se connecter")',
#     ],
#     "post_login_wait_ms": 2500,
# }


# def _first_visible(page, selectors):
#     for sel in selectors:
#         loc = page.locator(sel)
#         try:
#             if loc.count() > 0:
#                 # pick first match
#                 el = loc.first
#                 if el.is_visible():
#                     return el, sel
#         except Exception:
#             continue
#     return None, None

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
#     # Augmenter le délai d'attente après soumission
#     post_login_wait = profile.get("post_login_wait", 5000)  # 5 secondes par défaut

#     async with async_playwright() as p:
#         browser = await p.chromium.launch(headless=True)
#         page = await browser.new_page()
#         try:
#             await page.goto(service_url, wait_until="networkidle")

#             # Attendre que les champs soient présents
#             await page.wait_for_selector(username_selector, timeout=10000)
#             await page.fill(username_selector, username)

#             await page.wait_for_selector(password_selector, timeout=5000)
#             await page.fill(password_selector, password)

#             await page.wait_for_selector(submit_selector, timeout=5000)
#             await page.click(submit_selector)

#             # Attendre après soumission
#             # Soit un temps fixe, soit attendre que l'URL change
#             try:
#                 # Attendre que l'URL ne soit plus l'URL de login (optionnel)
#                 await page.wait_for_function(
#                     f"window.location.href !== '{service_url}'",
#                     timeout=post_login_wait
#                 )
#             except PlaywrightTimeoutError:
#                 # Si l'URL ne change pas, on attend juste un peu
#                 await page.wait_for_timeout(post_login_wait)

#             # Attendre un état stable
#             await page.wait_for_load_state("networkidle")

#             # Récupérer les cookies
#             cookies = await page.context.cookies()

#             # Optionnel : récupérer le localStorage si la session y est stockée
#             localStorage = await page.evaluate("() => JSON.stringify(window.localStorage)")

#             return {
#                 "cookies": cookies,
#                 "localStorage": localStorage if localStorage != "{}" else None,
#                 "current_url": page.url,
#                 "title": await page.title(),
#                 "used_selectors": {
#                     "username": username_selector,
#                     "password": password_selector,
#                     "submit": submit_selector,
#                 }
#             }
#         except Exception as e:
#             # En cas d'erreur, on peut prendre une capture d'écran pour debug
#             await page.screenshot(path="debug_login_failure.png")
#             raise Exception(f"Playwright login failed: {str(e)}")
#         finally:
#             await browser.close()





