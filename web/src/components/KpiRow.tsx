import { Activity, Battery, Flame, Gauge, HeartPulse, CalendarRange } from "lucide-react";
import type { ReactNode } from "react";
import type { Overview } from "../types";
import { C } from "../lib/colors";
import { fmt, formZone, signed } from "../lib/format";

export function Kpi({
  label,
  value,
  unit,
  sub,
  accent,
  icon,
  children,
}: {
  label: string;
  value: string;
  unit?: string;
  sub?: ReactNode;
  accent: string;
  icon: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="relative overflow-hidden rounded-2xl border border-white/[0.06] bg-ink-900/80 p-4">
      <div className="absolute inset-x-0 top-0 h-0.5" style={{ background: `linear-gradient(90deg, ${accent}, transparent)` }} />
      <div
        className="pointer-events-none absolute -right-6 -top-6 h-20 w-20 rounded-full opacity-20 blur-2xl"
        style={{ background: accent }}
      />
      <div className="flex items-center justify-between text-[12px] font-medium uppercase tracking-wider text-slate-400">
        {label}
        <span style={{ color: accent }}>{icon}</span>
      </div>
      <div className="mt-2 flex items-baseline gap-1">
        <span className="text-[28px] font-semibold tabular-nums leading-none text-white">{value}</span>
        {unit && <span className="text-sm text-slate-400">{unit}</span>}
      </div>
      {sub && <div className="mt-1.5 text-xs text-slate-400">{sub}</div>}
      {children}
    </div>
  );
}

export function KpiRow({ data }: { data: Overview }) {
  const f = data.fitness;
  const zone = f ? formZone(f.tsb) : null;
  const a = data.athlete;
  const hrv = data.hrv;
  const hrvColor = !hrv ? "#64748b" : hrv.status === "low" ? "#f87171" : hrv.status === "below" ? "#fbbf24" : "#34d399";
  const drift = a.ftp_drift;
  const driftColor = !drift || drift.status === "ok" ? undefined : drift.status === "below" ? "#fbbf24" : "#38bdf8";
  const block = data.block;
  const targetMax = block?.target_weekly_load_max ?? null;
  const targetMin = block?.target_weekly_load_min ?? null;
  const weekPct = targetMax ? Math.min(100, (data.week.load / targetMax) * 100) : null;

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 2xl:grid-cols-6">
      <Kpi label="Fitness" value={fmt(f?.ctl, 0)} unit="CTL" accent={C.ctl} icon={<Activity size={16} />}
        sub={<><span className={f && (f.ctl_change_7d ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"}>{signed(f?.ctl_change_7d, 1)}</span> last 7 days · ramp {signed(f?.ramp_rate, 1)}/wk</>} />
      <Kpi label="Fatigue" value={fmt(f?.atl, 0)} unit="ATL" accent={C.atl} icon={<Flame size={16} />}
        sub={f ? `Acute:chronic ${(f.atl / Math.max(f.ctl, 1)).toFixed(2)}` : undefined} />
      <Kpi label="Form" value={signed(f?.tsb, 0)} unit="TSB" accent={zone?.color ?? C.tsb} icon={<Battery size={16} />}
        sub={zone && <span style={{ color: zone.color }}>{zone.label}</span>} />
      <Kpi label={a.ftp_source === "eftp" ? "FTP (from eFTP)" : "FTP"} value={fmt(a.ftp, 0)} unit="W" accent="#fb923c" icon={<Gauge size={16} />}
        sub={<span title={drift?.message}>
          {fmt(a.wkg, 2)} W/kg
          {a.eftp ? <> · <span style={{ color: driftColor }}>eFTP {fmt(a.eftp, 0)} W{drift && drift.status !== "ok" ? ` (${drift.pct > 0 ? "+" : ""}${drift.pct.toFixed(0)}%)` : ""}</span></> : null}
          {drift && drift.status !== "ok" && (
            <span className="mt-0.5 block" style={{ color: driftColor }}>
              {drift.status === "below" ? "Retest or lower FTP" : "FTP looks outdated - raise it"}
            </span>
          )}
        </span>} />
      <Kpi label="HRV (7d)" value={fmt(hrv?.avg7, 0)} unit="ms" accent={hrvColor} icon={<HeartPulse size={16} />}
        sub={hrv ? <>baseline {fmt(hrv.baseline, 0)} ±{fmt(hrv.sd / 2, 0)} · <span style={{ color: hrvColor }}>{hrv.status}</span></> : "not enough data"} />
      <Kpi label="This week" value={fmt(data.week.load, 0)} unit="TSS" accent="#2dd4bf" icon={<CalendarRange size={16} />}
        sub={<>{fmt(data.week.hours, 1)} h · {data.week.activities} sessions{targetMin && targetMax ? <> · plan {fmt(targetMin, 0)}–{fmt(targetMax, 0)}</> : null}</>}>
        {weekPct !== null && (
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-white/5">
            <div className="h-full rounded-full bg-gradient-to-r from-teal-400 to-sky-400" style={{ width: `${weekPct}%` }} />
          </div>
        )}
      </Kpi>
    </div>
  );
}
