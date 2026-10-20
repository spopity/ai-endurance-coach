"""
Logs every outgoing call to an external service into the api_calls table, for the
dashboard's System page (calls per service, Gemini tokens and estimated cost).

- LLM calls (Gemini, Ollama): a LangChain callback attached by the factories in
  core/llm.py records tokens, duration and an estimated cost per call.
- HTTP calls made with `requests` (Intervals.icu, Strava, Telegram notifications,
  Open-Meteo, Withings): requests.Session.send is wrapped once per process
  (install_http_hook, called from core/db.py). The Telegram bot's own traffic uses
  httpx and isn't counted.

`trigger("briefing")` tags every call made inside it with why it happened; the process
name (app, telegram_bot, post_strava_notes...) is recorded automatically. Logging never
breaks the call it describes: any failure to write the row is only logged.
"""
import contextvars
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger("AI_COACH_V1.api_log")

PROCESS = os.path.splitext(os.path.basename(sys.argv[0] or "python"))[0] or "python"

# USD per million tokens: (input, output incl. thinking, cached input), and the higher tier
# Google charges when a prompt exceeds LONG_PROMPT_TOKENS. Google's list prices as of 2026-10.
LONG_PROMPT_TOKENS = 200_000
PRICES = {
    "gemini-3.1-pro": {"base": (2.00, 12.00, 0.20), "long": (4.00, 18.00, 0.40)},
    "gemini-3-pro": {"base": (2.00, 12.00, 0.20), "long": (4.00, 18.00, 0.40)},
    "gemini-2.5-pro": {"base": (1.25, 10.00, 0.125), "long": (2.50, 15.00, 0.25)},
    "gemini-2.5-flash": {"base": (0.30, 2.50, 0.03), "long": (0.30, 2.50, 0.03)},
}
DEFAULT_PRICE = "gemini-3.1-pro"  # what gemini-pro-latest resolves to today

SERVICES = (  # host suffix -> service name
    ("intervals.icu", "intervals"),
    ("strava.com", "strava"),
    ("api.telegram.org", "telegram"),
    ("open-meteo.com", "open_meteo"),
    ("withings.net", "withings"),
    ("googleapis.com", "google"),
)

_trigger: contextvars.ContextVar[str | None] = contextvars.ContextVar("api_trigger", default=None)


def trigger_start(name: str):
    return _trigger.set(name)


def trigger_end(token):
    try:
        _trigger.reset(token)
    except ValueError:  # reset from another context (e.g. a generator closed elsewhere) - harmless
        pass


@contextmanager
def trigger(name: str):
    """Tags calls made inside the block (threads started with asyncio.to_thread inherit it)."""
    token = _trigger.set(name)
    try:
        yield
    finally:
        _trigger.reset(token)


def estimate_cost(model: str | None, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    key = next((k for k in PRICES if model and model.startswith(k)), DEFAULT_PRICE)
    price_in, price_out, price_cached = PRICES[key]["long" if input_tokens > LONG_PROMPT_TOKENS else "base"]
    fresh = max(0, input_tokens - cached_tokens)
    return (fresh * price_in + cached_tokens * price_cached + output_tokens * price_out) / 1_000_000


def record(service: str, **fields):
    """Writes one api_calls row. Never raises."""
    row = {"service": service, "process": PROCESS, "trigger": _trigger.get(), **fields}
    try:
        from core.db import get_connection  # lazy: core.db installs the HTTP hook from this module
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cols = list(row)
                cur.execute(f"INSERT INTO api_calls ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))});",
                            [row[c] for c in cols])
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Could not log {service} call: {e}")


# --- LLM calls ---

class UsageCallback(BaseCallbackHandler):
    """Records each chat-model call: tokens, duration, estimated cost (Gemini) or 0 (Ollama)."""

    def __init__(self, service: str, component: str):
        self.service, self.component = service, component
        self._started: dict = {}

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self._started[run_id] = time.monotonic()

    def on_llm_end(self, response, *, run_id, **kwargs):
        started = self._started.pop(run_id, None)
        message = getattr(response.generations[0][0], "message", None) if response.generations else None
        usage = getattr(message, "usage_metadata", None) or {}
        meta = getattr(message, "response_metadata", None) or {}
        model = meta.get("model_name") or meta.get("model")
        tokens_in, tokens_out = usage.get("input_tokens") or 0, usage.get("output_tokens") or 0
        cached = (usage.get("input_token_details") or {}).get("cache_read") or 0
        record(
            self.service, operation="chat", component=self.component, model=model,
            duration_ms=int((time.monotonic() - started) * 1000) if started else None,
            input_tokens=tokens_in, output_tokens=tokens_out, cached_tokens=cached,
            thinking_tokens=(usage.get("output_token_details") or {}).get("reasoning"),
            cost_usd=estimate_cost(model, tokens_in, tokens_out, cached) if self.service == "gemini" else 0,
        )

    def on_llm_error(self, error, *, run_id, **kwargs):
        started = self._started.pop(run_id, None)
        record(self.service, operation="chat", component=self.component, ok=False,
               duration_ms=int((time.monotonic() - started) * 1000) if started else None, error=str(error)[:500])


# --- HTTP calls ---

def _service(host: str) -> str | None:
    return next((name for suffix, name in SERVICES if host == suffix or host.endswith("." + suffix)), None)


def _operation(method: str, path: str) -> str:
    """'GET /api/v1/athlete/i12345/activities' -> 'GET /api/v1/athlete/{id}/activities'.
    Any path segment with a digit is an id (this also hides the Telegram bot token)."""
    parts = [("{id}" if re.search(r"\d", p) and not re.fullmatch(r"v\d+", p) else p) for p in path.split("/")]
    return f"{method} {'/'.join(parts) or '/'}"


def install_http_hook():
    import requests

    if getattr(requests.Session.send, "_api_logged", False):
        return
    original = requests.Session.send

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        service = _service(url.hostname or "")
        if not service:
            return original(self, request, **kwargs)
        started = time.monotonic()
        try:
            resp = original(self, request, **kwargs)
        except Exception as e:
            record(service, operation=_operation(request.method, url.path), ok=False,
                   duration_ms=int((time.monotonic() - started) * 1000), error=type(e).__name__)
            raise
        record(service, operation=_operation(request.method, url.path), status=resp.status_code,
               ok=resp.status_code < 400, duration_ms=int((time.monotonic() - started) * 1000))
        return resp

    send._api_logged = True
    requests.Session.send = send
