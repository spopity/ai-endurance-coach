"""
One-time OAuth setup for Strava (the settings-page access token only has the
`read` scope, which can't list activities). Requests `activity:read_all` (name
sync) and `activity:write` (coach notes in activity descriptions).

Prerequisite: an app at https://www.strava.com/settings/api with
Authorization Callback Domain = localhost, and its Client ID / Client Secret
in ai-coach-v1/.env as STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET.

Run:
    python ingest/strava_auth.py               # prints the link, prompts for the code
    python ingest/strava_auth.py --code CODE   # exchange a code you already have
"""
import os
import sys
import json
import argparse
import urllib.parse
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.db import load_env  # noqa: E402

TOKEN_PATH = os.path.join(ROOT, "strava_tokens.json")
REDIRECT_URI = "http://localhost"
SCOPE = "activity:read_all,activity:write"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", help="authorization code from the redirect URL")
    args = parser.parse_args()

    env = load_env()
    client_id = env.get("STRAVA_CLIENT_ID")
    client_secret = env.get("STRAVA_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("ERROR: STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET are not set in .env.")
        print("Create an app at https://www.strava.com/settings/api (callback domain: localhost).")
        return 1

    code = args.code
    if not code:
        auth_url = (
            "https://www.strava.com/oauth/authorize"
            f"?client_id={client_id}&response_type=code&approval_prompt=force"
            f"&scope={SCOPE}&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        )
        print("\n=== STEP 1: AUTHORIZE ===")
        print("Open this exact link in your web browser:\n")
        print(auth_url)
        print("\nLog in and click 'Authorize' - keep ALL boxes ticked: viewing your private activities, and")
        print("uploading activities to Strava (that permission also covers editing descriptions).")
        print("Your browser will redirect to a broken page (localhost). This is NORMAL.")
        print("Look at the URL bar - it will look like: http://localhost/?state=&code=YOUR_CODE&scope=read,activity:write,activity:read_all")
        code = input("\nPaste the YOUR_CODE value here: ").strip()

    print("\nExchanging code for tokens...")
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
    }, timeout=30)
    tokens = resp.json()
    if resp.status_code != 200 or "refresh_token" not in tokens:
        print(f"Failed! Strava responded with: {resp.text}")
        return 1

    granted = set(tokens.get("scope", "").replace(" ", ",").split(","))  # Strava separates with spaces
    missing = set(SCOPE.split(",")) - granted
    if missing:
        print(f"Warning: Strava did not grant {', '.join(sorted(missing))} - re-run and keep all boxes ticked.")
    tokens.pop("athlete", None)
    with open(TOKEN_PATH, "w") as f:
        json.dump(tokens, f)
    os.chmod(TOKEN_PATH, 0o600)
    print(f"Success! Tokens saved to {TOKEN_PATH}.")
    print("You're ready to run scripts/sync_strava_names.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
