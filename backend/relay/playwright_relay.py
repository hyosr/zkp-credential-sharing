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
    


async def _dump_web_storage(page) -> tuple[str | None, str | None]:
    """
    Returns (localStorageJSON, sessionStorageJSON) for the current origin.
    Uses page.evaluate in the page context.
    """
    local_storage = await page.evaluate(
        """() => {
          try {
            const o = {};
            for (let i = 0; i < window.localStorage.length; i++) {
              const k = window.localStorage.key(i);
              o[k] = window.localStorage.getItem(k);
            }
            return JSON.stringify(o);
          } catch (e) {
            return null;
          }
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
            return JSON.stringify(o);
          } catch (e) {
            return null;
          }
        }"""
    )
    if local_storage == "{}":
        local_storage = None
    if session_storage == "{}":
        session_storage = None
    return local_storage, session_storage



async def _dump_storage(page) -> tuple[str | None, str | None]:
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
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(DEFAULT_TIMEOUT_MS)


            if service_url.startswith("http://"):
                service_url = "https://" + service_url[len("http://"):]
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


            # After submitting, force-load the home page so the SPA initializes with cookies
            try:
                await page.goto("https://recolyse.com/", wait_until="domcontentloaded")
                # give the app time to run JS and potentially populate storage / render logged-in UI
                await page.wait_for_timeout(2500)
            except Exception:
                pass


            # ✅ IMPORTANT: load home so SPA writes persist:root/token/user into localStorage
            try:
                await page.goto("https://recolyse.com/", wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)
            except Exception:
                pass

            cookies = await context.cookies()



            # If site uses localStorage token (Recolyse does), seed it from cookie token
            try:
                jwt_token = None
                for c in cookies:
                    if c.get("name") == "token" and c.get("value"):
                        jwt_token = c["value"]
                        break

                if jwt_token:
        # Make sure we are on correct origin
                    await page.goto("https://recolyse.com/", wait_until="domcontentloaded")
                    await page.wait_for_timeout(1000)

                    await page.evaluate(
                        """(t) => {
                        try {
                            window.localStorage.setItem("token", t);
                        } catch (e) {}
                    }""",
                    jwt_token,
        )

        # Let the SPA read storage and settle
                    await page.reload(wait_until="domcontentloaded")
                    await page.wait_for_timeout(1500)
            except Exception:
                pass

            local_storage, session_storage = await _dump_storage(page)

            return {
                "cookies": cookies,
                "localStorage": local_storage,
                "sessionStorage": session_storage,
                "current_url": page.url,
                "title": await page.title(),
                "used_selectors": used,
            }# ✅ IMPORTANT: load home so SPA writes persist:root/token/user into localStorage




            

            # cookies = await context.cookies()

            # local_storage = await page.evaluate("() => JSON.stringify(window.localStorage)")

            # return {
            #     "cookies": cookies,
            #     "localStorage": local_storage if local_storage != "{}" else None,
            #     "current_url": page.url if page else service_url,
            #     "title": (await page.title()) if page else "",
            #     "used_selectors": used,
            #     "domain": _domain_from_url(service_url),
            #     "login_detected": login_detected,
            # }


            # session_storage = await page.evaluate("() => JSON.stringify(window.sessionStorage)")


#             cookies = await context.cookies()

# # ✅ ensure we are on the correct origin before dumping storage
# # Some sites set storage only after redirect; visit homepage once after login.
#             try:
#                 await page.goto("https://recolyse.com/", wait_until="domcontentloaded")
#             except Exception:
#                 pass

#             local_storage, session_storage = await _dump_web_storage(page)

#             if "/login" in page.url:
#                 raise Exception("Login did not stick (still on /login after submit).")






#             return {
#                 "cookies": cookies,
#                 "localStorage": local_storage,
#                 "sessionStorage": session_storage,
#                 "current_url": page.url,
#                 "title": await page.title(),
#                 "used_selectors": used,
#                 "login_detected": login_detected,
#             }





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


























