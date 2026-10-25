/**
 * Synthetic athlete for the demo build: ~14 months of training, wellness and plan,
 * generated deterministically relative to today, so the demo always looks current.
 *
 * Loads follow the same adaptive Base -> Build -> Recovery logic as core/periodization.py
 * (targets from CTL), and CTL/ATL use the Intervals.icu exponential model
 * (core/simulators.py), so the charts, the plan and the chat's calculations agree.
 */
import type { Activity, Finding, InjuryReport, PlanWeek, SessionKind } from "../types";

// --- Deterministic randomness and dates ---------------------------------------------

function mulberry32(seed: number) {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const rand = mulberry32(20261020);
const between = (lo: number, hi: number) => lo + rand() * (hi - lo);
const noise = (sd: number) => (rand() + rand() + rand() - 1.5) * sd * 1.4;
const pick = <T,>(xs: readonly T[]): T => xs[Math.floor(rand() * xs.length)];
const round = (v: number, d = 0) => Math.round(v * 10 ** d) / 10 ** d;

export const iso = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
export const addDays = (d: Date, n: number) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);

const now = new Date();
export const TODAY = new Date(now.getFullYear(), now.getMonth(), now.getDate());
/** Monday of the current week. */
export const THIS_MONDAY = addDays(TODAY, -((TODAY.getDay() + 6) % 7));

// --- The athlete ----------------------------------------------------------------------

export const ATHLETE = { ftp: 265, lthr: 168, max_hr: 188, resting_hr: 47, eftp: 262 };
export const CTL_DECAY = Math.exp(-1 / 42);
export const ATL_DECAY = Math.exp(-1 / 7);

// --- The plan (core/periodization.py) -------------------------------------------------

const FOCUS = {
  base_early: "Base - aerobic foundation: Zone 2 endurance, cadence work and strides only, no intervals. Consistency matters more than any single session.",
  base_late: "Base - aerobic + tempo: mostly Zone 2, one tempo / low sweet spot session, no VO2max.",
  build_1: "Build - threshold development: sweet spot and FTP-building intervals.",
  build_2: "Build - threshold progression: longer sweet spot / threshold intervals.",
  build_3: "Build - peak load week: VO2max/anaerobic work at the top of this block before recovery.",
  recovery: "Deload - reduced volume and intensity to absorb the last weeks. No high-intensity intervals this week.",
} as const;
type FocusKey = keyof typeof FOCUS;

const SESSIONS: Record<FocusKey, Record<SessionKind, number>> = {
  base_early: { endurance: 4, sst: 0, vo2: 0, run: 1, weights: 3 },
  base_late: { endurance: 3, sst: 1, vo2: 0, run: 1, weights: 4 },
  build_1: { endurance: 3, sst: 1, vo2: 0, run: 1, weights: 4 },
  build_2: { endurance: 2, sst: 2, vo2: 0, run: 1, weights: 4 },
  build_3: { endurance: 2, sst: 1, vo2: 1, run: 1, weights: 3 },
  recovery: { endurance: 3, sst: 0, vo2: 0, run: 1, weights: 3 },
};

/** Weeks relative to this week (0). Earlier weeks are unstructured training. */
const PLAN: { week: number; key: FocusKey; why: string }[] = [
  { week: -13, key: "base_early", why: "start of the replay - Base week 1" },
  { week: -12, key: "base_early", why: "1/6 base weeks" },
  { week: -11, key: "base_early", why: "2/6 base weeks" },
  { week: -10, key: "recovery", why: "3 loading weeks - recovery week" },
  { week: -9, key: "base_late", why: "after recovery - 3/6 base weeks" },
  { week: -8, key: "base_late", why: "4/6 base weeks" },
  { week: -7, key: "base_late", why: "last week only partly done - doesn't count (4/6 base weeks)" },
  { week: -6, key: "recovery", why: "3 loading weeks - recovery week" },
  { week: -5, key: "base_late", why: "after recovery - 5/6 base weeks" },
  { week: -4, key: "build_1", why: "6 base weeks completed - Build unlocked" },
  { week: -3, key: "build_2", why: "build week 1 completed" },
  { week: -2, key: "recovery", why: "form below -25 after 2 loading weeks - early recovery" },
  { week: -1, key: "build_1", why: "after recovery, next build block" },
  { week: 0, key: "build_2", why: "build week completed - next level" },
  { week: 1, key: "build_3", why: "projected - assumes the weeks before are completed" },
  { week: 2, key: "recovery", why: "projected - assumes the weeks before are completed" },
  { week: 3, key: "build_1", why: "projected - assumes the weeks before are completed" },
  { week: 4, key: "build_2", why: "projected - race week (taper before the A race)" },
  { week: 5, key: "recovery", why: "projected - assumes the weeks before are completed" },
];
const planFor = (week: number) => PLAN.find((p) => p.week === week);
const phaseOf = (key: FocusKey) => (key === "recovery" ? "Recovery" : key.startsWith("base") ? "Base" : "Build");

