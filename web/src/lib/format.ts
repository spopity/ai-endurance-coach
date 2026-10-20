export const fmt = (v: number | null | undefined, digits = 0, suffix = ""): string =>
  v === null || v === undefined || Number.isNaN(v) ? "–" : `${v.toFixed(digits)}${suffix}`;

export const signed = (v: number | null | undefined, digits = 0): string =>
  v === null || v === undefined ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}`;

export function duration(hours: number | null | undefined): string {
  if (hours === null || hours === undefined) return "–";
  const total = Math.round(hours * 60);
  const h = Math.floor(total / 60);
  const m = total % 60;
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
}

export function shortDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export function weekday(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}

export function timeAgo(iso: string | null): string {
  if (!iso) return "never";
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

/** Intervals.icu / TrainingPeaks-style form (TSB) zones. */
export function formZone(tsb: number): { label: string; color: string } {
  if (tsb > 25) return { label: "Transition", color: "#94a3b8" };
  if (tsb > 5) return { label: "Fresh", color: "#38bdf8" };
  if (tsb > -10) return { label: "Grey zone", color: "#a3a3a3" };
  if (tsb > -30) return { label: "Optimal", color: "#34d399" };
  return { label: "High risk", color: "#f87171" };
}
