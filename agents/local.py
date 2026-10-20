"""
"Local only" versions of the Gemini agents (ai_coach, athlete_twin, racing_strategy),
used when a front-end sets `local_only` on the graph state.

The local model (an 8B model on an 8 GB GPU) is weak at choosing and chaining tools,
so these never let it drive tools: code gathers the data and runs the simulators /
race planner, and the model only writes the answer from that finished context - the
same pattern as the injury agent. What can't be done that way (open-ended
multi-source analysis, knowledge about specific events) is stated in the answer.
"""
import logging
from datetime import date
from typing import Literal, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from core.history import split_state
from core.injury_checks import SLEEP_MIN_HOURS, format_injury_report, run_injury_checks
from core.llm import local_llm
from core.focus import get_focus
from core.plan import RIDE_TYPES, plan_summary
from core.race_planner import build_race_plan
from core.races import fetch_upcoming_races, list_upcoming_races
from core.simulators import estimate_ride_time, project_training_load

logger = logging.getLogger("AI_COACH_V1.local")

writer = local_llm("local_writer", temperature=0.3, num_predict=700)
extractor = local_llm("local_extractor", temperature=0)

LIMITS_NOTE = ("If the question needs data that is not in the context below (older history, interval-level detail of "
               "a ride, knowledge about a specific race course), say so in one sentence and suggest turning off "
               "'Local only' for a full answer.")


def _answer(system: str, context: str, state) -> str:
    prior, question = split_state(state)
    messages = [SystemMessage(content=f"{system}\n\n{LIMITS_NOTE}\n\nToday's date is {date.today().isoformat()}."),
                *prior, HumanMessage(content=f"{question}\n\n<context>\n{context}\n</context>")]
    return str(writer.invoke(messages).content).strip()


def _safe(label: str, fn, *args, **kwargs) -> str:
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning(f"Local context '{label}' failed: {e}")
        return f"({label} unavailable: {e})"


def _intensity(a: dict) -> str | None:
    ifactor = a.get("intensity_factor")
    if not ifactor or a.get("avg_power_w") is None:
        return None
    if ifactor < 0.75:
        return "easy/endurance"
    if ifactor < 0.85:
        return "tempo"
    if ifactor < 0.95:
        return "sweet spot/threshold"
    return "very hard"


def _drift(a: dict) -> str | None:
    d = a.get("decoupling_pct")
    if d is None or (a.get("duration_hours") or 0) < 1:
        return None
    if d < -5:
        return f"HR drift {d:.1f}% (implausible - likely HR strap glitch)"
    if d <= 5:
        return f"HR drift {d:.1f}% (good aerobic durability)"
    return f"HR drift {d:.1f}% ({'significant' if d > 10 else 'mild'} - fatigue, heat or dehydration)"


def _metrics(state) -> str:
    """Compact plain-text athlete summary (far fewer tokens than the raw JSON), with the
    interpretation done in code so the small model doesn't have to judge numbers."""
    m = state.get("metrics_context", {})
    w = m.get("wellness") or {}
    sleep_h = round(w["sleep_duration_sec"] / 3600, 1) if w.get("sleep_duration_sec") else None
    lines = [
        f"Now: {m.get('current_local_time')} (athlete's local time).",
        f"FTP {m.get('ftp')} W, eFTP {m.get('eftp')} W, weight {m.get('athlete_weight')} kg, threshold HR {m.get('fthr')} bpm.",
        f"Fitness (CTL) {m.get('ctl') or 0:.1f}, fatigue (ATL) {m.get('atl') or 0:.1f}, form (TSB) {m.get('tsb')}, ramp {m.get('ramp_rate') or 0:.1f}/week.",
        f"Wellness today: HRV {w.get('hrv')}, resting HR {w.get('resting_hr')}, sleep score {w.get('sleep_score')}, sleep {sleep_h} h.",
    ]
    if m.get("ftp_check"):
        lines.append(f"FTP check: {m['ftp_check']}")
    r = m.get("readiness")
    if r:
        lines.append(f"READINESS: {r['level'].upper()} - {r['verdict']}.")
    nd = m.get("next_session_decided")
    if nd:
        lines.append(f"Next session ALREADY DECIDED after {nd['after_activity']} (the athlete has seen it): "
                     + " ".join(nd["decision"].split())
                     + (" (the athlete chose this one themselves - keep it unless unsafe)" if nd.get("decided_by") == "athlete" else ""))
    ftp = m.get("ftp")
    if ftp:
        lines.append(f"Power zones from FTP {ftp} W: Zone 2 {0.56 * ftp:.0f}-{0.75 * ftp:.0f} W, tempo {0.76 * ftp:.0f}-{0.87 * ftp:.0f} W, "
                     f"sweet spot {0.88 * ftp:.0f}-{0.94 * ftp:.0f} W, threshold {0.95 * ftp:.0f}-{1.05 * ftp:.0f} W, "
                     f"VO2max {1.06 * ftp:.0f}-{1.20 * ftp:.0f} W.")
    rides = [a for a in m.get("activities", []) if a.get("type") in RIDE_TYPES]
    if rides:
        lines.append("MOST RECENT RIDE (use this line, and only this line, when asked about the last ride): "
                     + _activity_line(rides[0]))
    lines.append("Recent activities, newest first (walks are not training):")
    lines += ["- " + _activity_line(a) for a in m.get("activities", [])]
    return "\n".join(lines)


