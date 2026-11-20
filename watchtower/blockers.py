"""Detect login walls and captchas. We never solve them, we only report them."""
from __future__ import annotations

import re

CAPTCHA_PATTERNS = [
    r"g-recaptcha", r"recaptcha/api", r"hcaptcha", r"h-captcha", r"cf-turnstile",
    r"challenges\.cloudflare\.com", r"friendlycaptcha", r"frc-captcha", r"captcha-image",
    r"name=[\"']captcha", r"id=[\"']captcha", r"cf-browser-verification",
    r"cf_chl_opt", r"<title>Just a moment\.\.\.",
]
LOGIN_URL = re.compile(r"/(login|signin|sign-in|sign_in|anmelden|anmeldung|einloggen|auth|sso|"
                       r"connexion|identification|account/login|logon|zaloguj|logowanie)"
                       r"(\b|[/?._-])", re.I)


def is_login_url(url: str) -> bool:
    return bool(LOGIN_URL.search(url or ""))
LOGIN_WORDS = [
    "login", "log in", "sign in", "anmelden", "anmeldung", "einloggen", "connexion",
    "se connecter", "identifiez", "iniciar sesión", "acceso usuario", "inloggen", "aanmelden",
    "zaloguj", "logowanie", "accedi", "registrieren", "benutzername", "username", "passwort",
    "mot de passe", "contraseña", "wachtwoord", "hasło",
]


def detect_captcha(html: str) -> bool:
    head = html[:200000]
    return any(re.search(p, head, re.I) for p in CAPTCHA_PATTERNS)


def detect_login(html: str, status: int = 200, url: str = "") -> bool:
    if status in (401, 407):
        return True
    low = html[:200000].lower()
    has_password = bool(re.search(r"<input[^>]+type=[\"']?password", low))
    if has_password:
        return True
    if is_login_url(url) and len(low) < 60000:
        return any(w in low for w in LOGIN_WORDS)
    return False


def classify_block(html: str, status: int = 200, url: str = "") -> str | None:
    """Return 'login', 'captcha', 'forbidden' or None."""
    if detect_login(html, status, url):
        return "login"
    if detect_captcha(html):
        return "captcha"
    if status == 403:
        return "forbidden"
    return None
