# """
# playwright_relay.py — Universal Relay Login (version finale)
# =============================================================

# Fonctionne avec :
#   ✓ Sites simples        : Pinterest, Recolyse, GitHub, Reddit, Discord, Snapchat
#   ✓ Sites anti-bot lourds: Facebook, Instagram, LinkedIn, Twitter/X, Google, TikTok
#   ✓ Vérification par code: SMS, email, authenticator app (2FA / OTP interactif)
#   ✓ Sites inconnus       : heuristique générique avec scoring des champs

# Correctifs vs version précédente :
#   - Bug 2FA corrigé : code_input était None quand is_challenge=False → recréé après détection
#   - FB : URL /login.php + sélecteurs alternatifs + popup "Save info" géré
#   - Instagram : popups post-login gérés
#   - Twitter : unusual-activity email verification géré
#   - Profil persistant par domaine → moins de CAPTCHAs
#   - ignore_default_args pour supprimer le flag --enable-automation de Playwright
#   - Polling 2FA : vérifie après chaque action, pas seulement après submit
#   - Détection "wrong password" pour éviter faux positifs login_detected
#   - playwright-stealth utilisé si installé (pip install playwright-stealth)
# """

# from __future__ import annotations

# import asyncio
# import os
# import random
# import re
# import urllib.parse
# from pathlib import Path
# from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

# from playwright.async_api import (
#     BrowserContext,
#     Page,
#     TimeoutError as PlaywrightTimeoutError,
#     async_playwright,
# )

# # ─── playwright-stealth (optionnel mais fortement recommandé) ─────────────────
# # pip install playwright-stealth
# try:
#     from playwright_stealth import stealth_async  # type: ignore
#     _HAS_STEALTH = True
# except ImportError:
#     _HAS_STEALTH = False

# # ─── Config ───────────────────────────────────────────────────────────────────

# DEFAULT_TIMEOUT_MS = 30_000
# HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "1") == "1"

# # Profils persistants : chaque domaine garde ses cookies entre les runs
# # → FB/IG auront moins de CAPTCHA au 2ème run
# PROFILE_DIR = Path(os.getenv("PLAYWRIGHT_PROFILE_DIR", "/tmp/zkp_relay_profiles"))

# _USER_AGENTS = [
#     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
#     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
#     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
#     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
# ]

# # ─── Stealth JS ───────────────────────────────────────────────────────────────

# _STEALTH_JS = """
# () => {
#     // Masquer webdriver
#     Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

#     // Plugins réalistes
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

#     Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
#     Object.defineProperty(navigator, 'platform',  { get: () => 'Win32' });

#     // Objet chrome présent dans un vrai Chrome
#     window.chrome = {
#         runtime: {},
#         loadTimes: function() {},
#         csi: function() {},
#         app: {},
#     };

#     // WebGL vendor/renderer spoofing
#     const _getParam = WebGLRenderingContext.prototype.getParameter;
#     WebGLRenderingContext.prototype.getParameter = function(p) {
#         if (p === 37445) return 'Intel Inc.';
#         if (p === 37446) return 'Intel Iris OpenGL Engine';
#         return _getParam.call(this, p);
#     };

#     // Notification permissions
#     const _origQuery = window.navigator.permissions?.query?.bind(navigator.permissions);
#     if (_origQuery) {
#         navigator.permissions.query = (p) =>
#             p.name === 'notifications'
#                 ? Promise.resolve({ state: Notification.permission })
#                 : _origQuery(p);
#     }

#     // Masquer l'automation dans toString()
#     const _origFn = Function.prototype.toString;
#     Function.prototype.toString = function() {
#         if (this === window.chrome?.runtime?.sendMessage) return 'function sendMessage() { [native code] }';
#         return _origFn.call(this);
#     };
# }
# """

# # ─── Stratégies par site ──────────────────────────────────────────────────────

# _SITE_STRATEGIES: dict[str, dict] = {

#     # ── Facebook ──────────────────────────────────────────────────────────────
#     # FB obfusque parfois les IDs → liste de sélecteurs de fallback
#     "facebook.com": {
#         "login_url": "https://www.facebook.com/login.php",   # plus fiable que /
#         "username": [
#             "#email",
#             "input[name='email']",
#             "input[type='email']",
#         ],
#         "password": [
#             "#pass",
#             "input[name='pass']",
#             "input[type='password']",
#         ],
#         "submit_selectors": [
#             "button[name='login']",
#             "[data-testid='royal_login_button']",
#             "button[type='submit']",
#             "input[type='submit'][value*='Log']",
#         ],
#         "success": [
#             "[aria-label='Facebook']",
#             "div[role='feed']",
#             "[data-pagelet='LeftRail']",
#             "[aria-label='Home']",
#         ],
#         "error_selectors": [
#             "#error_box",
#             ".login_error_box",
#             "[data-testid='royal_login_button'] + div",
#         ],
#         "pre_wait_ms": 2500,
#         "multi_step": False,
#         "post_popups": [
#             # "Save login info?"
#             "button:has-text('Save')",          # accepter (optionnel)
#             "button:has-text('Not Now')",
#             "button:has-text('Not now')",
#             # "Turn on notifications?"
#             "button:has-text('Not Now')",
#         ],
#         # URL patterns supplémentaires de challenge FB
#         "challenge_url_patterns": [
#             r"checkpoint",
#             r"login/device-based",
#             r"login/identify",
#             r"recover",
#         ],
#     },

#     # ── Instagram ─────────────────────────────────────────────────────────────
#     "instagram.com": {
#         "login_url": "https://www.instagram.com/accounts/login/",
#         "username": ["input[name='username']"],
#         "password": ["input[name='password']"],
#         "submit_selectors": [
#             "button[type='submit']",
#             "button:has-text('Log in')",
#             "button:has-text('Log In')",
#         ],
#         "success": [
#             "svg[aria-label='Home']",
#             "a[href*='/direct/inbox/']",
#             "[aria-label='Home']",
#         ],
#         "pre_wait_ms": 2500,
#         "multi_step": False,
#         "post_popups": [
#             "button:has-text('Not Now')",
#             "button:has-text('Not now')",
#             "button:has-text('Skip')",
#         ],
#         "challenge_url_patterns": [
#             r"/challenge/",
#             r"accounts/suspended",
#         ],
#     },

#     # ── Twitter / X ───────────────────────────────────────────────────────────
#     # Twitter a un flow en 2 étapes ET peut demander un email de vérif
#     "twitter.com": {
#         "login_url": "https://x.com/i/flow/login",
#         "username": ["input[autocomplete='username']"],
#         "password": ["input[name='password']"],
#         "submit_selectors": [
#             "[data-testid='LoginForm_Login_Button']",
#             "button[type='submit']",
#         ],
#         "username_next_selectors": [
#             "[data-testid='LoginForm_Login_Button']",
#             "div[role='button']:has-text('Next')",
#         ],
#         "success": ["[data-testid='primaryColumn']", "[aria-label='Home timeline']"],
#         "pre_wait_ms": 2000,
#         "multi_step": True,
#         "post_popups": [],
#         "challenge_url_patterns": [
#             r"i/flow/login",    # si toujours sur la page de login
#             r"account/access",
#         ],
#         # Twitter peut demander un email de vérif supplémentaire entre username et password
#         "middle_verification": True,
#     },
#     "x.com": {
#         "login_url": "https://x.com/i/flow/login",
#         "username": ["input[autocomplete='username']"],
#         "password": ["input[name='password']"],
#         "submit_selectors": [
#             "[data-testid='LoginForm_Login_Button']",
#             "button[type='submit']",
#         ],
#         "username_next_selectors": [
#             "[data-testid='LoginForm_Login_Button']",
#             "div[role='button']:has-text('Next')",
#         ],
#         "success": ["[data-testid='primaryColumn']", "[aria-label='Home timeline']"],
#         "pre_wait_ms": 2000,
#         "multi_step": True,
#         "post_popups": [],
#         "challenge_url_patterns": [],
#         "middle_verification": True,
#     },

