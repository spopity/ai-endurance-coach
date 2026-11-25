# AI Coach v1: How the System Works

A self-hosted, multi-agent endurance coach. It pulls training data from Intervals.icu,
health data straight from Garmin, weight from Withings and names from Strava into a
Postgres database. Specialist
AI agents reason over that data. Results reach the athlete in three places: a web
dashboard, a Telegram bot, and coach notes written into Strava activity descriptions.

This document explains the overall design, the logic behind the main decisions, and then
lists **every file in the project** with what it does and how it affects the whole.

---

## 1. The big picture

```
 Garmin ──► Intervals.icu ──┐                      ┌──► Web dashboard (React, /)
 Garmin (health) ───────────┤                      │
 Withings ──────────────────┼──► ingest/ ──► Postgres ◄──┤
 Strava (names) ◄──────────►┘    (sync)    athlete_twin_v2 ├──► Telegram bot (briefing + chat)
                                              ▲            └──► Strava descriptions (coach notes)
                                              │
                              core/ (deterministic logic: metrics, readiness,
                              injury checks, plan, simulators, race planner)
                                              │
                              graph.py (LangGraph): preprocessor ─► supervisor ─► one agent
                                              │
                              agents/ (Gemini in the cloud, or the local Ollama model)
```

### Processes that run

| What | How it runs | Purpose |
|---|---|---|
| `app.py` | systemd `ai-coach-v1.service` (port 8000) | FastAPI: dashboard, JSON API, chat streaming, `/sync-data` |
| `telegram_bot.py` | systemd `ai-coach-telegram.service` | Morning briefing and check-in, today's workouts with Push to Intervals.icu, session feedback, chat, `/week`, `/workout`, `/strava` note editor |
| Data sync | host crontab, every 30 min: `POST /sync-data` | Pulls the last 5 days of activities and fitness from Intervals.icu and health data from Garmin |
| Strava notes | `ai-coach-strava-notes.timer`, every 10 min | Writes a coach note into each new activity's Strava description |
| Intervals.icu health | `ai-coach-intervals-health.timer`, every 15 min | Sends a Telegram alert when the API goes down or recovers |
| Sync check | `ai-coach-sync-check.timer`, 03:30 nightly | Re-imports the last 15 days from Garmin, then finds and imports any Intervals.icu records of the last 15 days the 5-day sync missed |
| Adaptive plan | `ai-coach-plan.timer`, 04:00 nightly | Recomputes `training_blocks` from the training done; Telegram message when this week's phase changes |
| Strava name sync | `ai-coach-strava-names.timer`, 04:15 nightly | Copies activity names from Strava (last 6 months) to Intervals.icu and the database |

### External services

- **Intervals.icu**: the source of truth for activities, fitness/fatigue (CTL/ATL),
  eFTP, VO2max, FTP/LTHR, and the race calendar.
- **Garmin Connect Personal API**: the only source of health data: sleep (score,
  stages, timing), HRV and HRV status, resting/sleeping HR, stress, Body Battery,
  respiration, SpO2, steps and daily activity, Fitness Age. Read-only; a personal API
  key (`GARMIN_PERSONAL_API_KEY`) is exchanged for a 24 h token. Garmin rate-limits
  token requests, so the token is shared by every process through
  `state/garmin_token.json` and renewed only shortly before it expires. The key itself expires (30 days to 1 year, chosen
  when it was created) and must then be re-created in Garmin Connect.
- **Gemini** (`gemini-pro-latest`): the cloud model for the AI Coach, Athlete Twin,
  Racing Strategy and Strava-note text.
- **Ollama** (`qwen3:8b` on a GTX 1070 in another LXC): the local model for routing,
  injury narration and "Local only" mode.
- **Claude** (Claude Code via the Claude Agent SDK on the athlete's subscription,
  `CLAUDE_CODE_OAUTH_TOKEN`): the default for the Coach, Twin, Racing and History chat answers.
  Same chat tools as Gemini (`core/claude_agent.py`). Gemini can be picked per chat, steps in when a Claude call
  fails, and is used whenever no Claude token is set.
- **Strava**: activity names (read) and coach notes in descriptions (write).
- **Withings**: body weight.
- **Open-Meteo**: race-day weather (free, no key).
- **Telegram**: the bot API.

---

## 2. Core design principles

These recurring ideas explain most of the code:

1. **Numbers come from code, words from the LLM.** Everything quantitative is computed
   deterministically: readiness, injury risk, CTL/ATL projections, ride-time physics,
   race pacing and fuelling, session classification, the facts block on Strava. The
   LLMs only explain, prioritise and phrase the result. This stops models from
   inventing numbers and lets a small local model do useful work.
2. **One source per fact, shared everywhere.** The dashboard, the Telegram briefing,
   the chat agents and the Strava notes all read the same modules (`core/overview.py`,
   `core/readiness.py`, `core/next_session.py`). They can't contradict each other.
3. **Intervals.icu's own models are trusted.** CTL/ATL/ramp rate are read from
   Intervals.icu rather than re-derived locally. The simulators use the same
   42-day/7-day model so projections line up with what Intervals.icu shows.
4. **Local first, cloud when it pays off.** The local model handles routing and
   narration of finished reports. Gemini handles open-ended multi-tool reasoning and
   anything that needs current world knowledge. Any front-end can force "Local only".
5. **Never lose the athlete's own data.** The full raw API payload is stored as JSONB
   next to the mapped columns. A Strava note replaces only its own earlier text in a
   description. `#nocoach` opts an activity out. The nightly sync check reports
   records that vanished upstream but never deletes them.

---

## 3. Data flow in detail

### 3.1 Ingest

- `ingest/import_intervals.py` and `ingest/import_wellness.py` fetch from the
  Intervals.icu REST API. `ingest/field_map*.py` maps payloads onto table rows.
  Rows are upserted on `(source, external_id)` for activities and on `day` for
  wellness. The wellness import writes only the fitness model, VO2max, blood pressure,
  body and subjective fields, never the health columns.
- `ingest/import_garmin.py` is the only writer of the health columns of `daily_metrics`
  (sleep, HRV, resting HR, stress, Body Battery, respiration, SpO2, steps, calories,
  intensity minutes). Garmin's values match what Intervals.icu used to relay, so the
  HRV/sleep baselines carry on. Per day: user summary, HR samples (for the average
  sleeping HR) and SpO2; sleep and HRV come in 28-day ranges. A day Garmin has no data
  for is never written, so older history stays. A Garmin failure doesn't stop the
  Intervals.icu sync.
- `ingest/import_profile.py` syncs FTP, LTHR, max HR, resting HR, weight and the latest
  eFTP into `athlete_profile`, so these values are edited in one place (Intervals.icu).
- `core/chat.run_sync()` runs all four in-process. It is called by `/sync-data` (cron),
  the dashboard and Telegram `/sync` buttons, and before the morning briefing.
- `scripts/check_intervals_sync.py` reconciles the last 15 days every night (`--full` for the whole history). Late
  uploads (a holiday synced a week later) fall outside the 5-day window otherwise.
- `scripts/sync_strava_names.py` treats Strava as the source of truth for names: you
  rename on Strava, and the name is written to Intervals.icu and the database. It only
  checks the last 6 months (`--full` for everything) to keep Strava calls down.
- Activities that only ever existed on Strava (Zwift, TrainingPeaks Virtual, old
  devices) were pushed to Intervals.icu once by `scripts/backfill_strava_history.py`
  (streams rebuilt as TCX, `external_id = strava_<id>`) and imported from there.

### 3.2 Database (`athlete_twin_v2`, Postgres 16 + TimescaleDB, separate container)

| Table | Holds |
|---|---|
| `activities` | One row per activity: about 50 mapped metrics, `raw_payload` JSONB, Strava id/note/note context, the next-session decision |
| `daily_metrics` | One row per day: Garmin health (sleep and stages, HRV and status, resting/sleeping HR, stress, Body Battery, respiration, SpO2, steps, `garmin_payload` JSONB), Intervals.icu CTL/ATL/ramp rate, eFTP, VO2max, Withings weight |
| `athlete_profile` | A single row: FTP, LTHR, max/resting HR, weight, eFTP, W′, peak power |
| `training_blocks` | One row per calendar week: phase, deload flag, target load range, focus, planned session mix |
| `athlete_settings` | Key/value choices made in the dashboard (the training focus) |
| `api_calls` | One row per outgoing call (Gemini, Ollama, Intervals.icu, Garmin, Strava, Telegram, Open-Meteo, Withings) with process, reason, duration, tokens and estimated cost |

The schema is defined only by `db/migrations/*.sql`, applied in order by
`scripts/create_database.py`.

### 3.3 The context every agent sees

`core/metrics.load_athlete_metrics()` builds one dict, `metrics_context`, containing the
profile, latest wellness, CTL/ATL/TSB/ramp, the last 5 activities and this week's
training block. Gym sessions carry a `gym_focus` label (see §4.6). `graph.py`'s
preprocessor then adds the local time, the `readiness` verdict and the stored
`next_session_decided`.

---

## 4. The decision logic

### 4.1 Routing (`graph.py`)

Every chat message goes through a LangGraph pipeline:

1. **Preprocessor** loads `metrics_context` (above).
2. **Supervisor** (local model, structured output) picks exactly one agent. It sees the
   recent conversation, so follow-ups like "and tomorrow?" stay with the same agent:
   - `racing_strategy`: real, scheduled races (pacing, fuelling, taper, race week,
     post-race review).
   - `injury_prevention`: pain, overtraining, current recovery, HRV/sleep/drift questions.
   - `history_analyst`: reports on any past period (HRV/sleep/CTL trends, what built
     fitness, records, past races and pacing). Numbers come from local calculators;
     Gemini only picks them and interprets. SQL fallback runs as the read-only
     `coach_reader` role. No local-only version.
   - `athlete_twin`: what-ifs, CTL/ATL projections, climb times, playful
     extrapolations.
   - `ai_coach`: everything else. It is the default and the fallback if routing fails.
   - (`life_optimization`: Google Calendar. Disabled until Calendar is set up.)
   A front-end can skip the supervisor by forcing an agent (Telegram `/twin`; the
   next-session decision forces `ai_coach`).
