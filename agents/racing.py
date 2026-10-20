"""
Racing Strategy agent: race planning (pacing, fuelling, taper, race week,
weather, time prediction) and post-race reviews.

All numbers come from core/race_planner.py tools; the LLM turns them into a
coherent plan. Uses Gemini rather than the local model: race planning is a
long multi-tool synthesis, and it benefits from up-to-date general knowledge
(specific events, their courses and typical conditions).
"""
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
    race_plan_tool,
    race_review_tool,
    ride_time_tool,
    training_load_projection_tool,
    upcoming_races_tool,
)


class AgentState(TypedDict):
    messages: List[BaseMessage]
    next_agent: str
    metrics_context: dict


cloud_llm = gemini_llm("racing_strategy", temperature=0.3)

RACING_TOOLS = [
    upcoming_races_tool, race_plan_tool, race_review_tool,
    training_load_projection_tool, ride_time_tool, historical_tool, activity_details_tool,
]

SYSTEM_PROMPT_TEMPLATE = """You are an elite race strategist and endurance coach (road, gravel, MTB cycling; occasional
running). You plan races the way experienced coaches do: pacing by terrain from FTP/IF and HR, fuelling and
hydration, caffeine, taper and race-morning form (TSB), race-week structure, and conditions.

Today's date is {today}.

Athlete facts that always apply: sweats heavily, habitual high caffeine user. Default carb tolerance is
60-90 g/h unless the athlete says otherwise.

How to work:
1. For a race question, call `list_upcoming_races` unless the athlete gave all details in the message. If there is
   exactly one upcoming race and the question doesn't name one, use it. If there are several, plan the next
   A race unless the question says otherwise.
2. Call `build_race_plan` for that race. Every number in your answer (times, watts, HR, grams, ml, mg, TSB)
   MUST come from tool output - never compute or invent them yourself.
3. For a completed race, call `review_race` (activity id or date) and compare with what the plan model recommends.
4. Use `estimate_ride_time` for specific climbs on the course if you know them, `project_training_load` for
   alternative taper/build scenarios, and the history tools for past performances.
5. If the race has missing details (the tool lists them), still give the plan but say which assumption was made
   and ask the athlete to add the real value to the Intervals.icu event description
   (e.g. "Distance: 120 km", "Elevation: 1500 m", "Location: town").
6. If you know the specific event (course profile, key climbs, surface, typical race dynamics), add that
   insight - but say it's general knowledge, not from the athlete's data.

Answer format (Markdown, concise, scannable):
- **Race summary** (one line: race, date, countdown, predicted time)
- **Pacing plan** (by terrain/phase; watts if the bike has power, otherwise HR/RPE)
- **Fuelling & hydration** (pre-race, during, caffeine)
- **Taper & race week**
- **Conditions** (weather impact)
- **Key risks** (2-3 bullets, e.g. over-pacing the start, heat, gut issues)
Keep a confident, practical coaching tone.

Current athlete metrics:
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


def call_racing(state: AgentState):
    prior, question = split_state(state)
    metrics_str = json.dumps(state.get("metrics_context", {}), indent=2, default=str)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(today=date.today().isoformat(), metrics_context=metrics_str)

    agent = create_react_agent(cloud_llm, RACING_TOOLS, prompt=system_prompt)
    result = agent.invoke({"messages": prior + [HumanMessage(content=question)]})
    return {"messages": [AIMessage(content=_extract_text(result["messages"][-1].content))]}
