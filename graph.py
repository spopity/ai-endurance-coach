import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import TypedDict, List, Literal
from langgraph.graph import StateGraph, END
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from pydantic import BaseModel, Field

from core.db import load_env
from core.llm import local_llm
from core.metrics import load_athlete_metrics
from core.next_session import latest_decision
from core.readiness import readiness_context
from core.history import split_state, format_for_supervisor
from agents.ai_coach import call_ai_coach
from agents.twin import call_twin
from agents.injury import call_injury
from agents.life import run_life_agent
from agents.racing import call_racing
from agents.history import call_history, call_history_local
from agents.local import call_coach_local, call_racing_local, call_twin_local

env = load_env()
logger = logging.getLogger("AI_COACH_V1.graph")

# Life Optimization agent is temporarily disabled until Google Calendar is set up -
# it was catching any date-related query and producing false positives. While
# disabled it is removed from the supervisor's allowed outputs, its prompt, and
# the graph itself. Re-enable with LIFE_AGENT_ENABLED=true in the env.
LIFE_AGENT_ENABLED = str(env.get("LIFE_AGENT_ENABLED", "false")).lower() in ("1", "true", "yes")

AGENT_NAMES = ["racing_strategy", "injury_prevention", "athlete_twin", "history_analyst", "ai_coach"]
if LIFE_AGENT_ENABLED:
    AGENT_NAMES.insert(0, "life_optimization")


class AgentState(TypedDict):
    messages: List[BaseMessage]
    next_agent: str
    metrics_context: dict
    # Optional: a front-end (e.g. Telegram's /twin) can pin the agent and skip LLM routing.
    forced_agent: str
    # Optional: answer with the local model only (agents/local.py) instead of Gemini.
    local_only: bool
    # Optional: set by a live chat turn (Telegram, dashboard) - lets the coach PROPOSE a change to the
    # saved next session, which the athlete must approve (core/next_session.py). Unset for the
    # briefing and the automatic decisions, so those can never change it.
    turn_id: str


def global_preprocessor(state: AgentState):
    logger.info("Running global preprocessor: fetching latest metrics from athlete_twin_v2...")
    metrics = load_athlete_metrics()
    metrics["current_local_time"] = datetime.now(ZoneInfo("Europe/Bratislava")).strftime("%A %Y-%m-%d %H:%M")
    for key, fn in (("readiness", lambda: readiness_context(metrics.get("tsb"))),
                    ("next_session_decided", latest_decision)):
        try:
            metrics[key] = fn()
        except Exception as e:
            logger.warning(f"Context '{key}' unavailable: {e}")
    return {"metrics_context": metrics}


# --- LLM-based Supervisor ---
# Local model (core/llm.py) with native tool calling, which with_structured_output()
# needs to reliably constrain output to one of the agent names below.
supervisor_llm = local_llm("supervisor", temperature=0)


class RouteDecision(BaseModel):
    agent: Literal[tuple(AGENT_NAMES)] = Field(description="The single specialist agent best suited to handle this query.")
    reasoning: str = Field(description="One short sentence explaining the routing choice.")


structured_supervisor = supervisor_llm.with_structured_output(RouteDecision)

SUPERVISOR_PROMPT = """You are a supervisor in a multi-agent system for an AI athlete coach.
Route the user's query to the single most appropriate specialist agent:
{life_line}
- racing_strategy: anything about a REAL race or event - the athlete's upcoming races (Intervals.icu calendar),
  race pacing, race-day fuelling/hydration/caffeine, taper and peaking for a race, race-week plans, race weather,
  predicted race times, and post-race reviews ("how did my race go?"). Requires an actual race (named, in the
  calendar, or clearly real) - NOT hypothetical or whimsical extrapolations, which go to athlete_twin.
- injury_prevention: pain, injury risk, overtraining/overreaching, CURRENT fatigue and recovery status (HRV, resting
  HR, sleep, heart-rate drift right now), and biomechanics questions - e.g. "am I overtraining?", "my knee hurts",
  "am I recovered?".
- history_analyst: reports and analysis of PAST periods or the athlete's whole history from the database - trends
  over weeks/months/seasons/years (HRV, resting HR, sleep, weight, CTL/fitness, ATL/fatigue, TSB/form, volume),
  comparisons between periods, records, "when did I last...", which training built fitness, past races and pacing -
  e.g. "how did my HRV look last summer?", "what workouts boosted my fitness two years ago?", "which was the last
  race where my power didn't drop?", "compare my training volume in 2023 vs 2024". Not for future projections
  (athlete_twin) or questions about one specific recent workout (ai_coach).
- athlete_twin: "what-if" scenarios, hypothetical simulations, and playful or absurd extrapolations
  from real data - e.g. "how long would it take me to ride to the moon at my average speed", or any
  question that starts from real numbers but imagines a scenario that isn't a real planned event.
  Also ALL projections of future fitness/fatigue/form (CTL/ATL/TSB) under a hypothetical load or plan -
  e.g. "where will my CTL be after 4 weeks at 350 TSS/week?", "what happens to my form if I rest 5 days?".
- ai_coach: the primary coach - general training advice, workout suggestions, and analysis of past
  activities. Default here for anything about a specific past workout. Also ALL requests to save, update, change or
  swap the planned next session ("update my plan", "make that my next session"), and moving or swapping
  sessions between days ("do the long ride Saturday instead of Tuesday"). Also ALL statements about the
  athlete's availability - sick / ill and can't train, away or travelling, no time, or only time for a shorter
  session ("I'm sick, no training for 3 days", "away until Sunday", "only 40 min today") - even when illness is
  mentioned, these go to ai_coach (it saves the dates), not injury_prevention.

Recent conversation (oldest first; [name] is the agent that answered that turn):
{history}

Route based on the LATEST user message below. Use the conversation only to understand it: a short
follow-up that continues the previous topic (e.g. "and tomorrow?", "why?", "make it shorter") should
normally go to the same agent that answered the previous turn.

Latest user message: "{user_input}"
"""

