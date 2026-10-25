/**
 * The demo's stand-in for dashboard_api.py: every /api endpoint the dashboard calls,
 * answered from the synthetic dataset. Actions (focus switch, next-session changes,
 * Strava notes, sync) change in-memory state for this visit only - nothing leaves
 * the browser.
 */
import type { FocusMode, NextSession, Overview, PlanWeek, Usage, UsageCall, WeekPoint } from "../types";
import {
  ATHLETE, DATA, RACE, THIS_MONDAY, TODAY, addDays, doneByKind, focusText, hrvStatus, injuryReport, iso, latest,
  planWeek, planWhy, readiness, sessionsFor, stravaNote, weekActivities,
} from "./data";
import { currentWeight, racePlan, raceWeather } from "./calc";

export class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

// --- State for this visit -------------------------------------------------------------

const state = {
  focus: { mode: "cycling" as FocusMode, since: iso(addDays(TODAY, -40)) },
  lastSync: new Date(Date.now() - 12 * 60e3).toISOString(),
  decision: null as NonNullable<NextSession["decision"]> | null,
  decisionVariant: 0,
  proposals: new Map<number, { session: string; why: string | null; status: "pending" | "applied" | "discarded"; at: number }>(),
  nextProposal: 1,
  previews: new Map<string, { id: string; note: string; context: string | null }>(),
  noteContext: new Map<string, string | null>(),
};

const dual = () => state.focus.mode === "dual";
const lastTraining = () => DATA.activities.find((a) => a.kind !== "walk")!;

/** Session options the coach picks from for the next session, given today's plan and readiness. */
const SESSION_OPTIONS = [
  {
    session: "Endurance ride, 90 min Z2 (180-200 W), cadence 90+, no intervals",
    why: "HRV is below your baseline and readiness is amber - bank aerobic volume today and keep Thursday's sweet spot fresh.",
  },
  {
    session: "Controlled sweet spot, 75 min: 3x10 min at 225-235 W (88% FTP), 5 min easy between",
    why: "Form is fine and it's a build week, but HRV is down - shorter blocks than planned keep the stimulus without digging deeper.",
  },
  {
    session: "Recovery spin, 45 min under 150 W, or a full rest day",
    why: "Three loading days in a row and a dip in HRV - one easy day now buys a quality session later this week.",
  },
];

function decide(variant: number): NonNullable<NextSession["decision"]> {
  const a = lastTraining();
  const opt = SESSION_OPTIONS[variant % SESSION_OPTIONS.length];
  return {
    after_activity: a.name, after_activity_id: a.id, decided_at: new Date(Date.now() - 3 * 3600e3).toISOString(),
    decided_by: "coach", decision: `Session: ${opt.session}\nWhy: ${opt.why}`, session: opt.session, why: opt.why,
  };
}
state.decision = decide(0);

export const currentDecision = () => state.decision;
export const getFocus = () => state.focus.mode;

export function createProposal(session: string, why: string | null) {
  for (const p of state.proposals.values()) if (p.status === "pending") p.status = "discarded";
  const id = state.nextProposal++;
  state.proposals.set(id, { session, why, status: "pending", at: Date.now() });
  return { id, session, why };
}

// --- Endpoints ------------------------------------------------------------------------

