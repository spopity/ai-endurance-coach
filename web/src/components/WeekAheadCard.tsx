import { useEffect, useState } from "react";
import { Bike, CalendarDays, Dumbbell, Footprints, Pin } from "lucide-react";
import { api } from "../api";
import type { WeekAhead, WeekSport } from "../types";
import { Card, Skeleton } from "./Card";

const SPORT: Record<WeekSport, { icon: typeof Bike; color: string }> = {
  ride: { icon: Bike, color: "text-sky-300" },
  run: { icon: Footprints, color: "text-emerald-300" },
  gym: { icon: Dumbbell, color: "text-violet-300" },
};

function dayLabel(iso: string) {
  const d = new Date(iso + "T00:00:00");
  return `${d.getDate()}.${d.getMonth() + 1}.`;
}

/** The rolling 7-day plan (core/schedule.py): re-derived after every sync from what was done. */
export function WeekAheadCard({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<WeekAhead | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    api.weekAhead().then(setData).catch(() => setFailed(true));
  }, [refreshKey]);

  return (
    <Card
      title="Next 7 days"
      icon={<CalendarDays size={15} className="text-orange-400" />}
      action={
        data && (
          <span className="text-[11px] text-slate-500">
            Re-plans itself after every sync{data.done_today.length ? ` · done today: ${data.done_today.join(", ")}` : ""}
          </span>
        )
      }
    >
      {failed ? (
        <p className="text-sm text-slate-500">The week ahead couldn't be loaded.</p>
      ) : !data ? (
        <Skeleton className="h-40" />
      ) : (
        <>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-7">
            {data.days.map((d, i) => {
              const off = d.status !== "available" && d.status !== "limited" && d.status !== "red readiness";
              return (
                <div
                  key={d.date}
                  className={`rounded-xl border p-2.5 ${
                    i === 0 ? "border-orange-400/30 bg-orange-500/[0.06]" : "border-white/[0.05] bg-white/[0.02]"
                  } ${off ? "opacity-50" : ""}`}
                >
                  <div className="mb-1.5 flex items-baseline justify-between gap-2">
                    <span className="text-xs font-semibold text-slate-200">
                      {d.weekday} <span className="font-normal text-slate-500">{dayLabel(d.date)}</span>
                    </span>
                    {d.max_minutes && <span className="text-[10px] text-amber-300/80">max {d.max_minutes} min</span>}
                  </div>
                  {d.fuel && !off && (
                    <p className="mb-1.5 text-[10px] leading-snug text-amber-200/70" title={d.fuel.why}>
                      🍝 {d.fuel.grams[0]}–{d.fuel.grams[1]} g carbs · {d.fuel.protein[0]}–{d.fuel.protein[1]} g protein
                    </p>
                  )}
                  {off ? (
                    <p className="text-xs text-slate-500">No training ({d.status})</p>
                  ) : d.rest ? (
                    <p className="text-xs text-slate-500">Rest</p>
                  ) : (
                    <ul className="space-y-2">
                      {d.sessions.map((s, j) => {
                        const { icon: Icon, color } = SPORT[s.sport];
                        return (
                          <li key={j} className="text-xs leading-snug">
                            <div className={`flex items-center gap-1.5 font-medium ${s.hard && s.sport !== "gym" ? "text-orange-300" : "text-slate-200"}`}>
                              <Icon size={13} className={color} />
                              {s.title}
                              {s.pinned && <Pin size={11} className="text-slate-400" />}
                            </div>
                            <p className="mt-0.5 text-[11px] text-slate-400">{s.detail}</p>
                            {s.note && <p className="mt-0.5 text-[11px] italic text-amber-300/80">{s.note}</p>}
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>
              );
            })}
          </div>
          {data.dropped.length > 0 && (
            <p className="mt-2 text-[11px] text-slate-500">
              Dropped for lack of days: {data.dropped.map((x) => x.session).join(", ")}.
            </p>
          )}
        </>
      )}
    </Card>
  );
}
