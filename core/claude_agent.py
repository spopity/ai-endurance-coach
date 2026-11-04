"""Answers a chat question with Claude Code (Claude Agent SDK) on the athlete's Claude
subscription - the default for the chat's cloud agents (Coach, Twin, Racing, History), with Gemini
as the alternative and as the fallback when Claude fails (claude_or_gemini).

Question/answer only - Claude can read data but never change anything:
- only the read-only coach tools in READ_ONLY_TOOLS are exposed, as an in-process MCP server;
  anything else an agent passes (e.g. propose_next_session_change, record_availability) is dropped;
- every Claude Code built-in tool is switched off (no Bash, Read, Edit, Write, Web...), no other MCP
  server, settings file, CLAUDE.md, skill or plugin is loaded, and can_use_tool denies anything else;
- it runs in an empty working directory with its own config dir (state/claude), never ~/.claude.

Auth: CLAUDE_CODE_OAUTH_TOKEN in .env, a long-lived subscription token from `claude setup-token`.
Model: CLAUDE_CHAT_MODEL in .env (default "opus"). Each answer is logged to api_calls as service
"claude" with cost 0 - the subscription is flat-rate.
"""
import asyncio
import concurrent.futures
import json
import logging
import os
import time
import warnings
from typing import Callable

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool

from core.api_log import record
from core.db import load_env

env = load_env()
logger = logging.getLogger("AI_COACH_V1.claude")

MODEL = env.get("CLAUDE_CHAT_MODEL") or "opus"
SERVER = "coach"
# Tool rounds per answer; the history analyst chains several SQL queries (its Gemini limit is 40 graph steps).
MAX_TURNS = 25

STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
CONFIG_DIR = os.path.join(STATE_DIR, "claude")
WORK_DIR = os.path.join(STATE_DIR, "claude-empty")

# Tools that only read. A new tool must be added here deliberately before Claude can use it.
READ_ONLY_TOOLS = {
    "search_historical_activities", "get_activity_details", "get_historical_wellness_trend", "check_injury_risk",
    "wellness_summary", "training_summary", "fitness_gain_windows", "race_history", "query_database",
    "analyze_power_pacing", "project_training_load", "estimate_ride_time", "list_upcoming_races",
    "build_race_plan", "review_race",
}

# Starts an answer Gemini gave because Claude failed (front-ends use it to label the answer).
FALLBACK_NOTE = "_Claude was unavailable, so Gemini answered this one._\n\n"

READ_ONLY_NOTE = """

**Read-only mode:** you answer questions only. You cannot save, change or record anything - not the next
session, availability, sick days or pinned sessions. If the athlete asks for such a change, say it can't be
done while Claude answers, give your recommendation, and tell them to switch the chat model back to Gemini
to save it. Your tools only read data."""


def configured() -> bool:
    return bool(env.get("CLAUDE_CODE_OAUTH_TOKEN"))


def _sdk_tool(t: BaseTool):
    from claude_agent_sdk import tool

    schema = t.tool_call_schema.model_json_schema() if t.args_schema else {"type": "object", "properties": {}}
    schema.pop("title", None)

    @tool(t.name, t.description, schema)
    async def run(args):
        try:
            result = await asyncio.to_thread(t.invoke, args)
            text = result if isinstance(result, str) else json.dumps(result, default=str)
            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:
            logger.warning(f"Tool {t.name} failed for Claude: {e}")
            return {"content": [{"type": "text", "text": f"Tool error: {e}"}], "is_error": True}

    return run


def _prompt(prior: list[BaseMessage], question: str) -> str:
    if not prior:
        return question
    lines = [f"{'Athlete' if isinstance(m, HumanMessage) else 'Coach'}: {m.content}" for m in prior]
    return ("Conversation so far (oldest first):\n" + "\n\n".join(lines)
            + f"\n\nThe athlete's new message:\n{question}")


