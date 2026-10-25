export const C = {
  ctl: "#38bdf8", // fitness - sky
  atl: "#f472b6", // fatigue - pink
  tsb: "#fbbf24", // form - amber
  load: "#64748b",
  hrv: "#a78bfa",
  rhr: "#fb7185",
  sleep: "#2dd4bf",
  grid: "rgba(148,163,184,0.10)",
  axis: "#64748b",
  text: "#cbd5e1",
};

export const SPORT_COLORS: Record<string, string> = {
  Bike: "#38bdf8",
  Run: "#fb923c",
  Strength: "#a78bfa",
  Swim: "#2dd4bf",
  Other: "#64748b",
};

export const SESSION_META = {
  endurance: { label: "Endurance", short: "End", color: "#38bdf8" },
  sst: { label: "Sweet spot", short: "SST", color: "#fbbf24" },
  vo2: { label: "VO2max", short: "VO2", color: "#f472b6" },
  run: { label: "Run", short: "Run", color: "#fb923c" },
  weights: { label: "Weights", short: "Lift", color: "#a78bfa" },
} as const;

export const SEVERITY = {
  high: { color: "#f87171", bg: "bg-red-500/10", ring: "ring-red-500/30", text: "text-red-300", label: "High risk" },
  caution: { color: "#fbbf24", bg: "bg-amber-500/10", ring: "ring-amber-500/30", text: "text-amber-300", label: "Caution" },
  ok: { color: "#34d399", bg: "bg-emerald-500/10", ring: "ring-emerald-500/30", text: "text-emerald-300", label: "Looks good" },
  info: { color: "#38bdf8", bg: "bg-sky-500/10", ring: "ring-sky-500/30", text: "text-sky-300", label: "Positive" },
} as const;

export const AGENT_META: Record<string, { label: string; color: string }> = {
  ai_coach: { label: "AI Coach", color: "#38bdf8" },
  athlete_twin: { label: "Athlete Twin", color: "#a78bfa" },
  racing_strategy: { label: "Racing Strategy", color: "#fb923c" },
  injury_prevention: { label: "Injury & Recovery", color: "#34d399" },
  life_optimization: { label: "Life Optimization", color: "#f472b6" },
  error: { label: "Error", color: "#f87171" },
};

/** External services on the System page (core/api_log.py names). */
export const SERVICE_META: Record<string, { label: string; color: string }> = {
  gemini: { label: "Gemini", color: "#a78bfa" },
  ollama: { label: "Ollama (local)", color: "#34d399" },
  intervals: { label: "Intervals.icu", color: "#38bdf8" },
  strava: { label: "Strava", color: "#fb923c" },
  telegram: { label: "Telegram", color: "#2dd4bf" },
  open_meteo: { label: "Open-Meteo", color: "#fbbf24" },
  withings: { label: "Withings", color: "#f472b6" },
  garmin: { label: "Garmin", color: "#60a5fa" },
  google: { label: "Google", color: "#fb7185" },
};

/** Shared ECharts base options for the dark theme. */
export const chartBase = {
  backgroundColor: "transparent",
  textStyle: { fontFamily: "Inter Variable, Inter, system-ui, sans-serif", color: C.text },
  grid: { left: 44, right: 16, top: 36, bottom: 28 },
  tooltip: {
    trigger: "axis" as const,
    backgroundColor: "rgba(15,23,42,0.95)",
    borderColor: "rgba(148,163,184,0.2)",
    textStyle: { color: "#e2e8f0", fontSize: 12 },
  },
  legend: { top: 0, right: 0, textStyle: { color: C.text, fontSize: 12 }, icon: "roundRect", itemWidth: 10, itemHeight: 10 },
};