#     # ── LinkedIn ──────────────────────────────────────────────────────────────
#     "linkedin.com": {
#         "login_url": "https://www.linkedin.com/login",
#         "username": ["input[name='session_key']", "input[id='username']"],
#         "password": ["input[name='session_password']", "input[id='password']"],
#         "submit_selectors": [
#             "button[type='submit']",
#             "button:has-text('Sign in')",
#             "button:has-text('Se connecter')",
#             ".btn__primary--large",
#         ],
#         "success": [".feed-identity-module", "a[href='/feed/']", ".global-nav__me"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#         "post_popups": [],
#         "challenge_url_patterns": [r"/checkpoint/challenge/", r"/checkpoint/lg/"],
#     },

#     # ── Google ────────────────────────────────────────────────────────────────
#     "google.com": {
#         "login_url": "https://accounts.google.com/signin",
#         "username": ["input[type='email']"],
#         "password": ["input[type='password']", "input[name='password']"],
#         "submit_selectors": ["#passwordNext", "button[type='submit']"],
#         "username_next_selectors": ["#identifierNext"],
#         "success": ["[data-ogsr-up]", "a[aria-label*='Google Account']", "#gbwa"],
#         "pre_wait_ms": 2000,
#         "multi_step": True,
#         "post_popups": [],
#         "challenge_url_patterns": [r"signin/challenge", r"accounts/v3/signin"],
#     },

#     # ── Pinterest ─────────────────────────────────────────────────────────────
#     "pinterest.com": {
#         "login_url": "https://www.pinterest.com/login/",
#         "username": ["input[name='id']", "input[type='email']"],
#         "password": ["input[name='password']", "input[type='password']"],
#         "submit_selectors": [
#             "button[type='submit']",
#             "button:has-text('Log in')",
#             "button:has-text('Continue')",
#             "div[data-test-id='loginButton']",
#         ],
#         "success": [
#             "[data-test-id='header-avatar']",
#             "[data-test-id='homefeed-feed']",
#             "[data-test-id='user-avatar']",
#         ],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#         "post_popups": [],
#         "challenge_url_patterns": [],
#     },

#     # ── Reddit ────────────────────────────────────────────────────────────────
#     "reddit.com": {
#         "login_url": "https://www.reddit.com/login/",
#         "username": ["#loginUsername", "input[name='username']"],
#         "password": ["#loginPassword", "input[name='password']"],
#         "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
#         "success": ["a[href*='/user/']", "#USER_AGENT_THEME_ROOT"],
#         "pre_wait_ms": 1500,
#         "multi_step": False,
#         "post_popups": [],
#         "challenge_url_patterns": [],
#     },

#     # ── GitHub ────────────────────────────────────────────────────────────────
#     "github.com": {
#         "login_url": "https://github.com/login",
#         "username": ["#login_field"],
#         "password": ["#password"],
#         "submit_selectors": ["input[type='submit'][name='commit']", "button[type='submit']"],
#         "success": [".Header-link--avatar", "[aria-label='View profile and more']"],
#         "pre_wait_ms": 1000,
#         "multi_step": False,
#         "post_popups": [],
#         "challenge_url_patterns": [r"/sessions/two-factor"],
#     },

#     # ── Discord ───────────────────────────────────────────────────────────────
#     "discord.com": {
#         "login_url": "https://discord.com/login",
#         "username": ["input[name='email']"],
#         "password": ["input[name='password']"],
#         "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
#         "success": ["nav[aria-label='Servers sidebar']", "[class*='guilds']"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#         "post_popups": [],
#         "challenge_url_patterns": [],
#     },

#     # ── TikTok ────────────────────────────────────────────────────────────────
#     "tiktok.com": {
#         "login_url": "https://www.tiktok.com/login/phone-or-email/email",
#         "username": ["input[name='username']", "input[placeholder*='email' i]"],
#         "password": ["input[type='password']"],
#         "submit_selectors": ["button[type='submit']", "button:has-text('Log in')"],
#         "success": ["[data-e2e='profile-icon']", "[class*='DivUserAvatar']"],
#         "pre_wait_ms": 3000,
#         "multi_step": False,
#         "post_popups": [],
#         "challenge_url_patterns": [],
#     },

#     # ── Snapchat ──────────────────────────────────────────────────────────────
#     "snapchat.com": {
#         "login_url": "https://accounts.snapchat.com/accounts/login",
#         "username": ["input[name='username']"],
#         "password": ["input[name='password']"],
#         "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
#         "success": ["[data-testid='web-header']"],
#         "pre_wait_ms": 2000,
#         "multi_step": False,
#         "post_popups": [],
#         "challenge_url_patterns": [],
#     },
# }


# # ─── Helpers URL ──────────────────────────────────────────────────────────────

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


# # ─── Browser factory ──────────────────────────────────────────────────────────

# async def _make_context(p, domain: str) -> Tuple[Any, BrowserContext]:
#     """
#     Crée un contexte Playwright avec profil persistant par domaine.
#     Le profil persistant conserve les cookies entre les runs → moins de CAPTCHA.
#     """
#     ua = random.choice(_USER_AGENTS)
#     profile_path = PROFILE_DIR / domain
#     profile_path.mkdir(parents=True, exist_ok=True)

#     context = await p.chromium.launch_persistent_context(
#         user_data_dir=str(profile_path),
#         headless=HEADLESS,
#         user_agent=ua,
#         viewport={"width": 1366, "height": 768},
#         locale="en-US",
#         timezone_id="America/New_York",
#         java_script_enabled=True,
#         is_mobile=False,
#         has_touch=False,
#         color_scheme="light",
#         device_scale_factor=1,
#         args=[
#             "--no-sandbox",
#             "--disable-blink-features=AutomationControlled",
#             "--disable-infobars",
#             "--disable-dev-shm-usage",
#             "--no-first-run",
#             "--no-default-browser-check",
#             "--disable-notifications",
#             "--window-size=1366,768",
#         ],
#         # Supprimer le flag --enable-automation que Playwright ajoute par défaut
#         ignore_default_args=["--enable-automation"],
#     )

#     # Injecter le stealth JS dans toutes les pages
#     await context.add_init_script(_STEALTH_JS)

#     # Si playwright-stealth est installé, l'appliquer aussi
#     if _HAS_STEALTH:
#         context.on("page", lambda page: asyncio.ensure_future(stealth_async(page)))

#     return context


# # ─── Storage ──────────────────────────────────────────────────────────────────

# async def _dump_storage(page) -> Tuple[Optional[str], Optional[str]]:
#     local_storage = await page.evaluate("""() => {
#         try {
#             const o = {};
#             for (let i = 0; i < window.localStorage.length; i++) {
#                 const k = window.localStorage.key(i);
#                 o[k] = window.localStorage.getItem(k);
#             }
#             const s = JSON.stringify(o);
#             return s === '{}' ? null : s;
#         } catch(e) { return null; }
#     }""")
#     session_storage = await page.evaluate("""() => {
#         try {
#             const o = {};
#             for (let i = 0; i < window.sessionStorage.length; i++) {
#                 const k = window.sessionStorage.key(i);
#                 o[k] = window.sessionStorage.getItem(k);
#             }
#             const s = JSON.stringify(o);
#             return s === '{}' ? null : s;
#         } catch(e) { return null; }
#     }""")
#     return local_storage, session_storage


