"""
Agent tools shared between specialists (ai_coach, athlete_twin, racing_strategy), so they read
history and run simulations through exactly the same code.
"""
import json

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from core.history_query import SCHEMA_GUIDE, query_database
from core.history_stats import (
    GROUP_BYS, SPORT_FILTERS, WELLNESS_METRICS,
    fitness_gain_windows, race_history, training_summary, wellness_summary,
)
from core.metrics import search_historical_activities, get_activity_details, get_historical_wellness_data
from core.pacing import analyze_power_pacing
from core.simulators import project_training_load, estimate_ride_time
from core.races import list_upcoming_races
from core.race_planner import build_race_plan, review_race

historical_tool = StructuredTool.from_function(
    func=search_historical_activities,
    name="search_historical_activities",
    description="Search the athlete's entire database history for specific lifetime achievements (e.g., marathons, longest rides) based on sport type and minimum distance.",
)

activity_details_tool = StructuredTool.from_function(
    func=get_activity_details,
    name="get_activity_details",
    description=(
        "Fetch the full interval-by-interval, power-zone, and HR-zone breakdown for one specific "
        "activity, given its external_id (visible in the `activities` list in metrics_context). "
        "Call this whenever the user asks for an extensive/detailed/interval-by-interval breakdown "
        "of a specific ride or run - the default metrics_context only has summary stats, not "
        "interval-level detail."
    ),
)

class HistoricalWellnessArgs(BaseModel):
    start_date: str = Field(description="The start date for the historical data query.", json_schema_extra={"format": "YYYY-MM-DD"})
    end_date: str = Field(description="The end date for the historical data query.", json_schema_extra={"format": "YYYY-MM-DD"})


historical_wellness_tool = StructuredTool.from_function(
    func=get_historical_wellness_data,
    name="get_historical_wellness_trend",
    description=(
        "Fetch historical daily wellness metrics (HRV, resting HR, sleep, CTL/ATL/TSB) over a specific date "
        "range to analyze seasonal trends or past baselines."
    ),
    args_schema=HistoricalWellnessArgs,
)

def _as_json(fn):
    """Calculator results go back to the model as JSON; bad input becomes a readable error."""
    def run(**kwargs) -> str:
        try:
            return json.dumps(fn(**kwargs), default=str)
        except ValueError as e:
            return json.dumps({"error": str(e)})
    return run


_DATE = "YYYY-MM-DD"
_GROUP_BY = f"Series granularity: one of {list(GROUP_BYS)} (coarsened automatically for long periods)."
_SPORT = f"One of {sorted(SPORT_FILTERS)}."


class WellnessSummaryArgs(BaseModel):
    metrics: list[str] = Field(description=f"Metrics to summarise, any of: {sorted(WELLNESS_METRICS)}.")
    start_date: str = Field(description=f"Period start ({_DATE}).")
    end_date: str = Field(description=f"Period end ({_DATE}).")
    group_by: str = Field("week", description=_GROUP_BY)
    compare_start_date: str | None = Field(None, description=f"Optional comparison period start ({_DATE}).")
    compare_end_date: str | None = Field(None, description=f"Optional comparison period end ({_DATE}).")


wellness_summary_tool = StructuredTool.from_function(
    func=_as_json(wellness_summary),
    name="wellness_summary",
    description=(
        "Locally computed statistics for daily wellness/fitness metrics (HRV, resting HR, sleep, CTL, ATL, TSB, "
        "ramp rate, weight, steps) over a period: data coverage, mean, median, SD, min/max with dates, first vs "
        "last 7 days, trend per week, a weekly/monthly series, and optionally the same stats for a comparison "
        "period with the difference. Use for any trend, baseline or 'compared to last year' question."
    ),
    args_schema=WellnessSummaryArgs,
)


class TrainingSummaryArgs(BaseModel):
    start_date: str = Field(description=f"Period start ({_DATE}).")
    end_date: str = Field(description=f"Period end ({_DATE}).")
    group_by: str = Field("month", description=_GROUP_BY)
    sport: str = Field("all", description=_SPORT)


