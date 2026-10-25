import { useEffect, useState } from "react";
import { CalendarRange, Flag } from "lucide-react";
import { api } from "../api";
import type { FocusMode, PlanWeek, SessionKind, TrainingFocus } from "../types";
import { SESSION_META } from "../lib/colors";
import { fmt, shortDate } from "../lib/format";
import { Card, Segmented, Skeleton } from "./Card";

const KINDS = Object.keys(SESSION_META) as SessionKind[];

function LoadBar({ w, scale }: { w: PlanWeek; scale: number }) {
  if (w.target_min === null || w.target_max === null) return <div className="h-1.5 rounded-full bg-white/5" />;
  const pct = (v: number) => `${Math.min(100, (v / scale) * 100)}%`;
  return (
    <div className="relative h-1.5 rounded-full bg-white/5">
      <div
        className={`absolute inset-y-0 rounded-full ${w.is_deload ? "bg-emerald-400/50" : "bg-gradient-to-r from-teal-400/60 to-sky-400/70"}`}
        style={{ left: pct(w.target_min), width: `calc(${pct(w.target_max)} - ${pct(w.target_min)})` }}
      />
      {w.load !== null && (
        <div
          className="absolute -top-1 h-3.5 w-1 -translate-x-1/2 rounded-full bg-white shadow-[0_0_6px_rgba(255,255,255,0.7)]"
          style={{ left: pct(w.load) }}
          title={`${w.load} TSS so far`}
        />
      )}
    </div>
  );
}

function SessionChips({ w }: { w: PlanWeek }) {
  if (!w.sessions) return <p className="text-xs text-slate-500">No plan for this week</p>;
  return (
    <div className="flex flex-wrap gap-1">
      {KINDS.filter((k) => w.sessions![k] > 0).map((k) => {
        const meta = SESSION_META[k];
        const planned = w.sessions![k];
        const done = w.done?.[k];
        const complete = done !== undefined && done >= planned;
        return (
          <span
            key={k}
            title={meta.label}
            className="inline-flex items-center gap-1 rounded-md bg-white/[0.04] px-1.5 py-0.5 text-[11px] tabular-nums text-slate-300"
            style={complete ? { boxShadow: `inset 0 0 0 1px ${meta.color}66` } : undefined}
          >
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: meta.color }} />
            {done !== undefined ? (
              <span className={complete ? "text-white" : ""}>
                {done}/{planned}
              </span>
            ) : (
              planned
            )}
            <span className="text-slate-500">{meta.short}</span>
          </span>
        );
      })}
    </div>
  );
}