# # ─── Field helpers ────────────────────────────────────────────────────────────

# def _norm(s: str) -> str:
#     return (s or "").strip().lower()


# async def _attr(locator, name: str) -> str:
#     try:
#         v = await locator.get_attribute(name)
#         return v or ""
#     except Exception:
#         return ""


# async def _fill_field(page, selectors: list[str], value: str, timeout_ms: int = 15_000) -> Optional[Any]:
#     """
#     Essaie chaque sélecteur dans l'ordre, remplit le premier visible.
#     Retourne le locator utilisé, ou None si aucun trouvé.
#     """
#     for sel in selectors:
#         try:
#             loc = page.locator(sel).first
#             await loc.wait_for(state="visible", timeout=timeout_ms)
#             await loc.scroll_into_view_if_needed()
#             await loc.click(timeout=3000)
#             await loc.fill("")
#             await page.wait_for_timeout(random.randint(80, 200))
#             await loc.type(value, delay=random.randint(35, 75))
#             return loc
#         except Exception:
#             continue
#     return None


# async def _click_first_visible(page, selectors: list[str], timeout_ms: int = 8_000) -> bool:
#     for sel in selectors:
#         try:
#             loc = page.locator(sel).first
#             await loc.wait_for(state="visible", timeout=timeout_ms)
#             await loc.click()
#             return True
#         except Exception:
#             continue
#     return False


# async def _dismiss_popups(page, selectors: list[str]) -> None:
#     """Tente de fermer les popups post-login (Save login, notifications...)."""
#     for sel in selectors:
#         try:
#             loc = page.locator(sel).first
#             if await loc.is_visible(timeout=3000):
#                 await loc.click()
#                 await page.wait_for_timeout(500)
#         except Exception:
#             continue


# # ─── Détection et gestion du 2FA ─────────────────────────────────────────────

# # Patterns d'URL indiquant une page de vérification
# _CHALLENGE_URL_PATTERNS = [
#     r"/checkpoint/challenge/",  # LinkedIn
#     r"/challenge/",             # générique
#     r"/login/challenge",
#     r"/2fa/",
#     r"/auth/verify",
#     r"verification",
#     r"two.factor",
#     r"two_step",
#     r"security.check",
#     r"identity.confirm",
#     r"confirm.identity",
#     r"unusual.activity",
#     r"login/device-based",      # Facebook
#     r"login/identify",          # Facebook
#     r"checkpoint",              # Facebook / LinkedIn
#     r"signin/challenge",        # Google
#     r"sessions/two-factor",     # GitHub
# ]

# # Sélecteurs de champs de code 2FA/OTP
# _CODE_INPUT_SELECTORS = [
#     "input[name='challengeCode']",
#     "input[name='2faCode']",
#     "input[name='verificationCode']",
#     "input[name='code']",
#     "input[name='otp']",
#     "input[name='token']",
#     "input[name='approvals_code']",   # Facebook
#     "input[id='approvals_code']",     # Facebook
#     "input[autocomplete='one-time-code']",
#     "input[placeholder*='code' i]",
#     "input[placeholder*='verification' i]",
#     "input[placeholder*='OTP' i]",
#     "input[placeholder*='6-digit' i]",
#     "input[aria-label*='code' i]",
#     # LinkedIn PIN
#     "input[name='pin']",
#     # Google
#     "input[id='totpPin']",
#     "input[id='idvPin']",
# ]


# async def _is_challenge_page(page, extra_patterns: list[str] = None) -> bool:
#     """Détecte si on est sur une page de vérification/challenge."""
#     url = page.url
#     patterns = _CHALLENGE_URL_PATTERNS + (extra_patterns or [])
#     if any(re.search(p, url, re.I) for p in patterns):
#         return True
#     # Vérifier aussi si un champ de code est visible
#     for sel in _CODE_INPUT_SELECTORS:
#         try:
#             loc = page.locator(sel).first
#             if await loc.count() > 0 and await loc.is_visible(timeout=1500):
#                 return True
#         except Exception:
#             continue
#     return False


# async def _get_code_input(page):
#     """Retourne le premier champ de code 2FA visible."""
#     for sel in _CODE_INPUT_SELECTORS:
#         try:
#             loc = page.locator(sel).first
#             if await loc.count() > 0 and await loc.is_visible(timeout=1500):
#                 return loc
#         except Exception:
#             continue
#     return None


# async def _prompt_2fa_interactive(challenge_url: str, page) -> str:
#     """
#     Prompt interactif dans le terminal pour entrer le code 2FA.
#     En mode headless : l'utilisateur doit regarder les logs du serveur.
#     En mode headful  : l'utilisateur voit la page ET le terminal.
#     """
#     print("\n" + "=" * 65)
#     print("🔐  VERIFICATION CODE REQUIRED")
#     print(f"    Page : {challenge_url}")
#     print("    Entrez le code reçu par SMS / email / authenticator :")
#     print("=" * 65)
#     code = input("    Code → ").strip()
#     print("=" * 65 + "\n")
#     return code


# async def _handle_2fa_if_needed(
#     page,
#     extra_url_patterns: list[str] = None,
#     code_callback: Optional[Callable[[str, Any], Awaitable[str]]] = None,
#     timeout_seconds: int = 120,
# ) -> bool:
#     """
#     Vérifie si une page de challenge 2FA est présente.
#     Si oui, demande le code (via callback ou terminal) et le soumet.

#     CORRECTION du bug original :
#     - code_input était créé AVANT la détection → était None quand is_challenge=False
#     - Maintenant : on détecte d'abord, puis on cherche le champ

#     Retourne True si 2FA résolu, False si pas de 2FA.
#     """
#     is_challenge = await _is_challenge_page(page, extra_url_patterns)
#     if not is_challenge:
#         return False

#     current_url = page.url
#     print(f"[2FA] Challenge détecté : {current_url}")

#     # Chercher le champ de code maintenant qu'on sait qu'on est sur une page challenge
#     code_input = await _get_code_input(page)
#     if not code_input:
#         print("[2FA] Page de challenge mais aucun champ de code trouvé — attente 5s...")
#         await page.wait_for_timeout(5000)
#         code_input = await _get_code_input(page)

#     if not code_input:
#         print("[2FA] Impossible de trouver le champ de code. Tentative de continuer...")
#         return False

#     callback = code_callback or _prompt_2fa_interactive
#     code = await callback(current_url, page)

#     if not code:
#         raise Exception("Code 2FA vide fourni")

#     try:
#         await code_input.wait_for(state="visible", timeout=5000)
#         await code_input.fill("")
#         await code_input.type(code, delay=random.randint(50, 120))
#         await page.wait_for_timeout(500)

#         # Chercher le bouton de validation
#         submit_btns = page.locator(
#             "button[type='submit'], "
#             "button:has-text('Verify'), button:has-text('Submit'), "
#             "button:has-text('Continue'), button:has-text('Confirm'), "
#             "button:has-text('Next'), button[id*='submit' i], "
#             "input[type='submit']"
#         )
#         if await submit_btns.count() > 0:
#             await submit_btns.first.click()
#         else:
#             await code_input.press("Enter")

#         await page.wait_for_load_state("networkidle", timeout=30_000)
#         await page.wait_for_timeout(2000)
#         print("[2FA] ✓ Code soumis avec succès")
#         return True

#     except Exception as e:
#         print(f"[2FA] Erreur lors de la soumission : {e}")
#         raise


# # ─── Stratégie par site ───────────────────────────────────────────────────────