export const RACE = {
  id: 9001,
  name: "Demo Gravel Classic",
  date: iso(addDays(THIS_MONDAY, 4 * 7 + 5)), // Saturday of week +4
  priority: "A",
  type: "GravelRide",
  distance_km: 120,
  elevation_m: 1600,
  location: "Girona, Spain",
  goal: "finish under 5h",
};

// --- Session templates ----------------------------------------------------------------

interface Template {
  kind: SessionKind | "walk";
  type: string;
  names: readonly string[];
  hours: number;
  if: number;
  interval?: string;
}
const T = {
  endurance: { kind: "endurance", type: "Ride", names: ["Zone 2 endurance", "Morning endurance spin", "Coffee ride", "Endurance loop"], hours: 1.6, if: 0.67 },
  indoor: { kind: "endurance", type: "VirtualRide", names: ["Zwift - Endurance", "Indoor Z2 + cadence drills"], hours: 1.25, if: 0.66 },
  long: { kind: "endurance", type: "GravelRide", names: ["Long gravel loop", "Weekend gravel adventure", "Long ride with the crew"], hours: 3.4, if: 0.7 },
  tempo: { kind: "sst", type: "VirtualRide", names: ["Tempo 3x12", "Low sweet spot 2x20"], hours: 1.3, if: 0.78, interval: "3x 12m 215w" },
  sst: { kind: "sst", type: "VirtualRide", names: ["Sweet spot 3x15", "Sweet spot 2x20", "Threshold 4x10"], hours: 1.4, if: 0.83, interval: "3x 15m 238w" },
  vo2: { kind: "vo2", type: "VirtualRide", names: ["VO2max 5x4", "VO2max 6x3", "40/20s"], hours: 1.2, if: 0.86, interval: "5x 4m 305w" },
  run: { kind: "run", type: "Run", names: ["Easy run", "Easy run + strides", "Lunch run"], hours: 0.75, if: 0.74 },
  legs: { kind: "weights", type: "WeightTraining", names: ["Legs", "Legs + core"], hours: 0.75, if: 0.3 },
  upper: { kind: "weights", type: "WeightTraining", names: ["Chest + arms", "Back + shoulders", "Core"], hours: 0.7, if: 0.25 },
  recovery: { kind: "endurance", type: "Ride", names: ["Recovery spin", "Easy spin"], hours: 0.9, if: 0.55 },
  walk: { kind: "walk", type: "Walk", names: ["Evening walk", "Walk with the dog"], hours: 0.8, if: 0.3 },
} satisfies Record<string, Template>;

/** Sessions per weekday (Mon=0) for each kind of week. */
const WEEK_TEMPLATES: Record<FocusKey | "free", Template[][]> = {
  base_early: [[], [T.indoor, T.upper], [T.endurance], [T.legs, T.recovery], [T.upper], [T.long], [T.endurance, T.run]],
  base_late: [[], [T.tempo, T.upper], [T.endurance, T.legs], [T.indoor, T.upper], [T.upper], [T.long], [T.endurance, T.run]],
  build_1: [[], [T.sst, T.upper], [T.endurance, T.legs], [T.indoor, T.upper], [T.upper], [T.long], [T.endurance, T.run]],
  build_2: [[], [T.sst, T.upper], [T.endurance, T.legs], [T.sst, T.upper], [T.upper], [T.long], [T.run]],
  build_3: [[], [T.vo2, T.upper], [T.endurance, T.legs], [T.sst], [T.upper], [T.long], [T.endurance, T.run]],
  recovery: [[], [T.recovery, T.upper], [T.walk], [T.indoor, T.legs], [], [T.endurance], [T.run, T.upper]],
  free: [[], [T.indoor], [T.endurance, T.upper], [T.run], [], [T.long], [T.endurance]],
};