3. **The agent** answers. With `local_only` set, the Coach, Twin and Racing nodes
   switch to their local versions in `agents/local.py`. With `use_claude` set (the chat default
   when a Claude token exists), the Coach,
   Twin, Racing and History nodes keep their prompts but answer through
   `core/claude_agent.py`: Claude Code with exactly the tools the agent passes (as Gemini gets them,
   including proposals and availability in a live chat turn), every built-in tool off, no settings or CLAUDE.md loaded, an empty
   working directory and its own config dir (`state/claude`). If the Claude call fails, the
   agent's own Gemini call answers with the same prompt and tools, prefixed with a
   one-line note (`claude_or_gemini`).

### 4.2 The AI Coach's rules (`agents/ai_coach.py`)

A Gemini ReAct agent with tools (injury check, history search, interval detail).
Its prompt holds a ranked rulebook. In short:

- One definitive session, never a menu. Gym can share a day with a ride.
- **Gym sessions:** only a session whose title names legs counts as leg work, and that
  blocks hard leg work the next day. Other gym sessions never justify going easier.
- **Graded return to training** (`return_to_training`, computed by
  `core/periodization.return_status()`): 3–6 days off → a normal endurance session,
  then back to the plan; 7–13 → 2 easy sessions, then the plan at ~75% for that week;
  14+ → 3 easy sessions, then the (stepped-back) plan. One poor night (score < 70 or under
  6.5 h, `SLEEP_MIN_HOURS`) keeps the planned session with a warm-up test; 2+ poor nights in a
  row (or under 5 h) is a recovery signal in readiness.
- **Periodization:** follow the current training block's focus and weekly load range.
  A deload week never contains intervals. Base allows a weekly VO2 touch or sprints
  (under 10 min hard, inside an endurance ride); full VO2max sessions are Build-only.
  When the week runs short (`sessions_remaining` vs `days_left_this_week`), keep the key
  sessions (quality + long) and drop filler; never cram missed load.
- **Readiness adjusts the plan:** red means rest or easy only; amber keeps the planned
  session, quality included, ~20% shorter with a warm-up test (15 min easy + the first
  interval: normal HR and legs → carry on, otherwise half the reps or endurance); green is
  the full plan. `readiness.running` limits running only, so the hard work moves to the bike.
- **The 7-day plan** (`week_ahead`, §4.6b): the next session is its first day, unless
  readiness, feedback or the athlete's request says otherwise.
- The injury-risk tool must be called before prescribing anything hard.
- **Push experiments** (§4.7b) may raise the week's top load to `push.week_ceiling`; they never change the plan.
- Stick to the already-decided next session unless something new justifies a change.
  Say "next session", not "tomorrow".
- The athlete's explicit "easy / hard / rest / short / long" overrides the logic.

### 4.3 Readiness (`core/readiness.py`)

A green/amber/red traffic light. Since 2026-11-22 single signals **adjust** the session
instead of cancelling it (what coaches do on "meh" days):

- **green:** the full plan.
- **amber:** the planned session stays, quality included, ~20% shorter, with a warm-up test.
- **red:** rest or easy only. It needs a hard stop or a cluster:
  - hard stops: ill (check-in or a sick day), TSB −30 or below, 7-day HRV "low", a HIGH
    non-running injury finding;
  - a cluster: 2+ **recovery** signals at once (7-day HRV "below", 2+ poor nights in a row
    or one under 5 h, a Recovery or Subjective caution such as resting HR up, the
    athlete's own amber feedback: very sore, sore + low motivation, "a bit off", a poor night
    + high life stress, sore legs after the last hard session).
- Load cautions (TSB −20, ACWR, monotony...) and single recovery signals are amber.
- One poor night is only a note (`sleep.note`): keep the session, start with the warm-up test.
- **Illness early warning** (`core/illness.py`, `illness_warning` in the context). Last night's
  numbers are compared with the 28 nights before, skipping the last 2:
  - overnight heart rate up (≥ +3 bpm and 2 SD);
  - HRV down (≥ 5 ms and 1.5 SD) or Garmin sleep stress up (≥ 8 and 2 SD). These two count as
    one "autonomic" signal;
  - breathing rate while asleep up (≥ +1/min and 2 SD).

  The levels are:
  - **watch:** 2 of the 3 systems, or the autonomic signal 2 nights running. It is amber, as a
    load-type signal, so it isn't counted twice with the HRV check in a cluster.
  - **red:** "watch" together with the "a bit off" check-in, or a **warning**: all 3 systems, or
    watch 2 nights running with heart or breathing still off.

  Missing data (the watch off the wrist) only drops that signal. A 13-month backtest gave about
  10 watch days a year, mostly after heavy blocks or short nights, and flagged a real illness
  on its first day.
- **Running findings never set the level.** `running_limit()` turns them into
  `running.limit`: `no_hard_run` (caution: easy runs, no extra running) or `easy_short`
  (high: easy runs of at most 30 min, or a ride instead).

The briefing, the coach's context, the local coach, push experiments, the 7-day plan and
(privately, for tone) the Strava notes all use the same verdict.

### 4.4 Injury and overreaching checks (`core/injury_checks.py`)

Each check is judged against **the athlete's own baselines**, not textbook constants:

- **Load:** ACWR, TSB, ramp rate, monotony, weeks without recovery.
- **Recovery:** HRV, resting HR, sleep versus baseline.
- **Cardio drift:** decoupling on steady rides, efficiency-factor trend.
- **Biomechanics:** cadence versus usual for that ride type, L/R balance.
- **Subjective:** feel/RPE versus objective data.
- **Running:** volume progression and single-run spikes. A run is compared with the
  longest run of the 30 days before it, but never with less than 8 km
  (`RUN_SPIKE_FLOOR_KM`), so short recent runs don't make a normal run a "spike".
- **Impact load** (`core/mechanical.py`): a second load stream next to the cardio one (TSS).
  Running, hills and leg day load bones and tendons; riding hardly does, so ride TSS can hide a
  running spike. Impact is counted in km-equivalents:
  - a run counts its km × (1 + share of time in HR Z4+), plus 1 per 100 m climbed;
  - a leg gym session counts RPE × min / 30;
  - a walk counts a quarter.

  The impact ACWR is the 7-day EWMA divided by the 28-day EWMA (only from a 10 km-eq/week base).
  At 1.5 or above it is a `caution` in area Running. That limits running only
  (`readiness.running_limit`: no hard run), never the bike or the readiness level. The finding
  shows the cardio ratio beside it, so "legs spiking, engine calm" is visible.
- **During a comeback** (`core/periodization.comeback`, CTL under 50) the ACWR thresholds
  widen from 1.3/1.5 to 1.7/2.0: at a low CTL the plan's own ramp puts ATL at ~1.5× CTL.
  The ramp-rate check and the recovery signals still watch the absolute progression.
- A single-night HRV dip is `info` (warm-up test), not a caution.

The result is a structured report with severities (high/caution/ok/info). The injury
agent narrates it, the coach calls it as a tool, and the dashboard renders it.

### 4.5 Next session: one decision shared everywhere (`core/next_session.py`)

After the most recent activity, the coach is asked once (forced to `ai_coach`) for
`Session: …` / `Why: …`. The answer is stored on that activity row. The Strava note,
the dashboard card and the chat context all read the stored decision, so they never
disagree. Rules:

- Only the latest activity has a decision.
- **It never just waits to be done.** `refresh()` re-decides it after every sync once it
  is 24 h old without a new activity, and every morning before the briefing when it was
  made before last night (an athlete-applied choice is kept for its full 24 h). The
  re-decide prompt gives the days since the last training, last night's sleep/HRV and
  the sessions still to do this week.
- The morning briefing shows this same decision (no separate suggestion), and the
  dashboard card decides automatically when there is none; "Re-decide" forces one.

**Changing it from a chat, with approval.** When the athlete explicitly asks in a chat
("update my plan", "save that ride as my next session"), the coach calls the
`propose_next_session_change` tool. That only stores a **pending** row in
`next_session_proposals`; nothing the athlete sees changes yet.

- Telegram shows Apply / Discard buttons under the reply; the dashboard chat shows a
  card with the same buttons (`POST /api/next-session/proposals/{id}/apply|discard`).
- Apply overwrites the stored decision and marks it `next_session_source = 'athlete'`.
  The dashboard card, the morning briefing, Strava notes and the chat then all use it,
  and the coach keeps an athlete-chosen session unless readiness is red or a HIGH
  injury finding makes it unsafe.
- Apply is refused when a newer activity was logged since (the coach has decided
  afresh), when the proposal is over 24 h old, or when it was already resolved.
- Only live chat turns get the tool (they pass a `turn_id` into the graph state). The
  briefing and the automatic decisions never can. Local only mode has no tools, so it
  tells the athlete to switch it off to save a change.

### 4.6 Training plan and focus (`core/plan.py`, `core/focus.py`, `core/gym.py`)

- `training_blocks` is an **adaptive** plan (`core/periodization.py`), not a fixed calendar.
  `scripts/update_plan.py` (nightly 04:00) replays every closed week since 2026-01-05 and
  rewrites the table:
  - Each closed week gets a verdict against its target: completed (≥85% of the target
    minimum and ≥3 training days), partial, missed (<50% or ≤1 day), or recovery.
  - Base comes first; Build unlocks after 6 completed Base weeks **within the last 10** - or,
    from 3 completed on, once the aerobic base is measured (heart-rate drift under 5% on the last
    2 steady long rides within 3 weeks, `core/durability.aerobic_ready`; Friel's test).
    Partial and missed weeks don't count but cost nothing (a week off costs little
    fitness). The only step back is CTL falling 25%+ below the phase peak: about one
    base week to redo per 10% of CTL lost (from Build: back to Base with that many to redo).
  - A recovery week comes when form or HRV call for it after at least 2 loading weeks
    (TSB < -25, HRV 7-day "low", or "below" with TSB < -15 - the readiness formula,
    `hrv_trend()`), and at the latest after 4. Only a week at or above ~7×CTL (the load
    that holds fitness) is a loading week; a missed week counts as rest.
  - Targets come from the current CTL: ~7×CTL holds fitness, plus a CTL ramp of
    2/week in Base and 3/week in Build; recovery weeks are 50-70% of 7×CTL.
  - **Comeback** (`comeback()`, from 2026-11-23): while CTL is under 50, loading weeks ramp
    at +5 CTL/week, capped at the athlete's top weeks (p90) at that CTL in 2022-24, and never
    below the normal ramp. Earlier weeks keep the targets they were judged by.
  - Future weeks are a projection assuming completion (this week: actual load so far
    plus the days left; deloads every 3 loading weeks, since future HRV is unknown).
    `notes` says why each week has its phase (shown on the plan card and given to the
    coach); hand edits are overwritten.
  - **Races anchor the plan** (Intervals.icu RACE_A/B events, past ones included so the
    replay knows a race happened). A race: race-specific "Race prep" 2–6 weeks out
    (Build starts early within 12 weeks, from 3/6 base weeks), "Taper" the week before
    (70–85% of the holding load, intensity kept), the "Race" week with openers (55–80%),
    then a recovery week; within 3 weeks of it the taper replaces a calendar deload. B race: a
    mini-taper in its own week. C races are hard sessions.
    - **Run A race** (`mix_kind`): race prep becomes run-first, with 2 runs a week 6-4 weeks out and 3 in the last 3 (even with the cycling focus). The taper cuts run volume but keeps race pace.
    - **Bike A race in dual focus:** race prep keeps 2 runs instead of 3 (`core/focus.py`).
    **No A/B race:** the plan
    works towards fitness and FTP (`GOAL_NO_RACE`, `current_training_block.goal`).
  - **Days off from the availability file** (§4.11) scale that week's targets pro rata;
    with at most 2 days available the week is "away" (rest, nothing lost, not counted
    in the base window).
  - Base keeps a little intensity: early Base a weekly sprint/VO2 touch inside an
    endurance ride, late Base adds a tempo session.