# async def _run_site_strategy(
#     page,
#     strat: dict,
#     username: str,
#     password: str,
#     code_callback=None,
# ) -> None:
#     """Exécute la stratégie de login pour un site connu."""
#     pre = strat.get("pre_wait_ms", 1500)
#     await page.wait_for_timeout(pre)

#     submit_sels = strat.get("submit_selectors", ["button[type='submit']"])
#     username_sels = strat["username"] if isinstance(strat["username"], list) else [strat["username"]]
#     password_sels = strat["password"] if isinstance(strat["password"], list) else [strat["password"]]

#     if strat.get("multi_step"):
#         # Étape 1 : username
#         user_loc = await _fill_field(page, username_sels, username)
#         if not user_loc:
#             raise Exception(f"Champ username introuvable. Sélecteurs : {username_sels}")

#         await page.wait_for_timeout(random.randint(400, 800))

#         # Cliquer "Next"
#         next_sels = strat.get("username_next_selectors", ["button[type='submit']"])
#         clicked = await _click_first_visible(page, next_sels)
#         if not clicked:
#             await page.keyboard.press("Enter")

#         await page.wait_for_timeout(random.randint(1500, 2500))

#         # Twitter / X : parfois demande email de vérif entre les deux étapes
#         if strat.get("middle_verification"):
#             middle_input = page.locator(
#                 "input[data-testid='ocfEnterTextTextInput'], "
#                 "input[placeholder*='phone' i], input[placeholder*='email' i]"
#             ).first
#             try:
#                 if await middle_input.count() > 0 and await middle_input.is_visible(timeout=3000):
#                     print("[relay][twitter] Vérification intermédiaire détectée (email/phone)")
#                     cb = code_callback or _prompt_2fa_interactive
#                     value = await cb(page.url + " [email/phone verification]", page)
#                     await middle_input.type(value, delay=40)
#                     await _click_first_visible(page, next_sels, timeout_ms=4000)
#                     await page.wait_for_timeout(1500)
#             except Exception:
#                 pass

#         # Étape 2 : password
#         pass_loc = await _fill_field(page, password_sels, password)
#         if not pass_loc:
#             raise Exception(f"Champ password introuvable. Sélecteurs : {password_sels}")

#         await page.wait_for_timeout(random.randint(300, 600))

#         clicked = await _click_first_visible(page, submit_sels)
#         if not clicked:
#             await page.keyboard.press("Enter")

#     else:
#         # Single page : username + password puis submit
#         user_loc = await _fill_field(page, username_sels, username)
#         if not user_loc:
#             raise Exception(f"Champ username introuvable. Sélecteurs : {username_sels}")

#         await page.wait_for_timeout(random.randint(200, 500))

#         pass_loc = await _fill_field(page, password_sels, password)
#         if not pass_loc:
#             raise Exception(f"Champ password introuvable. Sélecteurs : {password_sels}")

#         await page.wait_for_timeout(random.randint(300, 600))

#         clicked = await _click_first_visible(page, submit_sels)
#         if not clicked:
#             await page.keyboard.press("Enter")


# # ─── Détection succès / échec ─────────────────────────────────────────────────

# _LOGIN_PAGE_KEYWORDS = [
#     "/login", "/signin", "/sign-in", "/accounts/login",
#     "/flow/login", "accounts.google", "/auth/",
# ]

# _ERROR_MESSAGES = [
#     "incorrect password", "wrong password", "invalid password",
#     "mot de passe incorrect", "invalid credentials",
#     "the password you entered", "didn't match",
#     "your account has been",
# ]


# async def _check_success(page, strat: Optional[dict]) -> bool:
#     """True si le login a réussi."""
#     current = page.url.lower()

#     # Si on est sur une page d'erreur connue
#     page_text = ""
#     try:
#         page_text = (await page.inner_text("body") or "").lower()
#     except Exception:
#         pass
#     if any(msg in page_text for msg in _ERROR_MESSAGES):
#         return False

#     # Si l'URL n'est plus une page de login → succès probable
#     if not any(kw in current for kw in _LOGIN_PAGE_KEYWORDS):
#         return True

#     # Vérifier les sélecteurs de succès
#     if strat:
#         for sel in strat.get("success", []):
#             try:
#                 loc = page.locator(sel).first
#                 if await loc.count() > 0 and await loc.is_visible(timeout=2000):
#                     return True
#             except Exception:
#                 pass

#     return False


# # ─── Heuristique générique ────────────────────────────────────────────────────

# def _score_username(attrs: dict) -> int:
#     hay = " ".join(_norm(attrs.get(k, "")) for k in ["name", "id", "placeholder", "autocomplete", "type", "aria"])
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
#     hay = " ".join(_norm(attrs.get(k, "")) for k in ["name", "id", "placeholder", "autocomplete", "aria"])
#     if "password" in hay or "pass" in hay: score += 5
#     if attrs.get("autocomplete", "") in ["current-password", "new-password"]: score += 3
#     return score


# def _score_submit(attrs: dict, text: str) -> int:
#     hay = " ".join([_norm(attrs.get(k, "")) for k in ["type", "name", "id", "aria"]] + [_norm(text)])
#     score = 0
#     if any(w in hay for w in ["sign in", "log in", "login", "connexion", "se connecter", "submit"]): score += 6
#     if any(w in hay for w in ["continue", "next"]): score += 2
#     if "submit" in _norm(attrs.get("type", "")): score += 2
#     if any(w in hay for w in ["cancel", "register", "sign up", "create", "forgot"]): score -= 4
#     return score


# async def _generic_login(page, username: str, password: str) -> None:
#     """Heuristique pour sites inconnus."""
#     await page.wait_for_timeout(1500)

#     pw_inputs = page.locator("input[type='password']")
#     pw_count = await pw_inputs.count()

#     # Pas de champ password visible → flow en 2 étapes (email d'abord)
#     if pw_count == 0:
#         email_sels = [
#             "input[type='email']", "input[name='email']", "input[id*='email' i]",
#             "input[name='username']", "input[id*='user' i]",
#             "input[autocomplete='email']", "input[autocomplete='username']",
#         ]
#         filled = False
#         for sel in email_sels:
#             try:
#                 loc = page.locator(sel).first
#                 await loc.wait_for(state="visible", timeout=3000)
#                 await loc.fill(username)
#                 filled = True
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
#             raise Exception("Impossible de trouver le champ email/username sur la page")

#         pw_count = await pw_inputs.count()
#         if pw_count == 0:
#             raise Exception("Le champ password n'est pas apparu après soumission du username")

#     # Choisir le meilleur champ password par score
#     password_locator = None
#     best_pw_score = -999
#     for i in range(min(pw_count, 10)):
#         loc = pw_inputs.nth(i)
#         try:
#             await loc.wait_for(state="visible", timeout=2000)
#             attrs = {k: await _attr(loc, k) for k in ["type", "name", "id", "placeholder", "autocomplete"]}
#             attrs["aria"] = await _attr(loc, "aria-label")
#             sc = _score_password(attrs)
#             if sc > best_pw_score:
#                 best_pw_score = sc
#                 password_locator = loc
#         except Exception:
#             continue

#     if not password_locator:
#         raise Exception("Aucun champ password visible")

#     # Remplir username si encore sur la page
#     user_inputs = page.locator("input:not([type='hidden']):not([type='password'])")
#     user_count = await user_inputs.count()
#     best_user_score = -999
#     username_locator = None
#     for i in range(min(user_count, 30)):
#         loc = user_inputs.nth(i)
#         try:
#             await loc.wait_for(state="visible", timeout=1200)
#             attrs = {k: await _attr(loc, k) for k in ["type", "name", "id", "placeholder", "autocomplete"]}
#             attrs["aria"] = await _attr(loc, "aria-label")
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