const tss = (hours: number, intensity: number) => hours * intensity * intensity * 100;

// --- Generation ------------------------------------------------------------------------

export interface DemoActivity extends Activity {
  kind: SessionKind | "walk" | "race";
  day: string;
  interval: string | null;
  strava_note: string | null;
}
export interface Day {
  day: string;
  load: number;
  ctl: number;
  atl: number;
  hrv: number | null;
  resting_hr: number | null;
  sleep_h: number | null;
  sleep_score: number | null;
  weight_kg: number | null;
}
export interface Week {
  monday: string;
  week: number;
  key: FocusKey | null;
  phase: string | null;
  target_min: number | null;
  target_max: number | null;
  status: string | null;
}

const HISTORY_WEEKS = 60;
const PAST_RACES = [
  { week: -40, name: "Spring MTB Marathon", type: "MountainBikeRide", hours: 3.6, if: 0.86, km: 62, elev: 1450 },
  { week: -31, name: "Gravel Grinder 100", type: "GravelRide", hours: 4.1, if: 0.82, km: 101, elev: 1250 },
  { week: -24, name: "Hill Climb Cup", type: "Ride", hours: 0.75, if: 1.0, km: 16, elev: 820 },
  { week: -18, name: "Autumn Cross-Country", type: "MountainBikeRide", hours: 1.6, if: 0.92, km: 31, elev: 780 },
];