def _activity_line(a: dict) -> str:
    parts = [str(a.get("start_time"))[:10], a.get("type"), f"'{a.get('name')}'",
             f"{a.get('duration_hours')} h", f"{a.get('distance_km')} km" if a.get("distance_km") else None,
             f"TSS {a.get('training_load'):.0f}" if a.get("training_load") is not None else None,
             f"NP {a.get('normalized_power_w'):.0f} W, IF {a.get('intensity_factor'):.2f} ({_intensity(a)})" if _intensity(a) else None,
             f"avg HR {a.get('avg_hr')}" if a.get("avg_hr") else None,
             a.get("gym_focus"),
             _drift(a) or ("HR drift not measured (ride under 1 h)" if a.get("type") in RIDE_TYPES else None)]
    return ", ".join(p for p in parts if p)


# --- coach ---

COACH_PROMPT = """You are an experienced endurance coach ({focus}; plus strength) answering your
athlete. Be practical, specific and brief (under 200 words), in Markdown.

Rules, in priority order:
1. If the athlete asks for rest/easy/hard/short/long, follow that.
2. READINESS RED, poor sleep (sleep_score < 70 or under {sleep_min_hours:g} h), 3+ days without training, or a HIGH finding in the
   injury report: only rest or an easy Zone 1-2 session, no intervals. READINESS AMBER: no VO2max, at most a
   controlled tempo or sweet spot session. CAUTION findings (such as HR drift) do NOT cancel the
   intervals the plan calls for - mention them briefly and keep the plan's session.
3. Follow this week's training plan: its focus, phase (deload week = no intervals), TSS target vs load so far, and
   the sessions still to do. If the next session falls in a new week, plan for THAT week. Weights can be on the same day as a ride; never hard leg work
   the day after a leg session. Only a gym session marked "legs worked" counts as one - any other gym session
   did not load the legs and is no reason to go easier on the bike or run.
4. When suggesting the next session, give ONE definitive session with duration and intensity: rides in watts
   from the power zones in the context (copy the watt ranges exactly); runs by heart rate
   or feel (easy = conversational), never watts; weights as a short description.
5. Pick ride sessions from this library, with watts from the context's power zones and the zone names as written:
   Endurance: 60-120 min in Zone 2. Tempo: 2x20 min tempo. Sweet spot: 3x15 min sweet spot, 5 min easy between.
   Threshold: 3x10 min threshold, 5 min easy between. VO2max: 5x4 min VO2max, 4 min easy between.
   Each interval session also has 15 min warm-up and 10 min cool-down in Zone 2.
Use only numbers and labels from the context (intensity and HR-drift labels are already computed - repeat them,
don't re-judge them); never invent data.
If a next session is ALREADY DECIDED in the context, stick to it unless the athlete's request changes things - then
say what changed. Call it "your next session", not "tomorrow" (early in the day, the next session is usually today).
You cannot save or change the decided next session. If the athlete asks you to update or save it, say that
saving a change needs Local only switched off, and never claim it is updated.{run_rules}"""

DUAL_RUN_RULES = """
Dual-sport winter block - runs matter as much as rides:
6. Pick run sessions from: Easy: 30-50 min conversational. Long run: 60-90 min easy. Tempo: 2x12 min at threshold
   heart rate, 3 min jog between. Hills: 8x60 s uphill hard, jog down. Quality runs get 10 min easy warm-up/cool-down.
7. Never a hard run the day after a hard ride or a leg gym session ("legs worked") (or the reverse) - alternate hard bike and hard run days."""