function overview(): Overview {
  const now = latest();
  const weekAgo = DATA.days[DATA.days.length - 8];
  const monday = iso(THIS_MONDAY);
  const acts = weekActivities(monday);
  const pw = planWeek(0)!;
  const report = injuryReport();
  const plan = racePlan();
  const weight = currentWeight();
  const pct = Math.round(((ATHLETE.eftp - ATHLETE.ftp) / ATHLETE.ftp) * 1000) / 10;
  return {
    today: iso(TODAY),
    athlete: {
      ftp: ATHLETE.ftp, ftp_set: ATHLETE.ftp, ftp_source: "set",
      lthr: ATHLETE.lthr, weight_kg: weight, wkg: Math.round((ATHLETE.ftp / weight) * 100) / 100,
      eftp: ATHLETE.eftp, max_hr: ATHLETE.max_hr,
      ftp_drift: { pct, status: "ok", message: `eFTP ${ATHLETE.eftp} W is within ${Math.abs(pct).toFixed(0)}% of your set FTP ${ATHLETE.ftp} W - zones are up to date.` },
      synced_at: state.lastSync,
    },
    fitness: {
      day: now.day, ctl: now.ctl, atl: now.atl, tsb: Math.round((now.ctl - now.atl) * 10) / 10,
      ramp_rate: Math.round((now.ctl - weekAgo.ctl) * 10) / 10, ctl_change_7d: Math.round((now.ctl - weekAgo.ctl) * 10) / 10,
    },
    hrv: hrvStatus(),
    week: {
      start: monday,
      load: acts.reduce((s, a) => s + (a.load ?? 0), 0),
      hours: Math.round(acts.reduce((s, a) => s + (a.hours ?? 0), 0) * 10) / 10,
      activities: acts.length,
    },
    block: {
      phase: pw.phase!, start_date: monday, end_date: iso(addDays(THIS_MONDAY, 6)),
      target_weekly_load_min: pw.target_min!, target_weekly_load_max: pw.target_max!,
      primary_focus: focusText(pw.key, dual())!, is_deload: pw.key === "recovery",
      load_so_far_this_block: acts.reduce((s, a) => s + (a.load ?? 0), 0),
    },
    injury: { overall: report.overall, counts: report.counts, top: report.findings.filter((f) => f.severity === "high" || f.severity === "caution").slice(0, 2) },
    next_race: {
      ...RACE, days_until: plan.days, missing: [],
      predicted_hours: Math.round(plan.hours * 100) / 100, target_np: plan.np,
      projected_tsb: Math.round(plan.tsb * 10) / 10, target_tsb: [5, 15], weather: raceWeather(),
    },
    last_sync: state.lastSync,
  };
}

function weekly(weeks: number): WeekPoint[] {
  return DATA.weeks.filter((w) => w.week > -weeks && w.week <= 0).map((w) => {
    const acts = weekActivities(w.monday);
    const bySport: Record<string, number> = {};
    for (const a of acts) bySport[a.group] = (bySport[a.group] ?? 0) + (a.load ?? 0);
    return {
      week: w.monday, load: acts.reduce((s, a) => s + (a.load ?? 0), 0),
      hours: Math.round(acts.reduce((s, a) => s + (a.hours ?? 0), 0) * 10) / 10,
      by_sport: bySport, target_min: w.target_min, target_max: w.target_max, phase: w.phase, status: w.status,
    } as WeekPoint;
  });
}

function plan(weeks: number): PlanWeek[] {
  return DATA.weeks.filter((w) => w.week >= 0 && w.week < weeks).map((w) => {
    const deload = w.key === "recovery";
    const current = w.week === 0;
    const end = iso(addDays(new Date(w.monday + "T00:00:00"), 6));
    return {
      week: w.monday, phase: w.phase, is_deload: deload, focus: focusText(w.key, dual()),
      target_min: w.target_min, target_max: w.target_max, sessions: sessionsFor(w.key, dual(), deload),
      why: planWhy(w.week), current,
      done: current ? doneByKind(w.monday) : null,
      load: current ? weekActivities(w.monday).reduce((s, a) => s + (a.load ?? 0), 0) : null,
      races: RACE.date >= w.monday && RACE.date <= end ? [{ name: RACE.name, date: RACE.date, priority: RACE.priority }] : [],
    };
  });
}

function nextSession(): NextSession {
  return { readiness: readiness(), decision: state.decision };
}

function activity(id: string) {
  const a = DATA.activities.find((x) => x.id === id);
  if (!a) throw new HttpError(404, "Activity not found.");
  return a;
}

// --- System page: a plausible week of external calls ------------------------------

