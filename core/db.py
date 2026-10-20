"""Shared DB connection helper for ai-coach-v1's application code."""
import os
import psycopg2
import psycopg2.extras

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path=None):
    path = path or os.path.join(ROOT, ".env")
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def get_connection():
    env = load_env()
    return psycopg2.connect(env["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor)


# Every process imports this module, so this is where outgoing HTTP calls start being logged.
from core import api_log  # noqa: E402

api_log.install_http_hook()