function generate() {
  const days: Day[] = [];
  const weeks: Week[] = [];
  const activities: DemoActivity[] = [];
  let ctl = 34;
  let atl = 30;
  let seq = 4000;
  let weight = 74.2;

  for (let w = -HISTORY_WEEKS; w <= 0; w++) {
    const monday = addDays(THIS_MONDAY, w * 7);
    const plan = planFor(w);
    const key = plan?.key ?? null;
    const recovery = key === "recovery";
    // Same rule as core/periodization.py: ~7xCTL holds fitness, plus a weekly ramp.
    const holding = 7 * ctl;
    const targetMin = plan ? Math.max(120, round(recovery ? holding * 0.5 : holding + 45 * (key!.startsWith("base") ? 2 : 3))) : null;
    const targetMax = targetMin !== null ? round(recovery ? holding * 0.7 : targetMin * 1.2) : null;
    // How the week actually went: mostly on target, one partial week, some unstructured noise before the plan.
    let completion = plan ? between(0.92, 1.08) : between(0.75, 1.15);
    if (w === -7) completion = 0.72;
    if (!plan && rand() < 0.1) completion = between(0.2, 0.45); // holidays, illness
    const template = WEEK_TEMPLATES[key ?? "free"];
    const templateLoad = template.flat().reduce((s, t) => s + tss(t.hours, t.if), 0);
    const goal = targetMin !== null && targetMax !== null ? (targetMin + targetMax) / 2 : 6.4 * ctl + 70 + noise(25);
    const scale = Math.min(1.5, Math.max(0.45, (goal * completion) / templateLoad));
    const race = PAST_RACES.find((r) => r.week === w);

    let weekLoad = 0;
    for (let d = 0; d < 7; d++) {
      const date = addDays(monday, d);
      if (date >= TODAY) break; // today: nothing logged yet
      const day = iso(date);
      let dayLoad = 0;
      let sessions = template[d];
      if (w === -7 && d === 5) sessions = []; // the partly-done week: skipped the long ride
      if (race && d === 6) sessions = [];
      for (const t of sessions) {
        if (!plan && rand() < 0.12) continue;
        const hours = round(t.hours * (t.kind === "weights" || t.kind === "walk" ? 1 : scale) * between(0.9, 1.1), 2);
        const intensity = round(t.if + noise(0.015), 2);
        activities.push(makeActivity(++seq, date, d, t, hours, intensity));
        dayLoad += activities[activities.length - 1].load ?? 0;
      }
      if (race && d === 6) {
        const a = makeActivity(++seq, date, d, { kind: "endurance", type: race.type, names: [race.name], hours: race.hours, if: race.if }, race.hours, race.if);
        Object.assign(a, { kind: "race", race: true, distance_km: race.km, elevation_m: race.elev, name: race.name });
        activities.push(a);
        dayLoad += a.load ?? 0;
      }
      ctl = ctl * CTL_DECAY + dayLoad * (1 - CTL_DECAY);
      atl = atl * ATL_DECAY + dayLoad * (1 - ATL_DECAY);
      weekLoad += dayLoad;

      // Wellness reacts to fatigue: HRV dips and resting HR rises when ATL runs above CTL.
      const strain = Math.max(0, atl - ctl);
      const recent = w === 0 || (w === -1 && d >= 5); // the last few days: HRV trending down
      weight += noise(0.05) - 0.006;
      days.push({
        day,
        load: round(dayLoad),
        ctl: round(ctl, 1),
        atl: round(atl, 1),
        hrv: w < -26 ? null : round(64 + noise(4.5) - strain * 0.18 - (recent ? 6.5 : 0)), // HRV strap since ~6 months
        resting_hr: round(ATHLETE.resting_hr + noise(1.3) + strain * 0.05 + (recent ? 2 : 0)),
        sleep_h: round(7.3 + noise(0.55) + (d >= 5 ? 0.4 : 0), 1),
        sleep_score: Math.min(98, round(79 + noise(7))),
        weight_kg: rand() < 0.4 ? round(weight, 1) : null,
      });
    }
    const done = targetMin !== null ? weekLoad / targetMin : 0;
    weeks.push({
      monday: iso(monday), week: w, key, phase: key ? phaseOf(key) : null,
      target_min: targetMin, target_max: targetMax,
      status: w === 0 || !plan ? null : recovery ? "recovery" : done >= 0.85 ? "completed" : done >= 0.5 ? "partial" : "missed",
    });
  }

  // Future weeks: projected targets from the current CTL, like the stored plan.
  let projected = ctl;
  for (let w = 1; w <= 5; w++) {
    const key = planFor(w)!.key;
    const recovery = key === "recovery";
    const holding = 7 * projected;
    const targetMin = round(recovery ? holding * 0.5 : holding + 45 * 3);
    weeks.push({
      monday: iso(addDays(THIS_MONDAY, w * 7)), week: w, key, phase: phaseOf(key),
      target_min: targetMin, target_max: round(recovery ? holding * 0.7 : targetMin * 1.2), status: null,
    });
    projected += recovery ? -1.5 : 2.5;
  }
  // Today: wellness logged in the morning, no training yet.
  const last = days[days.length - 1];
  days.push({
    day: iso(TODAY), load: 0,
    ctl: round(last.ctl * CTL_DECAY, 1), atl: round(last.atl * ATL_DECAY, 1),
    hrv: 55, resting_hr: 50, sleep_h: 6.9, sleep_score: 71, weight_kg: round(weight, 1),
  });
  activities.reverse();
  activities.forEach((a, i) => {
    a.strava_note = i < 12 && a.kind !== "walk" ? stravaNote(a, days) : null;
    a.strava_note_at = a.strava_note ? new Date(new Date(a.start).getTime() + (a.hours ?? 1) * 3600e3 + 25 * 60e3).toISOString() : null;
  });
  return { days, weeks, activities };
}

const GROUPS: Record<string, string> = {
  Ride: "Bike", VirtualRide: "Bike", GravelRide: "Bike", MountainBikeRide: "Bike",
  Run: "Run", WeightTraining: "Strength", Walk: "Other",
};

