"""KOM agent: Strava segments, KOMs/CRs, QOMs, Local Legends and KOM attempts.

It is a separate project (/opt/kom, its own service on 127.0.0.1:8010, its own database) that always
answers with Claude. This node only forwards the question and the recent conversation to it
(POST {KOM_URL}/ask with the shared KOM_API_TOKEN) and returns its answer.
"""
import logging
import re
import time

import requests
from langchain_core.messages import AIMessage, HumanMessage

from core.api_log import record
from core.db import load_env
from core.history import split_state

env = load_env()
logger = logging.getLogger("AI_COACH_V1.kom")

KOM_URL = (env.get("KOM_URL") or "http://127.0.0.1:8010").rstrip("/")
# Unmistakable KOM/segment questions: the supervisor sends them here without the (small, local) routing model.
KOM_WORDS = re.compile(r"\b(koms?|qoms?|local legends?|segments?)\b", re.IGNORECASE)
# A Claude answer with several Strava lookups; the first question on a segment loads its history.
TIMEOUT_S = 300


def configured() -> bool:
    return bool(env.get("KOM_API_TOKEN"))


def ask_kom(question: str, prior: list) -> str:
    history = [{"role": "user" if isinstance(m, HumanMessage) else "assistant", "content": str(m.content),
                "agent": getattr(m, "name", None)} for m in prior]
    resp = requests.post(f"{KOM_URL}/ask", json={"message": question, "history": history},
                         headers={"X-Kom-Token": env.get("KOM_API_TOKEN", "")}, timeout=TIMEOUT_S)
    if resp.status_code != 200:
        detail = resp.json().get("detail") if resp.headers.get("content-type", "").startswith("application/json") else resp.text
        raise RuntimeError(f"KOM agent answered {resp.status_code}: {str(detail)[:300]}")
    return resp.json()["reply"]


def call_kom(state) -> dict:
    prior, question = split_state(state)
    if not configured():
        return {"messages": [AIMessage(content="The KOM agent isn't set up (KOM_API_TOKEN missing in .env).")]}
    started = time.monotonic()
    try:
        reply = ask_kom(question, prior)
        record("kom", operation="ask", component="kom", model="claude",
               duration_ms=int((time.monotonic() - started) * 1000), cost_usd=0)
    except Exception as e:
        logger.error(f"KOM agent failed: {e}")
        record("kom", operation="ask", component="kom", ok=False,
               duration_ms=int((time.monotonic() - started) * 1000), error=str(e)[:500])
        reply = f"The KOM agent couldn't answer right now ({e}). Try again in a minute."
    return {"messages": [AIMessage(content=reply)]}
