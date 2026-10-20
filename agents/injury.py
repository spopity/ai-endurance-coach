import logging
from typing import List, TypedDict
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, HumanMessage
from langchain_core.tools import StructuredTool

from core.focus import get_focus
from core.history import split_state
from core.llm import local_llm
from core.injury_checks import run_injury_checks, format_injury_report, format_injury_markdown

logger = logging.getLogger("AI_COACH_V1.injury")


class AgentState(TypedDict):
    messages: List[BaseMessage]
    next_agent: str
    metrics_context: dict


# Local model on purpose: every number is already computed by core/injury_checks.py,
# so the LLM only has to explain a finished report - no up-to-date knowledge needed.
# num_predict caps the narration so the answer arrives in seconds, not minutes.
llm = local_llm("injury_prevention", temperature=0.2, num_predict=220)

SYSTEM_PROMPT = """You are the Injury Prevention & Recovery agent for an endurance athlete (mainly cycling:
road, gravel, MTB, indoor; some running).

You receive a risk report ALREADY computed from the athlete's data, with every check judged against the
athlete's own baselines. The full report is shown to the athlete below your answer, so do NOT repeat or list
the findings.

Write 2-4 sentences that directly answer the athlete's question in plain language:
- Give the bottom line (using the report's OVERALL level) and the single most important thing to do next.
- Use only facts from the report; never invent numbers or add problems the report doesn't flag.
- If the athlete describes pain or a specific complaint, address it and advise a physio or doctor for sharp,
  worsening or persistent pain - you are not a medical diagnosis.
No headings, no bullet points.
"""


def build_injury_report() -> str:
    try:
        return format_injury_report(run_injury_checks())
    except Exception as e:
        logger.error(f"Injury checks failed: {e}", exc_info=True)
        return f"Injury checks could not run: {e}"


def build_injury_check_tool(metrics_context: dict) -> StructuredTool:
    """Tool for ai_coach: the deterministic report as plain text - instant, no LLM call,
    so the coach can check risk before recommending a hard session. metrics_context is
    accepted for interface compatibility; the checks read their own history from the DB
    because baselines need months of data, not just the recent snapshot."""
    def check_injury_risk() -> str:
        return build_injury_report()

    return StructuredTool.from_function(
        func=check_injury_risk,
        name="check_injury_risk",
        description=(
            "Runs the athlete's injury/overreaching risk checks against their own baselines: training load "
            "(acute:chronic ratio, form, ramp rate, monotony, weeks without recovery), recovery (HRV, resting HR, "
            "sleep), cardiovascular drift (aerobic decoupling, efficiency trend), biomechanics (cadence vs personal "
            "norm per ride type, L/R balance), feel/RPE, and running volume. Call this BEFORE recommending any hard "
            "or high-intensity workout, and downgrade the recommendation if it reports HIGH findings."
        ),
    )


def call_injury(state: AgentState):
    """Graph node for direct injury/pain/recovery questions: a short local-LLM answer to the
    athlete's actual question, followed by the deterministic report (always exact)."""
    prior, question = split_state(state)
    try:
        result = run_injury_checks()
    except Exception as e:
        logger.error(f"Injury checks failed: {e}", exc_info=True)
        return {"messages": [AIMessage(content=f"Injury checks could not run: {e}")]}

    system = SYSTEM_PROMPT
    if get_focus()["mode"] == "dual":
        system += ("\nThe athlete is in a dual-sport winter block (cycling and running both primary), so running "
                   "volume is higher than usual - weigh running findings accordingly.\n")
    messages = [SystemMessage(content=system), *prior,
                HumanMessage(content=f"Athlete's question: {question}\n\nComputed risk report:\n{format_injury_report(result)}")]
    try:
        answer = str(llm.invoke(messages).content).strip()
    except Exception as e:
        logger.error(f"Ollama answer failed: {e}", exc_info=True)
        answer = ""
    content = f"{answer}\n\n{format_injury_markdown(result)}" if answer else format_injury_markdown(result)
    return {"messages": [AIMessage(content=content)]}