function makeActivity(seq: number, date: Date, weekday: number, t: Template, hours: number, intensity: number): DemoActivity {
  const startHour = weekday >= 5 ? 8.5 : t.kind === "weights" ? 18 : 17.25;
  const start = new Date(date.getTime() + (startHour + between(-0.5, 0.5)) * 3600e3);
  const bike = GROUPS[t.type] === "Bike";
  const run = t.type === "Run";
  const np = bike ? round(ATHLETE.ftp * intensity) : null;
  const avgHr = round(ATHLETE.lthr * (0.62 + intensity * 0.36) + noise(3));
  const speed = t.type === "VirtualRide" ? 30 : t.type === "GravelRide" ? 24 : t.type === "MountainBikeRide" ? 17 : bike ? 27.5 : run ? 10.6 : t.type === "Walk" ? 5 : 0;
  const steady = bike && t.kind === "endurance" && hours >= 1;
  return {
    id: `i9${String(seq).padStart(7, "0")}`,
    start: start.toISOString(),
    type: t.type,
    group: GROUPS[t.type] ?? "Other",
    name: pick(t.names),
    distance_km: speed ? round(hours * speed * between(0.94, 1.04), 1) : null,
    hours,
    elevation_m: speed ? round(hours * (t.type === "VirtualRide" ? 120 : t.type === "GravelRide" ? 330 : bike ? 250 : run ? 40 : 15)) : null,
    avg_power: np ? round(np * (t.kind === "endurance" ? 0.93 : 0.88)) : null,
    np,
    if: intensity,
    load: round(tss(hours, intensity)),
    avg_hr: t.kind === "weights" ? round(92 + noise(6)) : avgHr,
    decoupling: steady ? round(1.5 + noise(1.8) + (hours > 3 ? 1.6 : 0), 1) : null,
    ef: np && avgHr ? round(np / avgHr, 2) : null,
    cadence: bike ? round(t.type === "MountainBikeRide" ? 74 : 88 + noise(2)) : run ? round(84 + noise(1.5)) : null,
    race: false,
    feel: null,
    strava_note_ok: true,
    strava_url: null, // no links to real Strava activities in the demo
    strava_note_at: null,
    kind: t.kind,
    day: iso(date),
    interval: t.interval ?? null,
    strava_note: null,
  };
}

const KIND_LABELS: Record<string, string> = {
  endurance: "Endurance", sst: "Sweet spot", vo2: "VO2max", run: "Run", weights: "Strength", walk: "Walk", race: "Race",
};

/** The Strava note format from core/strava_notes.py: facts block, comment, signature. */
export function stravaFacts(a: DemoActivity, days: Day[]): string {
  const label = a.kind === "endurance" && (a.if ?? 1) < 0.6 ? "Recovery" : KIND_LABELS[a.kind];
  const first = [`🎯 ${label}`, ...(a.interval ? [a.interval.replace(/(\d)w\b/, "$1 W")] : [])];
  const minutes = Math.round((a.hours ?? 0) * 60);
  const stats = [minutes >= 60 ? `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, "0")}` : `${minutes} min`];
  if (a.distance_km) stats.push(`${a.distance_km.toFixed(1)} km`);
  if (a.np) stats.push(`NP ${a.np} W`, `IF ${a.if?.toFixed(2)}`);
  if (a.type === "Run" && a.distance_km && a.hours) {
    const pace = (a.hours * 60) / a.distance_km;
    stats.push(`${Math.floor(pace)}:${String(Math.round((pace % 1) * 60)).padStart(2, "0")} /km`);
  }
  if (a.load !== null) stats.push(`TSS ${a.load}`);
  const lines = [first.join(" · "), "⚡ " + stats.join(" · ")];
  const i = days.findIndex((d) => d.day === a.day);
  if (i > 0) lines.push(`📈 Fitness ${days[i - 1].ctl.toFixed(1)} → ${days[i].ctl.toFixed(1)}`);
  return lines.join("\n");
}

const COMMENTS: Record<string, string[]> = {
  endurance: [
    "Textbook aerobic work - steady, controlled and exactly what builds the engine for the long days. An easy spin next to soak this up.",
    "Quiet miles, loud adaptations: mitochondria were busy today. Keep the next one just as relaxed.",
  ],
  sst: [
    "Sweet spot done right: right under threshold, long enough to move FTP, short of digging a hole. Easy endurance next to let it land.",
    "Three blocks held evenly from first to last - that's the pacing that pays off on race day. Easy riding next.",
  ],
  vo2: ["Top-end work delivered: those efforts stretch the ceiling everything else sits under. A gentle spin next."],
  run: ["Relaxed, conversational running - the kind that builds durability without stealing from the bike. Next up stays aerobic."],
  weights: ["Strength in the bank - the quiet support act for every climb. Back on the bike next."],
  race: ["Race day, race effort - paced with patience and finished strong. Recovery comes first now."],
};

