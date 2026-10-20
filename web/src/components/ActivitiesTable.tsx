import { useEffect, useState } from "react";
import { Bike, Dumbbell, Footprints, ListOrdered, NotebookPen, Waves, Activity as ActivityIcon, Trophy } from "lucide-react";
import { api } from "../api";
import type { Activity } from "../types";
import { SPORT_COLORS } from "../lib/colors";
import { duration, fmt, weekday } from "../lib/format";
import { Card, Skeleton } from "./Card";
import { StravaNoteDialog } from "./StravaNoteDialog";

const ICONS: Record<string, React.ReactNode> = {
  Bike: <Bike size={15} />,
  Run: <Footprints size={15} />,
  Strength: <Dumbbell size={15} />,
  Swim: <Waves size={15} />,
  Other: <ActivityIcon size={15} />,
};

function ifColor(v: number | null) {
  if (v === null) return "text-slate-500";
  if (v >= 0.95) return "text-rose-300";
  if (v >= 0.85) return "text-orange-300";
  if (v >= 0.75) return "text-amber-200";
  return "text-sky-300";
}

/** Opens the Strava coach-note editor; orange once a note is posted. */
function StravaNoteButton({ a, onOpen }: { a: Activity; onOpen: (a: Activity) => void }) {
  if (!a.strava_note_ok) return null;
  return (
    <button
      onClick={(e) => { e.stopPropagation(); onOpen(a); }}
      title={a.strava_note_at ? "Coach note on Strava - click to rewrite or add context" : "Write a coach note on Strava"}
      className={`rounded-lg p-1.5 transition hover:bg-white/10 ${a.strava_note_at ? "text-[#fc4c02]" : "text-slate-500 hover:text-slate-200"}`}
    >
      <NotebookPen size={15} />
    </button>
  );
}

function MobileList({ rows, onAsk, onNote }: { rows: Activity[]; onAsk: (q: string) => void; onNote: (a: Activity) => void }) {
  return (
    <ul className="-mx-1 divide-y divide-white/[0.04]">
      {rows.map((a) => {
        const color = SPORT_COLORS[a.group] ?? SPORT_COLORS.Other;
        const chips = [
          a.hours !== null && duration(a.hours),
          a.distance_km && `${fmt(a.distance_km, 1)} km`,
          a.np && `${a.np} W`,
          a.avg_hr && `${a.avg_hr} bpm`,
        ].filter(Boolean) as string[];
        return (
          <li key={a.id} className="flex items-center gap-1">
            <button
              onClick={() => onAsk(`Analyse my ${a.type} "${a.name}" from ${a.start.slice(0, 10)} (activity ${a.id}).`)}
              className="flex min-w-0 flex-1 items-center gap-3 rounded-xl px-1 py-2.5 text-left active:bg-white/[0.04]"
            >
              <span className="shrink-0 rounded-xl p-2" style={{ color, background: `${color}1a` }}>{ICONS[a.group] ?? ICONS.Other}</span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5 text-[13.5px] font-medium text-slate-100">
                  {a.race && <Trophy size={13} className="shrink-0 text-amber-300" />}
                  <span className="truncate">{a.name}</span>
                </div>
                <div className="mt-0.5 truncate text-[11px] text-slate-500">
                  {weekday(a.start)} · {chips.join(" · ")}
                </div>
              </div>
              <div className="shrink-0 text-right">
                <div className="text-sm font-semibold tabular-nums text-white">{fmt(a.load, 0)}</div>
                <div className={`text-[11px] tabular-nums ${ifColor(a.if)}`}>IF {fmt(a.if, 2)}</div>
              </div>
            </button>
            <div className="w-8 shrink-0"><StravaNoteButton a={a} onOpen={onNote} /></div>
          </li>
        );
      })}
    </ul>
  );
}

