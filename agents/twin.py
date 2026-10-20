import json
from datetime import date
from typing import List, TypedDict
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.prebuilt import create_react_agent

from core.llm import gemini_llm
from core.history import split_state
from agents.tools import (
    activity_details_tool,
    historical_tool,
    historical_wellness_tool,
    ride_time_tool,
    training_load_projection_tool,
)


class AgentState(TypedDict):
    messages: List[BaseMessage]
    next_agent: str
    metrics_context: dict


cloud_llm = gemini_llm("athlete_twin", temperature=0.7)

TWIN_TOOLS = [training_load_projection_tool, ride_time_tool, historical_tool, historical_wellness_tool, activity_details_tool]

SYSTEM_PROMPT_TEMPLATE = """You are a specialized Digital Twin AI for an athlete. Your role is to simulate future scenarios and analyze past data based on the provided performance metrics and activity data.

Today's date is {today}.

- You are the expert for "what if" scenarios and simulations.
- When asked to simulate, use the provided data as a baseline and create a plausible narrative. Clearly state that this is a simulation.
- When asked a factual question, you MUST answer based *strictly* on the provided `metrics_context` JSON data or on tool results.
- If the factual answer is NOT in the data and no tool can fetch it, you MUST state that the information is not available.
- Do NOT hallucinate facts for non-simulation questions.

**Tools - numbers must come from tools, never from mental arithmetic:**
- `project_training_load`: ANY future CTL/ATL/TSB (fitness/fatigue/form) question - build blocks, tapers, race-day form,
  missed weeks (use 0 TSS). Translate the scenario into a weekly TSS plan; if the user gives hours instead of TSS,
  estimate TSS from their recent activities (training_load per hour) and say so. For a race date, pass it as report_date.
- `estimate_ride_time`: ANY climb/segment time, speed, or W/kg comparison. Use well-known climb stats (distance,
  average gradient, altitude) when the user names a famous climb, and state the values you used. Call once per
  scenario when comparing (e.g. current weight vs. lighter).
- `search_historical_activities` / `get_activity_details`: when the scenario should be grounded in older history or
  in the detail of a specific ride - metrics_context only contains the latest day and the last few activities.
- `get_historical_wellness_trend`: daily HRV, resting HR, sleep and CTL/ATL/TSB for a date range (YYYY-MM-DD) - use it for
  seasonal trends, past baselines or "same time last year" comparisons.
Always mention the key assumptions behind a simulated number (TSS plan, power, weight, position).

Here is the user's data:
<metrics_context>
{metrics_context}
</metrics_context>
"""


def _extract_text(raw_content) -> str:
    if isinstance(raw_content, list):
        return "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in raw_content
        )
    return str(raw_content)


def call_twin(state: AgentState):
    prior, question = split_state(state)
    metrics_context = state.get("metrics_context", {})
    metrics_str = json.dumps(metrics_context, indent=2, default=str)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(today=date.today().isoformat(), metrics_context=metrics_str)

    agent = create_react_agent(cloud_llm, TWIN_TOOLS, prompt=system_prompt)
    result = agent.invoke({"messages": prior + [HumanMessage(content=question)]})

    return {"messages": [AIMessage(content=_extract_text(result["messages"][-1].content))]}
