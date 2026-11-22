import { useEffect, useState } from "react";
import { Bike, CloudUpload, Dumbbell, FileText, Footprints, Trash2, X } from "lucide-react";
import { api } from "../api";
import type { WeekSport, Workout, WorkoutDay } from "../types";
import { Card, Skeleton } from "./Card";

const SPORT: Record<WeekSport, { icon: typeof Bike; color: string }> = {
  ride: { icon: Bike, color: "text-sky-300" },
  run: { icon: Footprints, color: "text-emerald-300" },
  gym: { icon: Dumbbell, color: "text-violet-300" },
};

function dayLabel(d: WorkoutDay) {
  const dt = new Date(d.date + "T00:00:00");
  return `${d.weekday ?? ""} ${dt.getDate()}.${dt.getMonth() + 1}.`.trim();
}

function StatusChip({ w }: { w: Workout }) {
  if (!w.in_plan) return <span className="text-[11px] text-amber-300">On Intervals.icu, no longer planned</span>;
  if (w.status === "pushed")
    return (
      <span className="text-[11px] text-emerald-300">
        On Intervals.icu{w.changed ? <span className="text-amber-300"> · plan changed since</span> : null}
      </span>
    );
  if (w.status === "skipped") return <span className="text-[11px] text-slate-500">Skipped</span>;
  return <span className="text-[11px] text-slate-500">Not pushed</span>;
}

/** Today's workouts as structured Intervals.icu workouts (core/workouts.py); pushed only on a click. */
export function WorkoutCard({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<WorkoutDay | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.workouts().then(setData).catch(() => setFailed(true));
  }, [refreshKey]);

  function act(w: Workout, action: "push" | "skip" | "remove") {
    if (!data) return;
    setBusy(w.kind + action);
    setError(null);
    api
      .workoutAction(data.date, w.kind, action)
      .then(setData)
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(null));
  }

  const btn = "inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium ring-1 transition disabled:opacity-50";

  return (
    <Card
      title={data ? `Workout · ${dayLabel(data)}` : "Workout"}
      icon={<FileText size={15} className="text-orange-400" />}
      action={<span className="text-[11px] text-slate-500">Push sends it to your Intervals.icu calendar</span>}
    >
      {failed ? (
        <p className="text-sm text-slate-500">The workouts couldn't be loaded.</p>
      ) : !data ? (
        <Skeleton className="h-32" />
      ) : data.workouts.length === 0 ? (
        <p className="text-sm text-slate-500">No training planned ({data.status}).</p>
      ) : (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {data.workouts.map((w) => {
            const { icon: Icon, color } = SPORT[w.sport];
            const pushed = w.status === "pushed" || !w.in_plan;
            return (
              <div key={w.kind} className="rounded-xl border border-white/[0.05] bg-white/[0.02] p-3">
                <div className="mb-2 flex items-start justify-between gap-2">
                  <div>
                    <div className="flex items-center gap-1.5 text-sm font-medium text-slate-200">
                      <Icon size={14} className={color} />
                      {w.name}
                    </div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11px] text-slate-500">
                      {w.moving_time_sec ? <span>{Math.round(w.moving_time_sec / 60)} min</span> : null}
                      {!w.structured && <span>text only</span>}
                      <StatusChip w={w} />
                    </div>
                  </div>
                  <div className="flex shrink-0 gap-1.5">
                    {(!pushed || (w.changed && w.in_plan)) && (
                      <button
                        className={`${btn} bg-orange-500/15 text-orange-200 ring-orange-400/30 hover:bg-orange-500/25`}
                        disabled={busy !== null}
                        onClick={() => act(w, "push")}
                      >
                        <CloudUpload size={13} />
                        {busy === w.kind + "push" ? "Pushing…" : w.changed ? "Update" : "Push"}
                      </button>
                    )}
                    {!pushed && w.status !== "skipped" && (
                      <button
                        className={`${btn} text-slate-300 ring-white/10 hover:bg-white/[0.05]`}
                        disabled={busy !== null}
                        onClick={() => act(w, "skip")}
                      >
                        <X size={13} />
                        Skip
                      </button>
                    )}
                    {pushed && (
                      <button
                        className={`${btn} text-rose-200 ring-rose-400/25 hover:bg-rose-500/10`}
                        disabled={busy !== null}
                        onClick={() => act(w, "remove")}
                      >
                        <Trash2 size={13} />
                        {busy === w.kind + "remove" ? "Removing…" : "Remove"}
                      </button>
                    )}
                  </div>
                </div>
                <pre className="whitespace-pre-wrap rounded-lg bg-black/20 p-2.5 font-mono text-[11px] leading-relaxed text-slate-300">
                  {w.description}
                </pre>
              </div>
            );
          })}
        </div>
      )}
      {error && <p className="mt-2 text-xs text-rose-300">{error}</p>}
    </Card>
  );
}
