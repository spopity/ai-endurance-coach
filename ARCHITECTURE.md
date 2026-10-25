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
| `telegram_bot.py` | systemd `ai-coach-telegram.service` | Morning briefing and check-in, session feedback, chat, `/strava` note editor |
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
   switch to their local versions in `agents/local.py`.

### 4.2 The AI Coach's rules (`agents/ai_coach.py`)

A Gemini ReAct agent with tools (injury check, history search, interval detail).
Its prompt holds a ranked rulebook. In short:

- One definitive session, never a menu. Gym can share a day with a ride.
- **Gym sessions:** only a session whose title names legs counts as leg work, and that
  blocks hard leg work the next day. Other gym sessions never justify going easier.
- **Graded return to training** (`return_to_training`, computed by
  `core/periodization.return_status()`): 3–6 days off → a normal endurance session,
  then back to the plan; 7–13 → 2 easy sessions, then the plan at ~75% for that week;
  14+ → 3 easy sessions, then the (stepped-back) plan. Poor sleep (score < 70 or under
  6.5 h, `SLEEP_MIN_HOURS`) means rest or easy.
- **Periodization:** follow the current training block's focus and weekly load range.
  A deload week never contains intervals. Base allows a weekly VO2 touch or sprints
  (under 10 min hard, inside an endurance ride); full VO2max sessions are Build-only.
  When the week runs short (`sessions_remaining` vs `days_left_this_week`), keep the key
  sessions (quality + long) and drop filler; never cram missed load.
- **Readiness overrides the plan:** red means rest or easy only, amber means no VO2max
  (not even the Base touches), green means the full plan.
- The injury-risk tool must be called before prescribing anything hard.
- Stick to the already-decided next session unless something new justifies a change.
  Say "next session", not "tomorrow".
- The athlete's explicit "easy / hard / rest / short / long" overrides the logic.

### 4.3 Readiness (`core/readiness.py`)

A green/amber/red traffic light built from four signals:

- **Form (TSB):** −20 or below is amber, −30 or below is red.
- **7-day HRV versus baseline:** "below" is amber, "low" is red.
- **The injury check's overall level:** caution is amber, high is red.
- **The athlete's own feedback** (`core/feedback.signals()`, §4.11): ill (check-in or a
  sick day in the availability file) is red; very sore, sore + low motivation, or sore legs
  after the last hard session are amber.

The briefing, the coach's context, the local coach and (privately) the Strava notes all
use the same verdict.

### 4.4 Injury and overreaching checks (`core/injury_checks.py`)

Each check is judged against **the athlete's own baselines**, not textbook constants:

- **Load:** ACWR, TSB, ramp rate, monotony, weeks without recovery.
- **Recovery:** HRV, resting HR, sleep versus baseline.
- **Cardio drift:** decoupling on steady rides, efficiency-factor trend.
- **Biomechanics:** cadence versus usual for that ride type, L/R balance.
- **Subjective:** feel/RPE versus objective data.
- **Running:** volume progression.

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
  - Base comes first; Build unlocks after 6 completed Base weeks **within the last 10**.
    Partial and missed weeks don't count but cost nothing (a week off costs little
    fitness). The only step back is CTL falling 25%+ below the phase peak: about one
    base week to redo per 10% of CTL lost (from Build: back to Base with that many to redo).
  - A recovery week comes when form or HRV call for it after at least 2 loading weeks
    (TSB < -25, HRV 7-day "low", or "below" with TSB < -15 - the readiness formula,
    `hrv_trend()`), and at the latest after 4. Only a week at or above ~7×CTL (the load
    that holds fitness) is a loading week; a missed week counts as rest.
  - Targets come from the current CTL: ~7×CTL holds fitness, plus a CTL ramp of
    2/week in Base and 3/week in Build; recovery weeks are 50-70% of 7×CTL.
  - Future weeks are a projection assuming completion (this week: actual load so far
    plus the days left; deloads every 3 loading weeks, since future HRV is unknown).
    `notes` says why each week has its phase (shown on the plan card and given to the
    coach); hand edits are overwritten.
  - **Races anchor the plan** (Intervals.icu RACE_A/B events, past ones included so the
    replay knows a race happened). A race: race-specific "Race prep" 2–6 weeks out
    (Build starts early within 12 weeks, from 3/6 base weeks), "Taper" the week before
    (70–85% of the holding load, intensity kept), the "Race" week with openers (55–80%),
    then a recovery week; within 3 weeks of it the taper replaces a calendar deload. B race: a
    mini-taper in its own week. C races are hard sessions. **No A/B race:** the plan
    works towards fitness and FTP (`GOAL_NO_RACE`, `current_training_block.goal`).
  - **Days off from the availability file** (§4.11) scale that week's targets pro rata;
    with at most 2 days available the week is "away" (rest, nothing lost, not counted
    in the base window).
  - Base keeps a little intensity: early Base a weekly sprint/VO2 touch inside an
    endurance ride, late Base adds a tempo session.
