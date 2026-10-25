export type Severity = "high" | "caution" | "ok" | "info";

export interface Finding {
  severity: Severity;
  area: string;
  title: string;
  detail: string;
}

export interface InjuryReport {
  date: string;
  overall: "high" | "caution" | "ok";
  counts: Partial<Record<Severity, number>>;
  findings: Finding[];
  checked: string[];
  data_gaps: string[];
}

export interface NextRace {
  id: number | string;
  name: string;
  date: string;
  days_until: number;
  priority: string;
  type: string;
  distance_km: number | null;
  elevation_m: number | null;
  location: string | null;
  goal: string | null;
  missing: string[];
  predicted_hours?: number;
  target_np?: number;
  projected_tsb?: number;
  target_tsb?: [number, number];
  weather?: string;
}

export interface Overview {
  today: string;
  athlete: {
    /** The FTP targets are built on: the set FTP, or eFTP after 8 weeks without an FTP test. */
    ftp: number | null;
    ftp_set: number | null;
    ftp_source: "set" | "eftp" | null;
    lthr: number | null;
    weight_kg: number | null;
    wkg: number | null;
    eftp: number | null;
    max_hr: number | null;
    ftp_drift: { pct: number; status: "below" | "above" | "ok" | "due"; message: string } | null;
    synced_at: string | null;
  };
  fitness: { day: string; ctl: number; atl: number; tsb: number; ramp_rate: number | null; ctl_change_7d: number | null } | null;
  hrv: { avg7: number; baseline: number; sd: number; latest: number; status: "normal" | "below" | "low" } | null;
  week: { start: string; load: number; hours: number; activities: number };
  block: {
    phase: string;
    start_date: string;
    end_date: string;
    target_weekly_load_min: number;
    target_weekly_load_max: number;
    primary_focus: string;
    is_deload: boolean;
    load_so_far_this_block: number;
  } | null;
  injury: { overall: InjuryReport["overall"]; counts: InjuryReport["counts"]; top: Finding[] } | null;
  next_race: NextRace | null;
  last_sync: string | null;
}

export interface PmcPoint {
  day: string;
  ctl: number;
  atl: number;
  tsb: number;
  load: number;
}

export interface WellnessPoint {
  day: string;
  hrv: number | null;
  resting_hr: number | null;
  sleep_h: number | null;
  sleep_score: number | null;
  weight_kg: number | null;
}

export interface WeekPoint {
  week: string;
  load: number;
  hours: number;
  by_sport: Record<string, number>;
  target_min: number | null;
  target_max: number | null;
  phase: string | null;
}

export interface Activity {
  id: string;
  start: string;
  type: string;
  group: string;
  name: string;
  distance_km: number | null;
  hours: number | null;
  elevation_m: number | null;
  avg_power: number | null;
  np: number | null;
  if: number | null;
  load: number | null;
  avg_hr: number | null;
  decoupling: number | null;
  ef: number | null;
  cadence: number | null;
  race: boolean;
  feel: number | null;
  /** Rides/runs can carry a coach note in their Strava description. */
  strava_note_ok: boolean;
  strava_url: string | null;
  strava_note_at: string | null;
}

export interface StravaNoteStatus {
  note: string | null;
  context: string | null;
  strava_url: string | null;
  posted_at: string | null;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  agent?: string;
  /** Which model wrote the answer: local (Ollama), gemini, or none (computed without an LLM). */
  model?: "local" | "gemini" | "none";
  error?: boolean;
  /** A next-session change the coach proposed in this answer, waiting for the athlete's Apply/Discard. */
  proposal?: SessionProposal;
}

export interface SessionProposal {
  id: number;
  session: string;
  why: string | null;
  /** Set once resolved here; `note` explains a refusal (e.g. a newer activity was logged). */
  status?: "applied" | "discarded" | "failed";
  note?: string;
}

export type SessionKind = "endurance" | "sst" | "vo2" | "run" | "weights";

export interface PlanWeek {
  week: string;
  phase: string | null;
  is_deload: boolean;
  focus: string | null;
  target_min: number | null;
  target_max: number | null;
  sessions: Record<SessionKind, number> | null;
  why: string | null;
  current: boolean;
  done: Record<SessionKind, number> | null;
  load: number | null;
  races: { name: string | null; date: string; priority: string | null }[];
}

export type FocusMode = "cycling" | "dual";

export interface TrainingFocus {
  mode: FocusMode;
  since: string | null;
  options: Record<FocusMode, string>;
}

export type ReadinessLevel = "green" | "amber" | "red";

export interface NextSession {
  readiness: { level: ReadinessLevel; verdict: string } | null;
  decision: {
    after_activity: string;
    after_activity_id: string;
    decided_at: string;
    /** "athlete" when the athlete applied a change agreed in chat. */
    decided_by: "coach" | "athlete";
    decision: string;
    session: string;
    why: string | null;
  } | null;
}

/** System page (GET /api/usage): external calls logged in api_calls. */
export interface UsageCall {
  ts: string;
  service: string;
  operation: string | null;
  component: string | null;
  trigger: string | null;
  process: string;
  model: string | null;
  status: number | null;
  ok: boolean;
  duration_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  thinking_tokens: number | null;
  cost_usd: number | null;
  error: string | null;
}

export interface Usage {
  days: number;
  bucket: "hour" | "day";
  logging_since: string | null;
  totals: { service: string; calls: number; errors: number; avg_ms: number | null }[];
  gemini: {
    calls: number;
    input_tokens: number;
    output_tokens: number;
    thinking_tokens: number;
    cost: number;
    avg_cost_per_call: number | null;
    projected_month: number | null;
    price: { model: string; input_per_m: number; output_per_m: number; cached_per_m: number };
  };
  timeline: { t: string; calls: Record<string, number>; cost: number }[];
  components: { service: string; component: string; calls: number; errors: number; input_tokens: number; output_tokens: number; cost: number; avg_ms: number | null }[];
  triggers: { trigger: string; calls: number; cost: number }[];
  flows: { process: string; trigger: string; service: string; calls: number }[];
  by_hour: { hour: number; calls: Record<string, number> }[];
  recent: UsageCall[];
}

