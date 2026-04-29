from __future__ import annotations

import asyncio
import os
import random
import urllib.parse
from typing import Any, Dict, Optional, Tuple

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

DEFAULT_TIMEOUT_MS = 30_000
HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "1") == "1"

# ─── Stealth ──────────────────────────────────────────────────────────────────
_STEALTH_JS = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const a = [
                { name:'Chrome PDF Plugin', filename:'internal-pdf-viewer', description:'Portable Document Format' },
                { name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', description:'' },
                { name:'Native Client', filename:'internal-nacl-plugin', description:'' },
            ];
            a.__proto__ = navigator.plugins.__proto__;
            return a;
        }
    });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
    window.chrome = { runtime:{}, loadTimes:function(){}, csi:function(){}, app:{} };
    try {
        const gp = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(p) {
            if (p === 37445) return 'Intel Inc.';
            if (p === 37446) return 'Intel Iris OpenGL Engine';
            return gp.call(this, p);
        };
    } catch(e){}
    const orig = window.navigator.permissions?.query?.bind(navigator.permissions);
    if (orig) navigator.permissions.query = (p) =>
        p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : orig(p);
}
"""

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# ─── 2FA / verification page detection ────────────────────────────────────────
_2FA_URL_PATTERNS = [
    "/two-factor", "/2fa", "/verify", "/otp", "/checkpoint",
    "/challenge", "/confirmation", "/security-code",
    "sms-verification", "phone-verify", "auth/mfa",
    "accounts/login/two_factor", "login/tfvc",
    # LinkedIn spécifique
    "/checkpoint/challenge", "/checkpoint/lg",
]
_2FA_SELECTOR_PATTERNS = [
    "input[name='verificationCode']",
    "input[name='code']",
    "input[name='otp']",
    "input[name='two_factor_code']",
    "input[name='pin']",                    # LinkedIn PIN
    "input[placeholder*='code' i]",
    "input[placeholder*='verification' i]",
    "input[placeholder*='pin' i]",
    "input[autocomplete='one-time-code']",
    "input[id*='code' i]",
    "#approvals_code",
    "[data-testid='ocfEnterTextTextInput']",
]

async def _detect_2fa(page) -> bool:
    url = page.url.lower()
    if any(p in url for p in _2FA_URL_PATTERNS):
        return True
    for sel in _2FA_SELECTOR_PATTERNS:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=1500):
                return True
        except Exception:
            pass
    return False


# ─── Per-site strategies ──────────────────────────────────────────────────────
#
# FIX LINKEDIN :
#   - Sélecteurs corrigés : session_key / session_password (pas #username/#password)
#   - Plusieurs fallbacks dans une liste pour résister aux changements de DOM
#   - pre_wait_ms augmenté à 3000 (LinkedIn charge le form en JS lentement)
#   - multi_step=False MAIS fill_sequence=True pour remplir l'un après l'autre
#     avec une vérification que le champ est bien interactif
#
_SITE_STRATEGIES: dict[str, dict] = {
    "pinterest.com": {
        "login_url": "https://www.pinterest.com/login/",
        "username": "input[name='id']",
        "password": "input[name='password']",
        "submit_selectors": [
            "button[type='submit']",
            "button:has-text('Log in')",
            "button:has-text('Continue')",
            "div[data-test-id='registerFormSubmitButton']",
            "div[data-test-id='loginButton']",
        ],
        "success": ["[data-test-id='header-avatar']", "[data-test-id='homefeed-feed']"],
        "pre_wait_ms": 2000,
        "multi_step": False,
    },

    # ── LinkedIn FIX COMPLET ───────────────────────────────────────────────────
    # Problèmes résolus :
    # 1. Mauvais sélecteurs : LinkedIn utilise name= pas id=
    # 2. pre_wait trop court : le formulaire se charge en JS
    # 3. Pas de vérification que le champ est éditable avant de taper
    # 4. Checkpoint 2FA : détecté et attendu manuellement
    "linkedin.com": {
        "login_url": "https://www.linkedin.com/login",
        "username_selectors": [          # liste = essayés dans l'ordre
            "input[name='session_key']",
            "input[autocomplete='username']",
            "input[id='username']",
            "input[type='email']",
        ],
        "password_selectors": [
            "input[name='session_password']",
            "input[autocomplete='current-password']",
            "input[id='password']",
            "input[type='password']",
        ],
        "submit_selectors": [
            "button[type='submit'][data-litms-control-urn*='login']",
            "button[type='submit'].btn__primary--large",
            "button[type='submit']",
            "button:has-text('Sign in')",
            "button:has-text('Se connecter')",
            ".btn__primary--large",
        ],
        "success": [
            ".feed-identity-module",
            "a[href='/feed/']",
            ".global-nav__me",
            "[data-test-global-nav-link]",
            "header.global-nav",
        ],
        "pre_wait_ms": 3500,    # LinkedIn charge le form en JS — plus long
        "multi_step": False,
        "use_selector_lists": True,   # Nouveau flag : utiliser username_selectors/password_selectors
    },

    "facebook.com": {
        "login_url": "https://www.facebook.com/",
        "username": "#email",
        "password": "#pass",
        "submit_selectors": [
            "[name='login']",
            "button[type='submit']",
            "[data-testid='royal_login_button']",
        ],
        "success": ["[aria-label='Facebook']", "div[role='feed']", "[data-pagelet='LeftRail']"],
        "pre_wait_ms": 2000,
        "multi_step": False,
    },
    "instagram.com": {
        "login_url": "https://www.instagram.com/accounts/login/",
        "username": "input[name='username']",
        "password": "input[name='password']",
        "submit_selectors": [
            "button[type='submit']",
            "button:has-text('Log in')",
            "button:has-text('Log In')",
        ],
        "success": ["svg[aria-label='Home']"],
        "pre_wait_ms": 2500,
        "multi_step": False,
    },
    "twitter.com": {
        "login_url": "https://x.com/i/flow/login",
        "username": "input[autocomplete='username']",
        "password": "input[name='password']",
        "submit_selectors": ["[data-testid='LoginForm_Login_Button']", "button[type='submit']"],
        "username_next_selectors": ["[data-testid='LoginForm_Login_Button']", "div[role='button']"],
        "success": ["[data-testid='primaryColumn']"],
        "pre_wait_ms": 2000,
        "multi_step": True,
    },
    "x.com": {
        "login_url": "https://x.com/i/flow/login",
        "username": "input[autocomplete='username']",
        "password": "input[name='password']",
        "submit_selectors": ["[data-testid='LoginForm_Login_Button']", "button[type='submit']"],
        "username_next_selectors": ["[data-testid='LoginForm_Login_Button']", "div[role='button']"],
        "success": ["[data-testid='primaryColumn']"],
        "pre_wait_ms": 2000,
        "multi_step": True,
    },
    "google.com": {
        "login_url": "https://accounts.google.com/signin",
        "username": "input[type='email']",
        "password": "input[type='password']",
        "submit_selectors": ["#passwordNext", "button[type='submit']"],
        "username_next_selectors": ["#identifierNext"],
        "success": ["[data-ogsr-up]"],
        "pre_wait_ms": 2000,
        "multi_step": True,
    },
    "reddit.com": {
        "login_url": "https://www.reddit.com/login/",
        "username": "#loginUsername",
        "password": "#loginPassword",
        "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
        "success": ["a[href*='/user/']"],
        "pre_wait_ms": 1500,
        "multi_step": False,
    },
    "github.com": {
        "login_url": "https://github.com/login",
        "username": "#login_field",
        "password": "#password",
        "submit_selectors": ["[type='submit'][name='commit']", "button[type='submit']"],
        "success": [".Header-link--avatar"],
        "pre_wait_ms": 1000,
        "multi_step": False,
    },
    "discord.com": {
        "login_url": "https://discord.com/login",
        "username": "input[name='email']",
        "password": "input[name='password']",
        "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
        "success": ["nav[aria-label='Servers sidebar']"],
        "pre_wait_ms": 2000,
        "multi_step": False,
    },
    "tiktok.com": {
        "login_url": "https://www.tiktok.com/login/phone-or-email/email",
        "username": "input[name='username']",
        "password": "input[type='password']",
        "submit_selectors": ["button[type='submit']"],
        "success": ["[data-e2e='profile-icon']"],
        "pre_wait_ms": 3000,
        "multi_step": False,
    },
}


def _domain_from_url(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.split(":")[0].lower().strip().lstrip("www.")
    except Exception:
        return ""

def _origin_from_url(url: str) -> str:
    p = urllib.parse.urlparse(url)
    scheme = p.scheme or "https"
    netloc = p.netloc or p.path.split("/")[0]
    return f"{scheme}://{netloc}".rstrip("/")


async def _dump_storage(page) -> Tuple[str | None, str | None]:
    ls = await page.evaluate("""() => {
        try {
            const o={};
            for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);o[k]=localStorage.getItem(k);}
            const s=JSON.stringify(o); return s==="{}"?null:s;
        } catch(e){return null;}
    }""")
    ss = await page.evaluate("""() => {
        try {
            const o={};
            for(let i=0;i<sessionStorage.length;i++){const k=sessionStorage.key(i);o[k]=sessionStorage.getItem(k);}
            const s=JSON.stringify(o); return s==="{}"?null:s;
        } catch(e){return null;}
    }""")
    return ls, ss


# ─── Stealth browser factory ──────────────────────────────────────────────────
async def _make_stealth_context(p):
    ua = random.choice(_USER_AGENTS)
    browser = await p.chromium.launch(
        headless=HEADLESS,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1366,768",
        ],
    )
    ctx = await browser.new_context(
        user_agent=ua,
        viewport={"width": 1366, "height": 768},
        locale="en-US",
        timezone_id="America/New_York",
        java_script_enabled=True,
        is_mobile=False,
        has_touch=False,
        color_scheme="light",
    )
    await ctx.add_init_script(_STEALTH_JS)
    return browser, ctx


# ─── Field helpers ────────────────────────────────────────────────────────────

async def _fill(page, selector: str, value: str, timeout_ms: int = 15_000):
    """Remplir un champ par sélecteur unique."""
    loc = page.locator(selector).first
    await loc.wait_for(state="visible", timeout=timeout_ms)
    await loc.scroll_into_view_if_needed()
    await loc.click()
    await loc.fill("")
    await page.wait_for_timeout(random.randint(80, 200))
    await loc.type(value, delay=random.randint(30, 60))


async def _fill_from_list(page, selectors: list[str], value: str, timeout_ms: int = 15_000) -> str | None:
    """
    Essayer chaque sélecteur dans l'ordre.
    Retourne le sélecteur qui a fonctionné, ou None.

    FIX LINKEDIN : LinkedIn charge ses champs en JS — on attend jusqu'à
    timeout_ms que l'un des sélecteurs devienne visible ET éditable.
    """
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            # Attendre que le champ existe dans le DOM
            await loc.wait_for(state="visible", timeout=timeout_ms)
            # Vérifier qu'il est bien éditable (pas disabled/readonly)
            is_editable = await loc.is_editable()
            if not is_editable:
                continue
            await loc.scroll_into_view_if_needed()
            await loc.click()
            await page.wait_for_timeout(random.randint(100, 250))
            await loc.fill("")
            await page.wait_for_timeout(random.randint(80, 150))
            await loc.type(value, delay=random.randint(35, 70))
            # Vérifier que la valeur a bien été saisie
            actual = await loc.input_value()
            if actual and len(actual) > 0:
                return sel
        except Exception:
            continue
    return None


async def _click_first(page, selectors: list[str], timeout_ms: int = 8_000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout_ms)
            await loc.click()
            return True
        except Exception:
            continue
    return False


# ─── LinkedIn-specific strategy ───────────────────────────────────────────────

async def _run_linkedin_strategy(page, strat: dict, username: str, password: str) -> None:
    """
    Stratégie dédiée à LinkedIn.

    LinkedIn a plusieurs particularités :
    1. Le formulaire est rendu par React/JS — apparaît ~1-2s après le chargement
    2. Les sélecteurs historiques (#username, #password) ne fonctionnent PAS
       → LinkedIn utilise name='session_key' et name='session_password'
    3. Après soumission, peut rediriger vers /checkpoint/challenge (code email/SMS)
    4. Le bouton submit a une classe spécifique .btn__primary--large

    Cette fonction gère tout ça proprement.
    """
    print(f"[relay][linkedin] Waiting {strat['pre_wait_ms']}ms for React form to render...")
    await page.wait_for_timeout(strat["pre_wait_ms"])

    # Attendre explicitement que le champ email soit présent dans le DOM
    print("[relay][linkedin] Waiting for email field to appear...")
    try:
        await page.wait_for_selector(
            "input[name='session_key'], input[autocomplete='username']",
            state="visible",
            timeout=15_000,
        )
    except PlaywrightTimeoutError:
        # Screenshot de debug si le champ n'apparaît pas
        await page.screenshot(path="/tmp/linkedin_debug_no_form.png")
        raise Exception(
            "LinkedIn: email field not found after 15s. "
            "Check /tmp/linkedin_debug_no_form.png"
        )

    print(f"[relay][linkedin] Filling username ({username[:3]}***)")
    used_user_sel = await _fill_from_list(
        page,
        strat["username_selectors"],
        username,
        timeout_ms=10_000,
    )
    if not used_user_sel:
        await page.screenshot(path="/tmp/linkedin_debug_no_user.png")
        raise Exception(
            "LinkedIn: could not fill username field. "
            "Check /tmp/linkedin_debug_no_user.png"
        )
    print(f"[relay][linkedin] Username filled via '{used_user_sel}'")

    await page.wait_for_timeout(random.randint(400, 700))

    print("[relay][linkedin] Filling password...")
    used_pw_sel = await _fill_from_list(
        page,
        strat["password_selectors"],
        password,
        timeout_ms=10_000,
    )
    if not used_pw_sel:
        await page.screenshot(path="/tmp/linkedin_debug_no_pw.png")
        raise Exception(
            "LinkedIn: could not fill password field. "
            "Check /tmp/linkedin_debug_no_pw.png"
        )
    print(f"[relay][linkedin] Password filled via '{used_pw_sel}'")

    await page.wait_for_timeout(random.randint(300, 600))

    print("[relay][linkedin] Clicking submit...")
    clicked = await _click_first(page, strat["submit_selectors"], timeout_ms=8_000)
    if not clicked:
        await page.keyboard.press("Enter")
        print("[relay][linkedin] Fallback: pressed Enter")
    else:
        print("[relay][linkedin] Submit clicked")


# ─── Generic strategy executor ────────────────────────────────────────────────

async def _run_strategy(page, strat: dict, username: str, password: str):
    """Exécuter la stratégie d'un site connu."""

    # LinkedIn a sa propre fonction dédiée
    if strat.get("use_selector_lists"):
        return await _run_linkedin_strategy(page, strat, username, password)

    await page.wait_for_timeout(strat.get("pre_wait_ms", 1500))
    submit_sels = strat.get("submit_selectors", ["button[type='submit']"])

    if strat.get("multi_step"):
        await _fill(page, strat["username"], username)
        await page.wait_for_timeout(random.randint(400, 700))
        next_sels = strat.get("username_next_selectors", ["button[type='submit']"])
        if not await _click_first(page, next_sels):
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(random.randint(1500, 2500))
        await _fill(page, strat["password"], password)
        await page.wait_for_timeout(random.randint(300, 600))
        if not await _click_first(page, submit_sels):
            await page.keyboard.press("Enter")
    else:
        await _fill(page, strat["username"], username)
        await page.wait_for_timeout(random.randint(200, 400))
        await _fill(page, strat["password"], password)
        await page.wait_for_timeout(random.randint(300, 500))
        if not await _click_first(page, submit_sels):
            await page.keyboard.press("Enter")


# ─── Success check ────────────────────────────────────────────────────────────

async def _is_logged_in(page, strat: dict | None) -> bool:
    url = page.url.lower()
    login_kw = ["/login", "/signin", "/sign-in", "/accounts/login", "/flow/login", "accounts.google"]
    if not any(k in url for k in login_kw):
        return True
    if strat:
        for sel in strat.get("success", []):
            try:
                if await page.locator(sel).first.is_visible(timeout=2000):
                    return True
            except Exception:
                pass
    return False


# ─── Generic heuristic (unknown sites) ───────────────────────────────────────

def _score_user(attrs: dict) -> int:
    hay = " ".join(attrs.get(k, "") for k in ["name","id","placeholder","autocomplete","type","aria"]).lower()
    s = 0
    if "email" in hay: s += 6
    if "user" in hay or "login" in hay: s += 5
    if "phone" in hay or "tel" in hay: s -= 2
    if attrs.get("type") in ["email","text"]: s += 1
    if attrs.get("autocomplete") in ["email","username"]: s += 3
    return s

def _score_pw(attrs: dict) -> int:
    s = 10 if attrs.get("type") == "password" else 0
    hay = " ".join(attrs.get(k,"") for k in ["name","id","placeholder","autocomplete"]).lower()
    if "password" in hay or "pass" in hay: s += 5
    return s

def _score_submit(attrs: dict, text: str) -> int:
    hay = (text + " " + " ".join(attrs.get(k,"") for k in ["type","name","id","aria"])).lower()
    s = 0
    if any(w in hay for w in ["sign in","login","log in","connexion","se connecter"]): s += 6
    if any(w in hay for w in ["continue","next"]): s += 2
    if "submit" in attrs.get("type",""): s += 2
    if any(w in hay for w in ["cancel","register","sign up","create"]): s -= 4
    return s

async def _get_attr(loc, name: str) -> str:
    try: return (await loc.get_attribute(name)) or ""
    except: return ""

async def _generic_login(page, username: str, password: str):
    await page.wait_for_timeout(1500)

    pw_locs = page.locator("input[type='password']")
    if await pw_locs.count() == 0:
        email_sels = ["input[type='email']","input[name='email']","input[name='username']",
                      "input[autocomplete='email']","input[autocomplete='username']","input[type='text']"]
        for sel in email_sels:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=3000)
                await loc.fill(username)
                await page.wait_for_timeout(500)
                await _click_first(page, ["button:has-text('Next')","button:has-text('Continue')",
                                          "button[type='submit']","input[type='submit']"])
                await page.wait_for_timeout(2000)
                break
            except Exception:
                continue

    pw_locs = page.locator("input[type='password']")
    pw_count = await pw_locs.count()
    if pw_count == 0:
        raise Exception("No password field found")
    pw_loc = pw_locs.first
    best_pw = -999
    for i in range(min(pw_count, 5)):
        loc = pw_locs.nth(i)
        try:
            await loc.wait_for(state="visible", timeout=1500)
            attrs = {k: await _get_attr(loc, k) for k in ["type","name","id","placeholder","autocomplete"]}
            sc = _score_pw(attrs)
            if sc > best_pw:
                best_pw = sc
                pw_loc = loc
        except Exception:
            pass

    user_locs = page.locator("input:not([type='hidden']):not([type='password'])")
    user_count = await user_locs.count()
    user_loc = None
    best_us = -999
    for i in range(min(user_count, 20)):
        loc = user_locs.nth(i)
        try:
            await loc.wait_for(state="visible", timeout=1000)
            attrs = {k: await _get_attr(loc, k) for k in ["type","name","id","placeholder","autocomplete","aria-label"]}
            sc = _score_user(attrs)
            if sc > best_us:
                best_us = sc
                user_loc = loc
        except Exception:
            pass

    if user_loc and best_us > 2:
        try:
            await user_loc.click()
            await user_loc.fill(username)
            await page.wait_for_timeout(200)
        except Exception:
            pass

    await pw_loc.wait_for(state="visible", timeout=8000)
    await pw_loc.click()
    await pw_loc.fill("")
    await pw_loc.type(password, delay=random.randint(30, 60))
    await page.wait_for_timeout(300)

    btns = page.locator("button, input[type='submit']")
    btn_count = await btns.count()
    submit_loc = None
    best_sub = -999
    for i in range(min(btn_count, 20)):
        b = btns.nth(i)
        try:
            await b.wait_for(state="visible", timeout=1000)
            text = ""
            try: text = await b.inner_text()
            except: pass
            attrs = {k: await _get_attr(b, k) for k in ["type","name","id","aria-label"]}
            sc = _score_submit(attrs, text)
            if sc > best_sub:
                best_sub = sc
                submit_loc = b
        except Exception:
            pass

    if submit_loc and best_sub >= 0:
        try: await submit_loc.click(timeout=8000)
        except: await pw_loc.press("Enter")
    else:
        await pw_loc.press("Enter")


# ─── 2FA wait loop ────────────────────────────────────────────────────────────

async def _wait_for_2fa_completion(page, strat: dict | None, max_wait_s: int = 180) -> None:
    """
    Après détection d'une page 2FA/checkpoint, attendre que l'utilisateur
    complète la vérification manuellement (visible seulement si headless=False).

    Polling toutes les 5s jusqu'à max_wait_s secondes.
    """
    print(f"[relay] 2FA/checkpoint détecté sur {page.url}")
    print(f"[relay] En attente de la complétion manuelle (max {max_wait_s}s)...")
    print("[relay] → Completez la vérification dans le navigateur ouvert")

    polls = max_wait_s // 5
    for i in range(polls):
        await page.wait_for_timeout(5000)
        still_2fa = await _detect_2fa(page)
        logged = await _is_logged_in(page, strat)
        if logged or not still_2fa:
            print(f"[relay] ✓ 2FA complété après {(i+1)*5}s")
            return
        print(f"[relay] Toujours sur la page de vérification ({page.url}) — {(i+1)*5}s écoulées...")

    print(f"[relay] ⚠ Timeout 2FA après {max_wait_s}s")


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