- Completed sessions are classified from Intervals.icu zone times:
  - Rides are VO2 (≥10 min in Z5), sweet spot (≥20 min SS or ≥15 min Z4), or endurance.
  - Runs, weights, and other types are counted separately.
- **Training focus** (dashboard toggle):
  - `cycling`: FTP first, running is maintenance.
  - `dual`: the winter block, with bike and run both primary.
  The dual setting is applied when the plan is *read* (one more run, one ride and one
  gym session fewer), so switching back restores the stored plan exactly.
- **Gym sessions:** the athlete titles each gym session after the muscles worked.
  `core/gym.gym_focus()` marks a session as leg work only if the title says so ("legs",
  "lower body", "full body", "squat", "deadlift", "lunge"). Anything else is treated as
  upper body/core with no effect on leg freshness.

### 4.7 Strava coach notes (`core/strava_notes.py`)

Every 10 minutes the timer looks for activities of **any type** that are at least
20 min long, less than 2 days old, at least 15 min past import, and not yet noted.

- **Text:** a facts block computed in code (session type, intervals, duration/distance,
  NP/IF or pace, TSS, kJ, temperature, fitness change, achievements), plus 2–3 Gemini
  sentences that end with the shared next session.
- **Privacy:** Strava descriptions are public, so the notes never mention HRV, sleep,
  readiness, injuries, weight or FTP tests. Readiness and the coach's reasons are passed
  to the model privately, only to steer the tone.
- **Description handling:** the note is appended below the athlete's own text. A
  re-post replaces only the previous note. `#nocoach` in the description skips the
  activity.
- **Manual posting:** Telegram `/strava [date] [context]` and the dashboard dialog
  preview a note, allow a rewrite with extra context, and post it. Manual posts
  override `#nocoach`.

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
  - **Chat** (with 3 h conversation memory), `/twin`, `/local`, `/strava`, `/briefing`,
    `/sync`, `/reset`.
  - **Feedback** (§4.11): RPE and legs after each new session (checked every 10 min,
    not between 22:00 and 07:00), and a check-in (body, motivation) after the briefing.
  - Only user ids listed in `TELEGRAM_ALLOWED_USER_IDS` get answers.

### 4.11 FTP, feedback and availability (`core/metrics.py`, `core/feedback.py`, `core/availability.py`)

- **Training FTP** (`metrics.training_ftp()`): targets use the set FTP while it is
  fresh. "Updated" is the later of the first ride with the current FTP (Intervals.icu
  stamps `ftp_at_time` on each activity) and the last ride named like an FTP test. It
  follows a test every 6-8 weeks: over 6 weeks old → `ftp_check` says a test is due;
  over 8 weeks without one → targets use eFTP (`ftp_source = "eftp"`, the dashboard says
  "FTP (from eFTP)") until the next test. The same profile feeds the coach, simulators
  and race planner.
- **Feedback** (tables `session_feedback`, `daily_checkin`, migration 016 - kept apart
  from `activities.rpe/feel`, which Intervals.icu overwrites): Telegram buttons only,
  so no number comes from a model. RPE and legs are asked **only after hard rides and
  runs** (`feedback.is_hard()`: rides classified sweet spot / VO2, runs with 10+ min in
  HR Z4+ or 25+ min in Z3+, FTP tests, races); gym, easy and endurance sessions are covered by the
  next morning's check-in, which names them. It feeds readiness (§4.3) and
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

---

## 5. Every file in the project

### Root