async def _ask(component: str, system_prompt: str, tools: list[BaseTool], prior, question: str) -> str:
    from claude_agent_sdk import (
        ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny, ResultMessage, create_sdk_mcp_server, query,
    )
    from claude_agent_sdk.types import CanUseToolShadowedWarning

    # Expected: the allowed read-only tools skip can_use_tool; it still denies everything else.
    warnings.simplefilter("ignore", CanUseToolShadowedWarning)

    read_only = [t for t in tools if t.name in READ_ONLY_TOOLS]
    dropped = sorted({t.name for t in tools} - READ_ONLY_TOOLS)
    if dropped:
        logger.info(f"{component}: not exposing write/unknown tools to Claude: {dropped}")
    allowed = [f"mcp__{SERVER}__{t.name}" for t in read_only]

    async def can_use_tool(name, _input, _context):
        if name in allowed:
            return PermissionResultAllow()
        logger.warning(f"{component}: Claude tried a tool outside the read-only set: {name}")
        return PermissionResultDeny(message="Read-only Q&A: only the coach data tools are available.")

    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(WORK_DIR, exist_ok=True)
    options = ClaudeAgentOptions(
        system_prompt=system_prompt + READ_ONLY_NOTE,
        model=MODEL,
        tools=[],  # no built-in tools at all
        mcp_servers={SERVER: create_sdk_mcp_server(name=SERVER, tools=[_sdk_tool(t) for t in read_only])},
        strict_mcp_config=True,
        allowed_tools=allowed,
        can_use_tool=can_use_tool,
        setting_sources=[],
        cwd=WORK_DIR,
        max_turns=MAX_TURNS,
        env={
            "CLAUDE_CODE_OAUTH_TOKEN": env.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            "CLAUDE_CONFIG_DIR": CONFIG_DIR,
            "HOME": CONFIG_DIR,  # the services run without HOME; never fall back to /root/.claude
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
    )

    # can_use_tool needs a streamed prompt.
    async def prompt_stream():
        yield {"type": "user", "message": {"role": "user", "content": _prompt(prior, question)}}

    started = time.monotonic()
    result = None
    try:
        async for message in query(prompt=prompt_stream(), options=options):
            if isinstance(message, ResultMessage):
                result = message
    except Exception as e:
        record("claude", operation="chat", component=component, model=MODEL, ok=False,
               duration_ms=int((time.monotonic() - started) * 1000), error=str(e)[:500])
        raise

    usage = (result.usage if result else None) or {}
    cached = usage.get("cache_read_input_tokens") or 0
    record("claude", operation="chat", component=component, model=MODEL,
           ok=bool(result and not result.is_error), duration_ms=int((time.monotonic() - started) * 1000),
           input_tokens=(usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0) + cached,
           output_tokens=usage.get("output_tokens"), cached_tokens=cached, cost_usd=0)
    if result is None or result.is_error or not result.result:
        raise RuntimeError(f"Claude returned no answer: {(result and (result.errors or result.subtype)) or 'no result'}")
    return result.result


def ask_claude(component: str, system_prompt: str, tools: list[BaseTool], prior: list[BaseMessage], question: str) -> str:
    """Blocking: answers `question` (after the `prior` conversation) with Claude, using only read-only tools."""
    if not configured():
        raise RuntimeError("CLAUDE_CODE_OAUTH_TOKEN is not set (run `claude setup-token`)")
    coro = _ask(component, system_prompt, tools, prior, question)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:  # called from inside an event loop
        return pool.submit(asyncio.run, coro).result()


def claude_or_gemini(component: str, system_prompt: str, tools: list[BaseTool], prior: list[BaseMessage],
                     question: str, gemini: Callable[[], str]) -> str:
    """Answers with Claude; if Claude isn't set up or fails, with `gemini()` - the agent's own Gemini call,
    given the same read-only prompt and tools - and says so at the top of the answer."""
    try:
        return ask_claude(component, system_prompt, tools, prior, question)
    except Exception as e:
        logger.warning(f"{component}: Claude failed ({e}) - answering with Gemini instead.", exc_info=True)
        return FALLBACK_NOTE + gemini()