const SERVICES = ["intervals", "gemini", "ollama", "strava", "telegram", "open_meteo"];
const PRICE = { model: "gemini-3.1-pro", input_per_m: 2, output_per_m: 12, cached_per_m: 0.2 };
const COMPONENTS = [
  { service: "gemini", component: "ai_coach", share: 0.42, inTok: 6400, outTok: 980, ms: 12800 },
  { service: "gemini", component: "history_analyst", share: 0.18, inTok: 5300, outTok: 690, ms: 8400 },
  { service: "gemini", component: "strava_note_writer", share: 0.22, inTok: 900, outTok: 140, ms: 3900 },
  { service: "gemini", component: "racing_strategy", share: 0.08, inTok: 7800, outTok: 1500, ms: 16500 },
  { service: "gemini", component: "athlete_twin", share: 0.1, inTok: 5100, outTok: 760, ms: 9900 },
  { service: "ollama", component: "supervisor", share: 0.8, inTok: 1200, outTok: 40, ms: 1400 },
  { service: "ollama", component: "injury_prevention", share: 0.2, inTok: 1600, outTok: 180, ms: 4200 },
];
const OPS: Record<string, [string, string, string][]> = {
  intervals: [["GET /api/v1/athlete/{id}/activities", "sync", "app"], ["GET /api/v1/athlete/{id}/wellness", "sync", "app"], ["GET /api/v1/athlete/{id}", "health_check", "check_intervals_api"]],
  strava: [["GET /api/v3/athlete/activities", "strava_note", "post_strava_notes"], ["PUT /api/v3/activities/{id}", "strava_note", "post_strava_notes"]],
  telegram: [["POST /bot{id}/sendMessage", "briefing", "telegram_bot"]],
  open_meteo: [["GET /v1/forecast", "chat", "app"]],
};
const cost = (inTok: number, outTok: number) => (inTok * PRICE.input_per_m + outTok * PRICE.output_per_m) / 1e6;