- Completed sessions are classified from Intervals.icu zone times:
  - Rides are VO2 (≥8 min in power Z5+Z6: 80% of a 5x2, whose ramps sit in Z4), sweet spot (≥20 min SS or ≥15 min Z4), or endurance.
  - Runs, weights, and other types are counted separately.
- **Training focus** (dashboard toggle):
  - `cycling`: FTP first, running is maintenance.
  - `dual`: the winter block, with bike and run both primary.
  The dual setting is applied when the plan is *read*, so switching back restores the
  stored plan exactly. It replaces the session mix with 2-3 runs, 3-4 rides and 2-3 gym
  sessions (`adjust_sessions`): loading weeks get 4 rides, 3 runs and 2 gym sessions (3 in
  Base), deloads get 3 rides, 2 runs and 2 gym sessions. The bike quality sessions from the
  cycling plan stay, capped at 2, so a week never holds more than 3 hard sessions
  including the quality run. Race and taper weeks keep their own mix. The coach's dual
  rule (`running_rule`) describes how to lay out the week: hard days hard and easy days
  easy, heavy leg gym on a hard day after the quality session, the long ride and long run
  at the weekend, and progressing one variable at a time.
- **Gym sessions:** the athlete titles each gym session after the muscles worked.
  `core/gym.gym_focus()` marks a session as leg work only if the title says so ("legs",
  "lower body", "full body", "squat", "deadlift", "lunge"). Anything else is treated as
  upper body/core with no effect on leg freshness.

### 4.6b The rolling 7-day plan (`core/schedule.py`)

What to do on each of the next 7 days, laid out in code (no LLM). It is **stateless**:
every read re-derives the week from what was actually done, so it adapts by itself after
every sync.

- **Input:** the week's session mix (`week_block`, with the dual focus applied), the
  sessions done this week (classified: bike quality, long ride ≥ 110 min, easy ride;
  quality run = 15+ min in HR Z3 or above, long run ≥ 60 min, easy run; leg / other gym),
  the availability file (days off, limited days, pinned sessions), and readiness.
- **Two sessions on one day:** once a session of today is done, today is no longer
  re-laid out. The morning's plan for today is kept in `state/plan_today.json` (fallback:
  today's `state/workouts/<date>.json`); what isn't done yet stays on today (the heavy legs
  gym after the VO2max), counts toward the week and is not placed again. A done session takes
  the planned one of its sport (an easy ride instead of the VO2max still keeps the gym); after
  an endurance session no second one is kept, and nothing new is added to today. Red/amber
  readiness adds its note to the kept session.
- **What's still to do:** planned minus done. A skipped session is still to do and moves
  to another day. A hard session cut short counts when it reached the quality threshold;
  otherwise it stays to do.
- **Layout:** pinned sessions take their day. Then the key sessions (bike quality, run
  quality, long ride, long run; a second bike quality ranks below one quality session per
  sport) are placed by trying every assignment:
  - hard sessions never on consecutive days, nor the day after yesterday's hard one;
  - long sessions prefer the weekend and need a day with enough time;
  - a limited day only takes what fits.
  Easy sessions fill the remaining days, alternating sports. Heavy legs gym goes on a hard
  day; other gym on easy days.
- **When days run out,** the filler is dropped (easy rides, easy runs, the third gym
  session) and listed in `dropped`. Nothing is doubled up.
- **The last week in the window is laid out in full,** so its long sessions still find
  the weekend; only 7 days are shown.
- **Day 1 follows readiness:** red means nothing hard and at most 45 min; amber notes
  "trim ~20% + warm-up test" on the hard session. Running limits turn quality runs easy.
- **Targets:** watts from the training FTP, heart rates from the last run's HR zones, the
  measured thresholds (§4.6d: Zone 2 up to just under LT1, easy runs under the run LT1,
  quality runs sub-threshold at 94-97% of LTHR with 1-min jogs, plus a pace from critical
  speed once a CS test measured it), and sets/reps from the progression levels (§4.12): the week's phase picks the session type (tempo in late
  Base, sweet spot in Build 1-2, threshold in Build 3 and race prep, VO2max for the VO2 slot), the level
  picks the dose.
- **Tests** come from the test calendar (§4.6d), which picks the week from the long-term plan;
  the 7-day plan puts the test in place of that week's quality session of its sport (in a
  recovery week, in place of an easy ride, on Fri-Sun). Day-level rules: never within 14 days
  of an illness, never on an amber/red first day (a later day of the week is fine), the CS test
  not under a running limit. Both are recognised afterwards by the activity name
  (`FTP_TEST_NAME`, `RUN_TEST_NAME`: "FTP test", "CS test"; a newly set FTP counts as tested).
- **Long-ride progression:** 15 min over the longest ride of the last 30 days (30 min while the
  progress check asks for volume, §4.12), between 2 and 4 h; 75-90 min in a deload week. In Build
  and race prep it ends with 2x10 min sweet spot in the last 40 min (durability), and every 6
  weeks (green week, no other test, not within 14 days of an illness) it is a **durability test**:
  Zone 2 until 15 kJ/kg, then 20 min all-out (name it 'Durability test'). An amber/red first day
  drops the efforts.
- **Long-run progression:** the long run is ~10% over the longest run of the last 30 days
  (75% of it in a deload week), between 45 and 120 min. Single-run spikes are the injury
  risk (§4.4).
- **Planned vs done:** a session pushed to Intervals.icu (§4.6c) and done with ≥ 80%
  Intervals.icu compliance counts as that session, whatever the zone times say. Under 80%
  it counts as whatever the zones show, so an unfinished quality session stays to do.
  The week's compliances are in the plan (`planned_done`) and the coach's summary.
- **Where it's used:** `week_ahead` in the coach's context (rule 19: the next session is
  its first day), the local coach's plan summary, the dashboard (`/api/week-ahead`,
  `WeekAheadCard`) and Telegram `/week`.

### 4.6d Thresholds and the test calendar (`core/thresholds.py`, `core/testing.py`)

**Thresholds from the athlete's own data** (nightly, `scripts/update_plan.py`):

- **LT1 from DFA-alpha1.** Intervals.icu keeps the beat-to-beat (RR) intervals of every
  activity recorded with a chest strap (the `hrv` stream). Each activity is cut into 2-min
  windows (from minute 10, every 60 s); a window counts when it is stationary (both halves
  within 4 bpm) and clean (at most 3% artifacts, dropped). The windows of the last 90 days
  are pooled per sport and binned by heart rate (and power on the bike); LT1 is where the
  binned median alpha1 first falls through 0.75 (Rogers et al. 2021). Quality gate: the
  curve must start at 1.0 or more and must not climb back over 0.9 after the crossing -
  noisy RR does that, and such data is reported as unreliable, not used. Runs: heart rate
  locks to the step rate when the two are close (cardiolocomotor coupling - at ~160
  steps/min exactly the threshold range), so run windows within 20 bpm of the step rate are
  dropped, and a run LT1 more than 5 bpm under the bike LT1 isn't trusted. Per-activity
  windows are cached (`state/dfa_windows.json`); indoor rides without RR are counted, and
  Telegram says when the first indoor ride with RR arrives (e.g. a new indoor app).
- **Zone 2** goes up to 95% of the LT1 power, kept within 70-82% of FTP; without a measured
  LT1 power it stays 56-75% (the plan's watts, and the workouts' % steps 3 points below).
  Easy runs stay under the run LT1 when it's measured, else under the top of HR Z2.