training_summary_tool = StructuredTool.from_function(
    func=_as_json(training_summary),
    name="training_summary",
    description=(
        "Locally computed training totals for a period and per week/month/year: activities, hours, km, TSS, hours "
        "per sport, workout-type mix (steady endurance Z2 / steady tempo / steady sweet spot-threshold = "
        "continuous efforts with variability index <= 1.05; variable endurance, intervals / hard variable, "
        "race / threshold+, strength, HR-based classes without power) and CTL at start/end and its change."
    ),
    args_schema=TrainingSummaryArgs,
)


class FitnessGainArgs(BaseModel):
    start_date: str = Field(description=f"Period to search ({_DATE}).")
    end_date: str = Field(description=f"Period end ({_DATE}).")
    window_days: int = Field(28, description="Length of each window in days (7-120).")
    top: int = Field(3, description="How many non-overlapping windows to return (1-5).")
    sport: str = Field("all", description=f"Which training to break down in each window. {_SPORT}")


fitness_gain_tool = StructuredTool.from_function(
    func=_as_json(fitness_gain_windows),
    name="fitness_gain_windows",
    description=(
        "Finds the windows in a period where CTL (fitness) rose the most and returns the training done in each "
        "(hours, TSS, sport and workout-type mix), plus CTL start/end/peak and the whole period's training. Use "
        "for 'what boosted / built my fitness' questions."
    ),
    args_schema=FitnessGainArgs,
)


class RaceHistoryArgs(BaseModel):
    before_date: str | None = Field(None, description=f"Only races before this date ({_DATE}); use for paging back.")
    after_date: str | None = Field(None, description=f"Only races after this date ({_DATE}).")
    limit: int = Field(5, description="Races per call, newest first (1-10).")
    include_pacing: bool = Field(True, description="Add the power pacing analysis (quarters, fade verdict) per race.")


race_history_tool = StructuredTool.from_function(
    func=_as_json(race_history),
    name="race_history",
    description=(
        "Past races newest first (Intervals.icu race flag or race-like names) with duration, distance, elevation, "
        "NP, IF, TSS, HR decoupling and - with include_pacing - the locally computed power pacing per race "
        "(NP per quarter, verdict held / faded / finished stronger). For 'last race where...' questions, page "
        "back with before_date until a race matches."
    ),
    args_schema=RaceHistoryArgs,
)


class QueryDatabaseArgs(BaseModel):
    sql: str = Field(description="One PostgreSQL SELECT (or WITH ... SELECT) statement, without a trailing ';'.")


query_database_tool = StructuredTool.from_function(
    func=query_database,
    name="query_database",
    description=(
        "LAST RESORT for lookups the other history tools don't cover (records, 'when did I last...', a specific "
        "filter): run one read-only SQL SELECT against the athlete's full local history and get the rows back as "
        "JSON (max 200 rows). Do every calculation INSIDE the SQL (AVG, SUM, COUNT, MIN/MAX, differences, "
        "ROUND(x::numeric, 1)) so the database computes it - never compute numbers yourself from returned rows. "
        "On an SQL error, read the message, fix the query and call again.\n\n" + SCHEMA_GUIDE
    ),
    args_schema=QueryDatabaseArgs,
)


class PowerPacingArgs(BaseModel):
    external_ids: list[str] = Field(description="Up to 10 activity external_ids (e.g. 'i123456789') to analyse.")


power_pacing_tool = StructuredTool.from_function(
    func=lambda external_ids: json.dumps(analyze_power_pacing(external_ids)),
    name="analyze_power_pacing",
    description=(
        "Power pacing / fade check from the second-by-second power stream: normalized power for each quarter of "
        "the activity, NP of the last 10%, the last quarter vs the strongest earlier quarter in %, and a verdict "
        "(held / faded below -5% / finished stronger above +5%). A `note` flags recordings that include a "
        "warm-up or the ride to the start. Use for 'did my power drop before the end', pacing, or 'last race where "
        "I held my power' - find candidate activities with query_database first, then pass their external_ids "
        "(newest first, up to 10 per call)."
    ),
    args_schema=PowerPacingArgs,
)