export function stravaNote(a: DemoActivity, days: Day[], context?: string): string {
  const comment = context?.trim()
    ? `${pick(COMMENTS[a.kind] ?? COMMENTS.endurance).split(" - ")[0]} - and noted: ${context.trim().replace(/\.$/, "")}. Smart riding, all part of the plan.`
    : pick(COMMENTS[a.kind] ?? COMMENTS.endurance);
  return [stravaFacts(a, days), "", comment, "— AI Coach"].join("\n");
}

export const DATA = generate();

// --- Derived views shared by the endpoints and the chat --------------------------------

export const latest = () => DATA.days[DATA.days.length - 1];
export const dayIndex = (day: string) => DATA.days.findIndex((d) => d.day === day);

export function hrvStatus() {
  const vals = DATA.days.map((d) => d.hrv).filter((v): v is number => v !== null);
  const last7 = vals.slice(-7);
  const base = vals.slice(-67, -7);
  const mean = base.reduce((s, v) => s + v, 0) / base.length;
  const sd = Math.sqrt(base.reduce((s, v) => s + (v - mean) ** 2, 0) / base.length);
  const avg7 = last7.reduce((s, v) => s + v, 0) / last7.length;
  const status = avg7 < mean - 2 * sd ? "low" : avg7 < mean - sd ? "below" : "normal";
  return { avg7: round(avg7, 1), baseline: round(mean, 1), sd: round(sd, 1), latest: last7[last7.length - 1], status } as const;
}

export function planWeek(week: number) {
  return DATA.weeks.find((w) => w.week === week) ?? null;
}

export function sessionsFor(key: FocusKey | null, dual: boolean, deload: boolean): Record<SessionKind, number> | null {
  if (!key) return null;
  const s = { ...SESSIONS[key] };
  if (dual) {
    s.run = Math.max(s.run, deload ? 3 : 4);
    s.endurance = Math.max(1, s.endurance - 1);
    s.weights = Math.max(2, s.weights - 1);
  }
  return s;
}

export function focusText(key: FocusKey | null, dual: boolean): string | null {
  if (!key) return null;
  const text: string = FOCUS[key];
  if (!dual) return text;
  if (key === "base_early") return text + " Run: easy and conversational, one long easy run, strides or short hill sprints at most.";
  return text + (key === "recovery" ? " Run: easy runs only, long run shortened." : " Run: one quality session (tempo/threshold by HR or hill repeats), one long easy run, the rest easy.");
}

export const planWhy = (week: number) => planFor(week)?.why ?? null;

export function weekActivities(monday: string) {
  const end = iso(addDays(new Date(monday + "T00:00:00"), 7));
  return DATA.activities.filter((a) => a.day >= monday && a.day < end);
}

export function doneByKind(monday: string): Record<SessionKind, number> {
  const done: Record<SessionKind, number> = { endurance: 0, sst: 0, vo2: 0, run: 0, weights: 0 };
  for (const a of weekActivities(monday)) {
    if (a.kind === "race") done.vo2 += 1;
    else if (a.kind !== "walk") done[a.kind] += 1;
  }
  return done;
}