async def login_and_get_cookies(
    service_url: str,
    username: str,
    password: str,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Universal relay login.

    Priorité :
      1. profile avec sélecteurs explicites (ex: recolyse.com)
      2. _SITE_STRATEGIES (pinterest, linkedin, facebook, twitter…)
      3. heuristique générique (sites inconnus)

    Retourne :
      cookies, localStorage, sessionStorage, current_url, title,
      used_selectors, login_detected, tfa_detected, domain, origin, debug
    """
    profile = profile or {}

    def _as_list(x, default):
        if not x: return default
        return [x] if isinstance(x, str) else (x if isinstance(x, list) else default)

    username_selectors = _as_list(profile.get("username_selector"), [])
    password_selectors = _as_list(profile.get("password_selector"), [])
    submit_selectors   = _as_list(profile.get("submit_selector"), [])
    has_profile = bool(username_selectors or password_selectors)

    open_login_sel    = profile.get("open_login_selector")
    goto_wait         = profile.get("goto_wait_until", "domcontentloaded")
    pre_fill_ms       = int(profile.get("pre_fill_wait_ms", 1200))
    between_ms        = int(profile.get("between_actions_wait_ms", 250))
    after_submit_ms   = int(profile.get("after_submit_wait_ms", 2500))
    post_timeout_ms   = int(profile.get("post_login_timeout_ms", 20_000))
    post_url_contains = profile.get("post_login_url_contains")
    post_selector     = profile.get("post_login_selector")
    post_goto         = profile.get("post_login_goto")
    stay_ms           = int(profile.get("stay_connected_ms", 4000))
    cookie_wait_name  = profile.get("cookie_wait_name")
    cookie_min        = int(profile.get("cookie_min_count", 1))
    cookie_timeout_ms = int(profile.get("cookie_wait_timeout_ms", 15_000))

    used = {"username": None, "password": None, "submit": None}
    browser = None

    try:
        service_url = (service_url or "").strip()
        if not service_url:
            raise Exception("service_url is empty")
        if not service_url.startswith(("http://","https://")):
            service_url = "https://" + service_url

        domain = _domain_from_url(service_url)
        origin = _origin_from_url(service_url)
        strat  = _SITE_STRATEGIES.get(domain)
        login_url = strat["login_url"] if (strat and not has_profile) else service_url

        async with async_playwright() as p:
            browser, context = await _make_stealth_context(p)
            page = await context.new_page()
            page.set_default_timeout(DEFAULT_TIMEOUT_MS)

            await page.goto(login_url, wait_until=goto_wait)
            try: await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception: pass

            before_url = page.url
            login_detected = False
            used_method = "unknown"

            # ── Branche 1 : stratégie site connu ─────────────────────────
            if strat and not has_profile:
                await _run_strategy(page, strat, username, password)
                used["username"] = used["password"] = used["submit"] = "strategy"
                used_method = f"strategy:{domain}"

            # ── Branche 2 : sélecteurs custom (profile) ───────────────────
            elif has_profile:
                await page.wait_for_timeout(pre_fill_ms)
                if open_login_sel:
                    try:
                        await page.locator(open_login_sel).first.click()
                        await page.wait_for_timeout(between_ms)
                    except Exception:
                        pass

                pw_loc = None
                for sel in username_selectors:
                    try:
                        loc = page.locator(sel).first
                        await loc.wait_for(state="visible", timeout=10_000)
                        await loc.scroll_into_view_if_needed()
                        await loc.click()
                        await loc.fill("")
                        await page.wait_for_timeout(between_ms)
                        await loc.type(username, delay=35)
                        used["username"] = sel
                        break
                    except Exception:
                        continue
                if not used["username"]:
                    raise Exception("Cannot find username field")

                for sel in password_selectors:
                    try:
                        loc = page.locator(sel).first
                        await loc.wait_for(state="visible", timeout=10_000)
                        await loc.scroll_into_view_if_needed()
                        await loc.click()
                        await loc.fill("")
                        await page.wait_for_timeout(between_ms)
                        await loc.type(password, delay=35)
                        used["password"] = sel
                        pw_loc = loc
                        break
                    except Exception:
                        continue
                if not used["password"]:
                    raise Exception("Cannot find password field")

                clicked = False
                for sel in (submit_selectors or ["button[type='submit']","input[type='submit']"]):
                    try:
                        btn = page.locator(sel).first
                        await btn.wait_for(state="visible", timeout=8_000)
                        await page.wait_for_timeout(between_ms)
                        await btn.click()
                        used["submit"] = sel
                        clicked = True
                        break
                    except Exception:
                        continue
                if not clicked:
                    await pw_loc.press("Enter")
                    used["submit"] = "Enter"
                used_method = "profile_selectors"

            # ── Branche 3 : heuristique générique ────────────────────────
            else:
                await _generic_login(page, username, password)
                used["username"] = used["password"] = used["submit"] = "generic"
                used_method = "generic"

            # ── Post-submit ───────────────────────────────────────────────
            await page.wait_for_timeout(after_submit_ms)
            try: await page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception: pass

            # ── 2FA / checkpoint detection ────────────────────────────────
            tfa_detected = await _detect_2fa(page)
            if tfa_detected:
                await _wait_for_2fa_completion(page, strat, max_wait_s=180)

            # ── Success detection ─────────────────────────────────────────
            if strat and not has_profile:
                await page.wait_for_timeout(2000)
                login_detected = await _is_logged_in(page, strat)
                if not login_detected:
                    await page.wait_for_timeout(3000)
                    login_detected = await _is_logged_in(page, strat)
            else:
                if post_url_contains:
                    try:
                        await page.wait_for_url(f"**{post_url_contains}**", timeout=post_timeout_ms)
                        login_detected = True
                    except Exception: pass
                if not login_detected and post_selector:
                    try:
                        await page.locator(post_selector).first.wait_for(state="visible", timeout=post_timeout_ms)
                        login_detected = True
                    except Exception: pass
                if post_goto:
                    try:
                        await page.goto(post_goto, wait_until="domcontentloaded")
                        await page.wait_for_timeout(800)
                        login_detected = True
                    except Exception: pass
                if not login_detected:
                    login_detected = await _is_logged_in(page, strat)

            # ── Cookie polling ────────────────────────────────────────────
            if stay_ms > 0:
                await page.wait_for_timeout(stay_ms)

            cookies, elapsed = [], 0
            while elapsed < cookie_timeout_ms:
                cookies = await context.cookies()
                domain_cookies = [c for c in cookies if (c.get("domain") or "").lstrip(".").endswith(domain)]
                ok = len(domain_cookies) >= cookie_min
                if cookie_wait_name:
                    ok = ok and any(c.get("name") == cookie_wait_name for c in domain_cookies)
                if ok:
                    break
                await page.wait_for_timeout(250)
                elapsed += 250

            await page.wait_for_timeout(300)
            local_storage, session_storage = await _dump_storage(page)

            return {
                "cookies": cookies,
                "localStorage": local_storage,
                "sessionStorage": session_storage,
                "current_url": page.url,
                "title": await page.title(),
                "used_selectors": used,
                "login_detected": login_detected,
                "tfa_detected": tfa_detected,
                "domain": domain,
                "origin": origin,
                "debug": {
                    "before_url": before_url,
                    "after_url": page.url,
                    "used_method": used_method,
                    "cookie_wait_elapsed_ms": elapsed,
                    "domain_cookie_count": len([c for c in cookies if (c.get("domain") or "").lstrip(".").endswith(domain)]),
                },
            }

    except Exception as e:
        raise Exception(f"Playwright login failed: {str(e)}")
    finally:
        try:
            if browser: await browser.close()
        except Exception:
            pass















































# from __future__ import annotations

# import asyncio
# import os
# import random
# import urllib.parse
# from typing import Any, Dict, Optional, Tuple

# from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# DEFAULT_TIMEOUT_MS = 30_000
# HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "1") == "1"

# # ─── Stealth JS ───────────────────────────────────────────────────────────────
# _STEALTH_JS = """
# () => {
#     Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
#     Object.defineProperty(navigator, 'plugins', {
#         get: () => {
#             const a = [
#                 { name:'Chrome PDF Plugin', filename:'internal-pdf-viewer', description:'Portable Document Format' },
#                 { name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', description:'' },
#                 { name:'Native Client', filename:'internal-nacl-plugin', description:'' },
#             ];
#             a.__proto__ = navigator.plugins.__proto__;
#             return a;
#         }
#     });
#     Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
#     Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
#     window.chrome = { runtime:{}, loadTimes:function(){}, csi:function(){}, app:{} };
#     try {
#         const gp = WebGLRenderingContext.prototype.getParameter;
#         WebGLRenderingContext.prototype.getParameter = function(p) {
#             if (p === 37445) return 'Intel Inc.';
#             if (p === 37446) return 'Intel Iris OpenGL Engine';
#             return gp.call(this, p);
#         };
#     } catch(e){}
#     const orig = window.navigator.permissions?.query?.bind(navigator.permissions);
#     if (orig) navigator.permissions.query = (p) =>
#         p.name === 'notifications'
#             ? Promise.resolve({ state: Notification.permission })
#             : orig(p);
# }
# """

# _USER_AGENTS = [
#     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
#     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
#     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
# ]

# # ─── 2FA detection ────────────────────────────────────────────────────────────
# _2FA_URL_PATTERNS = [
#     "/two-factor", "/2fa", "/verify", "/otp", "/checkpoint",
#     "/challenge", "/confirmation", "/security-code",
#     "sms-verification", "phone-verify", "auth/mfa",
#     "accounts/login/two_factor", "login/tfvc",
#     "/checkpoint/challenge", "/checkpoint/lg",
# ]
# _2FA_INPUT_SELECTORS = [
#     "input[name='verificationCode']",
#     "input[name='code']",
#     "input[name='otp']",
#     "input[name='two_factor_code']",
#     "input[name='pin']",
#     "input[name='challengeCode']",
#     "input[placeholder*='code' i]",
#     "input[placeholder*='verification' i]",
#     "input[placeholder*='pin' i]",
#     "input[autocomplete='one-time-code']",
#     "input[id*='code' i]",
#     "#approvals_code",
#     "[data-testid='ocfEnterTextTextInput']",
#     # LinkedIn checkpoint
#     "input[name='challengeInput']",
#     "input[id='input__email_verification_pin']",
#     "input[id='input__phone_verification_pin']",
# ]
# _2FA_SUBMIT_SELECTORS = [
#     "button[type='submit']",
#     "button:has-text('Verify')",
#     "button:has-text('Submit')",
#     "button:has-text('Continue')",
#     "button:has-text('Confirm')",
#     "button:has-text('Done')",
#     "input[type='submit']",
#     # LinkedIn
#     "button[data-litms-control-urn*='verify']",
#     "button[data-litms-control-urn*='submit']",
# ]

# async def _detect_2fa(page) -> bool:
#     """True si la page actuelle est une page de vérification/2FA."""
#     url = page.url.lower()
#     if any(p in url for p in _2FA_URL_PATTERNS):
#         return True
#     for sel in _2FA_INPUT_SELECTORS:
#         try:
#             loc = page.locator(sel).first
#             if await loc.count() > 0 and await loc.is_visible(timeout=1000):
#                 return True
#         except Exception:
#             pass
#     return False

# async def _find_2fa_input(page):
#     """Retourne le locator du champ de code 2FA, ou None."""
#     for sel in _2FA_INPUT_SELECTORS:
#         try:
#             loc = page.locator(sel).first
#             if await loc.count() > 0 and await loc.is_visible(timeout=1000):
#                 return loc
#         except Exception:
#             pass
#     return None


# # ─── Pause/resume store (2FA interactif via API) ──────────────────────────────
# #
# # Quand Playwright détecte une page 2FA, il met la session en pause et publie
# # un événement dans ce dictionnaire. Le backend (sharing.py) expose deux endpoints :
# #   GET  /sharing/relay-login/{relay_id}/2fa-status  → { waiting: bool, url: str }
# #   POST /sharing/relay-login/{relay_id}/2fa-submit  → { code: "123456" }
# # Le frontend poll le premier et affiche une modale pour le second.
# #
# _RELAY_2FA_STORE: dict[str, dict] = {}

# def relay_2fa_is_waiting(relay_id: str) -> dict | None:
#     """Retourne l'état 2FA pour un relay_id, ou None."""
#     return _RELAY_2FA_STORE.get(relay_id)

# def relay_2fa_submit_code(relay_id: str, code: str) -> bool:
#     """Soumettre le code 2FA pour un relay en attente. Retourne False si relay_id inconnu."""
#     entry = _RELAY_2FA_STORE.get(relay_id)
#     if not entry:
#         return False
#     entry["code"] = code.strip()
#     entry["event"].set()   # débloquer l'attente dans Playwright
#     return True

# def relay_2fa_cleanup(relay_id: str):
#     _RELAY_2FA_STORE.pop(relay_id, None)


# # ─── Per-site strategies ──────────────────────────────────────────────────────
# _SITE_STRATEGIES: dict[str, dict] = {
#     "pinterest.com": {
#         "login_url": "https://www.pinterest.com/login/",
#         "username": "input[name='id']",
#         "password": "input[name='password']",
#         "submit_selectors": [
#             "button[type='submit']",
#             "button:has-text('Log in')",
#             "div[data-test-id='loginButton']",
#         ],
#         "success": ["[data-test-id='header-avatar']", "[data-test-id='homefeed-feed']"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#     },
#     "linkedin.com": {
#         "login_url": "https://www.linkedin.com/login",
#         "username_selectors": [
#             "input[name='session_key']",
#             "input[autocomplete='username']",
#             "input[id='username']",
#             "input[type='email']",
#         ],
#         "password_selectors": [
#             "input[name='session_password']",
#             "input[autocomplete='current-password']",
#             "input[id='password']",
#             "input[type='password']",
#         ],
#         "submit_selectors": [
#             "button[type='submit'].btn__primary--large",
#             "button[type='submit']",
#             "button:has-text('Sign in')",
#             "button:has-text('Se connecter')",
#         ],
#         "success": [
#             ".feed-identity-module",
#             "a[href='/feed/']",
#             ".global-nav__me",
#             "header.global-nav",
#         ],
#         "pre_wait_ms": 3500,
#         "multi_step": False,
#         "use_selector_lists": True,
#     },
#     "facebook.com": {
#         "login_url": "https://www.facebook.com/",
#         "username": "#email",
#         "password": "#pass",
#         "submit_selectors": [
#             "[name='login']",
#             "button[type='submit']",
#             "[data-testid='royal_login_button']",
#         ],
#         "success": ["[aria-label='Facebook']", "div[role='feed']", "[data-pagelet='LeftRail']"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#     },
#     "instagram.com": {
#         "login_url": "https://www.instagram.com/accounts/login/",
#         "username": "input[name='username']",
#         "password": "input[name='password']",
#         "submit_selectors": ["button[type='submit']", "button:has-text('Log in')"],
#         "success": ["svg[aria-label='Home']"],
#         "pre_wait_ms": 2500,
#         "multi_step": False,
#     },
#     "twitter.com": {
#         "login_url": "https://x.com/i/flow/login",
#         "username": "input[autocomplete='username']",
#         "password": "input[name='password']",
#         "submit_selectors": ["[data-testid='LoginForm_Login_Button']", "button[type='submit']"],
#         "username_next_selectors": ["[data-testid='LoginForm_Login_Button']", "div[role='button']"],
#         "success": ["[data-testid='primaryColumn']"],
#         "pre_wait_ms": 2000,
#         "multi_step": True,
#     },
#     "x.com": {
#         "login_url": "https://x.com/i/flow/login",
#         "username": "input[autocomplete='username']",
#         "password": "input[name='password']",
#         "submit_selectors": ["[data-testid='LoginForm_Login_Button']", "button[type='submit']"],
#         "username_next_selectors": ["[data-testid='LoginForm_Login_Button']", "div[role='button']"],
#         "success": ["[data-testid='primaryColumn']"],
#         "pre_wait_ms": 2000,
#         "multi_step": True,
#     },
#     "google.com": {
#         "login_url": "https://accounts.google.com/signin",
#         "username": "input[type='email']",
#         "password": "input[type='password']",
#         "submit_selectors": ["#passwordNext", "button[type='submit']"],
#         "username_next_selectors": ["#identifierNext"],
#         "success": ["[data-ogsr-up]"],
#         "pre_wait_ms": 2000,
#         "multi_step": True,
#     },
#     "reddit.com": {
#         "login_url": "https://www.reddit.com/login/",
#         "username": "#loginUsername",
#         "password": "#loginPassword",
#         "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
#         "success": ["a[href*='/user/']"],
#         "pre_wait_ms": 1500,
#         "multi_step": False,
#     },
#     "github.com": {
#         "login_url": "https://github.com/login",
#         "username": "#login_field",
#         "password": "#password",
#         "submit_selectors": ["[type='submit'][name='commit']", "button[type='submit']"],
#         "success": [".Header-link--avatar"],
#         "pre_wait_ms": 1000,
#         "multi_step": False,
#     },
#     "discord.com": {
#         "login_url": "https://discord.com/login",
#         "username": "input[name='email']",
#         "password": "input[name='password']",
#         "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
#         "success": ["nav[aria-label='Servers sidebar']"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#     },
#     "tiktok.com": {
#         "login_url": "https://www.tiktok.com/login/phone-or-email/email",
#         "username": "input[name='username']",
#         "password": "input[type='password']",
#         "submit_selectors": ["button[type='submit']"],
#         "success": ["[data-e2e='profile-icon']"],
#         "pre_wait_ms": 3000,
#         "multi_step": False,
#     },
# }


# def _domain_from_url(url: str) -> str:
#     try:
#         return urllib.parse.urlparse(url).netloc.split(":")[0].lower().strip().lstrip("www.")
#     except Exception:
#         return ""

# def _origin_from_url(url: str) -> str:
#     p = urllib.parse.urlparse(url)
#     return f"{p.scheme or 'https'}://{p.netloc}".rstrip("/")


# async def _dump_storage(page) -> Tuple[str | None, str | None]:
#     ls = await page.evaluate("""() => {
#         try {
#             const o={};
#             for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);o[k]=localStorage.getItem(k);}
#             const s=JSON.stringify(o); return s==="{}"?null:s;
#         } catch(e){return null;}
#     }""")
#     ss = await page.evaluate("""() => {
#         try {
#             const o={};
#             for(let i=0;i<sessionStorage.length;i++){const k=sessionStorage.key(i);o[k]=sessionStorage.getItem(k);}
#             const s=JSON.stringify(o); return s==="{}"?null:s;
#         } catch(e){return null;}
#     }""")
#     return ls, ss


# async def _make_stealth_context(p):
#     ua = random.choice(_USER_AGENTS)
#     browser = await p.chromium.launch(
#         headless=HEADLESS,
#         args=[
#             "--no-sandbox",
#             "--disable-blink-features=AutomationControlled",
#             "--disable-infobars",
#             "--disable-dev-shm-usage",
#             "--no-first-run",
#             "--no-default-browser-check",
#             "--window-size=1366,768",
#         ],
#     )
#     ctx = await browser.new_context(
#         user_agent=ua,
#         viewport={"width": 1366, "height": 768},
#         locale="en-US",
#         timezone_id="America/New_York",
#         java_script_enabled=True,
#         is_mobile=False,
#         has_touch=False,
#         color_scheme="light",
#     )
#     await ctx.add_init_script(_STEALTH_JS)
#     return browser, ctx


# async def _fill(page, selector: str, value: str, timeout_ms: int = 15_000):
#     loc = page.locator(selector).first
#     await loc.wait_for(state="visible", timeout=timeout_ms)
#     await loc.scroll_into_view_if_needed()
#     await loc.click()
#     await loc.fill("")
#     await page.wait_for_timeout(random.randint(80, 200))
#     await loc.type(value, delay=random.randint(30, 60))


# async def _fill_from_list(page, selectors: list[str], value: str, timeout_ms: int = 15_000) -> str | None:
#     for sel in selectors:
#         try:
#             loc = page.locator(sel).first
#             await loc.wait_for(state="visible", timeout=timeout_ms)
#             if not await loc.is_editable():
#                 continue
#             await loc.scroll_into_view_if_needed()
#             await loc.click()
#             await page.wait_for_timeout(random.randint(100, 250))
#             await loc.fill("")
#             await page.wait_for_timeout(random.randint(80, 150))
#             await loc.type(value, delay=random.randint(35, 70))
#             actual = await loc.input_value()
#             if actual and len(actual) > 0:
#                 return sel
#         except Exception:
#             continue
#     return None


# async def _click_first(page, selectors: list[str], timeout_ms: int = 8_000) -> bool:
#     for sel in selectors:
#         try:
#             loc = page.locator(sel).first
#             await loc.wait_for(state="visible", timeout=timeout_ms)
#             await loc.click()
#             return True
#         except Exception:
#             continue
#     return False


# # ─── 2FA handler — attend un code via l'API (relay_id) ───────────────────────

# async def _handle_2fa_via_api(page, relay_id: str, strat: dict | None) -> bool:
#     """
#     Met Playwright en pause, publie l'état 2FA dans _RELAY_2FA_STORE,
#     attend que le frontend soumette le code via l'API, puis le saisit.

#     Le frontend doit :
#       1. Poll GET /sharing/relay-login/{relay_id}/2fa-status
#       2. Afficher une modale quand waiting=True
#       3. POST /sharing/relay-login/{relay_id}/2fa-submit avec { code }

#     Timeout : 5 minutes.
#     """
#     checkpoint_url = page.url
#     event = asyncio.Event()

#     _RELAY_2FA_STORE[relay_id] = {
#         "waiting": True,
#         "checkpoint_url": checkpoint_url,
#         "code": None,
#         "event": event,
#     }
#     print(f"[relay][2fa] En attente du code pour relay_id={relay_id[:8]}... URL: {checkpoint_url}")

#     try:
#         # Attendre jusqu'à 5 minutes que le code soit soumis via l'API
#         await asyncio.wait_for(event.wait(), timeout=300)
#     except asyncio.TimeoutError:
#         print(f"[relay][2fa] Timeout 5min — relay_id={relay_id[:8]}")
#         _RELAY_2FA_STORE.pop(relay_id, None)
#         raise Exception("2FA timeout: no code submitted within 5 minutes")

#     code = _RELAY_2FA_STORE.get(relay_id, {}).get("code", "").strip()
#     _RELAY_2FA_STORE.pop(relay_id, None)

#     if not code:
#         raise Exception("2FA code was empty")

#     print(f"[relay][2fa] Code reçu ({len(code)} chars), soumission...")

#     # Trouver le champ de saisie du code
#     code_input = await _find_2fa_input(page)
#     if not code_input:
#         # Fallback : chercher n'importe quel input visible
#         inputs = page.locator("input:not([type='hidden'])").first
#         try:
#             await inputs.wait_for(state="visible", timeout=5000)
#             code_input = inputs
#         except Exception:
#             raise Exception(f"2FA input field not found on {checkpoint_url}")

#     # Saisir le code
#     await code_input.wait_for(state="visible", timeout=5000)
#     await code_input.click()
#     await code_input.fill("")
#     await page.wait_for_timeout(200)
#     await code_input.type(code, delay=random.randint(50, 100))
#     await page.wait_for_timeout(500)

#     # Soumettre
#     submitted = await _click_first(page, _2FA_SUBMIT_SELECTORS, timeout_ms=5000)
#     if not submitted:
#         await code_input.press("Enter")

#     print("[relay][2fa] Code soumis, attente de la navigation...")
#     try:
#         await page.wait_for_load_state("networkidle", timeout=15_000)
#     except Exception:
#         pass
#     await page.wait_for_timeout(2000)

#     # Vérifier si on est sorti du checkpoint
#     still_2fa = await _detect_2fa(page)
#     logged = await _is_logged_in(page, strat)
#     if still_2fa and not logged:
#         # Code incorrect ou 2ème facteur
#         raise Exception(f"2FA code rejected or additional verification required. Current URL: {page.url}")

#     print(f"[relay][2fa] ✓ Vérification complète. URL: {page.url}")
#     return True


# # ─── LinkedIn strategy ────────────────────────────────────────────────────────

# async def _run_linkedin_strategy(page, strat: dict, username: str, password: str) -> None:
#     print(f"[relay][linkedin] Waiting {strat['pre_wait_ms']}ms for form...")
#     await page.wait_for_timeout(strat["pre_wait_ms"])

#     try:
#         await page.wait_for_selector(
#             "input[name='session_key'], input[autocomplete='username']",
#             state="visible", timeout=15_000,
#         )
#     except PlaywrightTimeoutError:
#         await page.screenshot(path="/tmp/linkedin_debug.png")
#         raise Exception("LinkedIn: email field not found after 15s — see /tmp/linkedin_debug.png")

#     used_user = await _fill_from_list(page, strat["username_selectors"], username, 10_000)
#     if not used_user:
#         await page.screenshot(path="/tmp/linkedin_no_user.png")
#         raise Exception("LinkedIn: cannot fill username")
#     print(f"[relay][linkedin] Username filled via '{used_user}'")

#     await page.wait_for_timeout(random.randint(400, 700))

#     used_pw = await _fill_from_list(page, strat["password_selectors"], password, 10_000)
#     if not used_pw:
#         await page.screenshot(path="/tmp/linkedin_no_pw.png")
#         raise Exception("LinkedIn: cannot fill password")
#     print(f"[relay][linkedin] Password filled via '{used_pw}'")

#     await page.wait_for_timeout(random.randint(300, 600))

#     clicked = await _click_first(page, strat["submit_selectors"], 8_000)
#     if not clicked:
#         await page.keyboard.press("Enter")
#     print("[relay][linkedin] Submit done")


# # ─── Generic strategy ─────────────────────────────────────────────────────────

# async def _run_strategy(page, strat: dict, username: str, password: str):
#     if strat.get("use_selector_lists"):
#         return await _run_linkedin_strategy(page, strat, username, password)

#     await page.wait_for_timeout(strat.get("pre_wait_ms", 1500))
#     submit_sels = strat.get("submit_selectors", ["button[type='submit']"])

#     if strat.get("multi_step"):
#         await _fill(page, strat["username"], username)
#         await page.wait_for_timeout(random.randint(400, 700))
#         next_sels = strat.get("username_next_selectors", ["button[type='submit']"])
#         if not await _click_first(page, next_sels):
#             await page.keyboard.press("Enter")
#         await page.wait_for_timeout(random.randint(1500, 2500))
#         await _fill(page, strat["password"], password)
#         await page.wait_for_timeout(random.randint(300, 600))
#         if not await _click_first(page, submit_sels):
#             await page.keyboard.press("Enter")
#     else:
#         await _fill(page, strat["username"], username)
#         await page.wait_for_timeout(random.randint(200, 400))
#         await _fill(page, strat["password"], password)
#         await page.wait_for_timeout(random.randint(300, 500))
#         if not await _click_first(page, submit_sels):
#             await page.keyboard.press("Enter")


# async def _is_logged_in(page, strat: dict | None) -> bool:
#     url = page.url.lower()
#     login_kw = ["/login", "/signin", "/sign-in", "/accounts/login", "/flow/login", "accounts.google", "/checkpoint"]
#     if not any(k in url for k in login_kw):
#         return True
#     if strat:
#         for sel in strat.get("success", []):
#             try:
#                 if await page.locator(sel).first.is_visible(timeout=2000):
#                     return True
#             except Exception:
#                 pass
#     return False


# # ─── Generic heuristic ───────────────────────────────────────────────────────

# def _score_user(attrs: dict) -> int:
#     hay = " ".join(attrs.get(k, "") for k in ["name","id","placeholder","autocomplete","type","aria"]).lower()
#     s = 0
#     if "email" in hay: s += 6
#     if "user" in hay or "login" in hay: s += 5
#     if "phone" in hay or "tel" in hay: s -= 2
#     if attrs.get("type") in ["email","text"]: s += 1
#     if attrs.get("autocomplete") in ["email","username"]: s += 3
#     return s

# def _score_pw(attrs: dict) -> int:
#     s = 10 if attrs.get("type") == "password" else 0
#     hay = " ".join(attrs.get(k,"") for k in ["name","id","placeholder","autocomplete"]).lower()
#     if "password" in hay or "pass" in hay: s += 5
#     return s

# def _score_submit(attrs: dict, text: str) -> int:
#     hay = (text + " " + " ".join(attrs.get(k,"") for k in ["type","name","id","aria"])).lower()
#     s = 0
#     if any(w in hay for w in ["sign in","login","log in","connexion","se connecter"]): s += 6
#     if any(w in hay for w in ["continue","next"]): s += 2
#     if "submit" in attrs.get("type",""): s += 2
#     if any(w in hay for w in ["cancel","register","sign up","create"]): s -= 4
#     return s

# async def _get_attr(loc, name: str) -> str:
#     try: return (await loc.get_attribute(name)) or ""
#     except: return ""

# async def _generic_login(page, username: str, password: str):
#     await page.wait_for_timeout(1500)
#     pw_locs = page.locator("input[type='password']")
#     if await pw_locs.count() == 0:
#         for sel in ["input[type='email']","input[name='email']","input[name='username']",
#                     "input[autocomplete='email']","input[autocomplete='username']","input[type='text']"]:
#             try:
#                 loc = page.locator(sel).first
#                 await loc.wait_for(state="visible", timeout=3000)
#                 await loc.fill(username)
#                 await page.wait_for_timeout(500)
#                 await _click_first(page, ["button:has-text('Next')","button:has-text('Continue')",
#                                           "button[type='submit']","input[type='submit']"])
#                 await page.wait_for_timeout(2000)
#                 break
#             except Exception:
#                 continue

#     pw_locs = page.locator("input[type='password']")
#     if await pw_locs.count() == 0:
#         raise Exception("No password field found")
#     pw_loc = pw_locs.first
#     best_pw = -999
#     for i in range(min(await pw_locs.count(), 5)):
#         loc = pw_locs.nth(i)
#         try:
#             await loc.wait_for(state="visible", timeout=1500)
#             attrs = {k: await _get_attr(loc, k) for k in ["type","name","id","placeholder","autocomplete"]}
#             sc = _score_pw(attrs)
#             if sc > best_pw:
#                 best_pw = sc
#                 pw_loc = loc
#         except Exception:
#             pass

#     user_locs = page.locator("input:not([type='hidden']):not([type='password'])")
#     user_loc = None
#     best_us = -999
#     for i in range(min(await user_locs.count(), 20)):
#         loc = user_locs.nth(i)
#         try:
#             await loc.wait_for(state="visible", timeout=1000)
#             attrs = {k: await _get_attr(loc, k) for k in ["type","name","id","placeholder","autocomplete","aria-label"]}
#             sc = _score_user(attrs)
#             if sc > best_us:
#                 best_us = sc
#                 user_loc = loc
#         except Exception:
#             pass

#     if user_loc and best_us > 2:
#         try:
#             await user_loc.click()
#             await user_loc.fill(username)
#             await page.wait_for_timeout(200)
#         except Exception:
#             pass

#     await pw_loc.wait_for(state="visible", timeout=8000)
#     await pw_loc.click()
#     await pw_loc.fill("")
#     await pw_loc.type(password, delay=random.randint(30, 60))
#     await page.wait_for_timeout(300)

#     btns = page.locator("button, input[type='submit']")
#     submit_loc = None
#     best_sub = -999
#     for i in range(min(await btns.count(), 20)):
#         b = btns.nth(i)
#         try:
#             await b.wait_for(state="visible", timeout=1000)
#             text = ""
#             try: text = await b.inner_text()
#             except: pass
#             attrs = {k: await _get_attr(b, k) for k in ["type","name","id","aria-label"]}
#             sc = _score_submit(attrs, text)
#             if sc > best_sub:
#                 best_sub = sc
#                 submit_loc = b
#         except Exception:
#             pass

#     if submit_loc and best_sub >= 0:
#         try: await submit_loc.click(timeout=8000)
#         except: await pw_loc.press("Enter")
#     else:
#         await pw_loc.press("Enter")


# # ─── PUBLIC API ───────────────────────────────────────────────────────────────

# async def login_and_get_cookies(
#     service_url: str,
#     username: str,
#     password: str,
#     profile: Optional[Dict[str, Any]] = None,
#     relay_id: Optional[str] = None,   # ← requis pour 2FA interactif
# ) -> Dict[str, Any]:
#     """
#     Universal relay login avec gestion du 2FA interactif.

#     relay_id : identifiant unique de la session relay (=share session_id).
#                Requis pour que le frontend puisse soumettre le code 2FA via API.
#                Si None, retombe sur l'attente passive (headless=False uniquement).
#     """
#     profile = profile or {}

#     def _as_list(x, default):
#         if not x: return default
#         return [x] if isinstance(x, str) else (x if isinstance(x, list) else default)

#     username_selectors = _as_list(profile.get("username_selector"), [])
#     password_selectors = _as_list(profile.get("password_selector"), [])
#     submit_selectors   = _as_list(profile.get("submit_selector"), [])
#     has_profile = bool(username_selectors or password_selectors)

#     open_login_sel    = profile.get("open_login_selector")
#     goto_wait         = profile.get("goto_wait_until", "domcontentloaded")
#     pre_fill_ms       = int(profile.get("pre_fill_wait_ms", 1200))
#     between_ms        = int(profile.get("between_actions_wait_ms", 250))
#     after_submit_ms   = int(profile.get("after_submit_wait_ms", 2500))
#     post_timeout_ms   = int(profile.get("post_login_timeout_ms", 20_000))
#     post_url_contains = profile.get("post_login_url_contains")
#     post_selector     = profile.get("post_login_selector")
#     post_goto         = profile.get("post_login_goto")
#     stay_ms           = int(profile.get("stay_connected_ms", 4000))
#     cookie_wait_name  = profile.get("cookie_wait_name")
#     cookie_min        = int(profile.get("cookie_min_count", 1))
#     cookie_timeout_ms = int(profile.get("cookie_wait_timeout_ms", 15_000))

#     used = {"username": None, "password": None, "submit": None}
#     browser = None

#     try:
#         service_url = (service_url or "").strip()
#         if not service_url.startswith(("http://","https://")):
#             service_url = "https://" + service_url

#         domain = _domain_from_url(service_url)
#         origin = _origin_from_url(service_url)
#         strat  = _SITE_STRATEGIES.get(domain)
#         login_url = strat["login_url"] if (strat and not has_profile) else service_url

#         async with async_playwright() as p:
#             browser, context = await _make_stealth_context(p)
#             page = await context.new_page()
#             page.set_default_timeout(DEFAULT_TIMEOUT_MS)

#             await page.goto(login_url, wait_until=goto_wait)
#             try: await page.wait_for_load_state("domcontentloaded", timeout=10_000)
#             except Exception: pass

#             before_url = page.url
#             login_detected = False
#             tfa_detected = False
#             used_method = "unknown"

#             # ── Branch 1 : site connu ─────────────────────────────────────
#             if strat and not has_profile:
#                 await _run_strategy(page, strat, username, password)
#                 used["username"] = used["password"] = used["submit"] = "strategy"
#                 used_method = f"strategy:{domain}"

#             # ── Branch 2 : profile custom ─────────────────────────────────
#             elif has_profile:
#                 await page.wait_for_timeout(pre_fill_ms)
#                 if open_login_sel:
#                     try:
#                         await page.locator(open_login_sel).first.click()
#                         await page.wait_for_timeout(between_ms)
#                     except Exception: pass

#                 pw_loc = None
#                 for sel in username_selectors:
#                     try:
#                         loc = page.locator(sel).first
#                         await loc.wait_for(state="visible", timeout=10_000)
#                         await loc.scroll_into_view_if_needed()
#                         await loc.click(); await loc.fill("")
#                         await page.wait_for_timeout(between_ms)
#                         await loc.type(username, delay=35)
#                         used["username"] = sel; break
#                     except Exception: continue
#                 if not used["username"]:
#                     raise Exception("Cannot find username field")

#                 for sel in password_selectors:
#                     try:
#                         loc = page.locator(sel).first
#                         await loc.wait_for(state="visible", timeout=10_000)
#                         await loc.scroll_into_view_if_needed()
#                         await loc.click(); await loc.fill("")
#                         await page.wait_for_timeout(between_ms)
#                         await loc.type(password, delay=35)
#                         used["password"] = sel; pw_loc = loc; break
#                     except Exception: continue
#                 if not used["password"]:
#                     raise Exception("Cannot find password field")

#                 clicked = False
#                 for sel in (submit_selectors or ["button[type='submit']","input[type='submit']"]):
#                     try:
#                         btn = page.locator(sel).first
#                         await btn.wait_for(state="visible", timeout=8_000)
#                         await page.wait_for_timeout(between_ms)
#                         await btn.click(); used["submit"] = sel; clicked = True; break
#                     except Exception: continue
#                 if not clicked:
#                     await pw_loc.press("Enter"); used["submit"] = "Enter"
#                 used_method = "profile_selectors"

#             # ── Branch 3 : générique ──────────────────────────────────────
#             else:
#                 await _generic_login(page, username, password)
#                 used["username"] = used["password"] = used["submit"] = "generic"
#                 used_method = "generic"

#             # ── Post-submit ───────────────────────────────────────────────
#             await page.wait_for_timeout(after_submit_ms)
#             try: await page.wait_for_load_state("networkidle", timeout=8_000)
#             except Exception: pass

#             # ── 2FA detection & handling ──────────────────────────────────
#             tfa_detected = await _detect_2fa(page)
#             if tfa_detected:
#                 if relay_id:
#                     # Mode interactif via API : attend le code du frontend
#                     await _handle_2fa_via_api(page, relay_id, strat)
#                 else:
#                     # Fallback passif : polling 3 minutes (headless=False requis)
#                     print(f"[relay] 2FA détecté, attente passive 3min (relay_id manquant)...")
#                     for _ in range(36):
#                         await page.wait_for_timeout(5000)
#                         if await _is_logged_in(page, strat):
#                             break

#             # ── Success detection ─────────────────────────────────────────
#             if strat and not has_profile:
#                 await page.wait_for_timeout(2000)
#                 login_detected = await _is_logged_in(page, strat)
#                 if not login_detected:
#                     await page.wait_for_timeout(3000)
#                     login_detected = await _is_logged_in(page, strat)
#             else:
#                 if post_url_contains:
#                     try:
#                         await page.wait_for_url(f"**{post_url_contains}**", timeout=post_timeout_ms)
#                         login_detected = True
#                     except Exception: pass
#                 if not login_detected and post_selector:
#                     try:
#                         await page.locator(post_selector).first.wait_for(state="visible", timeout=post_timeout_ms)
#                         login_detected = True
#                     except Exception: pass
#                 if post_goto:
#                     try:
#                         await page.goto(post_goto, wait_until="domcontentloaded")
#                         await page.wait_for_timeout(800)
#                         login_detected = True
#                     except Exception: pass
#                 if not login_detected:
#                     login_detected = await _is_logged_in(page, strat)

#             # ── Cookie polling ────────────────────────────────────────────
#             if stay_ms > 0:
#                 await page.wait_for_timeout(stay_ms)

#             cookies, elapsed = [], 0
#             while elapsed < cookie_timeout_ms:
#                 cookies = await context.cookies()
#                 domain_cookies = [c for c in cookies if (c.get("domain") or "").lstrip(".").endswith(domain)]
#                 ok = len(domain_cookies) >= cookie_min
#                 if cookie_wait_name:
#                     ok = ok and any(c.get("name") == cookie_wait_name for c in domain_cookies)
#                 if ok:
#                     break
#                 await page.wait_for_timeout(250)
#                 elapsed += 250

#             await page.wait_for_timeout(300)
#             local_storage, session_storage = await _dump_storage(page)

#             return {
#                 "cookies": cookies,
#                 "localStorage": local_storage,
#                 "sessionStorage": session_storage,
#                 "current_url": page.url,
#                 "title": await page.title(),
#                 "used_selectors": used,
#                 "login_detected": login_detected,
#                 "tfa_detected": tfa_detected,
#                 "domain": domain,
#                 "origin": origin,
#                 "debug": {
#                     "before_url": before_url,
#                     "after_url": page.url,
#                     "used_method": used_method,
#                     "cookie_wait_elapsed_ms": elapsed,
#                     "domain_cookie_count": len([c for c in cookies if (c.get("domain") or "").lstrip(".").endswith(domain)]),
#                 },
#             }

#     except Exception as e:
#         raise Exception(f"Playwright login failed: {str(e)}")
#     finally:
#         try:
#             if browser: await browser.close()
#         except Exception:
#             pass






































# from __future__ import annotations

# import asyncio
# import os
# import random
# import urllib.parse
# from typing import Any, Dict, Optional, Tuple

# from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# DEFAULT_TIMEOUT_MS = 30_000
# HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "1") == "1"

# # ─── Stealth ──────────────────────────────────────────────────────────────────
# _STEALTH_JS = """
# () => {
#     Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
#     Object.defineProperty(navigator, 'plugins', {
#         get: () => {
#             const a = [
#                 { name:'Chrome PDF Plugin', filename:'internal-pdf-viewer', description:'Portable Document Format' },
#                 { name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', description:'' },
#                 { name:'Native Client', filename:'internal-nacl-plugin', description:'' },
#             ];
#             a.__proto__ = navigator.plugins.__proto__;
#             return a;
#         }
#     });
#     Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
#     Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
#     window.chrome = { runtime:{}, loadTimes:function(){}, csi:function(){}, app:{} };
#     try {
#         const gp = WebGLRenderingContext.prototype.getParameter;
#         WebGLRenderingContext.prototype.getParameter = function(p) {
#             if (p === 37445) return 'Intel Inc.';
#             if (p === 37446) return 'Intel Iris OpenGL Engine';
#             return gp.call(this, p);
#         };
#     } catch(e){}
#     const orig = window.navigator.permissions?.query?.bind(navigator.permissions);
#     if (orig) navigator.permissions.query = (p) =>
#         p.name === 'notifications'
#             ? Promise.resolve({ state: Notification.permission })
#             : orig(p);
# }
# """

# _USER_AGENTS = [
#     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
#     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
#     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
# ]

# # ─── 2FA / verification page detection ────────────────────────────────────────
# _2FA_URL_PATTERNS = [
#     "/two-factor", "/2fa", "/verify", "/otp", "/checkpoint",
#     "/challenge", "/confirmation", "/security-code",
#     "sms-verification", "phone-verify", "auth/mfa",
#     "accounts/login/two_factor", "login/tfvc",
#     # LinkedIn spécifique
#     "/checkpoint/challenge", "/checkpoint/lg",
# ]
# _2FA_SELECTOR_PATTERNS = [
#     "input[name='verificationCode']",
#     "input[name='code']",
#     "input[name='otp']",
#     "input[name='two_factor_code']",
#     "input[name='pin']",                    # LinkedIn PIN
#     "input[placeholder*='code' i]",
#     "input[placeholder*='verification' i]",
#     "input[placeholder*='pin' i]",
#     "input[autocomplete='one-time-code']",
#     "input[id*='code' i]",
#     "#approvals_code",
#     "[data-testid='ocfEnterTextTextInput']",
# ]

# async def _detect_2fa(page) -> bool:
#     url = page.url.lower()
#     if any(p in url for p in _2FA_URL_PATTERNS):
#         return True
#     for sel in _2FA_SELECTOR_PATTERNS:
#         try:
#             loc = page.locator(sel).first
#             if await loc.count() > 0 and await loc.is_visible(timeout=1500):
#                 return True
#         except Exception:
#             pass
#     return False


# # ─── Per-site strategies ──────────────────────────────────────────────────────
# #
# # FIX LINKEDIN :
# #   - Sélecteurs corrigés : session_key / session_password (pas #username/#password)
# #   - Plusieurs fallbacks dans une liste pour résister aux changements de DOM
# #   - pre_wait_ms augmenté à 3000 (LinkedIn charge le form en JS lentement)
# #   - multi_step=False MAIS fill_sequence=True pour remplir l'un après l'autre
# #     avec une vérification que le champ est bien interactif
# #
# _SITE_STRATEGIES: dict[str, dict] = {
#     "pinterest.com": {
#         "login_url": "https://www.pinterest.com/login/",
#         "username": "input[name='id']",
#         "password": "input[name='password']",
#         "submit_selectors": [
#             "button[type='submit']",
#             "button:has-text('Log in')",
#             "button:has-text('Continue')",
#             "div[data-test-id='registerFormSubmitButton']",
#             "div[data-test-id='loginButton']",
#         ],
#         "success": ["[data-test-id='header-avatar']", "[data-test-id='homefeed-feed']"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#     },

#     # ── LinkedIn FIX COMPLET ───────────────────────────────────────────────────
#     # Problèmes résolus :
#     # 1. Mauvais sélecteurs : LinkedIn utilise name= pas id=
#     # 2. pre_wait trop court : le formulaire se charge en JS
#     # 3. Pas de vérification que le champ est éditable avant de taper
#     # 4. Checkpoint 2FA : détecté et attendu manuellement
#     "linkedin.com": {
#         "login_url": "https://www.linkedin.com/login",
#         "username_selectors": [          # liste = essayés dans l'ordre
#             "input[name='session_key']",
#             "input[autocomplete='username']",
#             "input[id='username']",
#             "input[type='email']",
#         ],
#         "password_selectors": [
#             "input[name='session_password']",
#             "input[autocomplete='current-password']",
#             "input[id='password']",
#             "input[type='password']",
#         ],
#         "submit_selectors": [
#             "button[type='submit'][data-litms-control-urn*='login']",
#             "button[type='submit'].btn__primary--large",
#             "button[type='submit']",
#             "button:has-text('Sign in')",
#             "button:has-text('Se connecter')",
#             ".btn__primary--large",
#         ],
#         "success": [
#             ".feed-identity-module",
#             "a[href='/feed/']",
#             ".global-nav__me",
#             "[data-test-global-nav-link]",
#             "header.global-nav",
#         ],
#         "pre_wait_ms": 3500,    # LinkedIn charge le form en JS — plus long
#         "multi_step": False,
#         "use_selector_lists": True,   # Nouveau flag : utiliser username_selectors/password_selectors
#     },

#     "facebook.com": {
#         "login_url": "https://www.facebook.com/",
#         "username": "#email",
#         "password": "#pass",
#         "submit_selectors": [
#             "[name='login']",
#             "button[type='submit']",
#             "[data-testid='royal_login_button']",
#         ],
#         "success": ["[aria-label='Facebook']", "div[role='feed']", "[data-pagelet='LeftRail']"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#     },
#     "instagram.com": {
#         "login_url": "https://www.instagram.com/accounts/login/",
#         "username": "input[name='username']",
#         "password": "input[name='password']",
#         "submit_selectors": [
#             "button[type='submit']",
#             "button:has-text('Log in')",
#             "button:has-text('Log In')",
#         ],
#         "success": ["svg[aria-label='Home']"],
#         "pre_wait_ms": 2500,
#         "multi_step": False,
#     },
#     "twitter.com": {
#         "login_url": "https://x.com/i/flow/login",
#         "username": "input[autocomplete='username']",
#         "password": "input[name='password']",
#         "submit_selectors": ["[data-testid='LoginForm_Login_Button']", "button[type='submit']"],
#         "username_next_selectors": ["[data-testid='LoginForm_Login_Button']", "div[role='button']"],
#         "success": ["[data-testid='primaryColumn']"],
#         "pre_wait_ms": 2000,
#         "multi_step": True,
#     },
#     "x.com": {
#         "login_url": "https://x.com/i/flow/login",
#         "username": "input[autocomplete='username']",
#         "password": "input[name='password']",
#         "submit_selectors": ["[data-testid='LoginForm_Login_Button']", "button[type='submit']"],
#         "username_next_selectors": ["[data-testid='LoginForm_Login_Button']", "div[role='button']"],
#         "success": ["[data-testid='primaryColumn']"],
#         "pre_wait_ms": 2000,
#         "multi_step": True,
#     },
#     "google.com": {
#         "login_url": "https://accounts.google.com/signin",
#         "username": "input[type='email']",
#         "password": "input[type='password']",
#         "submit_selectors": ["#passwordNext", "button[type='submit']"],
#         "username_next_selectors": ["#identifierNext"],
#         "success": ["[data-ogsr-up]"],
#         "pre_wait_ms": 2000,
#         "multi_step": True,
#     },
#     "reddit.com": {
#         "login_url": "https://www.reddit.com/login/",
#         "username": "#loginUsername",
#         "password": "#loginPassword",
#         "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
#         "success": ["a[href*='/user/']"],
#         "pre_wait_ms": 1500,
#         "multi_step": False,
#     },
#     "github.com": {
#         "login_url": "https://github.com/login",
#         "username": "#login_field",
#         "password": "#password",
#         "submit_selectors": ["[type='submit'][name='commit']", "button[type='submit']"],
#         "success": [".Header-link--avatar"],
#         "pre_wait_ms": 1000,
#         "multi_step": False,
#     },
#     "discord.com": {
#         "login_url": "https://discord.com/login",
#         "username": "input[name='email']",
#         "password": "input[name='password']",
#         "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
#         "success": ["nav[aria-label='Servers sidebar']"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#     },
#     "tiktok.com": {
#         "login_url": "https://www.tiktok.com/login/phone-or-email/email",
#         "username": "input[name='username']",
#         "password": "input[type='password']",
#         "submit_selectors": ["button[type='submit']"],
#         "success": ["[data-e2e='profile-icon']"],
#         "pre_wait_ms": 3000,
#         "multi_step": False,
#     },
# }


# def _domain_from_url(url: str) -> str:
#     try:
#         return urllib.parse.urlparse(url).netloc.split(":")[0].lower().strip().lstrip("www.")
#     except Exception:
#         return ""

# def _origin_from_url(url: str) -> str:
#     p = urllib.parse.urlparse(url)
#     scheme = p.scheme or "https"
#     netloc = p.netloc or p.path.split("/")[0]
#     return f"{scheme}://{netloc}".rstrip("/")


# async def _dump_storage(page) -> Tuple[str | None, str | None]:
#     ls = await page.evaluate("""() => {
#         try {
#             const o={};
#             for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);o[k]=localStorage.getItem(k);}
#             const s=JSON.stringify(o); return s==="{}"?null:s;
#         } catch(e){return null;}
#     }""")
#     ss = await page.evaluate("""() => {
#         try {
#             const o={};
#             for(let i=0;i<sessionStorage.length;i++){const k=sessionStorage.key(i);o[k]=sessionStorage.getItem(k);}
#             const s=JSON.stringify(o); return s==="{}"?null:s;
#         } catch(e){return null;}
#     }""")
#     return ls, ss


# # ─── Stealth browser factory ──────────────────────────────────────────────────
# async def _make_stealth_context(p):
#     ua = random.choice(_USER_AGENTS)
#     browser = await p.chromium.launch(
#         headless=HEADLESS,
#         args=[
#             "--no-sandbox",
#             "--disable-blink-features=AutomationControlled",
#             "--disable-infobars",
#             "--disable-dev-shm-usage",
#             "--no-first-run",
#             "--no-default-browser-check",
#             "--window-size=1366,768",
#         ],
#     )
#     ctx = await browser.new_context(
#         user_agent=ua,
#         viewport={"width": 1366, "height": 768},
#         locale="en-US",
#         timezone_id="America/New_York",
#         java_script_enabled=True,
#         is_mobile=False,
#         has_touch=False,
#         color_scheme="light",
#     )
#     await ctx.add_init_script(_STEALTH_JS)
#     return browser, ctx


# # ─── Field helpers ────────────────────────────────────────────────────────────

# async def _fill(page, selector: str, value: str, timeout_ms: int = 15_000):
#     """Remplir un champ par sélecteur unique."""
#     loc = page.locator(selector).first
#     await loc.wait_for(state="visible", timeout=timeout_ms)
#     await loc.scroll_into_view_if_needed()
#     await loc.click()
#     await loc.fill("")
#     await page.wait_for_timeout(random.randint(80, 200))
#     await loc.type(value, delay=random.randint(30, 60))


# async def _fill_from_list(page, selectors: list[str], value: str, timeout_ms: int = 15_000) -> str | None:
#     """
#     Essayer chaque sélecteur dans l'ordre.
#     Retourne le sélecteur qui a fonctionné, ou None.

#     FIX LINKEDIN : LinkedIn charge ses champs en JS — on attend jusqu'à
#     timeout_ms que l'un des sélecteurs devienne visible ET éditable.
#     """
#     for sel in selectors:
#         try:
#             loc = page.locator(sel).first
#             # Attendre que le champ existe dans le DOM
#             await loc.wait_for(state="visible", timeout=timeout_ms)
#             # Vérifier qu'il est bien éditable (pas disabled/readonly)
#             is_editable = await loc.is_editable()
#             if not is_editable:
#                 continue
#             await loc.scroll_into_view_if_needed()
#             await loc.click()
#             await page.wait_for_timeout(random.randint(100, 250))
#             await loc.fill("")
#             await page.wait_for_timeout(random.randint(80, 150))
#             await loc.type(value, delay=random.randint(35, 70))
#             # Vérifier que la valeur a bien été saisie
#             actual = await loc.input_value()
#             if actual and len(actual) > 0:
#                 return sel
#         except Exception:
#             continue
#     return None


# async def _click_first(page, selectors: list[str], timeout_ms: int = 8_000) -> bool:
#     for sel in selectors:
#         try:
#             loc = page.locator(sel).first
#             await loc.wait_for(state="visible", timeout=timeout_ms)
#             await loc.click()
#             return True
#         except Exception:
#             continue
#     return False


# # ─── LinkedIn-specific strategy ───────────────────────────────────────────────

# async def _run_linkedin_strategy(page, strat: dict, username: str, password: str) -> None:
#     """
#     Stratégie dédiée à LinkedIn.

#     LinkedIn a plusieurs particularités :
#     1. Le formulaire est rendu par React/JS — apparaît ~1-2s après le chargement
#     2. Les sélecteurs historiques (#username, #password) ne fonctionnent PAS
#        → LinkedIn utilise name='session_key' et name='session_password'
#     3. Après soumission, peut rediriger vers /checkpoint/challenge (code email/SMS)
#     4. Le bouton submit a une classe spécifique .btn__primary--large

#     Cette fonction gère tout ça proprement.
#     """
#     print(f"[relay][linkedin] Waiting {strat['pre_wait_ms']}ms for React form to render...")
#     await page.wait_for_timeout(strat["pre_wait_ms"])

#     # Attendre explicitement que le champ email soit présent dans le DOM
#     print("[relay][linkedin] Waiting for email field to appear...")
#     try:
#         await page.wait_for_selector(
#             "input[name='session_key'], input[autocomplete='username']",
#             state="visible",
#             timeout=15_000,
#         )
#     except PlaywrightTimeoutError:
#         # Screenshot de debug si le champ n'apparaît pas
#         await page.screenshot(path="/tmp/linkedin_debug_no_form.png")
#         raise Exception(
#             "LinkedIn: email field not found after 15s. "
#             "Check /tmp/linkedin_debug_no_form.png"
#         )

#     print(f"[relay][linkedin] Filling username ({username[:3]}***)")
#     used_user_sel = await _fill_from_list(
#         page,
#         strat["username_selectors"],
#         username,
#         timeout_ms=10_000,
#     )
#     if not used_user_sel:
#         await page.screenshot(path="/tmp/linkedin_debug_no_user.png")
#         raise Exception(
#             "LinkedIn: could not fill username field. "
#             "Check /tmp/linkedin_debug_no_user.png"
#         )
#     print(f"[relay][linkedin] Username filled via '{used_user_sel}'")

#     await page.wait_for_timeout(random.randint(400, 700))

#     print("[relay][linkedin] Filling password...")
#     used_pw_sel = await _fill_from_list(
#         page,
#         strat["password_selectors"],
#         password,
#         timeout_ms=10_000,
#     )
#     if not used_pw_sel:
#         await page.screenshot(path="/tmp/linkedin_debug_no_pw.png")
#         raise Exception(
#             "LinkedIn: could not fill password field. "
#             "Check /tmp/linkedin_debug_no_pw.png"
#         )
#     print(f"[relay][linkedin] Password filled via '{used_pw_sel}'")

#     await page.wait_for_timeout(random.randint(300, 600))

#     print("[relay][linkedin] Clicking submit...")
#     clicked = await _click_first(page, strat["submit_selectors"], timeout_ms=8_000)
#     if not clicked:
#         await page.keyboard.press("Enter")
#         print("[relay][linkedin] Fallback: pressed Enter")
#     else:
#         print("[relay][linkedin] Submit clicked")


# # ─── Generic strategy executor ────────────────────────────────────────────────

# async def _run_strategy(page, strat: dict, username: str, password: str):
#     """Exécuter la stratégie d'un site connu."""

#     # LinkedIn a sa propre fonction dédiée
#     if strat.get("use_selector_lists"):
#         return await _run_linkedin_strategy(page, strat, username, password)

#     await page.wait_for_timeout(strat.get("pre_wait_ms", 1500))
#     submit_sels = strat.get("submit_selectors", ["button[type='submit']"])

#     if strat.get("multi_step"):
#         await _fill(page, strat["username"], username)
#         await page.wait_for_timeout(random.randint(400, 700))
#         next_sels = strat.get("username_next_selectors", ["button[type='submit']"])
#         if not await _click_first(page, next_sels):
#             await page.keyboard.press("Enter")
#         await page.wait_for_timeout(random.randint(1500, 2500))
#         await _fill(page, strat["password"], password)
#         await page.wait_for_timeout(random.randint(300, 600))
#         if not await _click_first(page, submit_sels):
#             await page.keyboard.press("Enter")
#     else:
#         await _fill(page, strat["username"], username)
#         await page.wait_for_timeout(random.randint(200, 400))
#         await _fill(page, strat["password"], password)
#         await page.wait_for_timeout(random.randint(300, 500))
#         if not await _click_first(page, submit_sels):
#             await page.keyboard.press("Enter")


# # ─── Success check ────────────────────────────────────────────────────────────

# async def _is_logged_in(page, strat: dict | None) -> bool:
#     url = page.url.lower()
#     login_kw = ["/login", "/signin", "/sign-in", "/accounts/login", "/flow/login", "accounts.google"]
#     if not any(k in url for k in login_kw):
#         return True
#     if strat:
#         for sel in strat.get("success", []):
#             try:
#                 if await page.locator(sel).first.is_visible(timeout=2000):
#                     return True
#             except Exception:
#                 pass
#     return False


# # ─── Generic heuristic (unknown sites) ───────────────────────────────────────

# def _score_user(attrs: dict) -> int:
#     hay = " ".join(attrs.get(k, "") for k in ["name","id","placeholder","autocomplete","type","aria"]).lower()
#     s = 0
#     if "email" in hay: s += 6
#     if "user" in hay or "login" in hay: s += 5
#     if "phone" in hay or "tel" in hay: s -= 2
#     if attrs.get("type") in ["email","text"]: s += 1
#     if attrs.get("autocomplete") in ["email","username"]: s += 3
#     return s

# def _score_pw(attrs: dict) -> int:
#     s = 10 if attrs.get("type") == "password" else 0
#     hay = " ".join(attrs.get(k,"") for k in ["name","id","placeholder","autocomplete"]).lower()
#     if "password" in hay or "pass" in hay: s += 5
#     return s

# def _score_submit(attrs: dict, text: str) -> int:
#     hay = (text + " " + " ".join(attrs.get(k,"") for k in ["type","name","id","aria"])).lower()
#     s = 0
#     if any(w in hay for w in ["sign in","login","log in","connexion","se connecter"]): s += 6
#     if any(w in hay for w in ["continue","next"]): s += 2
#     if "submit" in attrs.get("type",""): s += 2
#     if any(w in hay for w in ["cancel","register","sign up","create"]): s -= 4
#     return s

# async def _get_attr(loc, name: str) -> str:
#     try: return (await loc.get_attribute(name)) or ""
#     except: return ""

# async def _generic_login(page, username: str, password: str):
#     await page.wait_for_timeout(1500)

#     pw_locs = page.locator("input[type='password']")
#     if await pw_locs.count() == 0:
#         email_sels = ["input[type='email']","input[name='email']","input[name='username']",
#                       "input[autocomplete='email']","input[autocomplete='username']","input[type='text']"]
#         for sel in email_sels:
#             try:
#                 loc = page.locator(sel).first
#                 await loc.wait_for(state="visible", timeout=3000)
#                 await loc.fill(username)
#                 await page.wait_for_timeout(500)
#                 await _click_first(page, ["button:has-text('Next')","button:has-text('Continue')",
#                                           "button[type='submit']","input[type='submit']"])
#                 await page.wait_for_timeout(2000)
#                 break
#             except Exception:
#                 continue

#     pw_locs = page.locator("input[type='password']")
#     pw_count = await pw_locs.count()
#     if pw_count == 0:
#         raise Exception("No password field found")
#     pw_loc = pw_locs.first
#     best_pw = -999
#     for i in range(min(pw_count, 5)):
#         loc = pw_locs.nth(i)
#         try:
#             await loc.wait_for(state="visible", timeout=1500)
#             attrs = {k: await _get_attr(loc, k) for k in ["type","name","id","placeholder","autocomplete"]}
#             sc = _score_pw(attrs)
#             if sc > best_pw:
#                 best_pw = sc
#                 pw_loc = loc
#         except Exception:
#             pass

#     user_locs = page.locator("input:not([type='hidden']):not([type='password'])")
#     user_count = await user_locs.count()
#     user_loc = None
#     best_us = -999
#     for i in range(min(user_count, 20)):
#         loc = user_locs.nth(i)
#         try:
#             await loc.wait_for(state="visible", timeout=1000)
#             attrs = {k: await _get_attr(loc, k) for k in ["type","name","id","placeholder","autocomplete","aria-label"]}
#             sc = _score_user(attrs)
#             if sc > best_us:
#                 best_us = sc
#                 user_loc = loc
#         except Exception:
#             pass

#     if user_loc and best_us > 2:
#         try:
#             await user_loc.click()
#             await user_loc.fill(username)
#             await page.wait_for_timeout(200)
#         except Exception:
#             pass

#     await pw_loc.wait_for(state="visible", timeout=8000)
#     await pw_loc.click()
#     await pw_loc.fill("")
#     await pw_loc.type(password, delay=random.randint(30, 60))
#     await page.wait_for_timeout(300)

#     btns = page.locator("button, input[type='submit']")
#     btn_count = await btns.count()
#     submit_loc = None
#     best_sub = -999
#     for i in range(min(btn_count, 20)):
#         b = btns.nth(i)
#         try:
#             await b.wait_for(state="visible", timeout=1000)
#             text = ""
#             try: text = await b.inner_text()
#             except: pass
#             attrs = {k: await _get_attr(b, k) for k in ["type","name","id","aria-label"]}
#             sc = _score_submit(attrs, text)
#             if sc > best_sub:
#                 best_sub = sc
#                 submit_loc = b
#         except Exception:
#             pass

#     if submit_loc and best_sub >= 0:
#         try: await submit_loc.click(timeout=8000)
#         except: await pw_loc.press("Enter")
#     else:
#         await pw_loc.press("Enter")


# # ─── 2FA wait loop ────────────────────────────────────────────────────────────

# async def _wait_for_2fa_completion(page, strat: dict | None, max_wait_s: int = 180) -> None:
#     """
#     Après détection d'une page 2FA/checkpoint, attendre que l'utilisateur
#     complète la vérification manuellement (visible seulement si headless=False).

#     Polling toutes les 5s jusqu'à max_wait_s secondes.
#     """
#     print(f"[relay] 2FA/checkpoint détecté sur {page.url}")
#     print(f"[relay] En attente de la complétion manuelle (max {max_wait_s}s)...")
#     print("[relay] → Completez la vérification dans le navigateur ouvert")

#     polls = max_wait_s // 5
#     for i in range(polls):
#         await page.wait_for_timeout(5000)
#         still_2fa = await _detect_2fa(page)
#         logged = await _is_logged_in(page, strat)
#         if logged or not still_2fa:
#             print(f"[relay] ✓ 2FA complété après {(i+1)*5}s")
#             return
#         print(f"[relay] Toujours sur la page de vérification ({page.url}) — {(i+1)*5}s écoulées...")

#     print(f"[relay] ⚠ Timeout 2FA après {max_wait_s}s")


# # ─── PUBLIC API ───────────────────────────────────────────────────────────────

# async def login_and_get_cookies(
#     service_url: str,
#     username: str,
#     password: str,
#     profile: Optional[Dict[str, Any]] = None,
# ) -> Dict[str, Any]:
#     """
#     Universal relay login.

#     Priorité :
#       1. profile avec sélecteurs explicites (ex: recolyse.com)
#       2. _SITE_STRATEGIES (pinterest, linkedin, facebook, twitter…)
#       3. heuristique générique (sites inconnus)

#     Retourne :
#       cookies, localStorage, sessionStorage, current_url, title,
#       used_selectors, login_detected, tfa_detected, domain, origin, debug
#     """
#     profile = profile or {}

#     def _as_list(x, default):
#         if not x: return default
#         return [x] if isinstance(x, str) else (x if isinstance(x, list) else default)

#     username_selectors = _as_list(profile.get("username_selector"), [])
#     password_selectors = _as_list(profile.get("password_selector"), [])
#     submit_selectors   = _as_list(profile.get("submit_selector"), [])
#     has_profile = bool(username_selectors or password_selectors)

#     open_login_sel    = profile.get("open_login_selector")
#     goto_wait         = profile.get("goto_wait_until", "domcontentloaded")
#     pre_fill_ms       = int(profile.get("pre_fill_wait_ms", 1200))
#     between_ms        = int(profile.get("between_actions_wait_ms", 250))
#     after_submit_ms   = int(profile.get("after_submit_wait_ms", 2500))
#     post_timeout_ms   = int(profile.get("post_login_timeout_ms", 20_000))
#     post_url_contains = profile.get("post_login_url_contains")
#     post_selector     = profile.get("post_login_selector")
#     post_goto         = profile.get("post_login_goto")
#     stay_ms           = int(profile.get("stay_connected_ms", 4000))
#     cookie_wait_name  = profile.get("cookie_wait_name")
#     cookie_min        = int(profile.get("cookie_min_count", 1))
#     cookie_timeout_ms = int(profile.get("cookie_wait_timeout_ms", 15_000))

#     used = {"username": None, "password": None, "submit": None}
#     browser = None

#     try:
#         service_url = (service_url or "").strip()
#         if not service_url:
#             raise Exception("service_url is empty")
#         if not service_url.startswith(("http://","https://")):
#             service_url = "https://" + service_url

#         domain = _domain_from_url(service_url)
#         origin = _origin_from_url(service_url)
#         strat  = _SITE_STRATEGIES.get(domain)
#         login_url = strat["login_url"] if (strat and not has_profile) else service_url

#         async with async_playwright() as p:
#             browser, context = await _make_stealth_context(p)
#             page = await context.new_page()
#             page.set_default_timeout(DEFAULT_TIMEOUT_MS)

#             await page.goto(login_url, wait_until=goto_wait)
#             try: await page.wait_for_load_state("domcontentloaded", timeout=10_000)
#             except Exception: pass

#             before_url = page.url
#             login_detected = False
#             used_method = "unknown"

#             # ── Branche 1 : stratégie site connu ─────────────────────────
#             if strat and not has_profile:
#                 await _run_strategy(page, strat, username, password)
#                 used["username"] = used["password"] = used["submit"] = "strategy"
#                 used_method = f"strategy:{domain}"

#             # ── Branche 2 : sélecteurs custom (profile) ───────────────────
#             elif has_profile:
#                 await page.wait_for_timeout(pre_fill_ms)
#                 if open_login_sel:
#                     try:
#                         await page.locator(open_login_sel).first.click()
#                         await page.wait_for_timeout(between_ms)
#                     except Exception:
#                         pass

#                 pw_loc = None
#                 for sel in username_selectors:
#                     try:
#                         loc = page.locator(sel).first
#                         await loc.wait_for(state="visible", timeout=10_000)
#                         await loc.scroll_into_view_if_needed()
#                         await loc.click()
#                         await loc.fill("")
#                         await page.wait_for_timeout(between_ms)
#                         await loc.type(username, delay=35)
#                         used["username"] = sel
#                         break
#                     except Exception:
#                         continue
#                 if not used["username"]:
#                     raise Exception("Cannot find username field")

#                 for sel in password_selectors:
#                     try:
#                         loc = page.locator(sel).first
#                         await loc.wait_for(state="visible", timeout=10_000)
#                         await loc.scroll_into_view_if_needed()
#                         await loc.click()
#                         await loc.fill("")
#                         await page.wait_for_timeout(between_ms)
#                         await loc.type(password, delay=35)
#                         used["password"] = sel
#                         pw_loc = loc
#                         break
#                     except Exception:
#                         continue
#                 if not used["password"]:
#                     raise Exception("Cannot find password field")

#                 clicked = False
#                 for sel in (submit_selectors or ["button[type='submit']","input[type='submit']"]):
#                     try:
#                         btn = page.locator(sel).first
#                         await btn.wait_for(state="visible", timeout=8_000)
#                         await page.wait_for_timeout(between_ms)
#                         await btn.click()
#                         used["submit"] = sel
#                         clicked = True
#                         break
#                     except Exception:
#                         continue
#                 if not clicked:
#                     await pw_loc.press("Enter")
#                     used["submit"] = "Enter"
#                 used_method = "profile_selectors"

#             # ── Branche 3 : heuristique générique ────────────────────────
#             else:
#                 await _generic_login(page, username, password)
#                 used["username"] = used["password"] = used["submit"] = "generic"
#                 used_method = "generic"

#             # ── Post-submit ───────────────────────────────────────────────
#             await page.wait_for_timeout(after_submit_ms)
#             try: await page.wait_for_load_state("networkidle", timeout=8_000)
#             except Exception: pass

#             # ── 2FA / checkpoint detection ────────────────────────────────
#             tfa_detected = await _detect_2fa(page)
#             if tfa_detected:
#                 await _wait_for_2fa_completion(page, strat, max_wait_s=180)

#             # ── Success detection ─────────────────────────────────────────
#             if strat and not has_profile:
#                 await page.wait_for_timeout(2000)
#                 login_detected = await _is_logged_in(page, strat)
#                 if not login_detected:
#                     await page.wait_for_timeout(3000)
#                     login_detected = await _is_logged_in(page, strat)
#             else:
#                 if post_url_contains:
#                     try:
#                         await page.wait_for_url(f"**{post_url_contains}**", timeout=post_timeout_ms)
#                         login_detected = True
#                     except Exception: pass
#                 if not login_detected and post_selector:
#                     try:
#                         await page.locator(post_selector).first.wait_for(state="visible", timeout=post_timeout_ms)
#                         login_detected = True
#                     except Exception: pass
#                 if post_goto:
#                     try:
#                         await page.goto(post_goto, wait_until="domcontentloaded")
#                         await page.wait_for_timeout(800)
#                         login_detected = True
#                     except Exception: pass
#                 if not login_detected:
#                     login_detected = await _is_logged_in(page, strat)

#             # ── Cookie polling ────────────────────────────────────────────
#             if stay_ms > 0:
#                 await page.wait_for_timeout(stay_ms)

#             cookies, elapsed = [], 0
#             while elapsed < cookie_timeout_ms:
#                 cookies = await context.cookies()
#                 domain_cookies = [c for c in cookies if (c.get("domain") or "").lstrip(".").endswith(domain)]
#                 ok = len(domain_cookies) >= cookie_min
#                 if cookie_wait_name:
#                     ok = ok and any(c.get("name") == cookie_wait_name for c in domain_cookies)
#                 if ok:
#                     break
#                 await page.wait_for_timeout(250)
#                 elapsed += 250

#             await page.wait_for_timeout(300)
#             local_storage, session_storage = await _dump_storage(page)

#             return {
#                 "cookies": cookies,
#                 "localStorage": local_storage,
#                 "sessionStorage": session_storage,
#                 "current_url": page.url,
#                 "title": await page.title(),
#                 "used_selectors": used,
#                 "login_detected": login_detected,
#                 "tfa_detected": tfa_detected,
#                 "domain": domain,
#                 "origin": origin,
#                 "debug": {
#                     "before_url": before_url,
#                     "after_url": page.url,
#                     "used_method": used_method,
#                     "cookie_wait_elapsed_ms": elapsed,
#                     "domain_cookie_count": len([c for c in cookies if (c.get("domain") or "").lstrip(".").endswith(domain)]),
#                 },
#             }

#     except Exception as e:
#         raise Exception(f"Playwright login failed: {str(e)}")
#     finally:
#         try:
#             if browser: await browser.close()
#         except Exception:
#             pass






































# from __future__ import annotations

# import asyncio
# import os
# import random
# import urllib.parse
# from typing import Any, Dict, Optional, Tuple

# from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# DEFAULT_TIMEOUT_MS = 30_000
# HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "1") == "1"

# # ─── Stealth ──────────────────────────────────────────────────────────────────
# _STEALTH_JS = """
# () => {
#     Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
#     Object.defineProperty(navigator, 'plugins', {
#         get: () => {
#             const a = [
#                 { name:'Chrome PDF Plugin', filename:'internal-pdf-viewer', description:'Portable Document Format' },
#                 { name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', description:'' },
#                 { name:'Native Client', filename:'internal-nacl-plugin', description:'' },
#             ];
#             a.__proto__ = navigator.plugins.__proto__;
#             return a;
#         }
#     });
#     Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
#     Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
#     window.chrome = { runtime:{}, loadTimes:function(){}, csi:function(){}, app:{} };
#     try {
#         const gp = WebGLRenderingContext.prototype.getParameter;
#         WebGLRenderingContext.prototype.getParameter = function(p) {
#             if (p === 37445) return 'Intel Inc.';
#             if (p === 37446) return 'Intel Iris OpenGL Engine';
#             return gp.call(this, p);
#         };
#     } catch(e){}
#     const orig = window.navigator.permissions?.query?.bind(navigator.permissions);
#     if (orig) navigator.permissions.query = (p) =>
#         p.name === 'notifications'
#             ? Promise.resolve({ state: Notification.permission })
#             : orig(p);
# }
# """

# _USER_AGENTS = [
#     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
#     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
#     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
# ]

# # ─── 2FA / verification page detection ────────────────────────────────────────
# # These patterns in the URL or page content indicate a 2FA / OTP / captcha wall
# _2FA_URL_PATTERNS = [
#     "/two-factor", "/2fa", "/verify", "/otp", "/checkpoint",
#     "/challenge", "/confirmation", "/security-code",
#     "sms-verification", "phone-verify", "auth/mfa",
#     "accounts/login/two_factor", "login/tfvc",
# ]
# _2FA_SELECTOR_PATTERNS = [
#     "input[name='verificationCode']",
#     "input[name='code']",
#     "input[name='otp']",
#     "input[name='two_factor_code']",
#     "input[placeholder*='code' i]",
#     "input[placeholder*='verification' i]",
#     "input[autocomplete='one-time-code']",
#     "input[id*='code' i]",
#     "#approvals_code",
#     "[data-testid='ocfEnterTextTextInput']",
# ]

# async def _detect_2fa(page) -> bool:
#     """Return True if the current page looks like a 2FA / verification wall."""
#     url = page.url.lower()
#     if any(p in url for p in _2FA_URL_PATTERNS):
#         return True
#     for sel in _2FA_SELECTOR_PATTERNS:
#         try:
#             loc = page.locator(sel).first
#             if await loc.count() > 0 and await loc.is_visible(timeout=1500):
#                 return True
#         except Exception:
#             pass
#     return False


# # ─── Per-site strategies ──────────────────────────────────────────────────────
# _SITE_STRATEGIES: dict[str, dict] = {
#     "pinterest.com": {
#         "login_url": "https://www.pinterest.com/login/",
#         "username": "input[name='id']",
#         "password": "input[name='password']",
#         "submit_selectors": [
#             "button[type='submit']",
#             "button:has-text('Log in')",
#             "button:has-text('Continue')",
#             "div[data-test-id='registerFormSubmitButton']",
#             "div[data-test-id='loginButton']",
#         ],
#         "success": ["[data-test-id='header-avatar']", "[data-test-id='homefeed-feed']"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#     },
#     "linkedin.com": {
#         "login_url": "https://www.linkedin.com/login",
#         "username": "#username",
#         "password": "#password",
#         "submit_selectors": [
#             "button[type='submit']",
#             "button:has-text('Sign in')",
#             ".btn__primary--large",
#         ],
#         "success": [".feed-identity-module", "a[href='/feed/']", ".global-nav__me"],
#         "pre_wait_ms": 1500,
#         "multi_step": False,
#     },
#     "facebook.com": {
#         "login_url": "https://www.facebook.com/",
#         "username": "#email",
#         "password": "#pass",
#         "submit_selectors": [
#             "[name='login']",
#             "button[type='submit']",
#             "[data-testid='royal_login_button']",
#         ],
#         "success": ["[aria-label='Facebook']", "div[role='feed']", "[data-pagelet='LeftRail']"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#     },
#     "instagram.com": {
#         "login_url": "https://www.instagram.com/accounts/login/",
#         "username": "input[name='username']",
#         "password": "input[name='password']",
#         "submit_selectors": [
#             "button[type='submit']",
#             "button:has-text('Log in')",
#             "button:has-text('Log In')",
#         ],
#         "success": ["svg[aria-label='Home']"],
#         "pre_wait_ms": 2500,
#         "multi_step": False,
#     },
#     "twitter.com": {
#         "login_url": "https://x.com/i/flow/login",
#         "username": "input[autocomplete='username']",
#         "password": "input[name='password']",
#         "submit_selectors": ["[data-testid='LoginForm_Login_Button']", "button[type='submit']"],
#         "username_next_selectors": ["[data-testid='LoginForm_Login_Button']", "div[role='button']"],
#         "success": ["[data-testid='primaryColumn']"],
#         "pre_wait_ms": 2000,
#         "multi_step": True,
#     },
#     "x.com": {
#         "login_url": "https://x.com/i/flow/login",
#         "username": "input[autocomplete='username']",
#         "password": "input[name='password']",
#         "submit_selectors": ["[data-testid='LoginForm_Login_Button']", "button[type='submit']"],
#         "username_next_selectors": ["[data-testid='LoginForm_Login_Button']", "div[role='button']"],
#         "success": ["[data-testid='primaryColumn']"],
#         "pre_wait_ms": 2000,
#         "multi_step": True,
#     },
#     "google.com": {
#         "login_url": "https://accounts.google.com/signin",
#         "username": "input[type='email']",
#         "password": "input[type='password']",
#         "submit_selectors": ["#passwordNext", "button[type='submit']"],
#         "username_next_selectors": ["#identifierNext"],
#         "success": ["[data-ogsr-up]"],
#         "pre_wait_ms": 2000,
#         "multi_step": True,
#     },
#     "reddit.com": {
#         "login_url": "https://www.reddit.com/login/",
#         "username": "#loginUsername",
#         "password": "#loginPassword",
#         "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
#         "success": ["a[href*='/user/']"],
#         "pre_wait_ms": 1500,
#         "multi_step": False,
#     },
#     "github.com": {
#         "login_url": "https://github.com/login",
#         "username": "#login_field",
#         "password": "#password",
#         "submit_selectors": ["[type='submit'][name='commit']", "button[type='submit']"],
#         "success": [".Header-link--avatar"],
#         "pre_wait_ms": 1000,
#         "multi_step": False,
#     },
#     "discord.com": {
#         "login_url": "https://discord.com/login",
#         "username": "input[name='email']",
#         "password": "input[name='password']",
#         "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
#         "success": ["nav[aria-label='Servers sidebar']"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#     },
#     "tiktok.com": {
#         "login_url": "https://www.tiktok.com/login/phone-or-email/email",
#         "username": "input[name='username']",
#         "password": "input[type='password']",
#         "submit_selectors": ["button[type='submit']"],
#         "success": ["[data-e2e='profile-icon']"],
#         "pre_wait_ms": 3000,
#         "multi_step": False,
#     },
# }


# def _domain_from_url(url: str) -> str:
#     try:
#         return urllib.parse.urlparse(url).netloc.split(":")[0].lower().strip().lstrip("www.")
#     except Exception:
#         return ""

# def _origin_from_url(url: str) -> str:
#     p = urllib.parse.urlparse(url)
#     scheme = p.scheme or "https"
#     netloc = p.netloc or p.path.split("/")[0]
#     return f"{scheme}://{netloc}".rstrip("/")


# async def _dump_storage(page) -> Tuple[str | None, str | None]:
#     ls = await page.evaluate("""() => {
#         try {
#             const o={};
#             for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);o[k]=localStorage.getItem(k);}
#             const s=JSON.stringify(o); return s==="{}"?null:s;
#         } catch(e){return null;}
#     }""")
#     ss = await page.evaluate("""() => {
#         try {
#             const o={};
#             for(let i=0;i<sessionStorage.length;i++){const k=sessionStorage.key(i);o[k]=sessionStorage.getItem(k);}
#             const s=JSON.stringify(o); return s==="{}"?null:s;
#         } catch(e){return null;}
#     }""")
#     return ls, ss


# # ─── Stealth browser factory ──────────────────────────────────────────────────
# async def _make_stealth_context(p):
#     ua = random.choice(_USER_AGENTS)
#     browser = await p.chromium.launch(
#         headless=HEADLESS,
#         args=[
#             "--no-sandbox",
#             "--disable-blink-features=AutomationControlled",
#             "--disable-infobars",
#             "--disable-dev-shm-usage",
#             "--no-first-run",
#             "--no-default-browser-check",
#             "--window-size=1366,768",
#         ],
#     )
#     ctx = await browser.new_context(
#         user_agent=ua,
#         viewport={"width": 1366, "height": 768},
#         locale="en-US",
#         timezone_id="America/New_York",
#         java_script_enabled=True,
#         is_mobile=False,
#         has_touch=False,
#         color_scheme="light",
#     )
#     await ctx.add_init_script(_STEALTH_JS)
#     return browser, ctx


# # ─── Field helpers ────────────────────────────────────────────────────────────
# async def _fill(page, selector: str, value: str, timeout_ms: int = 15_000):
#     loc = page.locator(selector).first
#     await loc.wait_for(state="visible", timeout=timeout_ms)
#     await loc.scroll_into_view_if_needed()
#     await loc.click()
#     await loc.fill("")
#     await page.wait_for_timeout(random.randint(80, 200))
#     await loc.type(value, delay=random.randint(30, 60))


# async def _click_first(page, selectors: list[str], timeout_ms: int = 8_000) -> bool:
#     for sel in selectors:
#         try:
#             loc = page.locator(sel).first
#             await loc.wait_for(state="visible", timeout=timeout_ms)
#             await loc.click()
#             return True
#         except Exception:
#             continue
#     return False


# # ─── Per-site strategy executor ───────────────────────────────────────────────
# async def _run_strategy(page, strat: dict, username: str, password: str):
#     await page.wait_for_timeout(strat.get("pre_wait_ms", 1500))
#     submit_sels = strat.get("submit_selectors", ["button[type='submit']"])

#     if strat.get("multi_step"):
#         await _fill(page, strat["username"], username)
#         await page.wait_for_timeout(random.randint(400, 700))
#         next_sels = strat.get("username_next_selectors", ["button[type='submit']"])
#         if not await _click_first(page, next_sels):
#             await page.keyboard.press("Enter")
#         await page.wait_for_timeout(random.randint(1500, 2500))
#         await _fill(page, strat["password"], password)
#         await page.wait_for_timeout(random.randint(300, 600))
#         if not await _click_first(page, submit_sels):
#             await page.keyboard.press("Enter")
#     else:
#         await _fill(page, strat["username"], username)
#         await page.wait_for_timeout(random.randint(200, 400))
#         await _fill(page, strat["password"], password)
#         await page.wait_for_timeout(random.randint(300, 500))
#         if not await _click_first(page, submit_sels):
#             await page.keyboard.press("Enter")


# # ─── Success check ────────────────────────────────────────────────────────────
# async def _is_logged_in(page, strat: dict | None) -> bool:
#     url = page.url.lower()
#     login_kw = ["/login", "/signin", "/sign-in", "/accounts/login", "/flow/login", "accounts.google"]
#     if not any(k in url for k in login_kw):
#         return True
#     if strat:
#         for sel in strat.get("success", []):
#             try:
#                 if await page.locator(sel).first.is_visible(timeout=2000):
#                     return True
#             except Exception:
#                 pass
#     return False


# # ─── Generic heuristic (unknown sites) ───────────────────────────────────────
# def _score_user(attrs: dict) -> int:
#     hay = " ".join(attrs.get(k, "") for k in ["name","id","placeholder","autocomplete","type","aria"]).lower()
#     s = 0
#     if "email" in hay: s += 6
#     if "user" in hay or "login" in hay: s += 5
#     if "phone" in hay or "tel" in hay: s -= 2
#     if attrs.get("type") in ["email","text"]: s += 1
#     if attrs.get("autocomplete") in ["email","username"]: s += 3
#     return s

# def _score_pw(attrs: dict) -> int:
#     s = 10 if attrs.get("type") == "password" else 0
#     hay = " ".join(attrs.get(k,"") for k in ["name","id","placeholder","autocomplete"]).lower()
#     if "password" in hay or "pass" in hay: s += 5
#     return s

# def _score_submit(attrs: dict, text: str) -> int:
#     hay = (text + " " + " ".join(attrs.get(k,"") for k in ["type","name","id","aria"])).lower()
#     s = 0
#     if any(w in hay for w in ["sign in","login","log in","connexion","se connecter"]): s += 6
#     if any(w in hay for w in ["continue","next"]): s += 2
#     if "submit" in attrs.get("type",""): s += 2
#     if any(w in hay for w in ["cancel","register","sign up","create"]): s -= 4
#     return s

# async def _get_attr(loc, name: str) -> str:
#     try: return (await loc.get_attribute(name)) or ""
#     except: return ""

# async def _generic_login(page, username: str, password: str):
#     await page.wait_for_timeout(1500)

#     # Check for password field — if absent, may be email-first flow
#     pw_locs = page.locator("input[type='password']")
#     if await pw_locs.count() == 0:
#         # Fill email and submit to reveal password
#         email_sels = ["input[type='email']","input[name='email']","input[name='username']",
#                       "input[autocomplete='email']","input[autocomplete='username']","input[type='text']"]
#         for sel in email_sels:
#             try:
#                 loc = page.locator(sel).first
#                 await loc.wait_for(state="visible", timeout=3000)
#                 await loc.fill(username)
#                 await page.wait_for_timeout(500)
#                 await _click_first(page, ["button:has-text('Next')","button:has-text('Continue')",
#                                           "button[type='submit']","input[type='submit']"])
#                 await page.wait_for_timeout(2000)
#                 break
#             except Exception:
#                 continue

#     # Pick best password field
#     pw_locs = page.locator("input[type='password']")
#     pw_count = await pw_locs.count()
#     if pw_count == 0:
#         raise Exception("No password field found")
#     pw_loc = pw_locs.first
#     best_pw = -999
#     for i in range(min(pw_count, 5)):
#         loc = pw_locs.nth(i)
#         try:
#             await loc.wait_for(state="visible", timeout=1500)
#             attrs = {k: await _get_attr(loc, k) for k in ["type","name","id","placeholder","autocomplete"]}
#             sc = _score_pw(attrs)
#             if sc > best_pw:
#                 best_pw = sc
#                 pw_loc = loc
#         except Exception:
#             pass

#     # Pick best username field (on same page)
#     user_locs = page.locator("input:not([type='hidden']):not([type='password'])")
#     user_count = await user_locs.count()
#     user_loc = None
#     best_us = -999
#     for i in range(min(user_count, 20)):
#         loc = user_locs.nth(i)
#         try:
#             await loc.wait_for(state="visible", timeout=1000)
#             attrs = {k: await _get_attr(loc, k) for k in ["type","name","id","placeholder","autocomplete","aria-label"]}
#             sc = _score_user(attrs)
#             if sc > best_us:
#                 best_us = sc
#                 user_loc = loc
#         except Exception:
#             pass

#     if user_loc and best_us > 2:
#         try:
#             await user_loc.click()
#             await user_loc.fill(username)
#             await page.wait_for_timeout(200)
#         except Exception:
#             pass

#     await pw_loc.wait_for(state="visible", timeout=8000)
#     await pw_loc.click()
#     await pw_loc.fill("")
#     await pw_loc.type(password, delay=random.randint(30, 60))
#     await page.wait_for_timeout(300)

#     # Pick best submit button — score ALL buttons including those without type=submit
#     btns = page.locator("button, input[type='submit']")
#     btn_count = await btns.count()
#     submit_loc = None
#     best_sub = -999
#     for i in range(min(btn_count, 20)):
#         b = btns.nth(i)
#         try:
#             await b.wait_for(state="visible", timeout=1000)
#             text = ""
#             try: text = await b.inner_text()
#             except: pass
#             attrs = {k: await _get_attr(b, k) for k in ["type","name","id","aria-label"]}
#             sc = _score_submit(attrs, text)
#             if sc > best_sub:
#                 best_sub = sc
#                 submit_loc = b
#         except Exception:
#             pass

#     if submit_loc and best_sub >= 0:
#         try: await submit_loc.click(timeout=8000)
#         except: await pw_loc.press("Enter")
#     else:
#         await pw_loc.press("Enter")


# # ─── PUBLIC API ───────────────────────────────────────────────────────────────
# async def login_and_get_cookies(
#     service_url: str,
#     username: str,
#     password: str,
#     profile: Optional[Dict[str, Any]] = None,
# ) -> Dict[str, Any]:
#     """
#     Universal relay login.

#     Priority:
#       1. profile with explicit selectors (e.g. recolyse.com custom profile)
#       2. _SITE_STRATEGIES  (pinterest, linkedin, facebook, twitter…)
#       3. generic heuristic (any unknown site)

#     Returns the standard dict:
#       cookies, localStorage, sessionStorage, current_url, title,
#       used_selectors, login_detected, domain, origin, debug
#     """
#     profile = profile or {}

#     def _as_list(x, default):
#         if not x: return default
#         return [x] if isinstance(x, str) else (x if isinstance(x, list) else default)

#     username_selectors = _as_list(profile.get("username_selector"), [])
#     password_selectors = _as_list(profile.get("password_selector"), [])
#     submit_selectors   = _as_list(profile.get("submit_selector"), [])
#     has_profile = bool(username_selectors or password_selectors)

#     open_login_sel      = profile.get("open_login_selector")
#     goto_wait           = profile.get("goto_wait_until", "domcontentloaded")
#     pre_fill_ms         = int(profile.get("pre_fill_wait_ms", 1200))
#     between_ms          = int(profile.get("between_actions_wait_ms", 250))
#     after_submit_ms     = int(profile.get("after_submit_wait_ms", 2500))
#     post_timeout_ms     = int(profile.get("post_login_timeout_ms", 20_000))
#     post_url_contains   = profile.get("post_login_url_contains")
#     post_selector       = profile.get("post_login_selector")
#     post_goto           = profile.get("post_login_goto")
#     stay_ms             = int(profile.get("stay_connected_ms", 4000))
#     cookie_wait_name    = profile.get("cookie_wait_name")
#     cookie_min          = int(profile.get("cookie_min_count", 1))
#     cookie_timeout_ms   = int(profile.get("cookie_wait_timeout_ms", 15_000))

#     used = {"username": None, "password": None, "submit": None}
#     browser = None

#     try:
#         service_url = (service_url or "").strip()
#         if not service_url:
#             raise Exception("service_url is empty")
#         if not service_url.startswith(("http://","https://")):
#             service_url = "https://" + service_url

#         domain = _domain_from_url(service_url)
#         origin = _origin_from_url(service_url)
#         strat  = _SITE_STRATEGIES.get(domain)
#         login_url = strat["login_url"] if (strat and not has_profile) else service_url

#         async with async_playwright() as p:
#             browser, context = await _make_stealth_context(p)
#             page = await context.new_page()
#             page.set_default_timeout(DEFAULT_TIMEOUT_MS)

#             await page.goto(login_url, wait_until=goto_wait)
#             try: await page.wait_for_load_state("domcontentloaded", timeout=10_000)
#             except Exception: pass

#             before_url = page.url
#             login_detected = False
#             used_method = "unknown"

#             # ── Branch 1: known-site strategy ────────────────────────────
#             if strat and not has_profile:
#                 await _run_strategy(page, strat, username, password)
#                 used["username"] = used["password"] = used["submit"] = "strategy"
#                 used_method = f"strategy:{domain}"

#             # ── Branch 2: explicit profile selectors ─────────────────────
#             elif has_profile:
#                 await page.wait_for_timeout(pre_fill_ms)
#                 if open_login_sel:
#                     try:
#                         await page.locator(open_login_sel).first.click()
#                         await page.wait_for_timeout(between_ms)
#                     except Exception:
#                         pass

#                 pw_loc = None
#                 for sel in username_selectors:
#                     try:
#                         loc = page.locator(sel).first
#                         await loc.wait_for(state="visible", timeout=10_000)
#                         await loc.scroll_into_view_if_needed()
#                         await loc.click()
#                         await loc.fill("")
#                         await page.wait_for_timeout(between_ms)
#                         await loc.type(username, delay=35)
#                         used["username"] = sel
#                         break
#                     except Exception:
#                         continue
#                 if not used["username"]:
#                     raise Exception("Cannot find username field")

#                 for sel in password_selectors:
#                     try:
#                         loc = page.locator(sel).first
#                         await loc.wait_for(state="visible", timeout=10_000)
#                         await loc.scroll_into_view_if_needed()
#                         await loc.click()
#                         await loc.fill("")
#                         await page.wait_for_timeout(between_ms)
#                         await loc.type(password, delay=35)
#                         used["password"] = sel
#                         pw_loc = loc
#                         break
#                     except Exception:
#                         continue
#                 if not used["password"]:
#                     raise Exception("Cannot find password field")

#                 clicked = False
#                 for sel in (submit_selectors or ["button[type='submit']","input[type='submit']"]):
#                     try:
#                         btn = page.locator(sel).first
#                         await btn.wait_for(state="visible", timeout=8_000)
#                         await page.wait_for_timeout(between_ms)
#                         await btn.click()
#                         used["submit"] = sel
#                         clicked = True
#                         break
#                     except Exception:
#                         continue
#                 if not clicked:
#                     await pw_loc.press("Enter")
#                     used["submit"] = "Enter"
#                 used_method = "profile_selectors"

#             # ── Branch 3: generic heuristic ──────────────────────────────
#             else:
#                 await _generic_login(page, username, password)
#                 used["username"] = used["password"] = used["submit"] = "generic"
#                 used_method = "generic"

#             # ── Post-submit ───────────────────────────────────────────────
#             await page.wait_for_timeout(after_submit_ms)
#             try: await page.wait_for_load_state("networkidle", timeout=8_000)
#             except Exception: pass

#             # ── 2FA / verification detection ─────────────────────────────
#             # If we detect a 2FA page, wait up to 3 minutes for the owner
#             # to complete it manually (headless=False required for this to work)
#             tfa_detected = await _detect_2fa(page)
#             if tfa_detected:
#                 print(f"[relay] 2FA/verification page detected on {domain}. Waiting up to 3 min for manual completion...")
#                 # Poll every 5 s for up to 3 minutes
#                 for _ in range(36):
#                     await page.wait_for_timeout(5000)
#                     still_2fa = await _detect_2fa(page)
#                     logged_in = await _is_logged_in(page, strat)
#                     if logged_in or not still_2fa:
#                         print("[relay] 2FA completed or page changed, continuing...")
#                         break
#                     print(f"[relay] Still on 2FA page ({page.url}), waiting...")

#             # ── Success detection ─────────────────────────────────────────
#             if strat and not has_profile:
#                 await page.wait_for_timeout(2000)
#                 login_detected = await _is_logged_in(page, strat)
#                 if not login_detected:
#                     await page.wait_for_timeout(3000)
#                     login_detected = await _is_logged_in(page, strat)
#             else:
#                 if post_url_contains:
#                     try:
#                         await page.wait_for_url(f"**{post_url_contains}**", timeout=post_timeout_ms)
#                         login_detected = True
#                     except Exception: pass
#                 if not login_detected and post_selector:
#                     try:
#                         await page.locator(post_selector).first.wait_for(state="visible", timeout=post_timeout_ms)
#                         login_detected = True
#                     except Exception: pass
#                 if post_goto:
#                     try:
#                         await page.goto(post_goto, wait_until="domcontentloaded")
#                         await page.wait_for_timeout(800)
#                         login_detected = True
#                     except Exception: pass
#                 if not login_detected:
#                     login_detected = await _is_logged_in(page, strat)

#             # ── Cookie readiness polling ──────────────────────────────────
#             if stay_ms > 0:
#                 await page.wait_for_timeout(stay_ms)

#             cookies, elapsed = [], 0
#             while elapsed < cookie_timeout_ms:
#                 cookies = await context.cookies()
#                 domain_cookies = [c for c in cookies if (c.get("domain") or "").lstrip(".").endswith(domain)]
#                 ok = len(domain_cookies) >= cookie_min
#                 if cookie_wait_name:
#                     ok = ok and any(c.get("name") == cookie_wait_name for c in domain_cookies)
#                 if ok:
#                     break
#                 await page.wait_for_timeout(250)
#                 elapsed += 250

#             await page.wait_for_timeout(300)
#             local_storage, session_storage = await _dump_storage(page)

#             return {
#                 "cookies": cookies,
#                 "localStorage": local_storage,
#                 "sessionStorage": session_storage,
#                 "current_url": page.url,
#                 "title": await page.title(),
#                 "used_selectors": used,
#                 "login_detected": login_detected,
#                 "tfa_detected": tfa_detected,
#                 "domain": domain,
#                 "origin": origin,
#                 "debug": {
#                     "before_url": before_url,
#                     "after_url": page.url,
#                     "used_method": used_method,
#                     "cookie_wait_elapsed_ms": elapsed,
#                     "domain_cookie_count": len([c for c in cookies if (c.get("domain") or "").lstrip(".").endswith(domain)]),
#                 },
#             }

#     except Exception as e:
#         raise Exception(f"Playwright login failed: {str(e)}")
#     finally:
#         try:
#             if browser: await browser.close()
#         except Exception:
#             pass































# functional with root me 

# from __future__ import annotations

# import os
# import random
# import urllib.parse

# import asyncio
# import re
# from typing import Optional, Callable, Awaitable


# from typing import Any, Dict, Optional, Tuple

# from playwright.async_api import (
#     TimeoutError as PlaywrightTimeoutError,
#     async_playwright,
# )

# DEFAULT_TIMEOUT_MS = 30_000
# HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "1") == "1"

# # ─── Stealth ──────────────────────────────────────────────────────────────────
# _STEALTH_JS = """
# () => {
#     Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
#     Object.defineProperty(navigator, 'plugins', {
#         get: () => {
#             const a = [
#                 { name:'Chrome PDF Plugin', filename:'internal-pdf-viewer', description:'Portable Document Format' },
#                 { name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', description:'' },
#                 { name:'Native Client', filename:'internal-nacl-plugin', description:'' },
#             ];
#             a.__proto__ = navigator.plugins.__proto__;
#             return a;
#         }
#     });
#     Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
#     Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
#     window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
#     const gp = WebGLRenderingContext.prototype.getParameter;
#     WebGLRenderingContext.prototype.getParameter = function(p) {
#         if (p === 37445) return 'Intel Inc.';
#         if (p === 37446) return 'Intel Iris OpenGL Engine';
#         return gp.call(this, p);
#     };
#     const orig = window.navigator.permissions?.query?.bind(navigator.permissions);
#     if (orig) {
#         navigator.permissions.query = (p) =>
#             p.name === 'notifications'
#                 ? Promise.resolve({ state: Notification.permission })
#                 : orig(p);
#     }
# }
# """

# _USER_AGENTS = [
#     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
#     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
#     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
# ]

# # ─── Per-site strategies ──────────────────────────────────────────────────────
# # Keys must match what _domain_from_url() returns (no "www.")
# _SITE_STRATEGIES: dict[str, dict] = {
#     "pinterest.com": {
#         "login_url": "https://www.pinterest.com/login/",
#         "username": "input[name='id']",
#         "password": "input[name='password']",
#         # Pinterest uses a <button> with no type — match by text
#         "submit_selectors": [
#             "button[type='submit']",
#             "button:has-text('Log in')",
#             "button:has-text('Continue')",
#             "div[data-test-id='registerFormSubmitButton']",
#             "div[data-test-id='loginButton']",
#             "button",   # last resort: first visible button after password
#         ],
#         "success": ["[data-test-id='header-avatar']", "[data-test-id='homefeed-feed']", "div[data-test-id='user-avatar']"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#     },
#     "linkedin.com": {
#         "login_url": "https://www.linkedin.com/login",
#         "username": "input[name='session_key']",          # champ email/téléphone
#         "password": "input[name='session_password']",     # champ mot de passe
#         "submit_selectors": [
#             "button[type='submit']",
#             "button:has-text('Sign in')",
#             "button:has-text('Se connecter')",
#             ".btn__primary--large",
#         ],
#         "success": [".feed-identity-module", "a[href='/feed/']", ".global-nav__me"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#     },

#     "facebook.com": {
#         "login_url": "https://www.facebook.com/",
#         "username": "#email",
#         "password": "#pass",
#         "submit_selectors": [
#             "[name='login']",
#             "button[type='submit']",
#             "[data-testid='royal_login_button']",
#         ],
#         "success": ["[aria-label='Facebook']", "div[role='feed']", "[data-pagelet='LeftRail']"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#     },
#     "instagram.com": {
#         "login_url": "https://www.instagram.com/accounts/login/",
#         "username": "input[name='username']",
#         "password": "input[name='password']",
#         "submit_selectors": [
#             "button[type='submit']",
#             "button:has-text('Log in')",
#             "button:has-text('Log In')",
#         ],
#         "success": ["svg[aria-label='Home']", "a[href='/']"],
#         "pre_wait_ms": 2500,
#         "multi_step": False,
#     },
#     "twitter.com": {
#         "login_url": "https://x.com/i/flow/login",
#         "username": "input[autocomplete='username']",
#         "password": "input[name='password']",
#         "submit_selectors": ["[data-testid='LoginForm_Login_Button']", "button[type='submit']"],
#         "username_next_selectors": ["[data-testid='LoginForm_Login_Button']", "div[role='button']"],
#         "success": ["[data-testid='primaryColumn']"],
#         "pre_wait_ms": 2000,
#         "multi_step": True,
#     },
#     "x.com": {
#         "login_url": "https://x.com/i/flow/login",
#         "username": "input[autocomplete='username']",
#         "password": "input[name='password']",
#         "submit_selectors": ["[data-testid='LoginForm_Login_Button']", "button[type='submit']"],
#         "username_next_selectors": ["[data-testid='LoginForm_Login_Button']", "div[role='button']"],
#         "success": ["[data-testid='primaryColumn']"],
#         "pre_wait_ms": 2000,
#         "multi_step": True,
#     },
#     "google.com": {
#         "login_url": "https://accounts.google.com/signin",
#         "username": "input[type='email']",
#         "password": "input[type='password']",
#         "submit_selectors": ["#passwordNext", "button[type='submit']"],
#         "username_next_selectors": ["#identifierNext"],
#         "success": ["[data-ogsr-up]", "a[aria-label*='Google Account']"],
#         "pre_wait_ms": 2000,
#         "multi_step": True,
#     },
#     "reddit.com": {
#         "login_url": "https://www.reddit.com/login/",
#         "username": "#loginUsername",
#         "password": "#loginPassword",
#         "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
#         "success": ["a[href*='/user/']", "#USER_AGENT_THEME_ROOT"],
#         "pre_wait_ms": 1500,
#         "multi_step": False,
#     },
#     "github.com": {
#         "login_url": "https://github.com/login",
#         "username": "#login_field",
#         "password": "#password",
#         "submit_selectors": ["[type='submit'][name='commit']", "button[type='submit']"],
#         "success": [".Header-link--avatar", "[aria-label='Homepage']"],
#         "pre_wait_ms": 1000,
#         "multi_step": False,
#     },
#     "discord.com": {
#         "login_url": "https://discord.com/login",
#         "username": "input[name='email']",
#         "password": "input[name='password']",
#         "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
#         "success": ["nav[aria-label='Servers sidebar']"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#     },
#     "tiktok.com": {
#         "login_url": "https://www.tiktok.com/login/phone-or-email/email",
#         "username": "input[name='username']",
#         "password": "input[type='password']",
#         "submit_selectors": ["button[type='submit']", "button:has-text('Log in')"],
#         "success": ["[data-e2e='profile-icon']"],
#         "pre_wait_ms": 3000,
#         "multi_step": False,
#     },
#     "snapchat.com": {
#         "login_url": "https://accounts.snapchat.com/accounts/login",
#         "username": "input[name='username']",
#         "password": "input[name='password']",
#         "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
#         "success": ["[data-testid='web-header']"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#     },
# }


# def _domain_from_url(url: str) -> str:
#     try:
#         d = urllib.parse.urlparse(url).netloc
#         return d.split(":")[0].lower().strip().lstrip("www.")
#     except Exception:
#         return ""


# def _origin_from_url(url: str) -> str:
#     parsed = urllib.parse.urlparse(url)
#     scheme = parsed.scheme or "https"
#     netloc = parsed.netloc or parsed.path
#     if "/" in netloc:
#         netloc = netloc.split("/")[0]
#     return f"{scheme}://{netloc}".rstrip("/")


# async def _dump_storage(page) -> Tuple[str | None, str | None]:
#     local_storage = await page.evaluate(
#         """() => {
#           try {
#             const o = {};
#             for (let i = 0; i < window.localStorage.length; i++) {
#               const k = window.localStorage.key(i);
#               o[k] = window.localStorage.getItem(k);
#             }
#             const s = JSON.stringify(o);
#             return s === "{}" ? null : s;
#           } catch (e) { return null; }
#         }"""
#     )
#     session_storage = await page.evaluate(
#         """() => {
#           try {
#             const o = {};
#             for (let i = 0; i < window.sessionStorage.length; i++) {
#               const k = window.sessionStorage.key(i);
#               o[k] = window.sessionStorage.getItem(k);
#             }
#             const s = JSON.stringify(o);
#             return s === "{}" ? null : s;
#           } catch (e) { return null; }
#         }"""
#     )
#     return local_storage, session_storage


# def _norm(s: str) -> str:
#     return (s or "").strip().lower()


# async def _attr(locator, name: str) -> str:
#     try:
#         v = await locator.get_attribute(name)
#         return v or ""
#     except Exception:
#         return ""


# # ─── Stealth browser factory ──────────────────────────────────────────────────

# async def _make_stealth_context(p):
#     ua = random.choice(_USER_AGENTS)
#     browser = await p.chromium.launch(
        
#         headless=HEADLESS,
#         args=[
#             "--no-sandbox",
#             "--disable-blink-features=AutomationControlled",
#             "--disable-infobars",
#             "--disable-dev-shm-usage",
#             "--disable-extensions",
#             "--no-first-run",
#             "--no-default-browser-check",
#             "--window-size=1366,768",
#         ],
#     )

#     # browser = await p.chromium.launch(
#     #             headless=False,
#     #             args=["--disable-dev-shm-usage", "--no-sandbox"],
#     #         )



#     context = await browser.new_context(
#         user_agent=ua,
#         viewport={"width": 1366, "height": 768},
#         locale="en-US",
#         timezone_id="America/New_York",
#         java_script_enabled=True,
#         is_mobile=False,
#         has_touch=False,
#         color_scheme="light",
#         device_scale_factor=1,
#     )
#     await context.add_init_script(_STEALTH_JS)
#     return browser, context


# # ─── Field helpers ────────────────────────────────────────────────────────────

# async def _fill(page, selector: str, value: str, timeout_ms: int = 15_000):
#     """Wait for a field, clear it, type value with human-like delay."""
#     loc = page.locator(selector).first
#     await loc.wait_for(state="visible", timeout=timeout_ms)
#     await loc.scroll_into_view_if_needed()
#     await loc.click()
#     await loc.fill("")
#     await page.wait_for_timeout(random.randint(100, 250))
#     await loc.type(value, delay=random.randint(30, 60))


# async def _click_first_visible(page, selectors: list[str], timeout_ms: int = 8_000) -> bool:
#     """Try selectors in order, click the first visible one. Returns True if clicked."""
#     for sel in selectors:
#         try:
#             loc = page.locator(sel).first
#             await loc.wait_for(state="visible", timeout=timeout_ms)
#             await loc.click()
#             return True
#         except Exception:
#             continue
#     return False


# # ─── Per-site login ───────────────────────────────────────────────────────────

# async def _run_site_strategy(page, strat: dict, username: str, password: str):
#     """Execute a known-site login strategy (single or multi-step)."""
#     pre = strat.get("pre_wait_ms", 1500)
#     await page.wait_for_timeout(pre)

#     submit_sels = strat.get("submit_selectors", ["button[type='submit']"])

#     if strat.get("multi_step"):
#         # Step 1: fill username
#         await _fill(page, strat["username"], username)
#         await page.wait_for_timeout(random.randint(400, 800))
#         # Click "Next" button
#         next_sels = strat.get("username_next_selectors", ["button[type='submit']"])
#         clicked = await _click_first_visible(page, next_sels)
#         if not clicked:
#             await page.keyboard.press("Enter")
#         # Wait for password field
#         await page.wait_for_timeout(random.randint(1500, 2500))
#         # Step 2: fill password
#         await _fill(page, strat["password"], password)
#         await page.wait_for_timeout(random.randint(300, 600))
#         # Submit
#         clicked = await _click_first_visible(page, submit_sels)
#         if not clicked:
#             await page.keyboard.press("Enter")
#     else:
#         # Single page: fill username + password then submit
#         await _fill(page, strat["username"], username)
#         await page.wait_for_timeout(random.randint(200, 500))
#         await _fill(page, strat["password"], password)
#         await page.wait_for_timeout(random.randint(300, 600))
#         clicked = await _click_first_visible(page, submit_sels)
#         if not clicked:
#             await page.keyboard.press("Enter")


# async def _check_success(page, strat: dict | None) -> bool:
#     """True if we left the login page or found a post-login indicator."""
#     current = page.url
#     login_kw = ["/login", "/signin", "/sign-in", "/accounts/login", "/flow/login", "accounts.google"]
#     if not any(kw in current.lower() for kw in login_kw):
#         return True
#     if strat:
#         for sel in strat.get("success", []):
#             try:
#                 loc = page.locator(sel).first
#                 if await loc.count() > 0 and await loc.is_visible(timeout=3000):
#                     return True
#             except Exception:
#                 pass
#     return False


# # ─── Generic heuristic (fallback for unknown sites) ──────────────────────────

# def _score_username(attrs: dict) -> int:
#     hay = " ".join([_norm(attrs.get(k, "")) for k in ["name", "id", "placeholder", "autocomplete", "type", "aria"]])
#     score = 0
#     if "email" in hay: score += 6
#     if "user" in hay or "username" in hay or "login" in hay: score += 5
#     if "phone" in hay or "tel" in hay: score -= 2
#     if attrs.get("type", "") in ["email", "text"]: score += 1
#     if attrs.get("autocomplete", "") in ["email", "username"]: score += 3
#     return score


# def _score_password(attrs: dict) -> int:
#     score = 0
#     if attrs.get("type", "") == "password": score += 10
#     hay = " ".join([_norm(attrs.get(k, "")) for k in ["name", "id", "placeholder", "autocomplete", "aria"]])
#     if "password" in hay or "pass" in hay: score += 5
#     if attrs.get("autocomplete", "") in ["current-password", "new-password"]: score += 3
#     return score


# def _score_submit(attrs: dict, text: str) -> int:
#     t = _norm(text)
#     hay = " ".join([_norm(attrs.get(k, "")), t])
#     score = 0
#     if "sign in" in hay or "login" in hay or "log in" in hay or "connexion" in hay or "se connecter" in hay: score += 6
#     if "continue" in hay or "next" in hay: score += 2
#     if "submit" in _norm(attrs.get("type", "")): score += 2
#     if "cancel" in hay or "register" in hay or "sign up" in hay or "create" in hay: score -= 4
#     return score


# async def _generic_login(page, username: str, password: str) -> dict:
#     """
#     Heuristic login for unknown sites.
#     Returns debug dict. Raises on failure.
#     """
#     await page.wait_for_timeout(1500)

#     # ── Find password field ──────────────────────────────────────────────────
#     pw_inputs = page.locator("input[type='password']")
#     pw_count = await pw_inputs.count()

#     # If no password field visible yet, check for single-field (email-first) flow
#     if pw_count == 0:
#         # Try to fill email and press Enter/Next to reveal password field
#         email_sels = [
#             "input[type='email']", "input[name='email']", "input[id*='email']",
#             "input[name='username']", "input[id*='user']", "input[autocomplete='email']",
#             "input[autocomplete='username']",
#         ]
#         filled = False
#         for sel in email_sels:
#             try:
#                 loc = page.locator(sel).first
#                 await loc.wait_for(state="visible", timeout=3000)
#                 await loc.fill(username)
#                 filled = True
#                 # Try clicking a "Next" / "Continue" button
#                 next_clicked = await _click_first_visible(page, [
#                     "button:has-text('Next')", "button:has-text('Continue')",
#                     "button[type='submit']", "input[type='submit']",
#                 ], timeout_ms=3000)
#                 if not next_clicked:
#                     await page.keyboard.press("Enter")
#                 await page.wait_for_timeout(2000)
#                 break
#             except Exception:
#                 continue

#         if not filled:
#             raise Exception("Cannot find username/email field on page")

#         # Re-check for password field
#         pw_count = await pw_inputs.count()
#         if pw_count == 0:
#             raise Exception("Password field did not appear after username submission")

#     # ── Score and pick best password field ───────────────────────────────────
#     password_locator = None
#     best_pw_score = -999
#     for i in range(min(pw_count, 10)):
#         loc = pw_inputs.nth(i)
#         try:
#             await loc.wait_for(state="visible", timeout=2000)
#             attrs = {
#                 "type": await _attr(loc, "type"),
#                 "name": await _attr(loc, "name"),
#                 "id": await _attr(loc, "id"),
#                 "placeholder": await _attr(loc, "placeholder"),
#                 "autocomplete": await _attr(loc, "autocomplete"),
#                 "aria": await _attr(loc, "aria-label"),
#             }
#             sc = _score_password(attrs)
#             if sc > best_pw_score:
#                 best_pw_score = sc
#                 password_locator = loc
#         except Exception:
#             continue

#     if not password_locator:
#         raise Exception("No visible password field found")

#     # ── Fill password ─────────────────────────────────────────────────────────
#     # Only fill username again if it's on the same page (not already submitted above)
#     user_inputs = page.locator("input:not([type='hidden']):not([type='password'])")
#     user_count = await user_inputs.count()
#     username_locator = None
#     best_user_score = -999
#     for i in range(min(user_count, 30)):
#         loc = user_inputs.nth(i)
#         try:
#             await loc.wait_for(state="visible", timeout=1200)
#             attrs = {
#                 "type": await _attr(loc, "type"),
#                 "name": await _attr(loc, "name"),
#                 "id": await _attr(loc, "id"),
#                 "placeholder": await _attr(loc, "placeholder"),
#                 "autocomplete": await _attr(loc, "autocomplete"),
#                 "aria": await _attr(loc, "aria-label"),
#             }
#             sc = _score_username(attrs)
#             if sc > best_user_score:
#                 best_user_score = sc
#                 username_locator = loc
#         except Exception:
#             continue

#     if username_locator and best_user_score > 2:
#         try:
#             await username_locator.click()
#             await username_locator.fill(username)
#             await page.wait_for_timeout(random.randint(150, 300))
#         except Exception:
#             pass

#     await password_locator.wait_for(state="visible", timeout=8000)
#     await password_locator.click()
#     await password_locator.fill("")
#     await page.wait_for_timeout(random.randint(100, 250))
#     await password_locator.type(password, delay=random.randint(30, 60))
#     await page.wait_for_timeout(random.randint(200, 400))

#     # ── Find and click submit ─────────────────────────────────────────────────
#     # Score-based button selection (broader — no type='submit' requirement)
#     buttons = page.locator("button, input[type='submit']")
#     btn_count = await buttons.count()
#     submit_locator = None
#     best_submit_score = -999
#     for i in range(min(btn_count, 30)):
#         b = buttons.nth(i)
#         try:
#             await b.wait_for(state="visible", timeout=1200)
#             text = ""
#             try:
#                 text = await b.inner_text()
#             except Exception:
#                 pass
#             attrs = {
#                 "type": await _attr(b, "type"),
#                 "name": await _attr(b, "name"),
#                 "id": await _attr(b, "id"),
#                 "aria": await _attr(b, "aria-label"),
#             }
#             sc = _score_submit(attrs, text)
#             if sc > best_submit_score:
#                 best_submit_score = sc
#                 submit_locator = b
#         except Exception:
#             continue

#     if submit_locator and best_submit_score >= 0:
#         try:
#             await submit_locator.click(timeout=8000)
#         except Exception:
#             await password_locator.press("Enter")
#     else:
#         # No good button found: just press Enter
#         await password_locator.press("Enter")

#     return {"method": "generic_heuristic"}


# # ─── Public API ───────────────────────────────────────────────────────────────
# async def _prompt_2fa_code_interactive(challenge_url: str, page) -> str:
#     """
#     Demande à l'utilisateur de saisir le code 2FA dans le terminal.
#     Fonctionne même en mode headless (mais le navigateur doit être visible pour que l'user voie la page).
#     """
#     print("\n" + "="*60)
#     print("🔐 2FA REQUIRED")
#     print(f"Page: {challenge_url}")
#     print("Veuillez entrer le code de vérification reçu par SMS / email / authenticator :")
#     code = input("Code: ").strip()
#     print("="*60 + "\n")
#     return code

# # Optionnel : récupération automatique par email (IMAP) – à configurer
# async def _fetch_2fa_from_email(email_address: str, email_password: str, imap_server: str = "imap.gmail.com", timeout_seconds: int = 120) -> str:
#     """
#     Exemple basique pour Gmail. Nécessite `pip install imap-tools`.
#     Non activé par défaut.
#     """
#     # Implémentation possible – je ne la détaille pas ici pour rester simple.
#     # Vous pouvez l'ajouter si besoin.
#     raise NotImplementedError("Lisez le code source pour implémenter cette partie.")

# # ============================================================================
# # Détection et gestion du 2FA dans Playwright
# # ============================================================================

# async def _handle_2fa_if_needed(
#     page,
#     timeout_seconds: int = 120,
#     code_callback: Optional[Callable[[str, any], Awaitable[str]]] = None
# ) -> bool:
#     """
#     Détecte si une page de challenge 2FA est présente.
#     Si oui, attend le code (via callback) et le soumet.
#     Retourne True si le 2FA a été résolu avec succès, False si aucun 2FA détecté.
#     """
#     # Liste de patterns d'URL de challenge (LinkedIn, Google, Facebook, etc.)
#     challenge_patterns = [
#         r"/checkpoint/challenge/",   # LinkedIn
#         r"/challenge/",              # générique
#         r"/login/challenge",         # autre
#         r"/2fa/",                    # Twitter, etc.
#         r"/auth/verify",             # générique
#         r"verification",             # fallback
#     ]

#     current_url = page.url
#     is_challenge = any(re.search(p, current_url, re.I) for p in challenge_patterns)

#     # Vérifier aussi la présence d'un champ de code visible
#     code_input_selector = "input[name='challengeCode'], input[name='2faCode'], input[name='verificationCode'], input[placeholder*='code'], input[placeholder*='verification']"
#     code_input = page.locator(code_input_selector).first if is_challenge else None

#     if not is_challenge and code_input:
#         # Parfois l'URL n'est pas typique mais le champ est là
#         try:
#             await code_input.wait_for(state="visible", timeout=3000)
#             is_challenge = True
#         except:
#             pass

#     if not is_challenge:
#         return False

#     print(f"[2FA] Challenge détecté : {current_url}")

#     # Si aucun callback fourni, utiliser le prompt interactif
#     if not code_callback:
#         code_callback = _prompt_2fa_code_interactive

#     # Attendre que l'utilisateur donne le code
#     code = await code_callback(current_url, page)

#     # Remplir et soumettre
#     try:
#         await code_input.fill(code)
#         await page.wait_for_timeout(500)
#         # Chercher le bouton de validation
#         submit_btns = page.locator("button[type='submit'], button:has-text('Verify'), button:has-text('Submit'), button:has-text('Continue')")
#         if await submit_btns.count() > 0:
#             await submit_btns.first.click()
#         else:
#             await code_input.press("Enter")
#         # Attendre la redirection
#         await page.wait_for_load_state("networkidle", timeout=30000)
#         await page.wait_for_timeout(2000)
#         print("[2FA] Code soumis avec succès.")
#         return True
#     except Exception as e:
#         print(f"[2FA] Erreur lors de la soumission : {e}")
#         raise























# async def login_and_get_cookies(
#     service_url: str,
#     username: str,
#     password: str,
#     profile: Optional[Dict[str, Any]] = None,
# ) -> Dict[str, Any]:
#     """
#     Universal relay login. Supports all major sites + generic heuristic fallback.
#     Backward-compatible signature.

#     Priority:
#       1. profile with explicit selectors (e.g. recolyse.com)
#       2. _SITE_STRATEGIES (pinterest, linkedin, facebook, twitter …)
#       3. generic heuristic (any unknown site)
#     """
#     profile = profile or {}

#     def _as_list(x, default_list):
#         if not x:
#             return default_list
#         return [x] if isinstance(x, str) else (x if isinstance(x, list) else default_list)

#     # Profile-provided selectors
#     username_selectors = _as_list(profile.get("username_selector"), [])
#     password_selectors = _as_list(profile.get("password_selector"), [])
#     submit_selectors   = _as_list(profile.get("submit_selector"), [])
#     has_profile_selectors = bool(username_selectors or password_selectors)

#     open_login_selector     = profile.get("open_login_selector")
#     goto_wait_until         = profile.get("goto_wait_until", "domcontentloaded")
#     pre_fill_wait_ms        = int(profile.get("pre_fill_wait_ms", 1200))
#     between_actions_wait_ms = int(profile.get("between_actions_wait_ms", 250))
#     after_submit_wait_ms    = int(profile.get("after_submit_wait_ms", 2500))
#     post_login_timeout_ms   = int(profile.get("post_login_timeout_ms", 20_000))
#     post_login_url_contains = profile.get("post_login_url_contains")
#     post_login_selector     = profile.get("post_login_selector")
#     post_login_goto         = profile.get("post_login_goto")
#     stay_connected_ms       = int(profile.get("stay_connected_ms", 4000))
#     cookie_wait_name        = profile.get("cookie_wait_name")
#     cookie_min_count        = int(profile.get("cookie_min_count", 1))
#     cookie_wait_timeout_ms  = int(profile.get("cookie_wait_timeout_ms", 15_000))

#     used = {"username": None, "password": None, "submit": None}
#     browser = None

#     try:
#         service_url = (service_url or "").strip()
#         if not service_url:
#             raise Exception("service_url is empty")
#         if not service_url.startswith(("http://", "https://")):
#             service_url = "https://" + service_url

#         domain = _domain_from_url(service_url)   # already strips "www."
#         origin = _origin_from_url(service_url)

#         # Decide which strategy to use
#         strat = _SITE_STRATEGIES.get(domain)

#         # Navigate to login URL
#         if strat and not has_profile_selectors:
#             login_url = strat["login_url"]
#         else:
#             login_url = service_url

#         async with async_playwright() as p:
#             browser, context = await _make_stealth_context(p)
#             page = await context.new_page()
#             page.set_default_timeout(DEFAULT_TIMEOUT_MS)

#             await page.goto(login_url, wait_until=goto_wait_until)
#             try:
#                 await page.wait_for_load_state("domcontentloaded", timeout=10_000)
#             except Exception:
#                 pass

#             before_url = page.url
#             login_detected = False
#             used_method = "unknown"

#             # ── Branch 1 : known-site strategy ───────────────────────────
#             if strat and not has_profile_selectors:
#                 await _run_site_strategy(page, strat, username, password)


#                 await _handle_2fa_if_needed(page, timeout_seconds=60, code_callback=None)

#                 used["username"] = used["password"] = used["submit"] = "strategy"
#                 used_method = f"strategy:{domain}"

#             # ── Branch 2 : explicit profile selectors (e.g. recolyse) ────
#             elif has_profile_selectors:
#                 await page.wait_for_timeout(pre_fill_wait_ms)
#                 if open_login_selector:
#                     try:
#                         await page.locator(open_login_selector).first.click()
#                         await page.wait_for_timeout(between_actions_wait_ms)
#                     except Exception:
#                         pass

#                 # username
#                 username_locator = None
#                 for sel in username_selectors:
#                     try:
#                         loc = page.locator(sel).first
#                         await loc.wait_for(state="visible", timeout=10_000)
#                         await loc.scroll_into_view_if_needed()
#                         await loc.click(timeout=2000)
#                         await loc.fill("")
#                         await page.wait_for_timeout(between_actions_wait_ms)
#                         await loc.type(username, delay=35)
#                         used["username"] = sel
#                         username_locator = loc
#                         break
#                     except Exception:
#                         continue
#                 if not username_locator:
#                     raise Exception("Cannot find/fill username field. Tried: " + ", ".join(username_selectors))

#                 # password
#                 password_locator = None
#                 for sel in password_selectors:
#                     try:
#                         loc = page.locator(sel).first
#                         await loc.wait_for(state="visible", timeout=10_000)
#                         await loc.scroll_into_view_if_needed()
#                         await loc.click(timeout=2000)
#                         await loc.fill("")
#                         await page.wait_for_timeout(between_actions_wait_ms)
#                         await loc.type(password, delay=35)
#                         used["password"] = sel
#                         password_locator = loc
#                         break
#                     except Exception:
#                         continue
#                 if not password_locator:
#                     raise Exception("Cannot find/fill password field. Tried: " + ", ".join(password_selectors))

#                 # submit
#                 clicked = False
#                 for sel in (submit_selectors or ["button[type='submit']", "input[type='submit']"]):
#                     try:
#                         btn = page.locator(sel).first
#                         await btn.wait_for(state="visible", timeout=8_000)
#                         await page.wait_for_timeout(between_actions_wait_ms)
#                         await btn.click()
#                         used["submit"] = sel
#                         clicked = True


#                         await _handle_2fa_if_needed(page)



#                         break
#                     except Exception:
#                         continue
#                 if not clicked:
#                     await password_locator.press("Enter")
#                     used["submit"] = "press:Enter"
#                 used_method = "profile_selectors"

#             # ── Branch 3 : generic heuristic ─────────────────────────────
#             else:
#                 await _generic_login(page, username, password)


#                 await _handle_2fa_if_needed(page)

#                 used["username"] = used["password"] = used["submit"] = "generic"
#                 used_method = "generic_heuristic"

#             # ── Post-submit ───────────────────────────────────────────────
#             await page.wait_for_timeout(after_submit_wait_ms)
#             try:
#                 await page.wait_for_load_state("networkidle", timeout=10_000)
#             except Exception:
#                 pass

#             # ── Success detection ─────────────────────────────────────────
#             if strat and not has_profile_selectors:
#                 await page.wait_for_timeout(2000)
#                 login_detected = await _check_success(page, strat)
#                 if not login_detected:
#                     await page.wait_for_timeout(3000)
#                     login_detected = await _check_success(page, strat)
#             else:
#                 if post_login_url_contains:
#                     try:
#                         await page.wait_for_url(f"**{post_login_url_contains}**", timeout=post_login_timeout_ms)
#                         login_detected = True
#                     except Exception:
#                         pass
#                 if not login_detected and post_login_selector:
#                     try:
#                         await page.locator(post_login_selector).first.wait_for(state="visible", timeout=post_login_timeout_ms)
#                         login_detected = True
#                     except Exception:
#                         pass
#                 if post_login_goto:
#                     try:
#                         await page.goto(post_login_goto, wait_until="domcontentloaded")
#                         await page.wait_for_timeout(800)
#                         login_detected = True
#                     except Exception:
#                         pass
#                 # Fallback: if URL changed away from login page, consider it success
#                 if not login_detected:
#                     login_detected = await _check_success(page, strat)

#             # ── Cookie readiness polling ──────────────────────────────────
#             if stay_connected_ms > 0:
#                 await page.wait_for_timeout(stay_connected_ms)

#             cookies = []
#             poll_ms = 250
#             elapsed = 0
#             while elapsed < cookie_wait_timeout_ms:
#                 cookies = await context.cookies()
#                 domain_cookies = [c for c in cookies if (c.get("domain") or "").lstrip(".").endswith(domain)]
#                 ok_count = len(domain_cookies) >= cookie_min_count
#                 ok_name = (not cookie_wait_name) or any(c.get("name") == cookie_wait_name for c in domain_cookies)
#                 if ok_count and ok_name:
#                     break
#                 await page.wait_for_timeout(poll_ms)
#                 elapsed += poll_ms

#             await page.wait_for_timeout(300)
#             local_storage, session_storage = await _dump_storage(page)

#             return {
#                 "cookies": cookies,
#                 "localStorage": local_storage,
#                 "sessionStorage": session_storage,
#                 "current_url": page.url,
#                 "title": await page.title(),
#                 "used_selectors": used,
#                 "login_detected": login_detected,
#                 "domain": domain,
#                 "origin": origin,
#                 "debug": {
#                     "before_url": before_url,
#                     "after_url": page.url,
#                     "used_method": used_method,
#                     "cookie_wait_elapsed_ms": elapsed,
#                     "domain_cookie_count": len([
#                         c for c in cookies
#                         if (c.get("domain") or "").lstrip(".").endswith(domain)
#                     ]),
#                 },
#             }

#     except Exception as e:
#         raise Exception(f"Playwright login failed: {str(e)}")

#     finally:
#         try:
#             if browser is not None:
#                 await browser.close()
#         except Exception:
#             pass





























#functional code before claude updates 
# from __future__ import annotations

# import os
# import re
# import urllib.parse
# from typing import Any, Dict, Optional, Tuple

# from playwright.async_api import (
#     TimeoutError as PlaywrightTimeoutError,
#     async_playwright,
# )

# DEFAULT_TIMEOUT_MS = 30_000

# # Requested: always show browser window
# HEADLESS = False


# def _domain_from_url(url: str) -> str:
#     try:
#         d = urllib.parse.urlparse(url).netloc
#         return d.split(":")[0].lower().strip()
#     except Exception:
#         return ""


# def _origin_from_url(url: str) -> str:
#     """
#     Return scheme://host[:port] for a given URL.
#     Falls back to https://<domain> if scheme missing.
#     """
#     parsed = urllib.parse.urlparse(url)
#     scheme = parsed.scheme or "https"
#     netloc = parsed.netloc or parsed.path  # handles malformed inputs like "example.com/login"
#     if "/" in netloc:
#         netloc = netloc.split("/")[0]
#     return f"{scheme}://{netloc}".rstrip("/")


# async def _dump_storage(page) -> Tuple[str | None, str | None]:
#     """
#     Returns (localStorageJSON_or_None, sessionStorageJSON_or_None) for the CURRENT page origin.
#     """
#     local_storage = await page.evaluate(
#         """() => {
#           try {
#             const o = {};
#             for (let i = 0; i < window.localStorage.length; i++) {
#               const k = window.localStorage.key(i);
#               o[k] = window.localStorage.getItem(k);
#             }
#             const s = JSON.stringify(o);
#             return s === "{}" ? null : s;
#           } catch (e) { return null; }
#         }"""
#     )
#     session_storage = await page.evaluate(
#         """() => {
#           try {
#             const o = {};
#             for (let i = 0; i < window.sessionStorage.length; i++) {
#               const k = window.sessionStorage.key(i);
#               o[k] = window.sessionStorage.getItem(k);
#             }
#             const s = JSON.stringify(o);
#             return s === "{}" ? null : s;
#           } catch (e) { return null; }
#         }"""
#     )
#     return local_storage, session_storage


# def _norm(s: str) -> str:
#     return (s or "").strip().lower()


# async def _attr(locator, name: str) -> str:
#     try:
#         v = await locator.get_attribute(name)
#         return v or ""
#     except Exception:
#         return ""


# def _score_username(attrs: dict) -> int:
#     hay = " ".join([_norm(attrs.get(k, "")) for k in ["name", "id", "placeholder", "autocomplete", "type", "aria"]])
#     score = 0
#     if "email" in hay:
#         score += 6
#     if "user" in hay or "username" in hay or "login" in hay:
#         score += 5
#     if "phone" in hay or "tel" in hay:
#         score -= 2
#     if attrs.get("type", "") in ["email", "text"]:
#         score += 1
#     if attrs.get("autocomplete", "") in ["email", "username"]:
#         score += 3
#     return score


# def _score_password(attrs: dict) -> int:
#     hay = " ".join([_norm(attrs.get(k, "")) for k in ["name", "id", "placeholder", "autocomplete", "type", "aria"]])
#     score = 0
#     if attrs.get("type", "") == "password":
#         score += 10
#     if "password" in hay or "pass" in hay:
#         score += 5
#     if attrs.get("autocomplete", "") in ["current-password", "new-password"]:
#         score += 3
#     return score


# def _score_submit(attrs: dict, text: str) -> int:
#     t = _norm(text)
#     hay = " ".join([_norm(attrs.get(k, "")), t])
#     score = 0
#     if "sign in" in hay or "login" in hay or "connexion" in hay or "se connecter" in hay:
#         score += 6
#     if "continue" in hay or "next" in hay:
#         score += 2
#     if "submit" in _norm(attrs.get("type", "")):
#         score += 2
#     if "cancel" in hay or "register" in hay or "sign up" in hay:
#         score -= 4
#     return score


# async def _auto_find_login_controls(page):
#     """
#     Returns (username_locator, password_locator, submit_locator, debug_dict)
#     best-effort for generic login forms.
#     """
#     debug = {"candidates": {"username": [], "password": [], "submit": []}}

#     pw = page.locator("input[type='password']")
#     pw_count = await pw.count()
#     if pw_count == 0:
#         return None, None, None, {"reason": "no password inputs found"}

#     password_locator = None
#     best_pw_score = -999
#     for i in range(min(pw_count, 25)):
#         loc = pw.nth(i)
#         try:
#             await loc.wait_for(state="visible", timeout=2000)
#             attrs = {
#                 "type": await _attr(loc, "type"),
#                 "name": await _attr(loc, "name"),
#                 "id": await _attr(loc, "id"),
#                 "placeholder": await _attr(loc, "placeholder"),
#                 "autocomplete": await _attr(loc, "autocomplete"),
#                 "aria": await _attr(loc, "aria-label"),
#             }
#             sc = _score_password(attrs)
#             debug["candidates"]["password"].append({"attrs": attrs, "score": sc})
#             if sc > best_pw_score:
#                 best_pw_score = sc
#                 password_locator = loc
#         except Exception:
#             continue

#     if not password_locator:
#         return None, None, None, {"reason": "no visible password input"}

#     user_inputs = page.locator("input:not([type='hidden']):not([type='password'])")
#     user_count = await user_inputs.count()

#     username_locator = None
#     best_user_score = -999
#     for i in range(min(user_count, 60)):
#         loc = user_inputs.nth(i)
#         try:
#             await loc.wait_for(state="visible", timeout=1200)
#             attrs = {
#                 "type": await _attr(loc, "type"),
#                 "name": await _attr(loc, "name"),
#                 "id": await _attr(loc, "id"),
#                 "placeholder": await _attr(loc, "placeholder"),
#                 "autocomplete": await _attr(loc, "autocomplete"),
#                 "aria": await _attr(loc, "aria-label"),
#             }
#             sc = _score_username(attrs)
#             debug["candidates"]["username"].append({"attrs": attrs, "score": sc})
#             if sc > best_user_score:
#                 best_user_score = sc
#                 username_locator = loc
#         except Exception:
#             continue

#     submit_locator = None
#     best_submit_score = -999
#     buttons = page.locator("button, input[type='submit'], button[type='submit']")
#     btn_count = await buttons.count()
#     for i in range(min(btn_count, 60)):
#         b = buttons.nth(i)
#         try:
#             await b.wait_for(state="visible", timeout=1200)
#             text = ""
#             try:
#                 text = await b.inner_text()
#             except Exception:
#                 pass
#             attrs = {
#                 "type": await _attr(b, "type"),
#                 "name": await _attr(b, "name"),
#                 "id": await _attr(b, "id"),
#                 "aria": await _attr(b, "aria-label"),
#             }
#             sc = _score_submit(attrs, text)
#             debug["candidates"]["submit"].append({"attrs": attrs, "text": text, "score": sc})
#             if sc > best_submit_score:
#                 best_submit_score = sc
#                 submit_locator = b
#         except Exception:
#             continue

#     if not submit_locator:
#         return username_locator, password_locator, None, {"reason": "no visible submit button", **debug}

#     return username_locator, password_locator, submit_locator, debug


# async def _wait_for_cookie_readiness(
#     context,
#     service_url: str,
#     cookie_wait_name: Optional[str],
#     timeout_ms: int,
# ) -> list[dict]:
#     """
#     Wait until cookies exist for target domain and (optionally) a cookie name exists.
#     Prevents extracting too early.
#     """
#     domain = _domain_from_url(service_url)
#     poll_ms = 250
#     elapsed = 0

#     while elapsed < timeout_ms:
#         cookies = await context.cookies()
#         has_domain_cookie = any((c.get("domain") or "").lstrip(".").endswith(domain) for c in cookies)

#         has_named_cookie = True
#         if cookie_wait_name:
#             has_named_cookie = any(c.get("name") == cookie_wait_name for c in cookies)

#         if has_domain_cookie and has_named_cookie:
#             return cookies

#         await context.pages[0].wait_for_timeout(poll_ms) if getattr(context, "pages", None) else None
#         elapsed += poll_ms

#     # last attempt
#     return await context.cookies()





# async def login_and_get_cookies(
#     service_url: str,
#     username: str,
#     password: str,
#     profile: Optional[Dict[str, Any]] = None,
# ) -> Dict[str, Any]:
#     """
#     Generic relay login with robust waits to avoid extracting cookies too early.
#     headless=False (browser visible).

#     profile options (all optional):
#       - pre_fill_wait_ms: int (default 1200)          # wait after goto before finding fields
#       - between_actions_wait_ms: int (default 250)    # small pauses between fill/click
#       - after_submit_wait_ms: int (default 2500)      # wait right after submit click
#       - post_login_timeout_ms: int (default 20000)    # total time to wait for "connected" state
#       - post_login_url_contains: str                  # best generic signal (e.g. "inventory.html")
#       - post_login_selector: str                      # selector visible only when logged in
#       - post_login_goto: str                          # explicit connected page to open after login
#       - stay_connected_ms: int (default 4000)         # keep page open a bit while cookies settle
#       - cookie_wait_name: str                         # wait for a specific cookie name
#       - cookie_min_count: int (default 1)             # require at least N cookies for the domain
#       - cookie_wait_timeout_ms: int (default 15000)   # polling time for cookie readiness
#       - username_selector/password_selector/submit_selector/open_login_selector/goto_wait_until: same as before
#     """
#     profile = profile or {}

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
#         ["input[type='email']", "input[name='email']", "input#email", "input[name='username']", "input[type='text']"],
#     )
#     password_selectors = _as_list(
#         profile.get("password_selector"),
#         ["input[type='password']", "input[name='password']", "input#password"],
#     )
#     submit_selectors = _as_list(
#         profile.get("submit_selector"),
#         ["button[type='submit']", "input[type='submit']", "button:has-text('Login')", "button:has-text('Sign in')"],
#     )

#     open_login_selector = profile.get("open_login_selector")
#     goto_wait_until = profile.get("goto_wait_until", "domcontentloaded")

#     pre_fill_wait_ms = int(profile.get("pre_fill_wait_ms", 1200))
#     between_actions_wait_ms = int(profile.get("between_actions_wait_ms", 250))
#     after_submit_wait_ms = int(profile.get("after_submit_wait_ms", 2500))

#     post_login_timeout_ms = int(profile.get("post_login_timeout_ms", 20_000))
#     post_login_url_contains = profile.get("post_login_url_contains")
#     post_login_selector = profile.get("post_login_selector")
#     post_login_goto = profile.get("post_login_goto")

#     stay_connected_ms = int(profile.get("stay_connected_ms", 4000))

#     cookie_wait_name = profile.get("cookie_wait_name")
#     cookie_min_count = int(profile.get("cookie_min_count", 1))
#     cookie_wait_timeout_ms = int(profile.get("cookie_wait_timeout_ms", 15_000))

#     used = {"username": None, "password": None, "submit": None}
#     browser = None
#     page = None

#     try:
#         service_url = (service_url or "").strip()
#         if not service_url:
#             raise Exception("service_url is empty")

#         if service_url.startswith("http://"):
#             service_url = "https://" + service_url[len("http://") :]

#         if not service_url.startswith(("http://", "https://")):
#             service_url = "https://" + service_url

#         origin = _origin_from_url(service_url)
#         domain = _domain_from_url(service_url)

#         async with async_playwright() as p:
#             # Always visible playwright browser for better reliability (some sites detect headless and hide login fields, plus we want to see what's going on)  
#             # browser = await p.chromium.launch(
#             #     headless=False,
#             #     args=["--disable-dev-shm-usage", "--no-sandbox"],
#             # )

#             browser = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"])



#             context = await browser.new_context()
#             page = await context.new_page()
#             page.set_default_timeout(DEFAULT_TIMEOUT_MS)

#             # 1) Go to login page and let it settle
#             await page.goto(service_url, wait_until=goto_wait_until)
#             try:
#                 await page.wait_for_load_state("domcontentloaded", timeout=10_000)
#             except Exception:
#                 pass

#             if pre_fill_wait_ms > 0:
#                 await page.wait_for_timeout(pre_fill_wait_ms)

#             # Optional open login modal
#             if open_login_selector:
#                 try:
#                     await page.locator(open_login_selector).first.click()
#                     await page.wait_for_timeout(between_actions_wait_ms)
#                 except Exception:
#                     pass

#             before_url = page.url

#             # 2) Auto-detect once (if your helper exists), otherwise fallback selectors
#             used_auto = False
#             try:
#                 auto_user, auto_pw, auto_submit, auto_debug = await _auto_find_login_controls(page)
#             except Exception:
#                 auto_user = auto_pw = auto_submit = None
#                 auto_debug = {"reason": "auto detection failed"}

#             if auto_pw is not None and auto_submit is not None:
#                 used_auto = True

#                 if auto_user is not None:
#                     try:
#                         await auto_user.wait_for(state="visible", timeout=8000)
#                         await auto_user.click(timeout=2000)
#                         await auto_user.fill("")
#                         await page.wait_for_timeout(between_actions_wait_ms)
#                         await auto_user.type(username, delay=35)
#                         used["username"] = "auto-detect"
#                     except Exception:
#                         pass

#                 await auto_pw.wait_for(state="visible", timeout=8000)
#                 await auto_pw.click(timeout=2000)
#                 await auto_pw.fill("")
#                 await page.wait_for_timeout(between_actions_wait_ms)
#                 await auto_pw.type(password, delay=35)
#                 used["password"] = "auto-detect"

#                 try:
#                     await page.wait_for_timeout(between_actions_wait_ms)
#                     await auto_submit.click(timeout=8000)
#                     used["submit"] = "auto-detect"
#                 except Exception:
#                     await auto_pw.press("Enter")
#                     used["submit"] = "auto-detect:press-enter"

#             else:
#                 # Fallback: explicit selectors
#                 username_locator = None
#                 for sel in username_selectors:
#                     try:
#                         loc = page.locator(sel).first
#                         await loc.wait_for(state="visible", timeout=10_000)
#                         await loc.scroll_into_view_if_needed()
#                         await loc.click(timeout=2000)
#                         await loc.fill("")
#                         await page.wait_for_timeout(between_actions_wait_ms)
#                         await loc.type(username, delay=35)
#                         used["username"] = sel
#                         username_locator = loc
#                         break
#                     except Exception:
#                         continue

#                 if not username_locator:
#                     raise Exception("Cannot find/fill username field. Tried: " + ", ".join(username_selectors))

#                 password_locator = None
#                 for sel in password_selectors:
#                     try:
#                         loc = page.locator(sel).first
#                         await loc.wait_for(state="visible", timeout=10_000)
#                         await loc.scroll_into_view_if_needed()
#                         await loc.click(timeout=2000)
#                         await loc.fill("")
#                         await page.wait_for_timeout(between_actions_wait_ms)
#                         await loc.type(password, delay=35)
#                         used["password"] = sel
#                         password_locator = loc
#                         break
#                     except Exception:
#                         continue

#                 if not password_locator:
#                     raise Exception("Cannot find/fill password field. Tried: " + ", ".join(password_selectors))

#                 clicked = False
#                 for sel in submit_selectors:
#                     try:
#                         btn = page.locator(sel).first
#                         await btn.wait_for(state="visible", timeout=8_000)
#                         await page.wait_for_timeout(between_actions_wait_ms)
#                         await btn.click()
#                         used["submit"] = sel
#                         clicked = True
#                         break
#                     except Exception:
#                         continue

#                 if not clicked:
#                     await password_locator.press("Enter")
#                     used["submit"] = "press:Enter"

#             # 3) After submit: give time for redirect + SPA routing
#             if after_submit_wait_ms > 0:
#                 await page.wait_for_timeout(after_submit_wait_ms)

#             # Try to settle network (don't fail hard if SPA keeps sockets open)
#             try:
#                 await page.wait_for_load_state("networkidle", timeout=10_000)
#             except Exception:
#                 pass

#             # 4) Wait for "connected" proof (URL contains or selector), best-effort
#             login_detected = False

#             if post_login_url_contains:
#                 try:
#                     await page.wait_for_url(f"**{post_login_url_contains}**", timeout=post_login_timeout_ms)
#                     login_detected = True
#                 except Exception:
#                     pass

#             if (not login_detected) and post_login_selector:
#                 try:
#                     await page.locator(post_login_selector).first.wait_for(state="visible", timeout=post_login_timeout_ms)
#                     login_detected = True
#                 except Exception:
#                     pass

#             # 5) Optional explicit connected page navigation (VERY effective for sites like saucedemo)
#             if post_login_goto:
#                 try:
#                     await page.goto(post_login_goto, wait_until="domcontentloaded")
#                     await page.wait_for_timeout(800)
#                     login_detected = True
#                 except Exception:
#                     pass

#             # 6) Stay connected a bit so cookies/storage fully settle BEFORE extraction
#             if stay_connected_ms > 0:
#                 await page.wait_for_timeout(stay_connected_ms)

#             # 7) Cookie readiness polling loop (avoid extracting too early)
#             cookies = []
#             poll_ms = 250
#             elapsed = 0
#             while elapsed < cookie_wait_timeout_ms:
#                 cookies = await context.cookies()

#                 domain_cookies = [
#                     c for c in cookies
#                     if (c.get("domain") or "").lstrip(".").endswith(domain)
#                 ]
#                 ok_count = len(domain_cookies) >= cookie_min_count

#                 ok_name = True
#                 if cookie_wait_name:
#                     ok_name = any(c.get("name") == cookie_wait_name for c in domain_cookies)

#                 if ok_count and ok_name:
#                     break

#                 await page.wait_for_timeout(poll_ms)
#                 elapsed += poll_ms

#             # One last tiny settle
#             await page.wait_for_timeout(300)

#             # 8) Dump storage AFTER cookies are ready
#             local_storage, session_storage = await _dump_storage(page)

#             return {
#                 "cookies": cookies,
#                 "localStorage": local_storage,
#                 "sessionStorage": session_storage,
#                 "current_url": page.url,
#                 "title": await page.title(),
#                 "used_selectors": used,
#                 "login_detected": login_detected,
#                 "domain": domain,
#                 "origin": origin,
#                 "debug": {
#                     "before_url": before_url,
#                     "after_url": page.url,
#                     "used_auto_detect": used_auto,
#                     "auto_debug": auto_debug if used_auto else None,
#                     "cookie_wait_elapsed_ms": elapsed,
#                     "domain_cookie_count": len([c for c in cookies if (c.get("domain") or "").lstrip(".").endswith(domain)]),
#                 },
#             }

#     except Exception as e:
#         raise Exception(f"Playwright login failed: {str(e)}")

#     finally:
#         try:
#             if browser is not None:
#                 await browser.close()
#         except Exception:
#             pass