#     # Remplir password
#     await password_locator.wait_for(state="visible", timeout=8000)
#     await password_locator.click()
#     await password_locator.fill("")
#     await page.wait_for_timeout(random.randint(100, 250))
#     await password_locator.type(password, delay=random.randint(35, 70))
#     await page.wait_for_timeout(random.randint(200, 400))

#     # Choisir le bouton submit par score
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
#             attrs = {k: await _attr(b, k) for k in ["type", "name", "id"]}
#             attrs["aria"] = await _attr(b, "aria-label")
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
#         await password_locator.press("Enter")


# # ─── API publique ─────────────────────────────────────────────────────────────

# async def login_and_get_cookies(
#     service_url: str,
#     username: str,
#     password: str,
#     profile: Optional[Dict[str, Any]] = None,
#     code_callback: Optional[Callable[[str, Any], Awaitable[str]]] = None,
# ) -> Dict[str, Any]:
#     """
#     Universal relay login.

#     Paramètres :
#         service_url   : URL du site cible
#         username      : identifiant / email
#         password      : mot de passe
#         profile       : dict de configuration optionnel (sélecteurs custom, timeouts…)
#         code_callback : async (url, page) → str  pour récupérer le code 2FA automatiquement
#                         Si None → prompt interactif dans le terminal

#     Retourne :
#         {
#           cookies, localStorage, sessionStorage,
#           current_url, title,
#           login_detected, domain, origin,
#           debug: { before_url, after_url, used_method, ... }
#         }

#     Priorité des stratégies :
#       1. Sélecteurs explicites dans `profile` (ex: recolyse.com custom)
#       2. Stratégie par domaine (_SITE_STRATEGIES)
#       3. Heuristique générique
#     """
#     profile = profile or {}

#     def _as_list(x):
#         if not x:
#             return []
#         return [x] if isinstance(x, str) else (x if isinstance(x, list) else [])

#     # Sélecteurs depuis le profil custom
#     username_selectors  = _as_list(profile.get("username_selector"))
#     password_selectors  = _as_list(profile.get("password_selector"))
#     submit_selectors    = _as_list(profile.get("submit_selector"))
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
#     context = None

#     try:
#         service_url = (service_url or "").strip()
#         if not service_url:
#             raise Exception("service_url est vide")
#         if not service_url.startswith(("http://", "https://")):
#             service_url = "https://" + service_url

#         domain = _domain_from_url(service_url)
#         origin = _origin_from_url(service_url)
#         strat  = _SITE_STRATEGIES.get(domain)

#         login_url = (strat["login_url"] if strat and not has_profile_selectors else service_url)

#         async with async_playwright() as p:
#             context = await _make_context(p, domain)
#             page: Page = await context.new_page()
#             page.set_default_timeout(DEFAULT_TIMEOUT_MS)

#             await page.goto(login_url, wait_until=goto_wait_until)
#             try:
#                 await page.wait_for_load_state("domcontentloaded", timeout=10_000)
#             except Exception:
#                 pass

#             before_url  = page.url
#             login_detected = False
#             used_method = "unknown"

#             # ── Branche 1 : stratégie par domaine connu ───────────────────
#             if strat and not has_profile_selectors:
#                 await _run_site_strategy(page, strat, username, password, code_callback)
#                 used["username"] = used["password"] = used["submit"] = "strategy"
#                 used_method = f"strategy:{domain}"

#                 # 2FA après submit
#                 await _handle_2fa_if_needed(
#                     page,
#                     extra_url_patterns=strat.get("challenge_url_patterns", []),
#                     code_callback=code_callback,
#                 )

#                 # Popups post-login (Save info, notifications…)
#                 await _dismiss_popups(page, strat.get("post_popups", []))

#             # ── Branche 2 : sélecteurs explicites (profile custom) ────────
#             elif has_profile_selectors:
#                 await page.wait_for_timeout(pre_fill_wait_ms)

#                 if open_login_selector:
#                     try:
#                         await page.locator(open_login_selector).first.click()
#                         await page.wait_for_timeout(between_actions_wait_ms)
#                     except Exception:
#                         pass

#                 user_loc = await _fill_field(page, username_selectors, username, 10_000)
#                 if not user_loc:
#                     raise Exception("Champ username introuvable. Sélecteurs : " + str(username_selectors))
#                 used["username"] = username_selectors[0]

#                 await page.wait_for_timeout(between_actions_wait_ms)

#                 pass_loc = await _fill_field(page, password_selectors, password, 10_000)
#                 if not pass_loc:
#                     raise Exception("Champ password introuvable. Sélecteurs : " + str(password_selectors))
#                 used["password"] = password_selectors[0]

#                 final_submit = submit_selectors or ["button[type='submit']", "input[type='submit']"]
#                 await page.wait_for_timeout(between_actions_wait_ms)
#                 clicked = await _click_first_visible(page, final_submit, timeout_ms=8_000)
#                 if not clicked:
#                     await pass_loc.press("Enter")
#                     used["submit"] = "press:Enter"
#                 else:
#                     used["submit"] = final_submit[0]

#                 used_method = "profile_selectors"

#                 await _handle_2fa_if_needed(page, code_callback=code_callback)

#             # ── Branche 3 : heuristique générique ────────────────────────
#             else:
#                 await _generic_login(page, username, password)
#                 used["username"] = used["password"] = used["submit"] = "generic"
#                 used_method = "generic_heuristic"

#                 await _handle_2fa_if_needed(page, code_callback=code_callback)

#             # ── Post-submit ───────────────────────────────────────────────
#             await page.wait_for_timeout(after_submit_wait_ms)
#             try:
#                 await page.wait_for_load_state("networkidle", timeout=10_000)
#             except Exception:
#                 pass

#             # ── Détection du succès ───────────────────────────────────────
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
#                         await page.locator(post_login_selector).first.wait_for(
#                             state="visible", timeout=post_login_timeout_ms
#                         )
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
#                 if not login_detected:
#                     login_detected = await _check_success(page, strat)

#             # ── Attente stabilisation cookies ─────────────────────────────
#             if stay_connected_ms > 0:
#                 await page.wait_for_timeout(stay_connected_ms)

#             # ── Polling cookies ───────────────────────────────────────────
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
#                 ok_name  = (not cookie_wait_name) or any(
#                     c.get("name") == cookie_wait_name for c in domain_cookies
#                 )
#                 if ok_count and ok_name:
#                     break
#                 await page.wait_for_timeout(poll_ms)
#                 elapsed += poll_ms

#             await page.wait_for_timeout(300)
#             local_storage, session_storage = await _dump_storage(page)

#             return {
#                 "cookies":        cookies,
#                 "localStorage":   local_storage,
#                 "sessionStorage": session_storage,
#                 "current_url":    page.url,
#                 "title":          await page.title(),
#                 "used_selectors": used,
#                 "login_detected": login_detected,
#                 "domain":         domain,
#                 "origin":         origin,
#                 "debug": {
#                     "before_url":            before_url,
#                     "after_url":             page.url,
#                     "used_method":           used_method,
#                     "cookie_wait_elapsed_ms": elapsed,
#                     "domain_cookie_count":   len([
#                         c for c in cookies
#                         if (c.get("domain") or "").lstrip(".").endswith(domain)
#                     ]),
#                 },
#             }

#     except Exception as e:
#         raise Exception(f"Playwright login failed: {str(e)}") from e

