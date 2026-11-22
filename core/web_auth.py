"""
Dashboard login: one shared token (DASHBOARD_TOKEN in .env), entered once per browser at
/login and kept as a cookie (HttpOnly, SameSite=Strict, ~13 months).

The firewall (nftables, port 8000 only from the allowlisted LAN devices) and Tailscale stay
the first layer; this is the second, so a device on the tailnet or a spoofed LAN address
can't post Strava notes or push workouts.

Always allowed without a login:
- calls from the container itself (127.0.0.1 / ::1): the host cron's /sync-data runs through
  `pct exec` and connects locally,
- the login page and the files a phone fetches before it has the cookie (manifest, icons,
  the hashed /assets bundle - no data in any of them).
Scripts can send `Authorization: Bearer <token>` instead of the cookie.

With no DASHBOARD_TOKEN set the dashboard stays open (and logs a warning at start).
"""
import hashlib
import hmac
import html
import logging
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from core.db import load_env

logger = logging.getLogger("AI_COACH_V1.web_auth")

COOKIE = "coach_auth"
MAX_AGE = 400 * 24 * 3600
LOCAL = {"127.0.0.1", "::1"}
OPEN_PATHS = {"/login", "/apple-touch-icon.png", "/icon-192.png", "/icon-512.png", "/manifest.webmanifest"}


def _token() -> str | None:
    return (load_env().get("DASHBOARD_TOKEN") or "").strip() or None


def _cookie_value(token: str) -> str:
    return hmac.new(token.encode(), b"ai-coach-dashboard-v1", hashlib.sha256).hexdigest()


def is_authorized(request: Request, token: str) -> bool:
    if request.client and request.client.host in LOCAL:
        return True
    cookie = request.cookies.get(COOKIE)
    if cookie and hmac.compare_digest(cookie, _cookie_value(token)):
        return True
    auth = request.headers.get("authorization", "")
    return auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), token)


async def middleware(request: Request, call_next):
    token = _token()
    path = request.url.path
    if not token or path in OPEN_PATHS or path.startswith("/assets/") or is_authorized(request, token):
        return await call_next(request)
    if path.startswith("/api/") or path in ("/stream_chat", "/sync-data"):
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})
    return RedirectResponse("/login", status_code=303)


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Coach login</title><link rel="manifest" href="/manifest.webmanifest">
<style>
:root{{--bg:#f6f7f9;--card:#fff;--text:#111827;--muted:#6b7280;--line:#e5e7eb;--accent:#f97316}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0b0f17;--card:#121826;--text:#e5e7eb;--muted:#94a3b8;--line:#1f2937}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:16px;box-sizing:border-box}}
form{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:24px;width:100%;max-width:340px}}
h1{{font-size:18px;margin:0 0 4px}}p{{color:var(--muted);margin:0 0 16px;font-size:13px}}
input{{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:10px;border:1px solid var(--line);
background:transparent;color:var(--text);font-size:15px}}
button{{margin-top:12px;width:100%;padding:10px;border:0;border-radius:10px;background:var(--accent);color:#fff;
font-weight:600;font-size:15px;cursor:pointer}}.err{{color:#ef4444;margin:10px 0 0;font-size:13px}}
</style></head><body>
<form method="post" action="/login"><h1>AI Coach</h1><p>Enter the dashboard token once on this device.</p>
<input type="password" name="token" autocomplete="current-password" autofocus required aria-label="Token">
<button type="submit">Log in</button>{error}</form></body></html>"""


def login_page(error: str = "") -> HTMLResponse:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(LOGIN_PAGE.format(error=err), headers={"Cache-Control": "no-store"})


async def login_submit(request: Request) -> Response:
    token = _token()
    given = (parse_qs((await request.body()).decode(errors="replace")).get("token") or [""])[0].strip()
    if not token or not hmac.compare_digest(given, token):
        logger.warning(f"Failed dashboard login from {request.client.host if request.client else '?'}")
        return login_page("Wrong token.")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, _cookie_value(token), max_age=MAX_AGE, httponly=True, samesite="strict")
    logger.info(f"Dashboard login from {request.client.host if request.client else '?'}")
    return resp


def install(app):
    app.middleware("http")(middleware)
    app.get("/login", include_in_schema=False)(lambda: login_page())
    app.post("/login", include_in_schema=False)(login_submit)
    if not _token():
        logger.warning("DASHBOARD_TOKEN is not set - the dashboard has no login.")