def call_coach_local(state):
    context = "\n\n".join([
        f"Athlete:\n{_metrics(state)}",
        f"Training plan:\n{_safe('plan', plan_summary)}",
        f"Injury/overreaching report:\n{_safe('injury report', lambda: format_injury_report(run_injury_checks()))}",
    ])
    focus = get_focus()
    prompt = COACH_PROMPT.format(focus=focus["short"], sleep_min_hours=SLEEP_MIN_HOURS, run_rules=DUAL_RUN_RULES if focus["mode"] == "dual" else "")
    return {"messages": [AIMessage(content=_answer(prompt, context, state))]}


# --- twin ---

class ScenarioKind(BaseModel):
    kind: Literal["load_projection", "ride_time", "other"]


class Block(BaseModel):
    weeks: int = Field(description="number of weeks in this block")
    weekly_tss: Optional[float] = Field(None, description="TSS per week, if given in TSS (0 = rest)")
    weekly_hours: Optional[float] = Field(None, description="hours per week, if given in hours instead of TSS")


class LoadScenario(BaseModel):
    blocks: list[Block] = Field(description="only the blocks the athlete describes, in order")
    target_date: Optional[str] = Field(None, description="a specific calendar date named in the question (e.g. race day 2026-11-20), else null")


class RideScenario(BaseModel):
    distance_km: float = Field(description="distance in km")
    avg_gradient_pct: Optional[float] = Field(None, description="average gradient in %, if stated")
    power_w: Optional[float] = Field(None, description="power in watts, if stated")
    pct_ftp: Optional[float] = Field(None, description="power as % of FTP, if stated (e.g. 85)")
    weight_change_kg: Optional[float] = Field(None, description="change in body weight, e.g. 'lost 5 kg' -> -5, 'gained 2 kg' -> 2")


# Few-shot prompts: the 8B model classifies/extracts reliably with examples, poorly from schema descriptions alone.
CLASSIFY_PROMPT = """Classify the athlete's what-if question into exactly one kind:
- load_projection: how fitness, fatigue or form (CTL, ATL, TSB) will change under a training load, a block, a taper or rest days.
  Examples: "Where will my CTL be after 4 weeks at 350 TSS/week?", "What happens to my form if I rest 5 days?", "If I train 8 hours a week until May, how fit will I be?"
- ride_time: how long a ride, climb or segment takes, or how fast, at some power, %FTP or body weight.
  Examples: "How long would Alpe d'Huez take me at 250 W?", "What if I lost 5 kg - how much faster up a 10 km 7% climb?", "How fast could I do a 40 km TT at 280 W?"
- other: anything else, including absurd or playful extrapolations.
  Examples: "How long would it take me to ride to the moon at my average speed?", "How many pizzas did I burn this year?"

Question: """

LOAD_PROMPT = """Extract the training blocks from the athlete's question. A taper without numbers = 1 week at half the
previous load. A week off = weekly_tss 0.

Examples:
'4 weeks at 350 TSS' -> blocks [{weeks: 4, weekly_tss: 350}], target_date null
'8 hours a week for 3 weeks' -> blocks [{weeks: 3, weekly_hours: 8}], target_date null
'6 weeks at 400 then a taper, race on 2026-12-01' -> blocks [{weeks: 6, weekly_tss: 400}, {weeks: 1, weekly_tss: 200}], target_date 2026-12-01

Question: """

RIDE_PROMPT = """Extract the ride parameters from the athlete's question.

Examples:
'Alpe d'Huez (13.8 km at 8.1%) at 250 W' -> distance_km 13.8, avg_gradient_pct 8.1, power_w 250
'lost 5 kg, how much faster up a 10 km 7% climb at 260 W' -> distance_km 10, avg_gradient_pct 7, power_w 260, weight_change_kg -5
'a 40 km flat TT at 90% of FTP' -> distance_km 40, avg_gradient_pct 0, pct_ftp 90

Question: """