#     finally:
#         if context is not None:
#             try:
#                 await context.close()
#             except Exception:
#                 pass


# # ─── Test direct ──────────────────────────────────────────────────────────────

# async def _main():
#     result = await login_and_get_cookies(
#         service_url="https://www.linkedin.com/login",
#         username="your@email.com",
#         password="yourpassword",
#     )
#     print("✓ Login réussi")
#     print(f"  Cookies       : {len(result['cookies'])}")
#     print(f"  localStorage  : {'présent' if result['localStorage'] else 'vide'}")
#     print(f"  URL finale    : {result['current_url']}")
#     print(f"  Méthode       : {result['debug']['used_method']}")
#     print(f"  Login détecté : {result['login_detected']}")


# if __name__ == "__main__":
#     asyncio.run(_main())





































from __future__ import annotations

import os
import random
import urllib.parse

import asyncio
import re
from typing import Optional, Callable, Awaitable


from typing import Any, Dict, Optional, Tuple

from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

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
    window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
    const gp = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return gp.call(this, p);
    };
    const orig = window.navigator.permissions?.query?.bind(navigator.permissions);
    if (orig) {
        navigator.permissions.query = (p) =>
            p.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : orig(p);
    }
}
"""

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# ─── Per-site strategies ──────────────────────────────────────────────────────
# Keys must match what _domain_from_url() returns (no "www.")
_SITE_STRATEGIES: dict[str, dict] = {
    "pinterest.com": {
        "login_url": "https://www.pinterest.com/login/",
        "username": "input[name='id']",
        "password": "input[name='password']",
        # Pinterest uses a <button> with no type — match by text
        "submit_selectors": [
            "button[type='submit']",
            "button:has-text('Log in')",
            "button:has-text('Continue')",
            "div[data-test-id='registerFormSubmitButton']",
            "div[data-test-id='loginButton']",
            "button",   # last resort: first visible button after password
        ],
        "success": ["[data-test-id='header-avatar']", "[data-test-id='homefeed-feed']", "div[data-test-id='user-avatar']"],
        "pre_wait_ms": 2000,
        "multi_step": False,
    },
    "linkedin.com": {
        "login_url": "https://www.linkedin.com/login",
        "username": "input[name='session_key']",          # champ email/téléphone
        "password": "input[name='session_password']",     # champ mot de passe
        "submit_selectors": [
            "button[type='submit']",
            "button:has-text('Sign in')",
            "button:has-text('Se connecter')",
            ".btn__primary--large",
        ],
        "success": [".feed-identity-module", "a[href='/feed/']", ".global-nav__me"],
        "pre_wait_ms": 2000,
        "multi_step": False,
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
        "success": ["svg[aria-label='Home']", "a[href='/']"],
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
        "success": ["[data-ogsr-up]", "a[aria-label*='Google Account']"],
        "pre_wait_ms": 2000,
        "multi_step": True,
    },
    "reddit.com": {
        "login_url": "https://www.reddit.com/login/",
        "username": "#loginUsername",
        "password": "#loginPassword",
        "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
        "success": ["a[href*='/user/']", "#USER_AGENT_THEME_ROOT"],
        "pre_wait_ms": 1500,
        "multi_step": False,
    },
    "github.com": {
        "login_url": "https://github.com/login",
        "username": "#login_field",
        "password": "#password",
        "submit_selectors": ["[type='submit'][name='commit']", "button[type='submit']"],
        "success": [".Header-link--avatar", "[aria-label='Homepage']"],
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
        "submit_selectors": ["button[type='submit']", "button:has-text('Log in')"],
        "success": ["[data-e2e='profile-icon']"],
        "pre_wait_ms": 3000,
        "multi_step": False,
    },
    "snapchat.com": {
        "login_url": "https://accounts.snapchat.com/accounts/login",
        "username": "input[name='username']",
        "password": "input[name='password']",
        "submit_selectors": ["button[type='submit']", "button:has-text('Log In')"],
        "success": ["[data-testid='web-header']"],
        "pre_wait_ms": 2000,
        "multi_step": False,
    },
}


def _domain_from_url(url: str) -> str:
    try:
        d = urllib.parse.urlparse(url).netloc
        return d.split(":")[0].lower().strip().lstrip("www.")
    except Exception:
        return ""


def _origin_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or parsed.path
    if "/" in netloc:
        netloc = netloc.split("/")[0]
    return f"{scheme}://{netloc}".rstrip("/")


async def _dump_storage(page) -> Tuple[str | None, str | None]:
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
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1366,768",
        ],
    )

    # browser = await p.chromium.launch(
    #             headless=False,
    #             args=["--disable-dev-shm-usage", "--no-sandbox"],
    #         )



    context = await browser.new_context(
        user_agent=ua,
        viewport={"width": 1366, "height": 768},
        locale="en-US",
        timezone_id="America/New_York",
        java_script_enabled=True,
        is_mobile=False,
        has_touch=False,
        color_scheme="light",
        device_scale_factor=1,
    )
    await context.add_init_script(_STEALTH_JS)
    return browser, context


# ─── Field helpers ────────────────────────────────────────────────────────────

async def _fill(page, selector: str, value: str, timeout_ms: int = 15_000):
    """Wait for a field, clear it, type value with human-like delay."""
    loc = page.locator(selector).first
    await loc.wait_for(state="visible", timeout=timeout_ms)
    await loc.scroll_into_view_if_needed()
    await loc.click()
    await loc.fill("")
    await page.wait_for_timeout(random.randint(100, 250))
    await loc.type(value, delay=random.randint(30, 60))


async def _click_first_visible(page, selectors: list[str], timeout_ms: int = 8_000) -> bool:
    """Try selectors in order, click the first visible one. Returns True if clicked."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout_ms)
            await loc.click()
            return True
        except Exception:
            continue
    return False


# ─── Per-site login ───────────────────────────────────────────────────────────

async def _run_site_strategy(page, strat: dict, username: str, password: str):
    """Execute a known-site login strategy (single or multi-step)."""
    pre = strat.get("pre_wait_ms", 1500)
    await page.wait_for_timeout(pre)

    submit_sels = strat.get("submit_selectors", ["button[type='submit']"])

    if strat.get("multi_step"):
        # Step 1: fill username
        await _fill(page, strat["username"], username)
        await page.wait_for_timeout(random.randint(400, 800))
        # Click "Next" button
        next_sels = strat.get("username_next_selectors", ["button[type='submit']"])
        clicked = await _click_first_visible(page, next_sels)
        if not clicked:
            await page.keyboard.press("Enter")
        # Wait for password field
        await page.wait_for_timeout(random.randint(1500, 2500))
        # Step 2: fill password
        await _fill(page, strat["password"], password)
        await page.wait_for_timeout(random.randint(300, 600))
        # Submit
        clicked = await _click_first_visible(page, submit_sels)
        if not clicked:
            await page.keyboard.press("Enter")
    else:
        # Single page: fill username + password then submit
        await _fill(page, strat["username"], username)
        await page.wait_for_timeout(random.randint(200, 500))
        await _fill(page, strat["password"], password)
        await page.wait_for_timeout(random.randint(300, 600))
        clicked = await _click_first_visible(page, submit_sels)
        if not clicked:
            await page.keyboard.press("Enter")


async def _check_success(page, strat: dict | None) -> bool:
    """True if we left the login page or found a post-login indicator."""
    current = page.url
    login_kw = ["/login", "/signin", "/sign-in", "/accounts/login", "/flow/login", "accounts.google"]
    if not any(kw in current.lower() for kw in login_kw):
        return True
    if strat:
        for sel in strat.get("success", []):
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible(timeout=3000):
                    return True
            except Exception:
                pass
    return False