function usage(days: number): Usage {
  const perDay = { intervals: 260, gemini: 12, ollama: 15, strava: 9, telegram: 3, open_meteo: 1 } as Record<string, number>;
  const bucket = days <= 2 ? "hour" : "day";
  const steps = bucket === "hour" ? days * 24 : days;
  const stepMs = bucket === "hour" ? 3600e3 : 86400e3;
  const startT = bucket === "hour" ? Math.floor(Date.now() / 3600e3) * 3600e3 - (steps - 1) * stepMs : addDays(TODAY, -(steps - 1)).getTime();
  const wave = (i: number) => 0.7 + 0.6 * Math.abs(Math.sin(i * 1.7));
  const timeline = Array.from({ length: steps }, (_, i) => {
    const calls: Record<string, number> = {};
    for (const s of SERVICES) {
      const n = (perDay[s] * wave(i + s.length)) / (bucket === "hour" ? 24 : 1);
      if (n >= 0.5 || (bucket === "hour" && s === "intervals")) calls[s] = Math.max(1, Math.round(n));
    }
    const g = calls.gemini ?? 0;
    return { t: new Date(startT + i * stepMs).toISOString().slice(0, 19), calls, cost: Math.round(g * 0.021 * 1e4) / 1e4 };
  });
  const sum = (s: string) => timeline.reduce((acc, t) => acc + (t.calls[s] ?? 0), 0);
  const geminiCalls = sum("gemini");
  const components = COMPONENTS.map((c) => {
    const calls = Math.max(1, Math.round(sum(c.service) * c.share));
    const inTok = calls * c.inTok;
    const outTok = calls * c.outTok;
    return { service: c.service, component: c.component, calls, errors: 0, input_tokens: inTok, output_tokens: outTok,
      cost: c.service === "gemini" ? Math.round(cost(inTok, outTok) * 1e4) / 1e4 : 0, avg_ms: c.ms };
  });
  const gem = components.filter((c) => c.service === "gemini");
  const inTok = gem.reduce((s, c) => s + c.input_tokens, 0);
  const outTok = gem.reduce((s, c) => s + c.output_tokens, 0);
  const total = gem.reduce((s, c) => s + c.cost, 0);
  const recent: UsageCall[] = Array.from({ length: 40 }, (_, i) => {
    const s = i % 7 === 0 ? "gemini" : i % 7 === 1 ? "ollama" : i % 11 === 2 ? "strava" : "intervals";
    const comp = s === "gemini" ? COMPONENTS[i % 5] : s === "ollama" ? COMPONENTS[5] : null;
    const op = OPS[s]?.[i % (OPS[s]?.length ?? 1)];
    return {
      ts: new Date(Date.now() - (i * 17 + 3) * 60e3).toISOString(), service: s,
      operation: comp ? null : op?.[0] ?? null, component: comp?.component ?? null,
      trigger: comp ? (comp.component === "strava_note_writer" ? "strava_note" : "chat") : op?.[1] ?? null,
      process: comp ? (comp.component === "strava_note_writer" ? "post_strava_notes" : "app") : op?.[2] ?? "app",
      model: s === "gemini" ? "gemini-pro-latest" : s === "ollama" ? "qwen3:8b" : null,
      status: comp ? null : 200, ok: true, duration_ms: comp ? comp.ms + ((i * 137) % 900) : 180 + ((i * 53) % 260),
      input_tokens: comp ? comp.inTok : null, output_tokens: comp ? comp.outTok : null,
      thinking_tokens: s === "gemini" ? Math.round(comp!.outTok * 0.6) : null,
      cost_usd: s === "gemini" ? Math.round(cost(comp!.inTok, comp!.outTok) * 1e4) / 1e4 : null, error: null,
    };
  });
  return {
    days, bucket, logging_since: addDays(TODAY, -45).toISOString(),
    totals: SERVICES.map((s) => ({ service: s, calls: sum(s), errors: s === "intervals" ? Math.round(sum(s) * 0.004) : 0,
      avg_ms: s === "gemini" ? 11000 : s === "ollama" ? 2100 : 280 })).sort((a, b) => b.calls - a.calls),
    gemini: {
      calls: geminiCalls, input_tokens: inTok, output_tokens: outTok, thinking_tokens: Math.round(outTok * 0.6),
      cost: Math.round(total * 1e4) / 1e4, avg_cost_per_call: geminiCalls ? Math.round((total / geminiCalls) * 1e4) / 1e4 : null,
      projected_month: Math.round((total / days) * 30 * 10) / 10, price: PRICE,
    },
    timeline,
    components,
    triggers: [
      { trigger: "sync", calls: Math.round(sum("intervals") * 0.6), cost: 0 },
      { trigger: "health_check", calls: Math.round(sum("intervals") * 0.37), cost: 0 },
      { trigger: "chat", calls: Math.round(geminiCalls * 0.6) + sum("ollama"), cost: Math.round(total * 0.62 * 1e4) / 1e4 },
      { trigger: "strava_note", calls: sum("strava") + Math.round(geminiCalls * 0.22), cost: Math.round(total * 0.08 * 1e4) / 1e4 },
      { trigger: "briefing", calls: sum("telegram") + days, cost: Math.round(total * 0.3 * 1e4) / 1e4 },
    ],
    flows: [
      { process: "app", trigger: "sync", service: "intervals", calls: Math.round(sum("intervals") * 0.6) },
      { process: "check_intervals_api", trigger: "health_check", service: "intervals", calls: Math.round(sum("intervals") * 0.37) },
      { process: "app", trigger: "chat", service: "gemini", calls: Math.round(geminiCalls * 0.6) },
      { process: "app", trigger: "chat", service: "ollama", calls: sum("ollama") },
      { process: "post_strava_notes", trigger: "strava_note", service: "strava", calls: sum("strava") },
      { process: "post_strava_notes", trigger: "strava_note", service: "gemini", calls: Math.round(geminiCalls * 0.22) },
      { process: "telegram_bot", trigger: "briefing", service: "gemini", calls: Math.round(geminiCalls * 0.18) },
      { process: "telegram_bot", trigger: "briefing", service: "telegram", calls: sum("telegram") },
    ].filter((f) => f.calls > 0),
    by_hour: Array.from({ length: 24 }, (_, h) => ({
      hour: h,
      calls: {
        intervals: Math.round((perDay.intervals / 24) * days),
        ...(h >= 6 && h <= 22 ? { gemini: Math.max(1, Math.round((perDay.gemini / 12) * days * (h === 7 || h === 19 ? 2 : 0.6))) } : {}),
        ...(h >= 6 && h <= 22 ? { ollama: Math.max(1, Math.round((perDay.ollama / 12) * days * 0.7)) } : {}),
      },
    })),
    recent,
  };
}

