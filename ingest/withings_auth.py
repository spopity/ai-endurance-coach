"""
One-time interactive OAuth setup for Withings. Run this yourself in a real
terminal (it needs to prompt you for input) - not from an automated script.

Prerequisite: register an app at
https://account.withings.com/partner/dashboard_oauth2, then put its
Client ID / Client Secret into ai-coach-v1/.env as WITHINGS_CLIENT_ID /
WITHINGS_CLIENT_SECRET. Callback/redirect URI to register: http://localhost

Run: python ingest/withings_auth.py
"""
import os
import json
import urllib.parse
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOKEN_PATH = os.path.join(ROOT, "withings_tokens.json")
REDIRECT_URI = "http://localhost"


def load_env():
    env = {}
    with open(os.path.join(ROOT, ".env")) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main():
    env = load_env()
    client_id = env.get("WITHINGS_CLIENT_ID")
    client_secret = env.get("WITHINGS_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("ERROR: WITHINGS_CLIENT_ID / WITHINGS_CLIENT_SECRET are not set in .env.")
        print("Register an app at https://account.withings.com/partner/dashboard_oauth2")
        print("(callback URI: http://localhost), then fill those two values into .env and re-run.")
        return

    auth_url = (
        "https://account.withings.com/oauth2_user/authorize2"
        f"?response_type=code&client_id={client_id}&state=setup"
        f"&scope=user.metrics&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
    )

    print("\n=== STEP 1: AUTHORIZE ===")
    print("Open this exact link in your web browser:\n")
    print(auth_url)
    print("\nLog in and click 'Allow'.")
    print("Your browser will redirect to a broken page (localhost). This is NORMAL.")
    print("Look at the URL bar - it will look like: http://localhost/?code=YOUR_CODE&state=setup")

    auth_code = input("\nPaste the YOUR_CODE value here: ").strip()

    print("\nExchanging code for tokens...")
    data = {
        "action": "requesttoken",
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
    }
    resp = requests.post("https://wbsapi.withings.net/v2/oauth2", data=data)

    if resp.status_code == 200:
        body = resp.json().get("body", {})
        if "access_token" not in body:
            print(f"Failed! API responded without tokens: {resp.text}")
            return
        with open(TOKEN_PATH, "w") as f:
            json.dump(body, f)
        print(f"Success! Tokens saved to {TOKEN_PATH}.")
        print("You're ready to run ingest/import_withings.py.")
    else:
        print(f"Failed! API responded with: {resp.text}")


if __name__ == "__main__":
    main()