# ─── Generic heuristic (fallback for unknown sites) ──────────────────────────

def _score_username(attrs: dict) -> int:
    hay = " ".join([_norm(attrs.get(k, "")) for k in ["name", "id", "placeholder", "autocomplete", "type", "aria"]])
    score = 0
    if "email" in hay: score += 6
    if "user" in hay or "username" in hay or "login" in hay: score += 5
    if "phone" in hay or "tel" in hay: score -= 2
    if attrs.get("type", "") in ["email", "text"]: score += 1
    if attrs.get("autocomplete", "") in ["email", "username"]: score += 3
    return score


def _score_password(attrs: dict) -> int:
    score = 0
    if attrs.get("type", "") == "password": score += 10
    hay = " ".join([_norm(attrs.get(k, "")) for k in ["name", "id", "placeholder", "autocomplete", "aria"]])
    if "password" in hay or "pass" in hay: score += 5
    if attrs.get("autocomplete", "") in ["current-password", "new-password"]: score += 3
    return score


def _score_submit(attrs: dict, text: str) -> int:
    t = _norm(text)
    hay = " ".join([_norm(attrs.get(k, "")), t])
    score = 0
    if "sign in" in hay or "login" in hay or "log in" in hay or "connexion" in hay or "se connecter" in hay: score += 6
    if "continue" in hay or "next" in hay: score += 2
    if "submit" in _norm(attrs.get("type", "")): score += 2
    if "cancel" in hay or "register" in hay or "sign up" in hay or "create" in hay: score -= 4
    return score


async def _generic_login(page, username: str, password: str) -> dict:
    """
    Heuristic login for unknown sites.
    Returns debug dict. Raises on failure.
    """
    await page.wait_for_timeout(1500)

    # ── Find password field ──────────────────────────────────────────────────
    pw_inputs = page.locator("input[type='password']")
    pw_count = await pw_inputs.count()

    # If no password field visible yet, check for single-field (email-first) flow
    if pw_count == 0:
        # Try to fill email and press Enter/Next to reveal password field
        email_sels = [
            "input[type='email']", "input[name='email']", "input[id*='email']",
            "input[name='username']", "input[id*='user']", "input[autocomplete='email']",
            "input[autocomplete='username']",
        ]
        filled = False
        for sel in email_sels:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=3000)
                await loc.fill(username)
                filled = True
                # Try clicking a "Next" / "Continue" button
                next_clicked = await _click_first_visible(page, [
                    "button:has-text('Next')", "button:has-text('Continue')",
                    "button[type='submit']", "input[type='submit']",
                ], timeout_ms=3000)
                if not next_clicked:
                    await page.keyboard.press("Enter")
                await page.wait_for_timeout(2000)
                break
            except Exception:
                continue

        if not filled:
            raise Exception("Cannot find username/email field on page")

        # Re-check for password field
        pw_count = await pw_inputs.count()
        if pw_count == 0:
            raise Exception("Password field did not appear after username submission")

    # ── Score and pick best password field ───────────────────────────────────
    password_locator = None
    best_pw_score = -999
    for i in range(min(pw_count, 10)):
        loc = pw_inputs.nth(i)
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
            if sc > best_pw_score:
                best_pw_score = sc
                password_locator = loc
        except Exception:
            continue

    if not password_locator:
        raise Exception("No visible password field found")

    # ── Fill password ─────────────────────────────────────────────────────────
    # Only fill username again if it's on the same page (not already submitted above)
    user_inputs = page.locator("input:not([type='hidden']):not([type='password'])")
    user_count = await user_inputs.count()
    username_locator = None
    best_user_score = -999
    for i in range(min(user_count, 30)):
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
            if sc > best_user_score:
                best_user_score = sc
                username_locator = loc
        except Exception:
            continue

    if username_locator and best_user_score > 2:
        try:
            await username_locator.click()
            await username_locator.fill(username)
            await page.wait_for_timeout(random.randint(150, 300))
        except Exception:
            pass

    await password_locator.wait_for(state="visible", timeout=8000)
    await password_locator.click()
    await password_locator.fill("")
    await page.wait_for_timeout(random.randint(100, 250))
    await password_locator.type(password, delay=random.randint(30, 60))
    await page.wait_for_timeout(random.randint(200, 400))

    # ── Find and click submit ─────────────────────────────────────────────────
    # Score-based button selection (broader — no type='submit' requirement)
    buttons = page.locator("button, input[type='submit']")
    btn_count = await buttons.count()
    submit_locator = None
    best_submit_score = -999
    for i in range(min(btn_count, 30)):
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
            if sc > best_submit_score:
                best_submit_score = sc
                submit_locator = b
        except Exception:
            continue

    if submit_locator and best_submit_score >= 0:
        try:
            await submit_locator.click(timeout=8000)
        except Exception:
            await password_locator.press("Enter")
    else:
        # No good button found: just press Enter
        await password_locator.press("Enter")

    return {"method": "generic_heuristic"}


# ─── Public API ───────────────────────────────────────────────────────────────
async def _prompt_2fa_code_interactive(challenge_url: str, page) -> str:
    """
    Demande à l'utilisateur de saisir le code 2FA dans le terminal.
    Fonctionne même en mode headless (mais le navigateur doit être visible pour que l'user voie la page).
    """
    print("\n" + "="*60)
    print("🔐 2FA REQUIRED")
    print(f"Page: {challenge_url}")
    print("Veuillez entrer le code de vérification reçu par SMS / email / authenticator :")
    code = input("Code: ").strip()
    print("="*60 + "\n")
    return code

# Optionnel : récupération automatique par email (IMAP) – à configurer
async def _fetch_2fa_from_email(email_address: str, email_password: str, imap_server: str = "imap.gmail.com", timeout_seconds: int = 120) -> str:
    """
    Exemple basique pour Gmail. Nécessite `pip install imap-tools`.
    Non activé par défaut.
    """
    # Implémentation possible – je ne la détaille pas ici pour rester simple.
    # Vous pouvez l'ajouter si besoin.
    raise NotImplementedError("Lisez le code source pour implémenter cette partie.")

# ============================================================================
# Détection et gestion du 2FA dans Playwright
# ============================================================================

async def _handle_2fa_if_needed(
    page,
    timeout_seconds: int = 120,
    code_callback: Optional[Callable[[str, any], Awaitable[str]]] = None
) -> bool:
    """
    Détecte si une page de challenge 2FA est présente.
    Si oui, attend le code (via callback) et le soumet.
    Retourne True si le 2FA a été résolu avec succès, False si aucun 2FA détecté.
    """
    # Liste de patterns d'URL de challenge (LinkedIn, Google, Facebook, etc.)
    challenge_patterns = [
        r"/checkpoint/challenge/",   # LinkedIn
        r"/challenge/",              # générique
        r"/login/challenge",         # autre
        r"/2fa/",                    # Twitter, etc.
        r"/auth/verify",             # générique
        r"verification",             # fallback
    ]

    current_url = page.url
    is_challenge = any(re.search(p, current_url, re.I) for p in challenge_patterns)

    # Vérifier aussi la présence d'un champ de code visible
    code_input_selector = "input[name='challengeCode'], input[name='2faCode'], input[name='verificationCode'], input[placeholder*='code'], input[placeholder*='verification']"
    code_input = page.locator(code_input_selector).first if is_challenge else None

    if not is_challenge and code_input:
        # Parfois l'URL n'est pas typique mais le champ est là
        try:
            await code_input.wait_for(state="visible", timeout=3000)
            is_challenge = True
        except:
            pass

    if not is_challenge:
        return False

    print(f"[2FA] Challenge détecté : {current_url}")

    # Si aucun callback fourni, utiliser le prompt interactif
    if not code_callback:
        code_callback = _prompt_2fa_code_interactive

    # Attendre que l'utilisateur donne le code
    code = await code_callback(current_url, page)

    # Remplir et soumettre
    try:
        await code_input.fill(code)
        await page.wait_for_timeout(500)
        # Chercher le bouton de validation
        submit_btns = page.locator("button[type='submit'], button:has-text('Verify'), button:has-text('Submit'), button:has-text('Continue')")
        if await submit_btns.count() > 0:
            await submit_btns.first.click()
        else:
            await code_input.press("Enter")
        # Attendre la redirection
        await page.wait_for_load_state("networkidle", timeout=30000)
        await page.wait_for_timeout(2000)
        print("[2FA] Code soumis avec succès.")
        return True
    except Exception as e:
        print(f"[2FA] Erreur lors de la soumission : {e}")
        raise