export function ActivitiesTable({ refreshKey, onAsk, compact = false }: { refreshKey: number; onAsk: (q: string) => void; compact?: boolean }) {
  const [rows, setRows] = useState<Activity[] | null>(null);
  const [noteFor, setNoteFor] = useState<Activity | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    api.activities(20).then(setRows).catch(() => setRows([]));
  }, [refreshKey, reload]);

  return (
    <Card title="Recent activities" icon={<ListOrdered size={15} className="text-sky-400" />}>
      {!rows ? (
        <Skeleton className="h-64" />
      ) : compact ? (
        <MobileList rows={rows} onAsk={onAsk} onNote={setNoteFor} />
      ) : (
        <div className="-mx-4 overflow-x-auto">
          <table className="w-full min-w-[820px] text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wider text-slate-500">
                <th className="px-4 py-2 font-medium">Activity</th>
                <th className="px-2 py-2 text-right font-medium">Time</th>
                <th className="px-2 py-2 text-right font-medium">Dist</th>
                <th className="px-2 py-2 text-right font-medium">Elev</th>
                <th className="px-2 py-2 text-right font-medium">NP</th>
                <th className="px-2 py-2 text-right font-medium">IF</th>
                <th className="px-2 py-2 text-right font-medium">Load</th>
                <th className="px-2 py-2 text-right font-medium">HR</th>
                <th className="px-2 py-2 text-right font-medium">Drift</th>
                <th className="w-10 px-2 py-2" aria-label="Strava note" />
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => {
                const color = SPORT_COLORS[a.group] ?? SPORT_COLORS.Other;
                return (
                  <tr
                    key={a.id}
                    onClick={() => onAsk(`Analyse my ${a.type} "${a.name}" from ${a.start.slice(0, 10)} (activity ${a.id}).`)}
                    className="cursor-pointer border-t border-white/[0.04] transition hover:bg-white/[0.03]"
                    title="Click to ask the coach about this activity"
                  >
                    <td className="px-4 py-2">
                      <div className="flex items-center gap-3">
                        <span className="rounded-lg p-1.5" style={{ color, background: `${color}1a` }}>{ICONS[a.group] ?? ICONS.Other}</span>
                        <div className="min-w-0">
                          <div className="flex items-center gap-1.5 truncate font-medium text-slate-100">
                            {a.race && <Trophy size={13} className="shrink-0 text-amber-300" />}
                            <span className="truncate">{a.name}</span>
                          </div>
                          <div className="text-[11px] text-slate-500">{weekday(a.start)} · {a.type}</div>
                        </div>
                      </div>
                    </td>
                    <td className="px-2 py-2 text-right tabular-nums text-slate-300">{duration(a.hours)}</td>
                    <td className="px-2 py-2 text-right tabular-nums text-slate-300">{a.distance_km ? `${fmt(a.distance_km, 1)} km` : "–"}</td>
                    <td className="px-2 py-2 text-right tabular-nums text-slate-400">{a.elevation_m ? `${a.elevation_m} m` : "–"}</td>
                    <td className="px-2 py-2 text-right tabular-nums text-slate-300">{a.np ? `${a.np} W` : "–"}</td>
                    <td className={`px-2 py-2 text-right tabular-nums ${ifColor(a.if)}`}>{fmt(a.if, 2)}</td>
                    <td className="px-2 py-2 text-right tabular-nums font-medium text-white">{fmt(a.load, 0)}</td>
                    <td className="px-2 py-2 text-right tabular-nums text-rose-200/80">{fmt(a.avg_hr, 0)}</td>
                    <td className={`px-2 py-2 text-right tabular-nums ${a.decoupling !== null && a.decoupling > 5 ? "text-amber-300" : "text-slate-400"}`}>
                      {a.decoupling !== null ? `${fmt(a.decoupling, 1)}%` : "–"}
                    </td>
                    <td className="px-2 py-1 text-right"><StravaNoteButton a={a} onOpen={setNoteFor} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {noteFor && (
        <StravaNoteDialog activity={noteFor} onClose={() => setNoteFor(null)} onPosted={() => setReload((r) => r + 1)} />
      )}
    </Card>
  );
}