training_load_projection_tool = StructuredTool.from_function(
    func=project_training_load,
    name="project_training_load",
    description=(
        "Projects CTL (fitness), ATL (fatigue) and TSB (form) forward from the athlete's latest real "
        "values for a planned weekly TSS schedule. Use for ANY question about future fitness/fatigue/form: "
        "build blocks, tapers, race-day freshness, missed weeks (TSS 0), 'where will my CTL be if...'. "
        "Also reports the athlete's actual average weekly TSS over the last 4 weeks, a good baseline for "
        "'at my current load' scenarios."
    ),
)

ride_time_tool = StructuredTool.from_function(
    func=estimate_ride_time,
    name="estimate_ride_time",
    description=(
        "Physics-based estimate of time and speed for a climb or ride segment at a steady power "
        "(watts or % of FTP), given distance and average gradient. Use for ANY climb-time, segment-time, "
        "speed or W/kg 'what if I weighed/produced X' question - call once per scenario to compare."
    ),
)

upcoming_races_tool = StructuredTool.from_function(
    func=list_upcoming_races,
    name="list_upcoming_races",
    description=(
        "Lists the athlete's upcoming races from their Intervals.icu calendar (Race A/B/C events) with id, date, "
        "priority, type, distance, elevation, location, goal and which details are missing. Call this first for "
        "any race question unless the athlete gave all race details in the message."
    ),
)

race_plan_tool = StructuredTool.from_function(
    func=build_race_plan,
    name="build_race_plan",
    description=(
        "Computes the complete race plan data for one race: predicted time, pacing targets per terrain (power, HR, "
        "RPE), fuelling (carbs, fluid, sodium, caffeine for this athlete), taper with projected race-morning form, "
        "race-week schedule, weather, and past races for reference. Pass race_id from list_upcoming_races, and/or "
        "the race details directly (distance_km, elevation_m, race_type, race_date, priority, location, goal)."
    ),
)

race_review_tool = StructuredTool.from_function(
    func=review_race,
    name="review_race",
    description=(
        "Post-race analysis of a completed race (activity external_id like 'i123456789', or a date YYYY-MM-DD): "
        "intensity vs what is sustainable for the duration, HR vs LTHR, fade (decoupling), zones, W' use, "
        "fuelling and heat."
    ),
)


def build_propose_session_tool(turn_id: str) -> StructuredTool:
    """Tool for ai_coach in a live chat turn only (not the briefing or automatic decisions):
    drafts a change to the shared next session. It changes nothing by itself - the athlete
    gets Apply/Discard buttons and only Apply replaces the decision (core/next_session.py)."""
    from core.next_session import ProposalError, propose

    def propose_next_session_change(session: str, why: str) -> str:
        try:
            p = propose(turn_id, session, why)
        except ProposalError as e:
            return f"Could not propose the change: {e}"
        return (f"Proposal #{p['id']} saved as PENDING. Nothing has changed yet: the athlete now sees Apply / "
                "Discard buttons under your reply, and only Apply updates the dashboard, morning briefing and "
                "Strava notes. Tell them that in one short line - do not claim the plan is already updated.")

    return StructuredTool.from_function(
        func=propose_next_session_change,
        name="propose_next_session_change",
        description=(
            "Draft a change to the athlete's saved next session (`next_session_decided`) for them to approve. "
            "Call it ONLY when the athlete explicitly asks to update, save, change or swap their planned/next "
            "session (e.g. 'update my plan', 'save that', 'swap today's run for the ride we discussed'). NEVER "
            "call it on your own initiative, and never just because you suggested a different session - then "
            "offer it instead ('want me to make that your next session?'). `session`: one line with sport, "
            "duration and intensity/targets (watts for rides, HR or feel for runs) or 'Rest'. `why`: one short "
            "reason, e.g. 'Swapped the run for a ride at the athlete's request'."
        ),
    )
