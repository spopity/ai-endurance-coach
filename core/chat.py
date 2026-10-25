"""
Shared helpers for front-ends (web UI, Telegram) that talk to the agent graph,
so every channel extracts replies and runs syncs the same way.
"""
import asyncio
import logging
import uuid

from langchain_core.messages import HumanMessage

from core.api_log import trigger, trigger_end, trigger_start
from core.history import to_messages
from core.next_session import proposal_for_turn, refresh as refresh_next_session
from graph import agent_platform
from ingest.import_intervals import sync_activities
from ingest.import_wellness import sync_wellness
from ingest.import_garmin import sync_garmin
from ingest.import_profile import sync_profile

logger = logging.getLogger("AI_COACH_V1.chat")

# Default window for "sync latest" from any front-end. The full historical
# backfill (365 days) is a one-off - run ingest/import_intervals.py and
# import_wellness.py directly with --days for that.
SYNC_WINDOW_DAYS = 5

AGENT_DISPLAY_NAMES = {
    "supervisor": "Supervisor",
    "ai_coach": "AI Coach",
    "racing_strategy": "Racing Strategy Agent",
    "injury_prevention": "Injury Prevention Agent",
    "life_optimization": "Life Optimization Agent",
    "athlete_twin": "Athlete Twin",
    "history_analyst": "History Analyst",
}


def extract_text(raw_content) -> str:
    """Normalises an AIMessage's content (str, or Gemini-style list of parts) to plain text."""
    if isinstance(raw_content, list) and raw_content and isinstance(raw_content[0], dict) and "text" in raw_content[0]:
        return raw_content[0]["text"]
    if isinstance(raw_content, str):
        return raw_content
    return str(raw_content)


def new_turn_id() -> str:
    """Marks a live chat turn: lets the coach propose a next-session change for the athlete to approve."""
    return uuid.uuid4().hex


def turn_proposal(turn_id: str | None) -> dict | None:
    """The next-session change this chat turn proposed (pending approval), if any."""
    if not turn_id:
        return None
    try:
        return proposal_for_turn(turn_id)
    except Exception as e:
        logger.warning(f"Could not look up a next-session proposal: {e}")
        return None


def _initial_state(message: str, history: list[dict] | None, agent: str | None, local_only: bool,
                   turn_id: str | None = None) -> dict:
    state = {"messages": to_messages(history or []) + [HumanMessage(content=message)]}
    if turn_id:
        state["turn_id"] = turn_id
    if agent:
        state["forced_agent"] = agent
    if local_only:
        state["local_only"] = True
    return state


async def ask_supervisor(
    message: str, history: list[dict] | None = None, agent: str | None = None, local_only: bool = False,
    turn_id: str | None = None,
) -> tuple[str, str]:
    """Runs one query through the full graph. Returns (agent_node_name, reply_text).

    `history` is the conversation so far as plain dicts ({role: "user"|"assistant",
    content, agent}) - only the most recent turns are passed on (see core/history.py).
    `agent` pins a specific agent node (e.g. "athlete_twin") and skips LLM routing.
    `local_only` answers with the local model instead of Gemini (agents/local.py).
    `turn_id` (new_turn_id()) marks a live chat turn - see turn_proposal().
    """
    # Graph nodes are synchronous (blocking Ollama/Gemini/DB calls) - keep them off the event loop.
    return await asyncio.to_thread(ask_supervisor_sync, message, history, agent, local_only, turn_id)


def ask_supervisor_sync(
    message: str, history: list[dict] | None = None, agent: str | None = None, local_only: bool = False,
    turn_id: str | None = None,
) -> tuple[str, str]:
    """Blocking ask_supervisor, for code that isn't async (Strava notes timer)."""
    final_state = agent_platform.invoke(_initial_state(message, history, agent, local_only, turn_id))
    agent = final_state.get("next_agent", "unknown")
    messages = final_state.get("messages") or []
    reply = extract_text(messages[-1].content) if messages else ""
    return agent, reply