- **Critical speed (run)** from a CS test: the best 3- and 9-min distances of that day
  (Intervals.icu pace curve), CS = (D9 - D3) / 360 s, D' = D3 - CS x 180 s; implausible
  results (D' outside 50-450 m) are reported, not used. Without a test, Intervals.icu's CS
  model of the last 90 days of training runs is shown as a floor (not all-out, so too low)
  and gives no paces.
- `state/thresholds.json` holds the result; the plan context gets a "Thresholds" line, and
  Telegram is told when Zone 2 moves or a CS test is read.

**The test calendar** places the tests by the long-term plan (training_blocks), so a test
lands where it is worth most and moves with the plan when a recovery week moves:

- **FTP test** (20 min, its warm-up a 4-step ramp at 55/65/72/80% FTP that also feeds LT1): at
  the **end of a recovery week** - fresh legs, and the new FTP sets the next block's watts.
- **CS test** (15 min easy + strides, 3 min all-out, 20 min easy, 9 min all-out): the quality
  run of the **first loading week after a recovery week**.
- Due at a slot when the last test is 35+ days old (about every second recovery week), or 21+
  days when the next block starts a new phase (Base -> Build -> Race prep). Never tested: the
  first slot. A missed slot is caught up the following week as its quality session; past 63
  days (FTP: the 8-week eFTP rule plus one recovery week) / 70 days (CS) it no longer waits
  for a recovery week. Never in a taper or race week; one test a week (FTP first).
- The 7-day plan asks `week_tests()` per week, projected from the current week; `calendar()`
  shows the next weeks on the dashboard plan card, in the coach's context and as "🧪 Next tests"
  under Telegram `/week`.

### 4.6c Structured workouts on Intervals.icu (`core/workouts.py`)

Each session of a plan day becomes a structured workout in Intervals.icu's workout-builder
text, and goes onto the Intervals.icu calendar **only when the athlete taps Push**.

- **When:** every morning after the briefing and check-in, the bot sends today's workouts,
  one message each with **Push to Intervals** / **Skip**. `/workout [date]` and the
  dashboard's `WorkoutCard` (`GET /api/workouts`, `POST /api/workouts/{day}/{kind}/{push|skip|remove}`)
  show the same workouts with the same buttons.
- **Text:** rides use % of FTP (Intervals' own FTP setting) with cadence cues; runs use
  Intervals' run HR zones (`Z2 HR`, `Z4 HR`). Sets and reps follow the plan's
  description (sweet spot 3x12/3x15, threshold 3x10, VO2max 5x3/5x4, tempo 2x15, sprints,
  tempo/threshold runs, hill repeats). Gym becomes a `WeightTraining` event with the
  prescription as text.
- **Pinned sessions** (the athlete's own words) are converted to steps by Gemini
  (temperature 0), keeping exactly what was written. Watts become % FTP, and bpm becomes the HR zone
  that contains it, because Intervals doesn't read absolute bpm. Code checks the result:
  - every step needs a duration and a target;
  - every "N x M" in the text must come back as an N-rep block with an M-long step;
  - the total must fit any total the text states.

  A result that fails stays plain text, like a "Zwift race". Conversions are cached in
  `state/workouts/pinned.json`, and a failure is retried after an hour. The preview shows
  the athlete's text above the steps.
- **Planned vs done:** Intervals.icu pairs each activity with the planned workout of its
  day and reports a compliance % (`paired_event_id`, `compliance`). `workouts.match()` finds
  the pushed workout behind an activity:
  - the 7-day plan counts ≥ 80% as done (§4.6b);
  - the Strava note shows "📋 Plan: <workout> · N% done", and the comment compares the steps
    with what was done.
- **Fueling:** every workout starts with a `Fuel:` line from `core/nutrition.py`, worked out
  from duration, intensity, sport and the athlete's weight. It sits above the steps, so it shows
  on Intervals.icu, Garmin, Telegram and the dashboard.
  - Before: ~1.5 g/kg carbs 2-3 h before a hard or 90+ min session, otherwise a normal meal.
  - During: water under 60 min, 30 g/h at 60-90 min (none for easy sessions under 75 min),
    60 g/h at 90-150 min (75 g/h when hard), and 90 g/h over 150 min (75 g/h running).
    Electrolyte fluids (750-1000 ml/h: the athlete sweats heavily) are added from 90 min.
  - After: ~0.3 g/kg protein (0.4 after gym), plus ~1 g/kg carbs within the hour after hard or
    long sessions.

  The steps carry no fuel cues of their own, and the durability test's carbs/h come from the same rule.
  The Strava note's plan comparison (§4.7) gets the steps without this line (`nutrition.without_fuel`):
  the note is public and the grams come from body weight.
- **Readiness and time:** amber drops one rep of the main set and adds the warm-up test. A
  "max N min" day shortens the warm-up and cool-down first, then drops reps.
- **Push:** `POST /api/v1/athlete/{id}/events` with category `WORKOUT` and `external_id`
  `aicoach-<date>-<kind>`. Intervals parses the text into steps, so the workout syncs on to
  Garmin or Zwift like any planned workout, and can be downloaded as .zwo/.fit/.mrc.
  Remove deletes the event.
- **State:** `state/workouts/<date>.json` (kept for 30 days) holds the pushed text,
  the event id and the status (draft / pushed / skipped).
  - The workout is rebuilt from the plan on every read.
  - If the plan changes after a push, the workout is marked `changed`, and Push becomes
    **Update**, which PUTs the same event.
  - If a pushed session is no longer in the plan, it can only be removed.

### 4.7 Strava coach notes (`core/strava_notes.py`)

Every 10 minutes the timer looks for activities of **any type** that are at least
20 min long, less than 2 days old, at least 15 min past import, and not yet noted.

- **Text:** a facts block computed in code (session type, intervals, duration/distance,
  NP/IF or pace, TSS, kJ, temperature, fitness change, achievements), plus 2–3 funny
  Gemini sentences that compare what was done with what was planned and say what the
  session did for the body. They never say what comes next (that is on the dashboard and
  Telegram), but the timer still triggers the next-session decision.
- **The 🎯 line:** the session type, and the planned type ("Threshold run", "VO2max") when it was
  done from a pushed workout. Rides add Intervals.icu's detected intervals. Runs don't, because
  that detection misses heart-rate reps; they show the time in HR Z3+ ("16m30s Z4 + 6m28s Z3 HR")
  or, for easy runs, the share in Z1-2 (`run_target`).
- **What was planned** (`planned_for`): the session pinned to that day, else the next
  session that stood when the day started (decided after the last activity of an earlier
  day). Walks and hikes have no plan (a "bonus outing"). Other sessions of the same day are
  passed along so a ride + gym day isn't called "skipped".
- **Privacy:** Strava descriptions are public, so the notes never mention HRV, sleep,
  readiness, injuries, weight or FTP tests. Readiness is passed to the model privately,
  only to steer the tone.
- **Description handling:** the note is appended below the athlete's own text. A
  re-post replaces only the previous note. `#nocoach` in the description skips the
  activity.
- **Manual posting:** Telegram `/strava [date] [context]` and the dashboard dialog
  preview a note, allow a rewrite with extra context, and post it. Manual posts
  override `#nocoach`.

### 4.7b Push experiments (`core/push.py`)

The athlete says "I can do more" in chat and the coach may go above the plan for a short window.
The plan doesn't change: `training_blocks`, its targets and verdicts are untouched, and the coach
gets `push` in its context (rule 18).

- `start_push` / `stop_push` are chat tools, saved in `athlete_settings` (key `push`). A push
  lasts up to 14 days (7 by default) and ends by itself.
- **The ceiling** is the load for a CTL ramp of 4/week (level 1) or 5/week (level 2) - 6 and 7 during a
  comeback, when the plan itself already ramps at 5 (`push.comeback`) - never above the
  90th percentile of what the athlete did at this CTL in the benchmark years (`BENCHMARK_YEARS`),
  scaled for days off and never below the plan's own maximum.
- **Checked every day.** Off, with reasons in `blocked_because`, only on hard stops: a red verdict, a HIGH
  injury finding, illness, a recovery/taper/race week, or while easing back in after a break. An amber
  verdict or a caution about load, recovery, biomechanics or how sessions felt keeps the extra volume and
  only holds the extra intensity (`intensity_held_because`). A running caution only sets
  `no_extra_running`. Intensity also needs a Build or race-prep week.
- **Each push is judged when it ends:** `absorbed` (more load than the plan, no warning) opens level 2,
  `too_much` (warning, illness, or the athlete says so) goes back to level 1 after a 7-day cooldown,
  `no_extra` changes nothing, and a push stopped within 3 days doesn't count.

### 4.8 Race planning (`core/race_planner.py`, `core/races.py`, `core/weather.py`)

- **Races:** read from the Intervals.icu calendar (RACE_A/B/C events). Distance,
  elevation, location and goal are parsed from the event description.
- **The planner is fully deterministic:**
  - **Course model:** climb, flat and descent split.
  - **Pacing:** target IF by duration, power for climb/flat/descent, HR caps, iterated
    with the physics model until intensity and duration agree. Heat lowers the targets.
  - **Fuelling:** carbs per hour, fluid and sodium for a heavy sweater, caffeine in
    mg/kg for a habitual user.
  - **Taper:** sized by race priority, with CTL/ATL/TSB projected to race morning.
  - **Race week:** a day-by-day plan.
  - **Post-race review:** compares the race against the same model.
- **Weather:** the Open-Meteo forecast within 15 days; for later dates, 5-year
  climatology for that calendar date.

### 4.9 Call logging and cost (`core/api_log.py`, `core/usage.py`)

Every outgoing call is written to `api_calls`, without changing any call site:
- **LLMs:** the factories in `core/llm.py` attach a callback that records tokens
  (including Gemini's thinking tokens, which are billed as output), duration and an
  estimated cost from the `PRICES` table (Gemini 3.1 Pro: $2/M in, $12/M out).
- **HTTP:** `requests.Session.send` is wrapped once per process (from `core/db.py`), so
  Intervals.icu, Garmin, Strava, Telegram notifications, Open-Meteo and Withings are counted.
  Paths are normalised (ids and the bot token replaced by `{id}`). The Telegram bot's own
  traffic uses httpx and is not counted.
- **Who and why:** each row stores the process (`app`, `telegram_bot`, a timer script) and
  a reason set with `api_log.trigger(...)`: chat, briefing, next_session, strava_note,
  sync, health_check, sync_check, name_sync.

`core/usage.py` aggregates the table for the dashboard's **System** page (`#system`):
cost and a monthly projection, calls over time, spend by component, a process → reason →
service flow chart, busy hours, per-component token table, and the latest calls.

### 4.10 Front-ends

- **Dashboard** (`web/` → built to `web/dist`, served at `/`): KPI row, next-session
  card, PMC chart, wellness chart, weekly load, plan card with the focus toggle, injury
  card, race card, activities table with a Strava-note dialog, and a streaming chat
  panel with Local-only mode. A **System** button (a tab on phones) opens the call log
  and Gemini cost page (§4.9). Reachable remotely through Tailscale. A minimal legacy
  chat page is still at `/classic`.
- **Telegram:**
  - **Morning briefing** at `BRIEFING_TIME`: syncs first, re-checks the next session
    with last night's sleep/HRV, then computed facts plus that shared decision.
  - **Chat** (with 3 h conversation memory), `/twin`, `/claude`, `/gemini`, `/local`, `/strava`, `/briefing`,
    `/week` (the 7-day plan, §4.6b), `/workout` (structured workouts with Push to
    Intervals.icu, §4.6c, also sent every morning after the check-in), `/gym` (log lifts,
    §4.11), `/sync`, `/reset`.
  - **Feedback** (§4.11): RPE and legs after each hard, long or gym session (checked every
    10 min, not between 22:00 and 07:00), and a check-in (body, motivation) after the briefing.
  - Only user ids listed in `TELEGRAM_ALLOWED_USER_IDS` get answers.

### 4.11 FTP, feedback and availability (`core/metrics.py`, `core/feedback.py`, `core/availability.py`)

- **Training FTP** (`metrics.training_ftp()`): targets use the set FTP while it is
  fresh. "Updated" is the later of the first ride with the current FTP (Intervals.icu
  stamps `ftp_at_time` on each activity) and the last ride named like an FTP test. The
  athlete's rule: a tested FTP is used for 8 weeks; over 6 weeks old → `ftp_check` says a
  test is due; over 8 weeks without one → targets use eFTP (`ftp_source = "eftp"`, the dashboard
  says "FTP (from eFTP)") until the next test. The same profile feeds the coach, simulators
  and race planner; the Intervals.icu workouts scale their % targets to it (Intervals reads %
  of its own set FTP); and `metrics.ftp_history()` applies the same rule to every past ride, so
  progression and the progress check judge each ride against the FTP of that day.
- **Morning check-in** (migration 018): body (fresh / normal / sore / very sore / a bit off /
  ill), the night (good / ok / poor), life stress (low / normal / high: work, family) and
  motivation. It is a short Hooper-style well-being check, four taps after the briefing.
  - "A bit off" is the athlete's half of the illness early warning (§4.3).
  - A poor night together with high stress trims the day; either one alone is context for the
    coach only.
  - Any amber answer re-plans.
- **Feedback** (tables `session_feedback`, `daily_checkin`, migration 016 - kept apart
  from `activities.rpe/feel`, which Intervals.icu overwrites): Telegram buttons only,
  so no number comes from a model. RPE and legs are asked after **hard, long and gym
  sessions** (`feedback.asks_rpe()`):
  - hard means `is_hard()`: rides classified sweet spot / VO2, runs with 10+ min in HR Z4+ or
    25+ min in Z3+, FTP tests, races;
  - long means a ride of 110+ min or a run of 60+ min.

  Easy sessions are covered by the next morning's check-in, which names them. The RPE is also
  written to the activity on Intervals.icu (`icu_rpe`), where it gives the session an RPE ×
  minutes load (`session_rpe`). That is the only real load of a gym session, whose
  heart-rate load is near zero; the coach sees it as `gym_last_14_days`.
- **Gym log** (`core/gym_log.py`, table `gym_sets`, migration 017): `/gym squat 80x5x3,
  rdl 60x8x3, pull-up bwx8x3` (kg × reps × sets, `bw` = bodyweight) stores the main lifts.
  The plan's gym sessions then prescribe the next step from the last log of each exercise
  (6 weeks back at most):
  - +2.5 kg for lower-body lifts and +1 kg for upper-body lifts, when that session was RPE 8
    or below (or not rated);
  - the same weight after RPE 9-10;
  - one more rep for bodyweight exercises.

  After a gym session's legs answer the bot shows the `/gym` format. It feeds readiness (§4.3) and
  `athlete_feedback` in the coach's context. An answer that changes things (ill, very
  sore, sore legs) re-plans and sends the updated next session; "Ill" also saves a sick day.
- **Availability** (`state/availability.json`): the coach's `record_availability` /
  `cancel_availability` tools (live chat turns, Telegram and dashboard) save what the
  athlete says - sick / away / unavailable / limited (`max_minutes`) - as exact dates
  ("3 days" = today and the next 2). **Planned** entries (`plan_session`, one per date -
  a swap is two) pin a session to a day: the coach prescribes exactly that on the day
  unless readiness is red, the athlete is ill or a HIGH injury flag says no. A later
  no-training entry on that date removes it (the coach says so and offers to re-pin it);
  a session can't be pinned onto a no-training day. The supervisor sends every such message to
  `ai_coach`. Every read moves fully past entries to `history` (kept 400 days for the
  replay and the after-illness return), so normal training resumes by itself. A change
  re-plans the week and the next session (`next_session.replan()`). The coach never
  plans training on a no-training day, fits limited days into `max_minutes`, and after
  an illness the first 2 sessions are easy (`return_to_training` "after_illness").
- **Intensity distribution** (`injury_checks.check_intensity()`): the last 28 days'
  time in zone (power Z1–2 / Z3–4 / Z5+, HR zones without power) against a pyramidal
  split - over 25% moderate, under 70% easy or over 12% hard is a caution finding; also
  `intensity_distribution` in the coach's context.

### 4.12 Progress check and progression levels (`core/progress.py`, `core/progression.py`)

The plan ramps the load and readiness checks it is absorbed; these two close the loop on the
*output*: is the athlete getting faster, and is each session the right size? Numbers only, no LLM.

- **Progression levels** (`core/progression.py`, stateless like the plan): every quality type
  (tempo, sweet spot, threshold, VO2max, run tempo, run threshold, hills) has a ladder of doses
  (e.g. sweet spot 3x10 → 3x12 → 2x20 → 3x15 → 2x25 → 3x20 → 2x30). Every read replays the last
  180 days of sessions:
  - rides are measured from Intervals.icu's interval detection (`interval_summary`, "3x 9m54s
    254w" → 3 reps at 90% of that day's FTP: sweet spot); runs from HR-zone time (lag-corrected,
    and only when the hard time is a real share of the run, so an easy run drifting into Z3 isn't
    a tempo run); a workout pushed from the plan counts as its named dose × compliance;
  - ≥ 95% of the level's work at RPE ≤ expected + 1 (or unrated) → one level up (two for 130%+);
    RPE expected + 2 → repeat; partial → repeat; two partials under 75% of *pushed* workouts →
    one level down (an unplanned ride with a few efforts can't); 28 days without the type → one
    level down per 28 days; an RPE 3+ under the expected one isn't a quality dose.
  - `schedule.bike_type` / `run_type` pick the type, `progression.dose` the size; the workout
    builder reads the reps from the plan text, so the Intervals.icu workout matches.
- **Progress check** (`core/progress.py`, nightly from `scripts/update_plan.py`, written to
  `state/progress.json`): four 28-day windows of markers - best 5/20-min power (Intervals.icu
  power curves, one API call), eFTP at the window's end, aerobic efficiency (median power:HR in
  Zone 2), durability (`core/durability.py`: median heart-rate drift on steady long rides - 90+
  min, VI ≤ 1.10, endurance IF - lower is better; and the best 20-min power after 15 kJ/kg from
  the power stream, cached in `state/durability.json`, as a share of that day's training FTP,
  counted only when it was a real effort ≥ 80%), easy-run economy (metres per heartbeat,
  grade-adjusted). Every ride is judged against the training FTP of its day (§4.11). Each is up / flat / down
  against the window before (smallest worthwhile change 1.5-2%), or up on a slow 8-week climb;
  best-effort power can only be "down" when the window had a session that would show it.
  - Verdict per sport by majority (improving / flat / declining / mixed); diagnosis in order:
    progressing, after_illness (sick in the last 4 weeks), fatigued (red readiness),
    inconsistent (< 2 of the last 3 loading weeks completed - the load is the lever), stalled.
  - **Stalled → a new stimulus for 21 days** (`athlete_settings` key `progress`): bike in Build
    more threshold (sweet spot becomes threshold), then a VO2max block (one quality ride a week
    becomes VO2max), then volume; in Base only volume (the long ride grows 30 min instead of 15);
    run: rotate the quality run (threshold / hills / tempo). One tried in the last 12 weeks goes
    last. When it ends it is judged (helped / no change) and Telegram says so. The plan's phase,
    targets and session mix never change.
  - Shown in Telegram `/progress`, `GET /api/progress`, and the coach's context (`progress`,
    `progression`, rule 20).

### 4.13 Fuelling for the day and the low-energy check (`core/nutrition.py`, `core/energy.py`)

- **Daily carbs:** each day of the 7-day plan gets carbs and protein for the whole day
  (`daily_carbs`, "fuel for the work required"). The sessions' minutes are read from their text.
  - **Carbs by the day's training:**
    - rest 3-4 g/kg; gym only 4-5;
    - easy under 60 min 4-5;
    - about an hour, or a hard session, 5-7;
    - 90-150 min, or hard and over 75 min, 6-8;
    - over 150 min 8-10.
  - **Day before a long or hard session:** +1 g/kg.
  - **Protein:** 1.6-2.0 g/kg every day.
  - **Where it shows:** the `fuel` field of each day, a 🍝 line in `/week` and on the dashboard.
- **Low-energy check (`energy.check`):**
  - **Weight trend:** the median of the weigh-ins of the last 21 days vs the 42 days before, with 2+ weigh-ins in each.
  - **Load:** CTL now vs 28 days ago.
  - **Flag ("watch"):** weight down 1.5%+ while CTL rose 3+, or down 2.5%+ alone. HRV under its normal is added as a second sign, never as a trigger.
  - **What a flag does:** the daily carbs move to the top of their range, a Telegram message goes out when the flag starts and when it clears (state in `state/energy.json`), and the coach sees it. Readiness is untouched.
  - **Weigh-ins:** synced nightly from Withings (`ingest/import_withings.py`, last 30 days).

### 4.14 Goals (`core/goals.py`)

- **Where they live:** `state/goals.json` holds the goals (personal: never in code or in the public copy). Both are optional.
  - **FTP goal:** a target by a date, with the starting FTP.
  - **Half-marathon goal:** a target pace, kept ready rather than tied to a race date.
- **FTP goal:**
  - **The path:** a straight line from the starting FTP to the target on the date.
  - **What's measured:** the FTP rule's value (the tested FTP for 8 weeks, then eFTP). A tested FTP is judged against the path on the day of the test.
  - **Status:** reached / ahead / on track / behind (±1.5%), plus the W/week still needed and eFTP as the day-to-day signal.
- **Half marathon:**
  - **Predicted pace:** the faster of two estimates:
    - Riegel (exponent 1.06) from the best 3 km+ efforts of the last 90 days (Intervals.icu pace curve);
    - 93% of critical speed, when a CS test measured it.
  - **Endurance:** the longest run of the last 6 weeks vs 85% of the race time at the target pace.
  - **Status:** ready (within 3 s/km and endurance there) / close (within 15 s/km) / building.
- **Cycling focus:** the half-marathon goal goes "on maintenance": tracked, but never pushed (1 run a week, for bone health).
- **Races:** an A/B run race of 15-25 km gives the half-marathon goal its date.
- **Priorities** (in the coach's "Goals" line and its rule 16):
  - an A race first; its sport shapes the race-specific weeks;
  - then the goals by `priority`: FTP first, half marathon second.
  - Both goals are kept through a race season:
    - before a bike race: Build blocks and the post-race FTP test for the FTP goal, 2 runs in dual-focus race prep for the run goal;
    - before a run race: the run-race weeks.
- **Nightly:** `update()` writes `state/goals_status.json` and sends Telegram when a status changes.
- **Where it shows:** `/progress`, `GET /api/goals` (the dashboard's plan card), and the coach's context ("Goals" line). The coach quotes the status and doesn't change the plan to chase it.

### 4.15 Adjustment log and quarterly review (`core/adjustments.py`)

- **Markers snapshot:** one line a night in `state/markers.jsonl`:
  - training FTP and eFTP, CTL;
  - critical speed and the predicted half-marathon pace;
  - weight median and 7-day HRV.
- **Adjustment log:** `state/adjustments.jsonl` gets one line for each change the coach makes on its own:
  - the week's phase adapted;
  - a progress-check stimulus started or ended;
  - a threshold change;
  - a new set FTP (found by comparing with the previous snapshot);
  - a goal status change;
  - the low-energy flag.
- **Quarterly review:** on the first night of a quarter, `state/reviews/<year>-Q<n>.md` and Telegram (on demand: `/review`). It covers:
  1. each adjustment of the quarter with the marker changes 4 weeks later;
  2. the results of the progress-check stimuli;
  3. the load the athlete absorbs: the weeks of the last year grouped by CTL ramp, with the next week's HRV vs its 4-week normal, and the highest ramp that kept HRV within -3%;
  4. the goals.

### 4.16 Device check (`core/devices.py`, in `scripts/check_health.py`)

- **The watch:**
  - **Overnight:** heart rate (resting / sleeping / min HR), HRV or sleep missing for 2 synced nights, when it was there on 70%+ of the 4 weeks before the gap began. A long gap doesn't become the normal, and a night that hasn't synced yet doesn't count.
  - **Daytime:** under 500 steps on 2 full days.
- **Activities (last 3 days):** a ride or run without heart rate, or a ride without power, when the same kind (sport, indoor / outdoor) usually has it.
- **The scale:** no weigh-in for 14 days by someone who weighs in.
- **Messages:** a Telegram message of its own ("Please check your devices", with what to check), once per problem, and "✅ Recording again" when the watch or scale records again. A single activity can't come back, so it gets no "recording again".

---

## 5. Every file in the project

### Root

| File | What it contains | Role in the system |
|---|---|---|
| `.env.example` | The names of every environment variable (DB URLs, Intervals.icu, Garmin, Gemini, Ollama, Withings, Strava, Telegram, optional settings) with no values | The template for the real `.env`; documents what must be configured |
| `.gitignore` | Excludes secrets, tokens, `logs/`, `state/`, `venv/`, `web/node_modules`, `web/dist` | Keeps credentials and runtime data out of GitHub |
| `ARCHITECTURE.md` | This document | The map of the system |
| `tests/` | pytest suite (186 tests): the 7-day plan (spacing, adapting, two sessions on one day, readiness, availability, tests, long-run growth, compliance), readiness/target/injury rules, workout text and the pinned-session checker, the gym log, progression levels, the progress check, durability, the aerobic Build gate and the FTP rule, workout fueling, impact load, the Strava facts line, the illness early warning and the check-in, the test calendar and the thresholds (DFA-alpha1, LT1, Zone 2, critical speed), daily carbs, the low-energy check, goals, the adjustment review and the device check. Everything the code would read is patched in - no database, files or APIs | Changing one rule can't quietly break another; run before every commit |
| `requirements-dev.txt` | pytest | Test runner only |
| `requirements.txt` | Pinned Python dependencies (FastAPI, LangGraph/LangChain, Ollama + Gemini bindings, psycopg2, python-telegram-bot…) | Makes the `venv` reproducible |
| `app.py` | The FastAPI app: serves the React dashboard and PWA files, the `/classic` chat page, `/chat` SSE streaming and `/sync-data`; includes the `/api` router and the login (`core/web_auth.py`) | The main web process and the target of the 30-min sync cron |
| `dashboard_api.py` | The `/api/*` JSON endpoints: overview, PMC, wellness, weekly load, plan, focus get/set, next session, activities, Strava-note preview/post, injury report, races, usage (System page), chat, sync | Everything the dashboard shows or does goes through here |
| `graph.py` | The LangGraph workflow: preprocessor (metrics + readiness + next session), local-model supervisor router, agent nodes, local/cloud switch | The brain: decides which agent answers and what context it gets |
| `telegram_bot.py` | The Telegram bot: access control, chat with memory, one `COMMANDS` list that builds both `/help` (every command, grouped, with its usage) and the "/" menu, `/twin`, `/claude`/`/gemini`/`/local` model choice, `/strava` preview/post buttons, `/briefing`, `/sync`, `/reset`, `/progress`, `/review`, `/week`, `/workout`, `/gym`, the daily briefing job, session-feedback and check-in buttons | The mobile front-end, the daily push message and the athlete's feedback |
| `logging_config.py` | Logging setup: daily-rotated files in `logs/` (14 days) plus stdout, quiets noisy libraries | Consistent logs for every process |

### `agents/`: the specialists

| File | What it contains | Role in the system |
|---|---|---|
| `__init__.py` | Empty package marker | Makes `agents` importable |
| `ai_coach.py` | The head coach: Gemini ReAct agent, the full coaching rulebook (scheduling, gym, readiness, periodization, sleep, weather, overrides), tools | Produces training advice and every next-session decision |
| `injury.py` | The injury agent: the local model narrates the precomputed report; also builds the `check_injury_risk` tool used by the coach | Answers injury/recovery questions; gates hard sessions |
| `racing.py` | The race strategist: Gemini ReAct agent with race-plan, race-review, upcoming-races, simulator and history tools | Race pacing, fuelling, taper, race week, post-race reviews |
| `history.py` | The History Analyst: Gemini ReAct agent that only chooses and interprets: wellness/training summaries, fitness-gain windows, race history and pacing are calculated locally (`core/history_stats.py`), read-only SQL as a last resort; plus a per-metric data-coverage summary | Reports and trends over any past period |
| `twin.py` | The Athlete Twin: Gemini ReAct agent with the load-projection and ride-time simulators | What-if scenarios and fitness projections |
| `local.py` | "Local only" versions of the Coach, Twin and Racing agents: code gathers the data and runs the simulators, the local model only writes the answer; compact text context builder | Lets everything run free on the home GPU; also the local coach rules |
| `tools.py` | Shared LangChain tool wrappers (history search, read-only SQL, wellness trend, power pacing, activity detail, load projection, ride time, upcoming races, race plan, race review, next-session proposal, availability record/cancel, planned sessions) | One implementation of each tool for every agent |
| `life.py` | Google Calendar read-only lookup | Life/schedule agent, currently disabled |

### `core/`: shared logic

| File | What it contains | Role in the system |
|---|---|---|
| `__init__.py` | Empty package marker | Makes `core` importable |
| `db.py` | `.env` loader and Postgres connection helper; installs the HTTP call logger | Every database access starts here; every process imports it |
| `metrics.py` | Builds `metrics_context` (profile with the training FTP, wellness, PMC, recent activities with gym focus, training block with sessions left and goal, return to training, availability, feedback, intensity distribution); history search; wellness history by date range; interval-level activity detail; FTP-vs-eFTP drift | The data every agent reasons over |
| `history_query.py` | Read-only SQL for the history analyst (as `coach_reader`, read-only transaction, timeout, row cap), the schema guide given to the model, per-metric data coverage | Lets chat answer any question about past data safely |
| `history_stats.py` | Local calculators for the history analyst: wellness stats/series/period comparison, training volume and workout-type mix (steady vs variable by IF/VI), biggest CTL-gain windows, race detection + pacing | Every history number is computed here, not by the LLM |
| `pacing.py` | Power fade/pacing per quarter from Intervals.icu watts streams (cached) | "Did my power hold to the end?" |
| `overview.py` | The shared overview (form, HRV status, week load vs plan, next race), a small TTL cache, the read-only query helper | Keeps the dashboard and briefing numbers identical |
| `illness.py` | Illness early warning: last night's heart rate, HRV / sleep stress and breathing rate vs the athlete's own 28-night normal; watch / warning | Catches an infection before training on it |
| `readiness.py` | The green/amber/red verdict from TSB, the HRV trend, sleep, the injury findings and the athlete's feedback (single signals trim, clusters stop); the running limit | The single "can I train hard?" answer |
| `mechanical.py` | Impact load: running, hills and leg day in km-equivalents, its acute:chronic EWMA ratio next to the cardio ratio | The second load stream; limits running, not riding |
| `injury_checks.py` | Deterministic risk checks (load, intensity distribution, recovery, drift, biomechanics, subjective, running) against personal baselines, plus text/Markdown renderers | Injury agent, coach tool, dashboard card, readiness input |
| `feedback.py` | Session RPE/legs and the morning check-in: what to ask, saving taps, readiness signals, coach context | The athlete's own voice in readiness and the plan |
| `availability.py` | `state/availability.json`: days off / limited days / planned sessions as exact dates, self-expiring into history; day status, unavailable days, last illness, coach context | Plans around sickness, travel and busy days, and resumes by itself |
| `next_session.py` | Decides, stores and reads the one shared next-session decision; `refresh()` re-decides a stale one (24 h without an activity, every morning) | Keeps Strava, the dashboard, the briefing and chat consistent, and never lets a skipped session sit there |
| `periodization.py` | Adaptive phase engine: replays closed weeks (completed/partial/missed), rolling Base→Build gate, CTL-drop step-back, form/HRV-timed deloads, CTL-based targets, projection; `hrv_trend()` and the graded `return_status()` | Decides what phase each week is and why, and how to come back after days off |
| `plan.py` | Session classification (endurance/SST/VO2/run/weights; runs as quality/long/easy), planned vs done per week, plan summary for prompts (with the 7-day plan) | The plan card and the coach's view of "what's left this week" |
| `progression.py` | Progression levels: dose ladders per quality type, the replay of sessions done (interval detection, HR zones, compliance, RPE), the next dose | Sizes every quality session from what was actually done |
| `nutrition.py` | Fueling per workout (carbs before, per hour during, protein and carbs after) and per day (`daily_carbs`: g/kg carbs by the day's training, protein), §4.13 | The `Fuel:` line on every workout, the 🍝 line on every day |
| `energy.py` | Low-energy check: weight trend (Withings) vs CTL, §4.13 | Catches under-eating for the load before performance stalls |
| `goals.py` | The athlete's goals (`state/goals.json`): FTP path to a target date, half-marathon pace readiness, §4.14 | On track / behind from the numbers |
| `adjustments.py` | Nightly markers snapshot, the adjustment log, the quarterly review, §4.15 | Learns what works for this athlete |
| `devices.py` | Device gaps: watch measurements, activities without HR / power, weigh-ins, §4.16 | Asks the athlete to check a device before the coach runs blind |
| `durability.py` | Durability: steady-ride heart-rate drift, the aerobic-base test for Build, best power after 15 kJ/kg from the power stream (cached) | Fatigue resistance as a marker, and when Build can start |
| `progress.py` | The progress check: 4-week performance markers, trends, verdict and diagnosis per sport, the stimulus change when progress stalls, its judgement; `state/progress.json` | Makes sure the training produces progress, not just load |
| `push.py` | Push experiments: time-boxed load above the plan, the ceiling from a CTL ramp and the benchmark years, a daily check against readiness and the injury checks, a result per push | Lets the athlete try more without changing the plan |
| `focus.py` | Cycling vs dual-sport focus: goals, running rule, session-mix and focus-text adjustments, saved in `athlete_settings` | Switches the whole coaching doctrine for winter |
| `gym_log.py` | Parses `/gym` lift logs into `gym_sets` and prescribes the next weights (double progression from the last session's RPE) | Makes the gym progressive instead of "some gym" |
| `web_auth.py` | Dashboard login: `DASHBOARD_TOKEN` entered once at `/login`, kept as an HttpOnly cookie; local calls and the PWA files stay open | A second lock behind the firewall and Tailscale |
| `gym.py` | `gym_focus()`: a gym session counts as leg work only if its title names legs | Stops upper-body/core sessions from limiting bike/run work |
| `simulators.py` | CTL/ATL/TSB forward projection (Intervals.icu model); cycling physics for speed and time on climbs/segments | Numbers for the Twin and the race planner |
| `race_planner.py` | Deterministic race plan (course, pacing, fuelling, caffeine, taper, race week) and post-race review | Racing agent output; the dashboard race card |
| `races.py` | Races from the Intervals.icu calendar (`fetch_races` for any range, `fetch_upcoming_races`; with description parsing) and past races from history | Race lists for the agents, briefing, dashboard and the race-anchored plan |
| `weather.py` | Open-Meteo geocoding, forecast and 5-year climatology | Race-day conditions for pacing and fuelling |
| `llm.py` | LLM factories: `local_llm` (Ollama `qwen3:8b`, thinking off, 8k context, 30 min keep-alive) and `gemini_llm` (`gemini-pro-latest`), each tagged with the component using it | One local model for everything (the GPU never reloads); every LLM call is logged |
| `claude_agent.py` | `claude_or_gemini` / `ask_claude`: answers one agent's question with Claude Code (Claude Agent SDK) on the subscription - the agent's own prompt, the tools it passes as an in-process MCP server, no built-in tools, settings or CLAUDE.md; logged to `api_calls` as `claude` (cost 0) | The default chat model, with Gemini as fallback |
| `api_log.py` | Records every outgoing call into `api_calls`: LangChain usage callback (tokens, cost estimate, price table), `requests` hook for HTTP services, `trigger()` reason tags | Source data for the System page and Gemini cost tracking |
| `usage.py` | Aggregates `api_calls` for `/api/usage`: totals, timeline, cost projection, components, flows, busy hours, recent calls | Feeds the dashboard's System page |
| `chat.py` | Shared front-end helpers: `ask_supervisor` (async/sync), SSE `stream_reply`, `run_sync`, agent display names | The web UI, Telegram and timers talk to the graph the same way |
| `history.py` | Conversation memory: the last 6 messages, the format the supervisor sees | Makes follow-up questions work |
| `briefing.py` | The morning briefing: computed facts (readiness, HRV, load vs plan, warnings, race countdown) plus the shared next-session decision, re-checked that morning | The daily Telegram message |
| `strava.py` | A minimal Strava client: token refresh, list/get/update activities, scope check | Used by name sync and coach notes |
| `workouts.py` | Turns a plan day's sessions into Intervals.icu workout text (power % FTP, HR zones, reps trimmed for amber / short days) and pushes, updates or removes them on the Intervals.icu calendar | Workouts on the watch / Zwift with one tap, never without it |
| `testing.py` | The test calendar: FTP test at the end of a recovery week, CS test as the first quality run after it, by the long-term plan (§4.6d) | Tests land where they are worth most and move with the plan |
| `thresholds.py` | LT1 from DFA-alpha1 (chest-strap RR data), Zone 2 from it, critical speed from a CS test; `state/thresholds.json` (§4.6d) | Zones from the athlete's own physiology, not fixed % of FTP |
| `schedule.py` | The rolling 7-day plan: lays out what's left of the week's session mix around availability, readiness and the hard/easy rules, with concrete targets | One day-by-day plan for the coach, dashboard and Telegram; adapts after every sync |
| `strava_notes.py` | Builds, merges and posts Strava coach notes for every activity type (facts + a funny Gemini comment: planned vs done and the training benefit), with privacy rules and `#nocoach` | The public coach voice on Strava |
| `notify.py` | Sends a plain Telegram message from scripts and timers | Alerts from the health check, sync check and name sync |
| `telegram_format.py` | Markdown → Telegram HTML conversion, splitting long messages | Correct formatting of bot messages |

### `ingest/`: getting data in

| File | What it contains | Role in the system |
|---|---|---|
| `field_map.py` | Maps an Intervals.icu activity payload to an `activities` row (with notes on units, e.g. IF stored as a ratio) | Correctness of every stored activity metric |
| `field_map_wellness.py` | Maps an Intervals.icu wellness record to a `daily_metrics` row (no health fields) | Correctness of CTL/ATL/eFTP data |
| `import_intervals.py` | Fetch and upsert activities (`sync_activities`); runnable as a backfill | Activity sync |
| `import_wellness.py` | Fetch and upsert Intervals.icu wellness days (`sync_wellness`); runnable as a backfill | Fitness-model sync |
| `import_garmin.py` | Garmin Personal API: shared token from the API key, fetch, map (`map_day`) and upsert health days (`sync_garmin`), committed per 28-day chunk; `--days N` or `--start YYYY-MM-DD` backfills and resumes | Every health metric (sleep, HRV, RHR, stress, Body Battery, SpO2...) |
| `import_profile.py` | Sync FTP/LTHR/max HR/resting HR/weight and the latest eFTP into `athlete_profile` | One place to edit thresholds |
| `import_withings.py` | Fetch Withings weight measurements into `daily_metrics.weight_kg` | Body-weight history |
| `withings_auth.py` | One-time interactive Withings OAuth that writes `state/withings_tokens.json` | Setup only |
| `strava_auth.py` | One-time Strava OAuth (read_all + write scopes) that writes `state/strava_tokens.json` | Setup only; required for names and notes |
| `_sample_wellness.json` | A synthetic example Intervals.icu wellness payload | Reference for the wellness field mapping |

### `scripts/`: maintenance and timers

| File | What it contains | Role in the system |
|---|---|---|
| `create_database.py` | Creates `athlete_twin_v2` if missing and applies any migration not yet recorded in `schema_migrations` | Database setup and upgrades |
| `setup_readonly_role.py` | Enables login for `coach_reader` with a random password and writes `READONLY_DATABASE_URL` to `.env` (re-run to rotate) | Run once after migration 014 |
| `update_plan.py` | Recomputes the adaptive plan, measures the thresholds (LT1, critical speed), syncs Withings weigh-ins, runs the low-energy check, the goals and the markers snapshot / adjustment log (quarterly review on the first night of a quarter), then the progress check (`--dry-run` to preview, `--notify` for Telegram on a phase change or a stimulus starting/ending) | Keeps `training_blocks` in step with the training done |
| `post_strava_notes.py` | CLI: post pending notes, `--dry-run`, `--activity` for one activity | Run by the Strava-notes timer |
| `sync_strava_names.py` | Matches Strava (last 6 months, `--full` for all) to local activities and pushes Strava names to Intervals.icu and the database | Run by the nightly name timer |
| `backfill_strava_history.py` | One-time: uploads Strava-only activities to Intervals.icu (TCX from streams), links double recordings, re-imports the DB; re-runnable, report in `state/strava_backfill_report.txt` | Run once by hand (2026-10-11) |
| `check_intervals_sync.py` | Reconciliation of activities and wellness over the last 15 days (`--days N`, `--full`), `--fix`, `--notify` | Run by the nightly sync-check timer |
| `backup.py` | Nightly encrypted backup: `pg_dump` (custom format) and `state/`, AES-256 with `BACKUP_PASSPHRASE`, each verified by decrypting and reading it back, into `/var/backups/ai-coach` (14 daily + 8 weekly), then mirrored to the NAS over SMB (`smbprotocol`; `NAS_*` in `.env`, folder `<NAS_SHARE>/ai-coach`, each upload read back and SHA-256-checked; SMB 3 encryption when offered, else a signed SMB 2 session - the files are encrypted anyway); `--nas-only` re-mirrors; restore steps in its docstring | Data survives losing the container, the database or the whole server |
| `check_health.py` | Hourly: any service with 3+ failures making half its calls in 3 h, any credits/billing/login error, a backup (or its NAS copy) older than 30 h, no Garmin HRV for 2 days, a failed ai-coach unit, device gaps (§4.16, their own message); Telegram on new problems and on recovery | Fallbacks no longer hide outages (e.g. Gemini credits running out) |
| `check_intervals_api.py` | One-request API health probe; alerts only on a state change | Run by the 15-min health timer |

### `db/migrations/`: schema history (applied in order)

| File | What it changes |
|---|---|
| `001_init_schema.sql` | Initial `athlete_profile`, `daily_metrics`, `activities` (unique `(source, external_id)`, raw JSONB payloads, indexes) |
| `002_wellness_schema.sql` | Recreates `daily_metrics` with the full Intervals.icu wellness field set |
| `003_training_blocks.sql` | The `training_blocks` table (one row per week of the rolling plan) |
| `004_profile_sync.sql` | Profile columns synced from Intervals.icu (max/resting HR, eFTP, W′, peak power, source, sync time) |
| `005_strava_names.sql` | `activities.strava_id` and `name_before_strava` (undo for renames) |
| `006_block_sessions.sql` | `training_blocks.sessions`, the planned session mix per week |
| `007_strava_notes.sql` | `activities.strava_note` and `strava_note_at` |
| `008_strava_note_context.sql` | `activities.strava_note_context` (the athlete's extra context, reused on re-posts) |
| `009_athlete_settings.sql` | The `athlete_settings` key/value table (training focus) |
| `010_next_session.sql` | `activities.next_session` and `next_session_at` (the shared decision) |
| `011_api_calls.sql` | The `api_calls` log table with time and service indexes |
| `012_adaptive_blocks.sql` | `training_blocks.status`, the verdict on each closed week |
| `013_next_session_proposals.sql` | `next_session_proposals` (chat changes awaiting Apply) and `activities.next_session_source` |
| `014_readonly_role.sql` | The `coach_reader` role: SELECT on `activities`, `daily_metrics`, `training_blocks`, `athlete_profile` only; read-only, 15 s timeout |
| `015_garmin_health.sql` | Garmin health columns on `daily_metrics` (sleep stages/timing, HRV status and baseline, stress, Body Battery, respiration, SpO2, daily activity, Fitness Age, `garmin_payload`) |
| `016_athlete_feedback.sql` | `session_feedback` (RPE, legs per activity) and `daily_checkin` (body, motivation per day), readable by `coach_reader` |
| `018_checkin_sleep_stress.sql` | `daily_checkin`: `sleep` and `stress` answers, body option `off` ("a bit off") |
| `017_gym_sets.sql` | `gym_sets`: logged lifts (exercise, kg, reps, sets) per day, linked to that day's gym activity |

### `deploy/`: systemd units (copied to `/etc/systemd/system/`)

| File | What it runs |
|---|---|
| `ai-coach-v1.service` | `app.py`, the web dashboard and API (restart on failure) |
| `ai-coach-telegram.service` | `telegram_bot.py` (a separate process, so a bot crash never takes down the web UI) |
| `ai-coach-strava-notes.service` / `.timer` | `post_strava_notes.py` every 10 min |
| `ai-coach-intervals-health.service` / `.timer` | `check_intervals_api.py --notify` every 15 min |
| `ai-coach-backup.service` / `.timer` | Nightly at 02:45: `scripts/backup.py --notify`, then the NAS copy (writes only `/var/backups/ai-coach`, `state/` and `logs/`) |
| `ai-coach-health.service` / `.timer` | Hourly at :07: `scripts/check_health.py --notify` |
| `logrotate-ai-coach` | Copied to `/etc/logrotate.d/ai-coach`: weekly rotation for the logs of short-lived jobs (`intervals_health`, `health`, `backup`) and `sync-cron.log`, which Python's own daily rotation never rotates |
| `pre-commit` | Git hook: runs `pytest tests` before every commit (`cp deploy/pre-commit .git/hooks/`) |
| `ai-coach-sync-check.service` / `.timer` | Nightly at 03:30: `import_garmin.py --days 15` (a failure doesn't stop the next step), then `check_intervals_sync.py --fix --notify` (last 15 days) |
| `ai-coach-strava-names.service` / `.timer` | `sync_strava_names.py --notify` nightly at 04:15 |

### `web/`: the React dashboard

| File | What it contains | Role in the system |
|---|---|---|
| `.gitignore` | Ignores `node_modules` and `dist` | Keeps build output out of git |
| `index.html` | The HTML shell: dark theme, PWA/iOS home-screen meta, favicon | The entry page Vite builds from |
| `package.json` / `package-lock.json` | Dependencies (React 19, ECharts, lucide icons, react-markdown, Tailwind 4, Vite, TypeScript) and scripts | Reproducible frontend build (`npm run build`) |
| `tsconfig.json` | TypeScript compiler settings | Type checking at build time |
| `vite.config.ts` | Vite with React and Tailwind; the dev server proxies `/api` to port 8000 | Build and dev workflow |
| `public/manifest.webmanifest` | PWA manifest (name, colours, icons) | "Add to home screen" as an app |
| `public/icon-192.png`, `public/icon-512.png`, `public/apple-touch-icon.png` | App icons | Home-screen icon on Android and iOS |
| `src/main.tsx` | Mounts `<App/>` | React entry point |
| `src/App.tsx` | Page layout (desktop grid / mobile tabs), header with System and Sync buttons, `#system` page switch, chat side panel, refresh wiring | Arranges every card and page |
| `src/api.ts` | Typed fetch functions for every `/api` endpoint plus the SSE chat stream parser | The only place the UI talks to the backend |
| `src/types.ts` | TypeScript types mirroring the API responses | Keeps the UI and API in step |
| `src/index.css` | Tailwind import, theme colours, fonts, base styles | The dark, colourful look |
| `src/components/Card.tsx` | The shared `Card`, `Segmented` toggle and `Skeleton` loader | Consistent card styling |
| `src/components/Chart.tsx` | A tree-shaken ECharts wrapper | Small bundle for every chart |
| `src/components/KpiRow.tsx` | Top tiles: fitness, fatigue, form, FTP (with eFTP drift), 7-day HRV, this week's load | At-a-glance status |
| `src/components/NextSessionCard.tsx` | The shared next-session decision with its reason; "decide again" button | Shows the coach's one decision |
| `src/components/PmcChart.tsx` | CTL/ATL/TSB chart with a range toggle | Fitness trend |
| `src/components/WellnessChart.tsx` | HRV, resting HR, sleep and overnight SpO2 chart against the baseline | Recovery trend |
| `src/components/WeeklyChart.tsx` | Weekly load by sport against the plan target | Load versus plan |
| `src/components/WorkoutCard.tsx` | The next training day's structured workouts with Push / Skip / Remove (Intervals.icu) | Send a workout to the calendar from the dashboard |
| `src/components/WeekAheadCard.tsx` | The next 7 days: sessions per day with targets, pinned and hard sessions marked, dropped filler | The day-by-day plan |
| `src/components/PlanCard.tsx` | Upcoming weeks: phase, load range, planned vs done sessions, cycling/dual focus toggle | The training plan view and focus switch |
| `src/components/InjuryCard.tsx` | Injury findings by severity, expandable, "ask about this" | Risk visibility |
| `src/components/RaceCard.tsx` | Next race: countdown, course, weather, predicted time, goal | Race focus |
| `src/components/ActivitiesTable.tsx` | Recent activities with sport icons, metrics, Strava link and note button | Activity log and the Strava-note entry point |
| `src/components/StravaNoteDialog.tsx` | Preview → add context → rewrite → post dialog for a Strava note | Manual note editing from the dashboard |
| `src/components/SystemPage.tsx` | The System page: cost KPIs, calls-over-time chart with a Gemini cost line, spend donut, process → reason → service Sankey, busy-hours chart, model table, recent-calls log | Shows how the system operates and what Gemini costs |
| `src/components/ChatPanel.tsx` | Streaming chat with agent badges, Markdown, Local-only toggle, history | Talking to the agents from the web |
| `src/hooks/useMediaQuery.ts` | `useMediaQuery` / `useIsMobile` hooks | The mobile layout switch |
| `src/lib/colors.ts` | The colour palette: sports, session kinds, severities, agents, shared chart styling | Visual consistency |
| `src/lib/format.ts` | Number, duration, date, "time ago" and form-zone formatters | Consistent text formatting |

### Not in git (present on the server only)

| Path | What it is |
|---|---|
| `.env` | Real secrets and settings (see `.env.example`) |
| `credentials.json`, `token.json` | Google Calendar OAuth (life agent) |
| `state/strava_tokens.json`, `state/withings_tokens.json` | OAuth tokens written by the auth scripts and refreshed by the app |
| `state/` | `telegram_bot.pickle` (chat memory, previews, settings), `intervals_health.json` (last API state), `garmin_token.json` (the current 24 h Garmin token, chmod 600), `availability.json` (days off / limited days, §4.11), `plan_today.json` (today's sessions, kept once training starts, §4.6b), `goals.json` + `goals_status.json` (§4.14), `energy.json` (§4.13), `markers.jsonl` + `adjustments.jsonl` + `reviews/` (§4.15), `workouts/<date>.json` (workouts pushed to Intervals.icu, §4.6c), `workouts/pinned.json` (pinned sessions converted to steps), `health_alerts.json` (open problems of the hourly health check), `thresholds.json` + `dfa_windows.json` (LT1 / critical speed and the per-activity DFA-alpha1 windows, §4.6d), `backup_nas.json` (last copy to the NAS), `daily_metrics_health_before_garmin.csv` (the Intervals.icu health values replaced by Garmin on 2026-10-25) |
| `logs/` | Rotating logs for each process, plus `sync-cron.log` |
| `venv/` | The Python virtualenv |
| `/var/backups/ai-coach/` | Encrypted nightly backups (`db-*.dump.enc`, `state-*.tar.gz.enc`), coach-only (0600) |
| `web/node_modules/`, `web/dist/` | Frontend dependencies and the built dashboard (rebuild with `cd web && npm install && npm run build`) |

---

## 6. Common operations

```bash
# restart after code changes
systemctl restart ai-coach-v1 ai-coach-telegram
# rebuild the dashboard after editing web/
cd web && npm run build
# preview Strava notes without posting
venv/bin/python scripts/post_strava_notes.py --dry-run
# apply new migrations
venv/bin/python scripts/create_database.py
# check data completeness against Intervals.icu
venv/bin/python scripts/check_intervals_sync.py          # last 15 days; --full for everything
# re-import Garmin health data by hand (the nightly job covers the last 15 days)
venv/bin/python ingest/import_garmin.py --days 30
# tests (no database or API needed; also run by the pre-commit hook)
venv/bin/python -m pytest -q tests
# backup now / health check now
sudo systemctl start ai-coach-backup.service ai-coach-health.service
# dependency audit (every few months; pip-audit in its own venv, not the app's)
python3 -m venv /tmp/audit && /tmp/audit/bin/pip install -q pip-audit && \
  PIPAPI_PYTHON_LOCATION=$PWD/venv/bin/python /tmp/audit/bin/pip-audit -l
```
