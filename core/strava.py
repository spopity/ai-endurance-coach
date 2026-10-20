"""
Minimal Strava API client shared by the name sync (scripts/sync_strava_names.py)
and the coach notes in activity descriptions (core/strava_notes.py).

Tokens come from ingest/strava_auth.py (strava_tokens.json) and are refreshed
here when close to expiry - Strava access tokens last 6 hours.
"""
import json
import os
import time
from datetime import datetime, timezone

import requests

from core.db import ROOT, load_env

TOKEN_PATH = os.path.join(ROOT, "strava_tokens.json")
STRAVA_API = "https://www.strava.com/api/v3"
PER_PAGE = 200


def access_token(env: dict | None = None) -> str:
    if not os.path.exists(TOKEN_PATH):
        raise RuntimeError(f"{TOKEN_PATH} not found. Run ingest/strava_auth.py first.")
    with open(TOKEN_PATH) as f:
        tokens = json.load(f)
    if tokens.get("expires_at", 0) > time.time() + 300:
        return tokens["access_token"]

    env = env or load_env()
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": env["STRAVA_CLIENT_ID"],
        "client_secret": env["STRAVA_CLIENT_SECRET"],
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }, timeout=30)
    new_tokens = resp.json()
    if resp.status_code != 200 or "access_token" not in new_tokens:
        raise RuntimeError(f"Failed to refresh Strava token: {resp.text}")
    new_tokens.setdefault("scope", tokens.get("scope"))  # refresh responses don't repeat the granted scope
    # Atomic replace: the name sync and the notes job can refresh at the same time.
    tmp = f"{TOKEN_PATH}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(new_tokens, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_PATH)
    return new_tokens["access_token"]


def granted_scopes() -> set[str]:
    try:
        with open(TOKEN_PATH) as f:
            return set(str(json.load(f).get("scope") or "").replace(" ", ",").split(",")) - {""}
    except OSError:
        return set()


def _request(method: str, path: str, token: str, **kwargs) -> requests.Response:
    resp = requests.request(method, f"{STRAVA_API}{path}", headers={"Authorization": f"Bearer {token}"},
                            timeout=60, **kwargs)
    if resp.status_code == 429:
        raise RuntimeError("Strava rate limit hit - try again in 15 minutes.")
    resp.raise_for_status()
    return resp


def fetch_activities(token: str, after: datetime | None = None) -> list[dict]:
    """Activity summaries, all pages (~1 request per 200). `after` limits to activities started after it."""
    activities, page = [], 1
    params = {"per_page": PER_PAGE}
    if after:
        params["after"] = int(after.timestamp())
    while True:
        batch = _request("GET", "/athlete/activities", token, params={**params, "page": page}).json()
        activities += batch
        if len(batch) < PER_PAGE:
            return activities
        page += 1


def get_activity(token: str, activity_id: str | int) -> dict:
    return _request("GET", f"/activities/{activity_id}", token).json()


def update_activity(token: str, activity_id: str | int, **fields) -> dict:
    """Needs the activity:write scope. Fields: description, name, ..."""
    return _request("PUT", f"/activities/{activity_id}", token, json=fields).json()


def utc_ts(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