// --- Router -----------------------------------------------------------------------------

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function handle(method: string, url: URL, body: Record<string, unknown>): Promise<unknown> {
  const path = url.pathname.replace(/^.*\/api\//, "/api/");
  const num = (k: string, d: number) => Number(url.searchParams.get(k) ?? d);
  await wait(120 + Math.random() * 180); // feels like a real server

  if (method === "GET") {
    if (path === "/api/overview") return overview();
    if (path === "/api/pmc") return DATA.days.slice(-num("days", 90)).map((d) => ({ day: d.day, ctl: d.ctl, atl: d.atl, tsb: Math.round((d.ctl - d.atl) * 10) / 10, load: d.load }));
    if (path === "/api/wellness") return DATA.days.slice(-num("days", 90)).map(({ day, hrv, resting_hr, sleep_h, sleep_score, weight_kg }) => ({ day, hrv, resting_hr, sleep_h, sleep_score, weight_kg }));
    if (path === "/api/weekly") return weekly(num("weeks", 12));
    if (path === "/api/plan") return plan(num("weeks", 8));
    if (path === "/api/next-session") return nextSession();
    if (path === "/api/focus") return { ...state.focus, options: { cycling: "Cycling focus", dual: "Dual-sport (bike + run)" } };
    if (path === "/api/activities") return DATA.activities.slice(0, num("limit", 20)).map(({ kind: _k, day: _d, interval: _i, strava_note: _n, ...a }) => a);
    if (path === "/api/injury") return injuryReport();
    if (path === "/api/usage") return usage(num("days", 7));
    if (path === "/api/races") return [overview().next_race];
    const note = path.match(/^\/api\/strava-note\/([^/]+)$/);
    if (note) {
      const a = activity(note[1]);
      return { note: a.strava_note, context: state.noteContext.get(a.id) ?? null, strava_url: a.strava_note_at ? "https://www.strava.com/" : null, posted_at: a.strava_note_at };
    }
  }

  if (method === "POST") {
    if (path === "/api/sync") {
      await wait(1800);
      state.lastSync = new Date().toISOString();
      return { message: "Activities: 0 new, 5 updated | Wellness: 0 new, 6 updated" };
    }
    if (path === "/api/focus") {
      const mode = body.mode as FocusMode;
      if (mode !== "cycling" && mode !== "dual") throw new HttpError(400, `Unknown training focus '${mode}'`);
      state.focus = { mode, since: iso(TODAY) };
      return { ...state.focus, options: { cycling: "Cycling focus", dual: "Dual-sport (bike + run)" } };
    }
    if (path === "/api/next-session") {
      await wait(2600 + Math.random() * 1500); // the coach thinking
      if (body.redo || !state.decision) state.decision = decide(++state.decisionVariant);
      return nextSession();
    }
    const prop = path.match(/^\/api\/next-session\/proposals\/(\d+)\/(apply|discard)$/);
    if (prop) {
      const p = state.proposals.get(Number(prop[1]));
      if (!p || p.status !== "pending") throw new HttpError(409, "That proposal was already resolved - ask the coach again.");
      if (prop[2] === "discard") p.status = "discarded";
      else {
        p.status = "applied";
        const a = lastTraining();
        state.decision = {
          after_activity: a.name, after_activity_id: a.id, decided_at: new Date().toISOString(), decided_by: "athlete",
          decision: `Session: ${p.session}${p.why ? `\nWhy: ${p.why}` : ""}`, session: p.session, why: p.why,
        };
      }
      return nextSession();
    }
    const preview = path.match(/^\/api\/strava-note\/([^/]+)\/preview$/);
    if (preview) {
      await wait(1800 + Math.random() * 1200); // Gemini writing the comment
      const a = activity(preview[1]);
      const context = String(body.context ?? "").trim() || null;
      const id = Math.random().toString(36).slice(2);
      const note = stravaNote(a, DATA.days, context ?? state.noteContext.get(a.id) ?? undefined);
      state.previews.set(id, { id: a.id, note, context });
      return { preview_id: id, note };
    }
    const post = path.match(/^\/api\/strava-note\/([^/]+)$/);
    if (post) {
      const p = state.previews.get(String(body.preview_id));
      if (!p || p.id !== post[1]) throw new HttpError(409, "Preview expired - please preview again.");
      await wait(900);
      const a = activity(post[1]);
      a.strava_note = p.note;
      a.strava_note_at = new Date().toISOString();
      state.noteContext.set(a.id, p.context);
      state.previews.delete(String(body.preview_id));
      // Demo: nothing is posted - the link goes to Strava's home page.
      return { strava_url: "https://www.strava.com/", posted_at: a.strava_note_at };
    }
  }
  throw new HttpError(404, "Not Found");
}