function WeekTile({ w, scale }: { w: PlanWeek; scale: number }) {
  return (
    <div
      className={`flex flex-col gap-2 rounded-xl p-2.5 sm:p-3 ${
        w.current ? "bg-sky-500/[0.07] ring-1 ring-sky-400/30" : "bg-white/[0.03]"
      }`}
    >
      <div className="flex items-center justify-between gap-1 text-xs">
        <span className="font-semibold text-white">{w.current ? "This week" : shortDate(w.week)}</span>
        {w.phase && (
          <span
            className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
              w.is_deload
                ? "bg-emerald-500/15 text-emerald-300"
                : w.phase === "Base"
                  ? "bg-amber-500/15 text-amber-300"
                  : w.phase === "Race" || w.phase === "Taper" || w.phase === "Race prep"
                    ? "bg-rose-500/15 text-rose-300"
                    : "bg-sky-500/15 text-sky-300"
            }`}
          >
            {w.is_deload ? "Deload" : w.phase}
          </span>
        )}
      </div>
      <div>
        <div className="mb-1 flex items-baseline justify-between text-[11px] text-slate-400">
          <span className="tabular-nums">
            {w.current && w.load !== null && <span className="text-white">{w.load} / </span>}
            {fmt(w.target_min)}–{fmt(w.target_max)} TSS
          </span>
        </div>
        <LoadBar w={w} scale={scale} />
      </div>
      <SessionChips w={w} />
      {w.races.map((r) => (
        <div key={r.date + r.name} className="flex items-center gap-1 text-[11px] text-orange-300">
          <Flag size={11} />
          <span className="truncate">
            {r.priority && <b>{r.priority} · </b>}
            {r.name} · {shortDate(r.date)}
          </span>
        </div>
      ))}
    </div>
  );
}

const FOCUS_OPTIONS: { label: string; value: FocusMode }[] = [
  { label: "🚴 Cycling · FTP", value: "cycling" },
  { label: "🚴 + 🏃 Dual-sport", value: "dual" },
];

const FOCUS_HINT: Record<FocusMode, string> = {
  cycling: "Coach builds FTP first; running is maintenance only.",
  dual: "Winter block: bike and run are both primary. More runs, a bit less riding and gym.",
};

function FocusSwitch({ onChanged, refreshKey }: { onChanged: () => void; refreshKey: number }) {
  const [focus, setFocus] = useState<TrainingFocus | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.focus().then(setFocus).catch(() => setFocus(null));
  }, [refreshKey]);

  const change = (mode: FocusMode) => {
    if (!focus || mode === focus.mode || saving) return;
    setSaving(true);
    setError(null);
    api
      .setFocus(mode)
      .then((f) => {
        setFocus(f);
        onChanged();
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setSaving(false));
  };

  if (!focus) return null;
  return (
    <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-xl bg-white/[0.03] p-2">
      <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Focus</span>
      <div className={saving ? "pointer-events-none opacity-60" : ""}>
        <Segmented options={FOCUS_OPTIONS} value={focus.mode} onChange={change} />
      </div>
      <span className={`text-[11px] ${focus.mode === "dual" ? "text-orange-300" : "text-slate-400"}`}>
        {FOCUS_HINT[focus.mode]}
        {focus.since && <span className="text-slate-500"> Since {shortDate(focus.since)}.</span>}
      </span>
      {error && <span className="text-[11px] text-rose-400">{error}</span>}
    </div>
  );
}

export function PlanCard({ refreshKey }: { refreshKey: number }) {
  const [weeks, setWeeks] = useState<PlanWeek[] | null>(null);
  const [focusKey, setFocusKey] = useState(0);

  useEffect(() => {
    api.plan(8).then(setWeeks).catch(() => setWeeks([]));
  }, [refreshKey, focusKey]);

  const scale = Math.max(1, ...(weeks ?? []).flatMap((w) => [w.target_max ?? 0, w.load ?? 0])) * 1.05;
  const current = weeks?.find((w) => w.current);

  return (
    <Card
      title="Training plan"
      icon={<CalendarRange size={15} className="text-sky-400" />}
      action={
        <div className="flex flex-wrap gap-x-2.5 gap-y-1 text-[11px] text-slate-400">
          {KINDS.map((k) => (
            <span key={k} className="inline-flex items-center gap-1">
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: SESSION_META[k].color }} />
              {SESSION_META[k].label}
            </span>
          ))}
        </div>
      }
    >
      <FocusSwitch refreshKey={refreshKey} onChanged={() => setFocusKey((k) => k + 1)} />
      {current?.why && (
        <p className="mb-1 text-xs text-slate-300">
          <span className="text-slate-500">Why this phase: </span>
          {current.why}
        </p>
      )}
      {current?.focus && <p className="mb-1 text-xs text-slate-400">{current.focus}</p>}
      {current && (
        <p className="mb-3 text-[11px] text-slate-500">
          Adaptive plan: Build unlocks after 6 completed Base weeks; missed weeks push it back. Later weeks are a
          projection.
        </p>
      )}
      {weeks === null ? (
        <Skeleton className="h-[260px]" />
      ) : weeks.length === 0 ? (
        <p className="text-sm text-slate-500">Could not load the plan.</p>
      ) : (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:gap-3">
          {weeks.map((w) => (
            <WeekTile key={w.week} w={w} scale={scale} />
          ))}
        </div>
      )}
    </Card>
  );
}
