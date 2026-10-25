# AI Coach

A self-hosted, multi-agent AI endurance coach for cyclists (with running and gym support).

It pulls your training data from **Intervals.icu**, your health data (sleep, HRV,
stress, Body Battery...) straight from **Garmin**, and optionally weight from
**Withings** and names from **Strava** into your own Postgres database. A team of specialist AI
agents then coaches you from it. All the numbers (readiness, injury risk, fitness
projections, race pacing) are computed in code; the LLMs only explain and prioritise them.

You reach the coach in three ways:

- **Web dashboard**, an installable PWA: fitness/fatigue/form chart, HRV and sleep,
  weekly load against the plan, adaptive training plan, injury-risk card, next-race
  card with weather and pacing, activity list, a streaming chat, and a System page with
  API-call and Gemini cost tracking.
- **Telegram bot**: a morning briefing and check-in, a two-tap RPE/legs question after
  each session, chat with memory, what-if simulations, and Strava note editing. Tell it
  "sick for 3 days", "only 40 min today" or "swap Tuesday's intervals with Thursday's
  long ride" and the plan works around those exact dates, then resumes by itself.
- **Strava**: an optional short coach note appended to each new activity's description.

> This is a personal project shared as a starter kit. It was built for one athlete on
> a home server, so expect to read some code and adapt it to your own setup. See
> [Customising it for yourself](#7-customising-it-for-yourself).

---

## Contents

1. [How it works](#1-how-it-works)
2. [What you need](#2-what-you-need)
3. [Installation](#3-installation)
4. [Connecting the data sources](#4-connecting-the-data-sources)
5. [Running it](#5-running-it)
6. [Running it as a service (systemd)](#6-running-it-as-a-service-systemd)
7. [Customising it for yourself](#7-customising-it-for-yourself)
8. [Security and privacy](#8-security-and-privacy)
9. [Day-to-day operations](#9-day-to-day-operations)
10. [Troubleshooting](#10-troubleshooting)
11. [Project layout](#11-project-layout)

For the full design (every decision rule and every file), read
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## 1. How it works

```
 Garmin/Wahoo/... ──► Intervals.icu ──┐                     ┌──► Web dashboard (React)
 Garmin (health) ─────────────────────┤                     │
 Withings (weight) ───────────────────┼──► ingest/ ──► Postgres ◄──┤
 Strava (names, notes) ◄─────────────►┘                     ├──► Telegram bot
                                                    ▲       └──► Strava descriptions
                                                    │
                     core/   deterministic logic: metrics, readiness, injury checks,
                             adaptive plan, simulators, race planner
                                                    │
                     graph.py (LangGraph): preprocessor ─► supervisor ─► one agent
                                                    │
                     agents/ Gemini (cloud) or a local Ollama model
```

When you ask a question, a small local model (the **supervisor**) routes it to one
specialist:

| Agent | Handles | Model |
|---|---|---|
| **AI Coach** | Training advice and the next session (the default) | Gemini |
| **Injury Prevention** | Pain, overtraining, recovery, HRV/sleep | Local (narrates a computed report) |
| **Racing Strategy** | Race pacing, fuelling, taper, race week, post-race review | Gemini |
| **History Analyst** | Any past period: trends, records, what built your fitness | Gemini + read-only SQL |
| **Athlete Twin** | What-ifs: CTL projections, climb times, weight/power changes | Gemini |

A **"Local only"** switch (dashboard and Telegram) answers everything with the local
model instead, which costs nothing but gives shorter answers.

**Intervals.icu is the source of truth for training.** Activities, the fitness/fatigue
model (CTL/ATL), FTP/LTHR and your race calendar all come from it, so you edit
thresholds and add races there. A/B races anchor the adaptive plan (race prep, taper,
race week, recovery); with no race it works towards fitness and FTP. Test FTP every 6-8 weeks:
after 8 weeks without a test, targets use Intervals.icu's eFTP until you retest. **Garmin is the only source of health data**: sleep
and sleep stages, HRV and HRV status, resting and sleeping HR, stress, Body Battery,
respiration, SpO2 and steps come straight from the Garmin Connect Personal API.

---

## 2. What you need

### Accounts and API keys

| Service | Required? | Used for | Cost |
|---|---|---|---|
| [Intervals.icu](https://intervals.icu) | **Yes** | All training data, fitness model, race calendar | Free |
| [Garmin Connect](https://connect.garmin.com) Personal API key | **Yes** for health data | Sleep, HRV, resting HR, stress, Body Battery, SpO2, steps | Free |
| [Google AI Studio](https://aistudio.google.com/apikey) (Gemini API key) | **Yes** for the cloud agents | Coach, Racing, History, Twin, Strava notes | Pay per use; the System page shows the cost |
| [Ollama](https://ollama.com) | **Yes** (routing and Local only) | Supervisor, injury narration, Local-only mode | Free; runs on your own machine |
| Telegram bot (via [@BotFather](https://t.me/BotFather)) | Optional | Morning briefing and chat on your phone | Free |
| [Strava API app](https://www.strava.com/settings/api) | Optional | Syncing activity names, coach notes on activities | Free |
| [Withings developer app](https://developer.withings.com/) | Optional | Body-weight history | Free |
| Google Cloud OAuth client | Optional (disabled by default) | Calendar-aware "life" agent | Free |

Your activities need to reach Intervals.icu. Connect Garmin, Wahoo, Zwift or Strava in
Intervals.icu → Settings. Health data is read from Garmin only (§4.6): without a Garmin
watch and key, the HRV, sleep and recovery checks have no data.

### Software

- **Linux** server or VM. Any always-on box works: a home server, an LXC container, a VPS.
- **Python 3.12+**
- **PostgreSQL 14+** (developed on 16)
- **Node.js 20+** and npm, to build the dashboard
- **Ollama** with a model that supports tool/structured output; the default is
  `qwen3:8b`, which needs about 6–7 GB of VRAM. It can run on another machine on your
  network.

---

## 3. Installation

The systemd units in `deploy/` assume the project lives in **`/opt/ai-coach-v1`**. If
you install somewhere else, edit the paths in those files (see §6).

### 3.1 Get the code and Python dependencies

```bash
sudo git clone https://github.com/<you>/<this-repo>.git /opt/ai-coach-v1
cd /opt/ai-coach-v1
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 3.2 Create your `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Fill it in:

| Variable | What to put there |
|---|---|
| `ADMIN_DATABASE_URL` | A Postgres superuser URL to the maintenance DB, used once to create the database, e.g. `postgresql://postgres:PASSWORD@localhost:5432/postgres` |
| `DATABASE_URL` | The app's database, e.g. `postgresql://postgres:PASSWORD@localhost:5432/athlete_twin_v2`. The last path segment is the database name that gets created. |
| `READONLY_DATABASE_URL` | Leave empty: `scripts/setup_readonly_role.py` writes it (§3.4) |
| `INTERVALS_ATHLETE_ID` | Your athlete id, e.g. `i12345` (Intervals.icu → Settings, or the number in your profile URL) |
| `INTERVALS_API_KEY` | Intervals.icu → Settings → Developer Settings → API key |
| `GARMIN_PERSONAL_API_KEY` | Garmin Connect (web) → Personal API Key (§4.6) |
| `GEMINI_API_KEY` | From Google AI Studio |
| `OLLAMA_BASE_URL` | e.g. `http://localhost:11434`, or the address of your GPU machine |
| `WITHINGS_CLIENT_ID` / `WITHINGS_CLIENT_SECRET` | Optional, from your Withings developer app (§4.3) |
| `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` | Optional, from your Strava API app (§4.2) |
| `TELEGRAM_BOT_TOKEN` | Optional, from @BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated numeric Telegram user ids allowed to use the bot. Ask [@userinfobot](https://t.me/userinfobot) for yours. **Required if the bot runs**: it refuses to start without it. |

Optional settings (commented out in `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `OLLAMA_MODEL` | `qwen3:8b` | Local model for routing, injury answers and Local-only mode |
| `HOME_LOCATION` | built-in default | Town used for race weather when a race has no location, e.g. `Girona, Spain` |
| `BRIEFING_TIME` | `07:00` | Daily Telegram briefing time, in the app's timezone (§7.1) |
| `LIFE_AGENT_ENABLED` | `false` | Enables the Google Calendar agent (§4.4) |

### 3.3 Create the database

```bash
venv/bin/python scripts/create_database.py
```

This creates the database named in `DATABASE_URL` and applies every migration in
`db/migrations/` in order. It's safe to re-run: applied migrations are tracked in
`schema_migrations`.

### 3.4 Create the read-only role for the History Analyst

The History Analyst can run SQL that the model writes, so that SQL runs as a separate
role that can only `SELECT` from four tables, inside a read-only transaction with a
15-second timeout:

```bash
venv/bin/python scripts/setup_readonly_role.py
```

This sets a random password on the `coach_reader` role and writes
`READONLY_DATABASE_URL` into `.env`. Re-run it any time to rotate the password.

### 3.5 Pull in your history

```bash
venv/bin/python ingest/import_intervals.py --days 3650   # activities (as far back as you like)
venv/bin/python ingest/import_wellness.py  --days 3650   # CTL/ATL, eFTP, VO2max
venv/bin/python ingest/import_garmin.py    --days 3650   # sleep, HRV, resting HR, stress, Body Battery (slow: ~1 s/day)
venv/bin/python ingest/import_profile.py                 # FTP, LTHR, max/resting HR, weight
venv/bin/python scripts/update_plan.py                   # build the adaptive training plan
```

### 3.6 Set up the local model

On the machine running Ollama:

```bash
ollama pull qwen3:8b
```

If Ollama runs on another machine, make it listen on the network
(`OLLAMA_HOST=0.0.0.0`) and point `OLLAMA_BASE_URL` at it.

### 3.7 Build the dashboard

```bash
cd web
npm install
npm run build      # outputs web/dist, which app.py serves at /
cd ..
```

---

## 4. Connecting the data sources

### 4.1 Intervals.icu (required)

Nothing to do beyond the `.env` values. To have races planned, add them to your
Intervals.icu calendar as **Race A / B / C** events and put details in the description:

```
Distance: 120 km
Elevation: 1500 m
Location: Girona, Spain
Goal: finish under 5h
```

### 4.2 Strava (optional: names and coach notes)

1. Create an app at <https://www.strava.com/settings/api> with **Authorization Callback
   Domain = `localhost`**.
2. Put its Client ID and Secret into `.env`.
3. Run the one-time OAuth flow in a terminal:
   ```bash
   venv/bin/python ingest/strava_auth.py
   ```
   Open the printed link and authorise with **all boxes ticked**. Your browser then
   redirects to a broken `localhost` page; that's expected. Paste the `code` value from
   the URL bar back into the script. This writes `strava_tokens.json`; see
   `strava_tokens.json.example` for its shape. The app refreshes the tokens on its own
   after that.

What it enables:
- `scripts/sync_strava_names.py`: Strava becomes the source of truth for activity
  names. Rename on Strava and the name propagates to Intervals.icu and the DB.
- `scripts/post_strava_notes.py`: a short coach note is appended to each new
  activity's description (see §8 for what it never mentions). Put `#nocoach` in a
  description to opt that activity out.
- `scripts/backfill_strava_history.py`: a one-time upload of Strava-only activities
  (old devices, Zwift...) to Intervals.icu. Try `--dry-run` first.

### 4.3 Withings (optional: body weight)

1. Register an app at <https://developer.withings.com/> (dashboard:
   <https://account.withings.com/partner/dashboard_oauth2>) with callback URI
   `http://localhost`.
2. Put its Client ID and Secret into `.env`.
3. Run `venv/bin/python ingest/withings_auth.py` in a terminal and follow the prompts.
   This writes `withings_tokens.json` (see `withings_tokens.json.example`).
4. Import: `venv/bin/python ingest/import_withings.py --days 365`

If your scale already syncs weight to Intervals.icu, you can skip Withings.

### 4.4 Google Calendar (optional, disabled by default)

The "life optimization" agent reads your upcoming calendar events. To use it, create
a Google Cloud OAuth client (type *Desktop app*) with the Calendar API enabled, save
the client JSON as `credentials.json` (see `credentials.json.example`), and produce a
`token.json` (see `token.json.example`) with the `calendar.readonly` scope using
Google's standard
[Python quickstart](https://developers.google.com/calendar/api/quickstart/python) flow.
Then set `LIFE_AGENT_ENABLED=true`.

### 4.5 Telegram (optional)

1. Create a bot with [@BotFather](https://t.me/BotFather) and put the token into
   `TELEGRAM_BOT_TOKEN`.
2. Put your own numeric user id into `TELEGRAM_ALLOWED_USER_IDS`. Messages from anyone
   else are ignored.
3. Run `venv/bin/python telegram_bot.py` (or the systemd unit, §6) and send `/start`.

Commands: `/twin <what-if>`, `/local` (toggle Local only), `/strava [date] [context]`,
`/briefing`, `/sync`, `/reset`. The bot uses long polling, so nothing needs to be exposed
to the internet.

### 4.6 Garmin (health data)

1. Log in to Garmin Connect on the web, open **Personal API Key**, select **Create Key**,
   accept the terms and pick an expiry (30 days to 1 year). The key is shown only once.
2. Put it into `GARMIN_PERSONAL_API_KEY`. The importer exchanges it for a 24-hour access
   token by itself and keeps it in `state/garmin_token.json`, because Garmin rate-limits
   token requests.
3. Import your history: `venv/bin/python ingest/import_garmin.py --days 3650`. It
   commits every 28 days and prints progress; if it stops, resume with
   `--start <the first day it didn't reach>`. After that, every sync pulls the last 5 days.

When the key expires, the sync logs "Garmin health: sync failed". Create a new key and
replace it in `.env`, then restart. Turn on Pulse Ox on the watch if you want SpO2.
The Personal API is meant for your own data only.

---

## 5. Running it

```bash
venv/bin/python app.py
```

Open `http://<server>:8000/`. The web app listens on all interfaces on port 8000. Read
§8 before exposing it anywhere.

New data is pulled by calling `POST /sync-data`, which imports the last 5 days of
activities and wellness and refreshes the profile. Schedule it with cron:

```bash
crontab -e
*/30 * * * * curl -s -X POST http://localhost:8000/sync-data >> /opt/ai-coach-v1/logs/sync-cron.log 2>&1
```

The dashboard's Sync button and Telegram's `/sync` do the same on demand.

### Frontend development

```bash
cd web && npm run dev     # Vite dev server; proxies /api to localhost:8000
```

---

## 6. Running it as a service (systemd)

`deploy/` contains ready-made units:

| Unit | Runs |
|---|---|
| `ai-coach-v1.service` | The web app and API (`app.py`) |
| `ai-coach-telegram.service` | The Telegram bot |
| `ai-coach-strava-notes.timer` | Coach notes on new Strava activities, every 10 min |
| `ai-coach-intervals-health.timer` | Intervals.icu API health check (Telegram alert on outage), every 15 min |
| `ai-coach-sync-check.timer` | Nightly at 03:30: the last 30 days from Garmin, then a full reconciliation with Intervals.icu |
| `ai-coach-plan.timer` | Nightly adaptive-plan update, 04:00 |
| `ai-coach-strava-names.timer` | Nightly Strava → Intervals.icu name sync, 04:15 |

```bash
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-coach-v1 ai-coach-telegram
sudo systemctl enable --now ai-coach-intervals-health.timer ai-coach-sync-check.timer ai-coach-plan.timer
# only if you set up Strava:
sudo systemctl enable --now ai-coach-strava-notes.timer ai-coach-strava-names.timer
```

Notes:
- The units use `/opt/ai-coach-v1`. Change `WorkingDirectory` and `ExecStart` if you
  installed elsewhere.
- The units don't set `User=`, so they run as root. On a shared machine, add a
  dedicated user (`User=aicoach`) and give it ownership of the project folder.
- Leave out the units for services you don't use.

---

## 7. Customising it for yourself

These personal assumptions are hardcoded. Review them before relying on the advice.

### 7.1 Timezone

The app uses **`Europe/Bratislava`** for "today", the briefing time and local-day
queries. Replace it with your own [IANA timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)
in all of these places:

```bash
grep -rn "Europe/Bratislava" --include=*.py .
```

The files are `core/briefing.py`, `core/usage.py`, `core/history_stats.py`,
`core/history_query.py`, `graph.py` and `telegram_bot.py`.

### 7.2 Athlete traits and coaching doctrine

- **Racing fuelling assumptions:** `agents/racing.py` and `agents/local.py` tell the
  model the athlete *sweats heavily* and is a *habitual high caffeine user* (60–90 g/h
  carbs). `core/race_planner.py` sizes fluids, sodium and caffeine the same way. Edit
  these to match you.
- **Training focus:** `core/focus.py` defines the two modes you toggle on the
  dashboard: `cycling` (maximise FTP; running is maintenance, at most one 5 km run a
  week) and `dual` (a winter block where bike and run are both primary).
- **Coach rulebook:** the ranked rules (gym scheduling, return-to-training, sleep,
  periodization, readiness) are in `agents/ai_coach.py`; the local version is in
  `agents/local.py`.
- **Gym sessions:** a gym session only counts as leg work if its **title** mentions
  legs, lower body, full body, squats, deadlifts or lunges (`core/gym.py`). Name your
  gym activities accordingly.
- **Sleep minimum:** `SLEEP_MIN_HOURS = 6.5` in `core/injury_checks.py`.
- **Race-name detection:** `core/history_stats.py` (`RACE_NAME_RE`) recognises race
  names in English and Slovak. Add words in your own language.

### 7.3 Adaptive plan start date

The plan engine replays every week since `REPLAY_FROM` in `core/periodization.py`
(`date(2026, 1, 5)`, a Monday). Set it to the Monday you want your plan to start from,
then run `venv/bin/python scripts/update_plan.py --dry-run` to preview it.

### 7.4 Fallback thresholds

Until `ingest/import_profile.py` has synced your real values from Intervals.icu, the
code falls back to generic numbers (FTP 250 W, LTHR 165 bpm, 75 kg). They're also
seeded by `db/migrations/001_init_schema.sql`. The first sync replaces them.

### 7.5 Models and prices

- Cloud model: `GEMINI_MODEL` in `core/llm.py` (default `gemini-pro-latest`).
- Local model: `OLLAMA_MODEL` in `.env`.
- Cost estimates on the System page use the `PRICES` table in `core/api_log.py`. Update
  it if Google's prices change or you switch models.

---

## 8. Security and privacy

- **The dashboard and API have no login.** Anyone who can reach port 8000 can read
  your health data, chat with the agents (which spends your Gemini credit) and post to
  your Strava. Keep it on your home network or behind a VPN such as Tailscale or
  WireGuard. If you must expose it, put it behind a reverse proxy with authentication.
  Everything lives under `/api`, plus `/`, `/chat`, `/stream_chat` and `/sync-data`.
- **Telegram:** only the user ids in `TELEGRAM_ALLOWED_USER_IDS` get answers, and the
  bot refuses to start without that list.
- **Strava notes are public.** Strava descriptions are visible to anyone who can see
  the activity, so the notes never mention HRV, sleep, readiness, injuries, body weight
  or FTP tests. Those signals only steer the tone privately. Disable the
  `ai-coach-strava-notes.timer` if you don't want notes at all.
- **Model-written SQL** runs as the read-only `coach_reader` role, not as the app's
  database user.
- **Secrets stay out of git.** `.gitignore` excludes `.env`, `credentials.json`,
  `token.json`, `strava_tokens.json`, `withings_tokens.json`, `logs/` and `state/`
  (the Telegram bot's chat memory). Keep `.env` and the token files `chmod 600`.
- **Data sent to Google:** cloud agents send your metrics and recent activities to the
  Gemini API. Use **Local only** mode if you'd rather keep a conversation on your own
  hardware.

---

## 9. Day-to-day operations

```bash
# restart after changing code
sudo systemctl restart ai-coach-v1 ai-coach-telegram
# rebuild the dashboard after editing web/
cd web && npm run build
# apply new migrations after pulling an update
venv/bin/python scripts/create_database.py
# check the DB holds everything Intervals.icu has (add --fix to import gaps)
venv/bin/python scripts/check_intervals_sync.py
# preview Strava notes without posting
venv/bin/python scripts/post_strava_notes.py --dry-run
# preview the adaptive plan
venv/bin/python scripts/update_plan.py --dry-run
# logs (14 days, rotated daily)
tail -f logs/app.log logs/telegram.log
```

---

## 10. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `/` shows a plain chat page instead of the dashboard | The dashboard isn't built: run `cd web && npm install && npm run build` |
| Every answer comes from the AI Coach | Ollama isn't reachable (the supervisor falls back to the coach). Check `OLLAMA_BASE_URL` and that the model is pulled. |
| History questions fail with `READONLY_DATABASE_URL is not set` | Run `scripts/setup_readonly_role.py` and restart |
| `strava_tokens.json not found` | Run `ingest/strava_auth.py` (§4.2) |
| `Strava token lacks activity:write` | Re-run `ingest/strava_auth.py` with every box ticked |
| Telegram bot exits at start | `TELEGRAM_BOT_TOKEN` or `TELEGRAM_ALLOWED_USER_IDS` is missing |
| Empty charts | No data imported yet: run the §3.5 imports, check `INTERVALS_ATHLETE_ID` and `INTERVALS_API_KEY` |
| No HRV/sleep data, sync says "Garmin health: sync failed" | `GARMIN_PERSONAL_API_KEY` missing or expired: create a new key (§4.6) |
| Dates are off by a few hours | The timezone is still the default (§7.1) |

---

## 11. Project layout

```
app.py               FastAPI: dashboard, chat streaming, /sync-data
dashboard_api.py     /api/* JSON endpoints for the dashboard
graph.py             LangGraph routing: preprocessor → supervisor → agent
telegram_bot.py      Telegram front-end
agents/              The specialist agents and their shared tools
core/                Deterministic logic: metrics, readiness, injury checks, plan,
                     simulators, race planner, Strava notes, call logging
ingest/              Intervals.icu / Garmin / Withings / Strava importers and OAuth helpers
scripts/             Database setup, timers' jobs, maintenance tools
db/migrations/       The database schema, applied in order
deploy/              systemd units and timers
web/                 React + Vite + Tailwind dashboard
*.example            Templates for the secret files you create yourself
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the details of every file and decision.