| File | What it contains | Role in the system |
|---|---|---|
| `.env.example` | The names of every environment variable (DB URLs, Intervals.icu, Garmin, Gemini, Ollama, Withings, Strava, Telegram, optional settings) with no values | The template for the real `.env`; documents what must be configured |
| `.gitignore` | Excludes secrets, tokens, `logs/`, `state/`, `venv/`, `web/node_modules`, `web/dist` | Keeps credentials and runtime data out of GitHub |
| `ARCHITECTURE.md` | This document | The map of the system |
| `requirements.txt` | Pinned Python dependencies (FastAPI, LangGraph/LangChain, Ollama + Gemini bindings, psycopg2, python-telegram-bot…) | Makes the `venv` reproducible |
| `app.py` | The FastAPI app: serves the React dashboard and PWA files, the `/classic` chat page, `/chat` SSE streaming and `/sync-data`; includes the `/api` router | The main web process and the target of the 30-min sync cron |
| `dashboard_api.py` | The `/api/*` JSON endpoints: overview, PMC, wellness, weekly load, plan, focus get/set, next session, activities, Strava-note preview/post, injury report, races, usage (System page), chat, sync | Everything the dashboard shows or does goes through here |
| `graph.py` | The LangGraph workflow: preprocessor (metrics + readiness + next session), local-model supervisor router, agent nodes, local/cloud switch | The brain: decides which agent answers and what context it gets |
| `telegram_bot.py` | The Telegram bot: access control, chat with memory, `/twin`, `/local`, `/strava` preview/post buttons, `/briefing`, `/sync`, `/reset`, the daily briefing job, session-feedback and check-in buttons | The mobile front-end, the daily push message and the athlete's feedback |
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
| `readiness.py` | The green/amber/red verdict from TSB, the HRV trend, the injury level and the athlete's feedback | The single "can I train hard?" answer |
| `injury_checks.py` | Deterministic risk checks (load, intensity distribution, recovery, drift, biomechanics, subjective, running) against personal baselines, plus text/Markdown renderers | Injury agent, coach tool, dashboard card, readiness input |
| `feedback.py` | Session RPE/legs and the morning check-in: what to ask, saving taps, readiness signals, coach context | The athlete's own voice in readiness and the plan |
| `availability.py` | `state/availability.json`: days off / limited days / planned sessions as exact dates, self-expiring into history; day status, unavailable days, last illness, coach context | Plans around sickness, travel and busy days, and resumes by itself |
| `next_session.py` | Decides, stores and reads the one shared next-session decision; `refresh()` re-decides a stale one (24 h without an activity, every morning) | Keeps Strava, the dashboard, the briefing and chat consistent, and never lets a skipped session sit there |
| `periodization.py` | Adaptive phase engine: replays closed weeks (completed/partial/missed), rolling Base→Build gate, CTL-drop step-back, form/HRV-timed deloads, CTL-based targets, projection; `hrv_trend()` and the graded `return_status()` | Decides what phase each week is and why, and how to come back after days off |
| `plan.py` | Session classification (endurance/SST/VO2/run/weights), planned vs done per week, plan summary for prompts | The plan card and the coach's view of "what's left this week" |
| `focus.py` | Cycling vs dual-sport focus: goals, running rule, session-mix and focus-text adjustments, saved in `athlete_settings` | Switches the whole coaching doctrine for winter |
| `gym.py` | `gym_focus()`: a gym session counts as leg work only if its title names legs | Stops upper-body/core sessions from limiting bike/run work |
| `simulators.py` | CTL/ATL/TSB forward projection (Intervals.icu model); cycling physics for speed and time on climbs/segments | Numbers for the Twin and the race planner |
| `race_planner.py` | Deterministic race plan (course, pacing, fuelling, caffeine, taper, race week) and post-race review | Racing agent output; the dashboard race card |
| `races.py` | Races from the Intervals.icu calendar (`fetch_races` for any range, `fetch_upcoming_races`; with description parsing) and past races from history | Race lists for the agents, briefing, dashboard and the race-anchored plan |
| `weather.py` | Open-Meteo geocoding, forecast and 5-year climatology | Race-day conditions for pacing and fuelling |
| `llm.py` | LLM factories: `local_llm` (Ollama `qwen3:8b`, thinking off, 8k context, 30 min keep-alive) and `gemini_llm` (`gemini-pro-latest`), each tagged with the component using it | One local model for everything (the GPU never reloads); every LLM call is logged |
| `api_log.py` | Records every outgoing call into `api_calls`: LangChain usage callback (tokens, cost estimate, price table), `requests` hook for HTTP services, `trigger()` reason tags | Source data for the System page and Gemini cost tracking |
| `usage.py` | Aggregates `api_calls` for `/api/usage`: totals, timeline, cost projection, components, flows, busy hours, recent calls | Feeds the dashboard's System page |
| `chat.py` | Shared front-end helpers: `ask_supervisor` (async/sync), SSE `stream_reply`, `run_sync`, agent display names | The web UI, Telegram and timers talk to the graph the same way |
| `history.py` | Conversation memory: the last 6 messages, the format the supervisor sees | Makes follow-up questions work |
| `briefing.py` | The morning briefing: computed facts (readiness, HRV, load vs plan, warnings, race countdown) plus the shared next-session decision, re-checked that morning | The daily Telegram message |
| `strava.py` | A minimal Strava client: token refresh, list/get/update activities, scope check | Used by name sync and coach notes |
| `strava_notes.py` | Builds, merges and posts Strava coach notes for every activity type (facts + Gemini comment + next session), with privacy rules and `#nocoach` | The public coach voice on Strava |
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
| `withings_auth.py` | One-time interactive Withings OAuth that writes `withings_tokens.json` | Setup only |
| `strava_auth.py` | One-time Strava OAuth (read_all + write scopes) that writes `strava_tokens.json` | Setup only; required for names and notes |
| `_sample_wellness.json` | A synthetic example Intervals.icu wellness payload | Reference for the wellness field mapping |