async def login_and_get_cookies(
    service_url: str,
    username: str,
    password: str,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Universal relay login. Supports all major sites + generic heuristic fallback.
    Backward-compatible signature.

    Priority:
      1. profile with explicit selectors (e.g. recolyse.com)
      2. _SITE_STRATEGIES (pinterest, linkedin, facebook, twitter …)
      3. generic heuristic (any unknown site)
    """
    profile = profile or {}

    def _as_list(x, default_list):
        if not x:
            return default_list
        return [x] if isinstance(x, str) else (x if isinstance(x, list) else default_list)

    # Profile-provided selectors
    username_selectors = _as_list(profile.get("username_selector"), [])
    password_selectors = _as_list(profile.get("password_selector"), [])
    submit_selectors   = _as_list(profile.get("submit_selector"), [])
    has_profile_selectors = bool(username_selectors or password_selectors)

    open_login_selector     = profile.get("open_login_selector")
    goto_wait_until         = profile.get("goto_wait_until", "domcontentloaded")
    pre_fill_wait_ms        = int(profile.get("pre_fill_wait_ms", 1200))
    between_actions_wait_ms = int(profile.get("between_actions_wait_ms", 250))
    after_submit_wait_ms    = int(profile.get("after_submit_wait_ms", 2500))
    post_login_timeout_ms   = int(profile.get("post_login_timeout_ms", 20_000))
    post_login_url_contains = profile.get("post_login_url_contains")
    post_login_selector     = profile.get("post_login_selector")
    post_login_goto         = profile.get("post_login_goto")
    stay_connected_ms       = int(profile.get("stay_connected_ms", 4000))
    cookie_wait_name        = profile.get("cookie_wait_name")
    cookie_min_count        = int(profile.get("cookie_min_count", 1))
    cookie_wait_timeout_ms  = int(profile.get("cookie_wait_timeout_ms", 15_000))

    used = {"username": None, "password": None, "submit": None}
    browser = None

    try:
        service_url = (service_url or "").strip()
        if not service_url:
            raise Exception("service_url is empty")
        if not service_url.startswith(("http://", "https://")):
            service_url = "https://" + service_url

        domain = _domain_from_url(service_url)   # already strips "www."
        origin = _origin_from_url(service_url)

        # Decide which strategy to use
        strat = _SITE_STRATEGIES.get(domain)

        # Navigate to login URL
        if strat and not has_profile_selectors:
            login_url = strat["login_url"]
        else:
            login_url = service_url

        async with async_playwright() as p:
            browser, context = await _make_stealth_context(p)
            page = await context.new_page()
            page.set_default_timeout(DEFAULT_TIMEOUT_MS)

            await page.goto(login_url, wait_until=goto_wait_until)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass

            before_url = page.url
            login_detected = False
            used_method = "unknown"

            # ── Branch 1 : known-site strategy ───────────────────────────
            if strat and not has_profile_selectors:
                await _run_site_strategy(page, strat, username, password)


                await _handle_2fa_if_needed(page, timeout_seconds=60, code_callback=None)

                used["username"] = used["password"] = used["submit"] = "strategy"
                used_method = f"strategy:{domain}"

            # ── Branch 2 : explicit profile selectors (e.g. recolyse) ────
            elif has_profile_selectors:
                await page.wait_for_timeout(pre_fill_wait_ms)
                if open_login_selector:
                    try:
                        await page.locator(open_login_selector).first.click()
                        await page.wait_for_timeout(between_actions_wait_ms)
                    except Exception:
                        pass

                # username
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

                # password
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

                # submit
                clicked = False
                for sel in (submit_selectors or ["button[type='submit']", "input[type='submit']"]):
                    try:
                        btn = page.locator(sel).first
                        await btn.wait_for(state="visible", timeout=8_000)
                        await page.wait_for_timeout(between_actions_wait_ms)
                        await btn.click()
                        used["submit"] = sel
                        clicked = True


                        await _handle_2fa_if_needed(page)



                        break
                    except Exception:
                        continue
                if not clicked:
                    await password_locator.press("Enter")
                    used["submit"] = "press:Enter"
                used_method = "profile_selectors"

            # ── Branch 3 : generic heuristic ─────────────────────────────
            else:
                await _generic_login(page, username, password)


                await _handle_2fa_if_needed(page)

                used["username"] = used["password"] = used["submit"] = "generic"
                used_method = "generic_heuristic"

            # ── Post-submit ───────────────────────────────────────────────
            await page.wait_for_timeout(after_submit_wait_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass

            # ── Success detection ─────────────────────────────────────────
            if strat and not has_profile_selectors:
                await page.wait_for_timeout(2000)
                login_detected = await _check_success(page, strat)
                if not login_detected:
                    await page.wait_for_timeout(3000)
                    login_detected = await _check_success(page, strat)
            else:
                if post_login_url_contains:
                    try:
                        await page.wait_for_url(f"**{post_login_url_contains}**", timeout=post_login_timeout_ms)
                        login_detected = True
                    except Exception:
                        pass
                if not login_detected and post_login_selector:
                    try:
                        await page.locator(post_login_selector).first.wait_for(state="visible", timeout=post_login_timeout_ms)
                        login_detected = True
                    except Exception:
                        pass
                if post_login_goto:
                    try:
                        await page.goto(post_login_goto, wait_until="domcontentloaded")
                        await page.wait_for_timeout(800)
                        login_detected = True
                    except Exception:
                        pass
                # Fallback: if URL changed away from login page, consider it success
                if not login_detected:
                    login_detected = await _check_success(page, strat)

            # ── Cookie readiness polling ──────────────────────────────────
            if stay_connected_ms > 0:
                await page.wait_for_timeout(stay_connected_ms)

            cookies = []
            poll_ms = 250
            elapsed = 0
            while elapsed < cookie_wait_timeout_ms:
                cookies = await context.cookies()
                domain_cookies = [c for c in cookies if (c.get("domain") or "").lstrip(".").endswith(domain)]
                ok_count = len(domain_cookies) >= cookie_min_count
                ok_name = (not cookie_wait_name) or any(c.get("name") == cookie_wait_name for c in domain_cookies)
                if ok_count and ok_name:
                    break
                await page.wait_for_timeout(poll_ms)
                elapsed += poll_ms

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
                "domain": domain,
                "origin": origin,
                "debug": {
                    "before_url": before_url,
                    "after_url": page.url,
                    "used_method": used_method,
                    "cookie_wait_elapsed_ms": elapsed,
                    "domain_cookie_count": len([
                        c for c in cookies
                        if (c.get("domain") or "").lstrip(".").endswith(domain)
                    ]),
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



