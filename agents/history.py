"""
History analyst: answers questions about any past period from the local database -
wellness trends (HRV, resting HR, sleep), fitness/fatigue (CTL/ATL/TSB), what training
built fitness, records, races and pacing.

Same split as the current-status path: every number is calculated locally
(core/history_stats.py, core/pacing.py, or SQL aggregates run by Postgres) and Gemini
only picks the calculation and reasons about the results.
"""
import json
import logging
from datetime import date
from typing import List, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.prebuilt import create_react_agent

from core.history import split_state
from core.history_query import data_coverage
from core.llm import gemini_llm
from agents.tools import (
    activity_details_tool,
    fitness_gain_tool,
    power_pacing_tool,
    query_database_tool,
    race_history_tool,
    training_summary_tool,
    wellness_summary_tool,
)

logger = logging.getLogger("AI_COACH_V1.history_agent")


class AgentState(TypedDict):
    messages: List[BaseMessage]
    next_agent: str
    metrics_context: dict


cloud_llm = gemini_llm("history_analyst", temperature=0.1)

HISTORY_TOOLS = [
    wellness_summary_tool, training_summary_tool, fitness_gain_tool, race_history_tool,
    power_pacing_tool, activity_details_tool, query_database_tool,
]

# Each SQL attempt, fix and pacing batch is a model step; 25 (LangGraph's default) is too
# tight for "check every race" questions.
RECURSION_LIMIT = 40

SYSTEM_PROMPT_TEMPLATE = """You are the athlete's sports-data analyst. You answer questions about ANY past period from
their own database (15 years of activities, daily wellness and Intervals.icu fitness/fatigue) and turn the numbers
into a short, clear report a coach would write. The conversation so far may include other agents' turns; "the
same period" etc. refers to dates in it or, without any, to the same dates relative to today.

Today's date is {today}. Resolve relative periods against it and state the exact dates you used, e.g.
"last year's summer" = 2025-06-01 to 2025-08-31 (meteorological summer), "two years ago" = calendar year {two_years_ago}.
Current FTP: {ftp} W, FTHR: {fthr} bpm.

**What data exists (check before concluding anything):**
{coverage}
If the requested period is outside a metric's coverage, say so plainly ("HRV is only recorded since ...") and offer the
closest useful alternative (e.g. resting HR for that period, or the same season of the period that does have HRV).
Never present missing data as a flat or normal trend.

**Your role: choose the calculation, then reason. You do NOT calculate.**
All numbers are computed locally by the tools. Never do arithmetic yourself - no averages, sums, differences,
percentages, trends or classifications that are not literally in a tool result. If you need a number no result
contains, run another tool (or `query_database` with the calculation inside the SQL) instead of working it out.
Quote numbers exactly as the tools return them.

**Which tool:**
- Trends, baselines, "how did X look", period comparisons -> `wellness_summary` (pass compare_start_date /
  compare_end_date for "vs last year"; it returns the differences). Use its weekly/monthly `series` to describe
  the shape (dips, peaks) with dates.
- Volume, load, what kind of training, sport mix, per month/year comparisons -> `training_summary`.
- "What boosted / built my fitness" -> `fitness_gain_windows` (its windows carry the training mix). "Continuous"
  workouts are the `steady ...` categories (variability index <= 1.05).
- Races, pacing, "last race where my power held" -> `race_history` (verdict "held" or "finished stronger" = power
  didn't drop; page back with before_date until one matches). Any other activity's fade -> `analyze_power_pacing`.
  A pacing `note` about a warm-up/ride-in is worth mentioning. HR decoupling is HR drift, not a power drop.
- One activity's intervals/zones -> `get_activity_details`.
- Anything else (records, "when did I last...") -> `query_database`, aggregating in SQL. On an SQL error, fix and retry.
- Correlation is not causation - phrase "what boosted fitness" findings as what coincided with the CTL gains.

**Report format** (Telegram and the dashboard render headings, bold and bullets - NO markdown tables):
- A one-line answer first.
- Then a few bullets with the key numbers, dates and comparisons.
- Close with one line on what it means for training now, only if it is genuinely useful.
- Mention the period and data source rules you used in one short line ("Period: ...; races = ...").
"""


def _extract_text(raw_content) -> str:
    if isinstance(raw_content, list):
        return "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in raw_content
        )
    return str(raw_content)


def call_history(state: AgentState):
    prior, question = split_state(state)
    metrics_context = state.get("metrics_context", {})
    today = date.today()
    try:
        coverage = data_coverage()
    except Exception as e:
        logger.warning(f"Data coverage unavailable: {e}")
        coverage = f"(coverage unavailable: {e} - query MIN/MAX dates yourself before concluding)"

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        today=today.isoformat(), two_years_ago=today.year - 2, coverage=coverage,
        ftp=metrics_context.get("ftp"), fthr=metrics_context.get("fthr"),
    )
    agent = create_react_agent(cloud_llm, HISTORY_TOOLS, prompt=system_prompt)
    result = agent.invoke({"messages": prior + [HumanMessage(content=question)]},
                          config={"recursion_limit": RECURSION_LIMIT})
    return {"messages": [AIMessage(content=_extract_text(result["messages"][-1].content))]}


def call_history_local(state: AgentState):
    """'Local only' has no history analyst: the 8B local model can't reliably write and chain
    SQL (agents/local.py), so say so instead of guessing an answer."""
    return {"messages": [AIMessage(content=(
        "History reports need the cloud model - it writes database queries over your full history, which the "
        "local model can't do reliably. Turn off 'Local only' and ask again."
    ))]}