### `scripts/`: maintenance and timers

| File | What it contains | Role in the system |
|---|---|---|
| `create_database.py` | Creates `athlete_twin_v2` if missing and applies any migration not yet recorded in `schema_migrations` | Database setup and upgrades |
| `setup_readonly_role.py` | Enables login for `coach_reader` with a random password and writes `READONLY_DATABASE_URL` to `.env` (re-run to rotate) | Run once after migration 014 |
| `update_plan.py` | Recomputes the adaptive plan (`--dry-run` to preview, `--notify` for Telegram on a phase change) | Keeps `training_blocks` in step with the training done |
| `post_strava_notes.py` | CLI: post pending notes, `--dry-run`, `--activity` for one activity | Run by the Strava-notes timer |
| `sync_strava_names.py` | Matches Strava (last 6 months, `--full` for all) to local activities and pushes Strava names to Intervals.icu and the database | Run by the nightly name timer |
| `backfill_strava_history.py` | One-time: uploads Strava-only activities to Intervals.icu (TCX from streams), links double recordings, re-imports the DB; re-runnable, report in `state/strava_backfill_report.txt` | Run once by hand (2026-10-11) |
| `check_intervals_sync.py` | Reconciliation of activities and wellness over the last 15 days (`--days N`, `--full`), `--fix`, `--notify` | Run by the nightly sync-check timer |
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

### `deploy/`: systemd units (copied to `/etc/systemd/system/`)

| File | What it runs |
|---|---|
| `ai-coach-v1.service` | `app.py`, the web dashboard and API (restart on failure) |
| `ai-coach-telegram.service` | `telegram_bot.py` (a separate process, so a bot crash never takes down the web UI) |
| `ai-coach-strava-notes.service` / `.timer` | `post_strava_notes.py` every 10 min |
| `ai-coach-intervals-health.service` / `.timer` | `check_intervals_api.py --notify` every 15 min |
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
| `src/components/WellnessChart.tsx` | HRV, resting HR and sleep chart against the baseline | Recovery trend |
| `src/components/WeeklyChart.tsx` | Weekly load by sport against the plan target | Load versus plan |
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
| `strava_tokens.json`, `withings_tokens.json` | OAuth tokens written by the auth scripts |
| `state/` | `telegram_bot.pickle` (chat memory, previews, settings), `intervals_health.json` (last API state), `garmin_token.json` (the current 24 h Garmin token, chmod 600), `availability.json` (days off / limited days, §4.11), `daily_metrics_health_before_garmin.csv` (the Intervals.icu health values replaced by Garmin on 2026-10-25) |
| `logs/` | Rotating logs for each process, plus `sync-cron.log` |
| `venv/` | The Python virtualenv |
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
```