LIFE_LINE_ENABLED = (
    "\n- life_optimization: real-world logistics, weather, or personal schedule (Google Calendar) questions."
)
LIFE_LINE_DISABLED = ""


def supervisor_node(state: AgentState):
    prior, user_input = split_state(state)
    forced = state.get("forced_agent")
    if forced:
        if forced in AGENT_NAMES:
            logger.info(f"Supervisor bypassed - agent forced to '{forced}' for query: {user_input!r}")
            return {"next_agent": forced}
        logger.warning(f"Ignoring forced agent '{forced}' (disabled/unknown); routing normally.")
    logger.info(f"Supervisor routing query: {user_input!r} ({len(prior)} prior messages)")
    try:
        prompt = SUPERVISOR_PROMPT.format(
            user_input=user_input,
            history=format_for_supervisor(prior),
            life_line=LIFE_LINE_ENABLED if LIFE_AGENT_ENABLED else LIFE_LINE_DISABLED,
        )
        decision = structured_supervisor.invoke(prompt)
        next_agent = decision.agent
        logger.info(f"Supervisor routed to '{next_agent}' - reasoning: {decision.reasoning}")
    except Exception as e:
        logger.warning(f"Supervisor structured routing failed ({e}); defaulting to ai_coach.")
        next_agent = "ai_coach"
    if next_agent not in AGENT_NAMES:
        logger.warning(f"Supervisor picked disabled/unknown agent '{next_agent}'; rerouting to ai_coach.")
        next_agent = "ai_coach"
    return {"next_agent": next_agent}


def local_or_cloud(local_fn, cloud_fn):
    """Graph node that answers with the local model when the request set local_only."""
    def node(state: AgentState):
        return local_fn(state) if state.get("local_only") else cloud_fn(state)
    return node


def log_agent_call(agent_name: str, original_function):
    def wrapper(state: AgentState):
        logger.info(f"--- Calling {agent_name} Agent ---")
        result = original_function(state)
        logger.info(f"--- {agent_name} Agent Finished ---")
        return result
    return wrapper


def call_life_wrapper(state: AgentState):
    query = state["messages"][-1].content
    result = run_life_agent(query)
    return {"messages": [AIMessage(content=result)]}


# --- Build the LangGraph Workflow ---
workflow = StateGraph(AgentState)

workflow.add_node("preprocessor", global_preprocessor)
workflow.add_node("supervisor", supervisor_node)
workflow.add_node("ai_coach", log_agent_call("AI Coach", local_or_cloud(call_coach_local, call_ai_coach)))
workflow.add_node("racing_strategy", log_agent_call("Racing Strategy", local_or_cloud(call_racing_local, call_racing)))
workflow.add_node("injury_prevention", log_agent_call("Injury Prevention", call_injury))
if LIFE_AGENT_ENABLED:
    workflow.add_node("life_optimization", log_agent_call("Life Optimization", call_life_wrapper))
workflow.add_node("athlete_twin", log_agent_call("Athlete Twin", local_or_cloud(call_twin_local, call_twin)))
workflow.add_node("history_analyst", log_agent_call("History Analyst", local_or_cloud(call_history_local, call_history)))

workflow.set_entry_point("preprocessor")
workflow.add_edge("preprocessor", "supervisor")

workflow.add_conditional_edges(
    "supervisor",
    lambda state: state["next_agent"],
    {name: name for name in AGENT_NAMES},
)

for node in AGENT_NAMES:
    workflow.add_edge(node, END)

agent_platform = workflow.compile()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    logger.info("Initializing ai-coach-v1 graph for a test run...")

    test_query = "What should I do for my training tomorrow?"
    initial_state = {"messages": [HumanMessage(content=test_query)]}

    logger.info(f"User Query: {test_query}")
    final_state = agent_platform.invoke(initial_state)
    logger.info(f"Final AI Response: {final_state['messages'][-1].content}")