/** Deterministic injury/overreaching checks (core/injury_checks.py wording). */
export function injuryReport(): InjuryReport {
  const d = DATA.days;
  const loads = d.map((x) => x.load);
  const acute = loads.slice(-7).reduce((s, v) => s + v, 0) / 7;
  const chronic = loads.slice(-28).reduce((s, v) => s + v, 0) / 28;
  const acwr = round(acute / chronic, 2);
  const now = latest();
  const tsb = round(now.ctl - now.atl, 1);
  const ramp = round(now.ctl - d[d.length - 8].ctl, 1);
  const hrv = hrvStatus();
  const sleep = d.slice(-7).map((x) => x.sleep_h ?? 0);
  const sleepAvg = round(sleep.reduce((s, v) => s + v, 0) / sleep.length, 1);
  const steady = DATA.activities.filter((a) => a.decoupling !== null).slice(0, 3);
  const findings: Finding[] = [];

  if (hrv.status !== "normal") {
    findings.push({
      severity: "caution", area: "Recovery", title: `HRV below normal (${hrv.avg7.toFixed(0)} vs ${hrv.baseline.toFixed(0)} ms)`,
      detail: `7-day average is more than 1 SD (±${hrv.sd.toFixed(0)}) under your 60-day norm - a classic sign of accumulated fatigue, illness coming on, or life stress. Prioritise easy days and sleep.`,
    });
  }
  findings.push(ramp > 5
    ? { severity: "caution", area: "Load", title: `Ramp rate ${ramp.toFixed(1)} CTL/week`, detail: "Productive but heavy training zone; keep an eye on HRV and how sessions feel." }
    : { severity: "ok", area: "Load", title: `Ramp rate ${ramp.toFixed(1)} CTL/week`, detail: "Fitness is building at a sustainable rate." });
  findings.push(acwr > 1.3
    ? { severity: "caution", area: "Load", title: `Acute:chronic load ratio ${acwr.toFixed(2)}`, detail: "Above ~1.3 is the start of the elevated-risk zone; fine for a planned overload week, but don't stack another one on top." }
    : { severity: "ok", area: "Load", title: `Acute:chronic load ratio ${acwr.toFixed(2)}`, detail: "Within the 0.8-1.3 range where load progression is well tolerated." });
  findings.push({ severity: "ok", area: "Load", title: `Form (TSB) ${tsb > 0 ? "+" : ""}${tsb.toFixed(1)}`, detail: "Productive training range - fatigue is under control." });
  findings.push(sleepAvg < 7
    ? { severity: "caution", area: "Recovery", title: `Sleep ${sleepAvg} h/night this week`, detail: "Under 7 h trims recovery - an extra 30-45 min a night is the cheapest performance gain there is." }
    : { severity: "ok", area: "Recovery", title: `Sleep ${sleepAvg} h/night this week`, detail: "Enough sleep to absorb the training." });
  findings.push({
    severity: "ok", area: "Cardio drift", title: "Heart rate tracking power well on steady rides",
    detail: "Decoupling within ~5% on recent steady rides. " + steady.map((a) => `${a.type} ${a.day} (${a.name}): ${a.decoupling!.toFixed(1)}%`).join("; "),
  });
  findings.push({ severity: "ok", area: "Biomechanics", title: "L/R balance 49.4/50.6", detail: "Within ±2% - symmetrical." });
  findings.push({ severity: "info", area: "Cardio drift", title: "Outdoor efficiency up 4%", detail: "More power for the same heart rate - aerobic fitness is improving." });

  const order = { high: 0, caution: 1, ok: 2, info: 3 };
  findings.sort((a, b) => order[a.severity] - order[b.severity]);
  const counts = { high: 0, caution: 0, ok: 0, info: 0 } as Record<Finding["severity"], number>;
  findings.forEach((f) => (counts[f.severity] += 1));
  return {
    date: iso(TODAY),
    overall: counts.high ? "high" : counts.caution ? "caution" : "ok",
    counts,
    findings,
    checked: [
      "training load (ACWR, TSB, ramp rate, monotony, recovery weeks)", "HRV vs 60-day baseline", "sleep duration",
      "aerobic decoupling (HR drift vs power) on steady aerobic rides 60min+", "efficiency factor trend (outdoor)",
      "GravelRide cadence vs personal baseline", "left/right power balance", "running volume progression",
    ],
    data_gaps: ["No feel/RPE logged recently - rating sessions in Intervals.icu makes fatigue checks much better."],
  };
}

export function readiness(): { level: "green" | "amber" | "red"; verdict: string } {
  const now = latest();
  const tsb = now.ctl - now.atl;
  const injury = injuryReport().overall;
  const hrv = hrvStatus().status;
  if (tsb <= -30 || hrv === "low" || injury === "high") return { level: "red", verdict: "Rest or easy only - recovery is behind" };
  if (tsb <= -20 || hrv === "below" || injury === "caution") return { level: "amber", verdict: "Train, but controlled - some risk flags" };
  return { level: "green", verdict: "Good to go - train as planned" };
}

export type { FocusKey, PlanWeek };
