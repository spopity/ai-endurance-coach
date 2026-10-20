import { Flag, MapPin, Mountain, Timer, CloudSun, Target } from "lucide-react";
import type { NextRace } from "../types";
import { duration, fmt, signed, weekday } from "../lib/format";
import { Card } from "./Card";

const TYPE_LABEL: Record<string, string> = {
  Ride: "Road", GravelRide: "Gravel", MountainBikeRide: "MTB", VirtualRide: "Indoor", Run: "Run", TrailRun: "Trail run",
};
const PRIORITY_STYLE: Record<string, string> = {
  A: "from-orange-500 to-rose-500",
  B: "from-sky-500 to-violet-500",
  C: "from-slate-500 to-slate-600",
};

export function RaceCard({ race, onAsk }: { race: NextRace | null; onAsk: (q: string) => void }) {
  if (!race) {
    return (
      <Card title="Next race" icon={<Flag size={15} className="text-orange-400" />} className="flex flex-col">
        <div className="flex flex-1 flex-col items-center justify-center gap-3 py-6 text-center">
          <div className="rounded-2xl bg-gradient-to-br from-orange-500/20 to-rose-500/20 p-4">
            <Flag size={28} className="text-orange-300" />
          </div>
          <p className="text-sm font-medium text-slate-200">No upcoming races</p>
          <p className="max-w-[280px] text-xs leading-relaxed text-slate-400">
            Add a <b>Race A/B/C</b> event in your Intervals.icu calendar with these lines in the description:
          </p>
          <div className="space-y-1 font-mono text-[11px] text-slate-300">
            {["Distance: 120 km", "Elevation: 1500 m", "Location: town", "Goal: sub 5h"].map((line) => (
              <div key={line} className="rounded bg-white/[0.06] px-2 py-0.5">{line}</div>
            ))}
          </div>
        </div>
      </Card>
    );
  }

  const tsbOk = race.projected_tsb !== undefined && race.target_tsb && race.projected_tsb >= race.target_tsb[0];

  return (
    <Card title="Next race" icon={<Flag size={15} className="text-orange-400" />} className="relative overflow-hidden">
      <div className={`pointer-events-none absolute -right-10 -top-10 h-40 w-40 rounded-full bg-gradient-to-br ${PRIORITY_STYLE[race.priority] ?? PRIORITY_STYLE.C} opacity-20 blur-3xl`} />
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className={`rounded-md bg-gradient-to-r px-1.5 py-0.5 text-[11px] font-bold text-white ${PRIORITY_STYLE[race.priority] ?? PRIORITY_STYLE.C}`}>
              {race.priority} RACE
            </span>
            <span className="text-xs text-slate-400">{TYPE_LABEL[race.type] ?? race.type}</span>
          </div>
          <h3 className="mt-1.5 text-lg font-semibold leading-tight text-white">{race.name}</h3>
          <p className="text-xs text-slate-400">{weekday(race.date)}</p>
        </div>
        <div className="text-right">
          <div className="bg-gradient-to-br from-orange-300 to-rose-400 bg-clip-text text-4xl font-bold tabular-nums leading-none text-transparent">
            {race.days_until}
          </div>
          <div className="text-[11px] uppercase tracking-wider text-slate-400">days</div>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-2 text-xs">
        <Stat icon={<MapPin size={13} />} label="Distance" value={race.distance_km ? `${fmt(race.distance_km, 0)} km` : "–"} />
        <Stat icon={<Mountain size={13} />} label="Elevation" value={race.elevation_m ? `${fmt(race.elevation_m, 0)} m` : "–"} />
        <Stat icon={<Timer size={13} />} label="Predicted" value={duration(race.predicted_hours)} />
        <Stat icon={<Target size={13} />} label="Race-day form" value={race.projected_tsb !== undefined ? signed(race.projected_tsb, 0) : "–"}
          hint={race.target_tsb ? `target ${signed(race.target_tsb[0])}…${signed(race.target_tsb[1])}` : undefined}
          tone={race.projected_tsb === undefined ? undefined : tsbOk ? "text-emerald-300" : "text-amber-300"} />
      </div>

      {race.weather && (
        <p className="mt-3 flex gap-2 rounded-lg bg-white/[0.03] p-2 text-[11px] leading-relaxed text-slate-400">
          <CloudSun size={14} className="mt-0.5 shrink-0 text-sky-300" />
          {race.weather}
        </p>
      )}
      {race.missing.length > 0 && (
        <p className="mt-2 text-[11px] text-amber-300/80">Missing in Intervals.icu: {race.missing.join(", ")} - add them for a better plan.</p>
      )}

      <div className="mt-3 flex gap-2">
        <button
          onClick={() => onAsk(`Build my race plan for ${race.name} on ${race.date}: pacing, fuelling, taper and race week.`)}
          className="flex-1 rounded-lg bg-gradient-to-r from-orange-500 to-rose-500 px-3 py-2 text-xs font-semibold text-white shadow-lg shadow-orange-900/30 transition hover:brightness-110"
        >
          Build race plan
        </button>
      </div>
    </Card>
  );
}

function Stat({ icon, label, value, hint, tone }: { icon: React.ReactNode; label: string; value: string; hint?: string; tone?: string }) {
  return (
    <div className="rounded-lg bg-white/[0.03] p-2">
      <div className="flex items-center gap-1 text-slate-500">{icon}{label}</div>
      <div className={`mt-0.5 text-sm font-semibold tabular-nums ${tone ?? "text-white"}`}>{value}</div>
      {hint && <div className="text-[10px] text-slate-500">{hint}</div>}
    </div>
  );
}