def _run_scenario(question: str, metrics: dict) -> str | None:
    """Classifies the what-if and runs the matching simulator. None = nothing computable."""
    kind = extractor.with_structured_output(ScenarioKind).invoke(CLASSIFY_PROMPT + question).kind
    if kind == "load_projection":
        sc = extractor.with_structured_output(LoadScenario).invoke(LOAD_PROMPT + question)
        rate = _tss_per_hour(metrics)
        plan, notes = [], []
        for b in sc.blocks:
            tss = b.weekly_tss
            if b.weekly_hours and not tss:
                if not rate:
                    return None
                tss = b.weekly_hours * rate
                notes.append(f"{b.weekly_hours:g} h/week converted at the recent average of {rate:g} TSS/hour = {tss:.0f} TSS/week")
            plan += [float(tss or 0)] * max(1, min(b.weeks, 52))
        if not plan:
            return None
        report = sc.target_date if sc.target_date and sc.target_date > date.today().isoformat() else None
        return "\n".join(notes + [project_training_load(plan, 5, report)])
    if kind == "ride_time":
        sc = extractor.with_structured_output(RideScenario).invoke(RIDE_PROMPT + question)
        params = sc.model_dump(include={"avg_gradient_pct", "power_w", "pct_ftp"}, exclude_none=True)
        weight = metrics.get("athlete_weight")
        if sc.weight_change_kg and weight:
            return "\n\n".join([
                f"At current weight ({weight} kg):\n" + estimate_ride_time(sc.distance_km, rider_weight_kg=weight, **params),
                f"At {weight + sc.weight_change_kg:g} kg:\n"
                + estimate_ride_time(sc.distance_km, rider_weight_kg=weight + sc.weight_change_kg, **params),
            ])
        return estimate_ride_time(sc.distance_km, **params)
    return None


TWIN_PROMPT = """You are the athlete's Digital Twin: you explain simulations and what-if scenarios based on their
real data. Be brief (under 180 words) and in Markdown. If a simulation result is in the context, explain what it
means and use ONLY its numbers - never compute your own. State the key assumptions (TSS plan, power, weight).
If there is NO simulation result in the context, do not give any computed time, speed, CTL/ATL/TSB or other
simulated number: answer qualitatively from the athlete's data, or - for playful questions - give a clearly labelled
rough back-of-the-envelope estimate showing the simple arithmetic."""


def _tss_per_hour(metrics: dict) -> float | None:
    acts = [a for a in metrics.get("activities", []) if a.get("training_load") and a.get("moving_time_sec")]
    hours = sum(a["moving_time_sec"] for a in acts) / 3600
    return round(sum(a["training_load"] for a in acts) / hours, 0) if hours else None


def call_twin_local(state):
    _, question = split_state(state)
    metrics = state.get("metrics_context", {})
    try:
        result = _run_scenario(question, metrics)
    except Exception as e:
        logger.warning(f"Scenario simulation failed: {e}")
        result = None

    context = f"Athlete:\n{_metrics(state)}"
    if result:
        context += f"\n\nSimulation result (exact, computed by code):\n{result}"
    answer = _answer(TWIN_PROMPT, context, state)
    if result:
        answer += f"\n\n```\n{result}\n```"
    return {"messages": [AIMessage(content=answer)]}


# --- racing ---

RACING_PROMPT = """You are the athlete's race strategist. The athlete sweats heavily and is a habitual high caffeine
user; default carb tolerance 60-90 g/h. Standard guidance for this athlete when no race plan data is given:
breakfast 2.5-3 h before the start (amounts in the context), low fat and fibre; 500 ml fluid with sodium (heavy
sweater) in the 2 h before; DO drink the usual morning coffee as normal (a habitual user who skips it gets
withdrawal symptoms), then take the pre-start caffeine dose from the context 45-60 min before the start; 60-90 g carbs per hour during the race. Answer in concise Markdown (under 250 words). If race plan data is in the
context, build the answer from it - pacing, fuelling & hydration, taper, conditions, key risks - using ONLY its
numbers (watts, HR, grams, ml, mg, times). Without race data, give general guidance and say that adding the race
to the Intervals.icu calendar (Distance / Elevation / Location in the description) enables an exact plan.
Post-race reviews are not available in local mode."""


def call_racing_local(state):
    races_text = _safe("races", list_upcoming_races)
    weight = state.get("metrics_context", {}).get("athlete_weight") or 70
    context = (f"Athlete:\n{_metrics(state)}\n\n{races_text}\n\nRace-morning amounts for {weight} kg (computed): "
               f"breakfast {weight:.0f}-{3 * weight:.0f} g carbs (1-3 g/kg), pre-start caffeine about {3 * weight:.0f} mg (3 mg/kg).")
    try:
        races = fetch_upcoming_races()
    except Exception:
        races = []
    if races:
        nxt = next((r for r in races if r.get("priority") == "A"), races[0])
        context += f"\n\nRace plan data for {nxt.get('name')} (exact, computed by code):\n" + _safe("race plan", build_race_plan, race_id=str(nxt["id"]))
    return {"messages": [AIMessage(content=_answer(RACING_PROMPT, context, state))]}