async def run_sync(days: int = SYNC_WINDOW_DAYS) -> str:
    """Pulls the last `days` of activities + wellness from Intervals.icu and health data from
    Garmin. Returns a one-line summary."""
    with trigger("sync"):  # to_thread copies the context, so the worker threads inherit the tag
        activities_result, wellness_result = await asyncio.gather(
            asyncio.to_thread(sync_activities, days),
            asyncio.to_thread(sync_wellness, days),
        )
    parts = [
        f"Activities: {activities_result['inserted']} new, {activities_result['updated']} updated"
        + (f", {len(activities_result['failed'])} failed" if activities_result["failed"] else ""),
        f"Wellness: {wellness_result['inserted']} new, {wellness_result['updated']} updated"
        + (f", {len(wellness_result['failed'])} failed" if wellness_result["failed"] else ""),
    ]
    try:
        with trigger("sync"):
            garmin = await asyncio.to_thread(sync_garmin, days)
        parts.append(f"Garmin health: {garmin['inserted']} new, {garmin['updated']} updated")
    except Exception as e:
        logger.warning(f"Garmin sync failed: {e}")
        parts.append("Garmin health: sync failed")
    try:
        with trigger("sync"):
            profile = await asyncio.to_thread(sync_profile)
        if profile["changed"]:
            changes = ", ".join(f"{k} {old}->{new}" for k, (old, new) in profile["changed"].items() if k != "eftp_day")
            logger.info(f"Profile updated from Intervals.icu: {changes}")
            parts.append(f"Profile: {changes}")
    except Exception as e:
        logger.warning(f"Profile sync failed: {e}")
        parts.append("Profile: sync failed")
    try:
        # No new activity for a day: the next session is re-decided instead of waiting to be done.
        if await asyncio.to_thread(refresh_next_session):
            parts.append("Next session: re-decided")
    except Exception as e:
        logger.warning(f"Next-session refresh failed: {e}")
    return " | ".join(parts)


# Every named node except the supervisor produces the final answer - derived, so a new agent
# can't be forgotten here (its answer would be silently dropped from the dashboard stream).
FINAL_AGENT_NODES = set(AGENT_DISPLAY_NAMES) - {"supervisor"}


async def stream_reply(message: str, history: list[dict] | None = None, agent: str | None = None,
                       local_only: bool = False):
    """Runs one query through the graph, yielding progress events as dicts:
    {"type": "status", "content"}, {"type": "route", "agent", "content"},
    {"type": "final", "agent", "model", "content"} (model: "local" | "gemini" | "none" for
    answers computed without an LLM), then {"type": "proposal", "proposal": {id, session, why}} when the
    coach proposed a next-session change for the athlete to approve, and always a closing {"type": "end"}."""
    turn_id = new_turn_id()
    initial_state = _initial_state(message, history, agent, local_only, turn_id)
    started = set()
    tag = trigger_start("chat")  # not a `with`: this generator yields between set and reset
    models = []  # chat models used after routing, to label the answer
    try:
        async for event in agent_platform.astream_events(initial_state, version="v2"):
            kind, name = event["event"], event.get("name", "")

            if kind == "on_chain_start" and name in AGENT_DISPLAY_NAMES and name not in started:
                started.add(name)
                yield {"type": "status", "content": f"Engaging {AGENT_DISPLAY_NAMES[name]}..."}

            elif kind == "on_chat_model_start":
                if "ChatOllama" in name:
                    models.append("local")
                    yield {"type": "status", "content": "Engaging local model (Ollama)..."}
                elif "ChatGoogleGenerativeAI" in name:
                    models.append("gemini")
                    yield {"type": "status", "content": "Engaging cloud model (Gemini)..."}

            elif kind == "on_tool_start":
                yield {"type": "status", "content": f"Running tool: {name}"}

            elif kind == "on_chain_end" and name == "supervisor":
                models.clear()  # the supervisor's own (local) routing call doesn't label the answer
                routed = (event.get("data", {}).get("output") or {}).get("next_agent")
                if routed in AGENT_DISPLAY_NAMES:
                    yield {"type": "route", "agent": routed,
                           "content": f"Supervisor routed to: {AGENT_DISPLAY_NAMES[routed]}"}

            elif kind == "on_chain_end" and name in FINAL_AGENT_NODES:
                output = event.get("data", {}).get("output") or {}
                if output.get("messages"):
                    text = extract_text(output["messages"][-1].content)
                    logger.info(f"Final reply from '{name}' ({len(text)} chars).")
                    model = "gemini" if "gemini" in models else "local" if models else "none"
                    yield {"type": "final", "agent": name, "model": model, "content": text}
                    break
        proposal = await asyncio.to_thread(turn_proposal, turn_id)
        if proposal:
            yield {"type": "proposal", "proposal": proposal}
    except Exception as e:
        logger.error("Streaming reply failed", exc_info=True)
        yield {"type": "final", "agent": "error", "content": f"An error occurred: {e}"}
    finally:
        trigger_end(tag)
        yield {"type": "end"}
