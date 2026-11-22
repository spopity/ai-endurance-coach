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

// --- Intervals.icu workout text -> sections and steps (core/workouts.py writes it) ---

type Step = { sec: number; target: string; cue: string; lo: number; hi: number; zone: number };
type Section = { title: string; reps: number; steps: Step[] };

/** Rough intensity (1.0 = threshold) for the profile bars. */
function intensity(target: string): [number, number] {
  const z = target.match(/\bZ(\d)\s*HR/i);
  if (z) {
    const v = [0.5, 0.55, 0.7, 0.85, 0.98, 1.12][Number(z[1])] ?? 0.7;
    return [v, v];
  }
  const p = target.match(/(\d+)(?:-(\d+))?%/);
  if (p) return [Number(p[1]) / 100, Number(p[2] ?? p[1]) / 100];
  return [0.6, 0.6];
}

function zoneOf(i: number) {
  return i < 0.6 ? 1 : i < 0.76 ? 2 : i < 0.88 ? 3 : i < 1.05 ? 4 : 5;
}

function parse(text: string): { notes: string[]; sections: Section[] } {
  const notes: string[] = [];
  const sections: Section[] = [];
  for (const block of text.split(/\n\s*\n/)) {
    const lines = block.split("\n").map((l) => l.trim()).filter(Boolean);
    const steps = lines.filter((l) => l.startsWith("-"));
    if (!steps.length) {
      notes.push(...lines);
      continue;
    }
    const head = lines[0].startsWith("-") ? "" : lines[0];
    const reps = Number(head.match(/(\d+)\s*x\s*$/i)?.[1] ?? 1);
    sections.push({
      title: head.replace(/\s*\d+\s*x\s*$/i, "") || "Set",
      reps,
      steps: steps.map((l) => {
        const body = l.slice(1).trim();
        const d = body.match(/(?:^|\s)(?:(\d+)h)?(?:(\d+)m(?!in)(\d+)?)?(?:(\d+)s)?(?=\s|$)/);
        let sec = 0;
        let cue = body;
        let target = "";
        if (d && d[0].trim()) {
          sec = Number(d[1] ?? 0) * 3600 + Number(d[2] ?? 0) * 60 + Number(d[3] ?? 0) + Number(d[4] ?? 0);
          cue = body.slice(0, d.index).trim();
          target = body.slice((d.index ?? 0) + d[0].length).trim();
        }
        const [lo, hi] = intensity(target);
        return { sec, target, cue, lo, hi, zone: zoneOf(Math.max(lo, hi)) };
      }),
    });
  }
  return { notes, sections };
}

const ZONE_BAR = ["", "bg-slate-400/60", "bg-sky-400/80", "bg-emerald-400/80", "bg-amber-400/90", "bg-rose-500/90"];
const ZONE_TEXT = ["", "text-slate-400", "text-sky-300", "text-emerald-300", "text-amber-300", "text-rose-300"];

function dur(sec: number) {
  if (sec >= 3600) return `${Math.floor(sec / 3600)}h${String(Math.round((sec % 3600) / 60)).padStart(2, "0")}`;
  if (sec >= 60) return sec % 60 ? `${Math.floor(sec / 60)}m${sec % 60}s` : `${sec / 60} min`;
  return `${sec} s`;
}

/** The workout as bars: width = time, height = intensity, colour = zone. */
function Profile({ sections }: { sections: Section[] }) {
  const bars = sections.flatMap((s) => Array.from({ length: s.reps }, () => s.steps).flat());
  const total = bars.reduce((a, b) => a + b.sec, 0) || 1;
  const top = Math.max(1.2, ...bars.map((b) => b.hi));
  return (
    <div className="flex h-16 items-end gap-px overflow-hidden rounded-lg bg-black/20 px-1 pt-1" aria-label="Workout profile">
      {bars.map((b, i) => {
        const h = (v: number) => `${Math.max(8, (v / top) * 100)}%`;
        const ramp = b.lo !== b.hi;
        return (
          <div
            key={i}
            title={`${b.cue ? b.cue + " · " : ""}${dur(b.sec)} · ${b.target}`}
            className={`${ZONE_BAR[b.zone]} rounded-t-[2px]`}
            style={{
              width: `${(b.sec / total) * 100}%`,
              height: h(Math.max(b.lo, b.hi)),
              clipPath: ramp ? `polygon(0 ${100 - (b.lo / Math.max(b.lo, b.hi)) * 100}%, 100% 0, 100% 100%, 0 100%)` : undefined,
            }}
          />
        );
      })}
    </div>
  );
}

function Steps({ sections }: { sections: Section[] }) {
  return (
    <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-2">
      {sections.map((s, i) => (
        <div key={i} className="rounded-lg border border-white/[0.05] bg-white/[0.02] px-2.5 py-2">
          <div className="mb-1 flex items-baseline justify-between text-[11px] font-semibold uppercase tracking-wide text-slate-400">
            <span>{s.title}</span>
            {s.reps > 1 && <span className="rounded bg-white/[0.07] px-1.5 text-slate-200">×{s.reps}</span>}
          </div>
          <ul className="space-y-0.5">
            {s.steps.map((st, j) => (
              <li key={j} className="flex items-baseline gap-1.5 text-xs">
                <span className="w-12 shrink-0 tabular-nums text-slate-200">{dur(st.sec)}</span>
                <span className={`shrink-0 font-medium ${ZONE_TEXT[st.zone]}`}>{st.target || "-"}</span>
                {st.cue && <span className="truncate text-slate-500" title={st.cue}>{st.cue}</span>}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function StatusChip({ w }: { w: Workout }) {
  if (!w.in_plan) return <span className="text-amber-300">On Intervals.icu, no longer planned</span>;
  if (w.status === "pushed")
    return (
      <span className="text-emerald-300">
        On Intervals.icu{w.changed ? <span className="text-amber-300"> · plan changed since</span> : null}
      </span>
    );
  if (w.status === "skipped") return <span className="text-slate-500">Skipped</span>;
  return <span className="text-slate-500">Not pushed yet</span>;
}

function WorkoutRow({ w, busy, act }: { w: Workout; busy: string | null; act: (w: Workout, a: "push" | "skip" | "remove") => void }) {
  const { icon: Icon, color } = SPORT[w.sport];
  const pushed = w.status === "pushed" || !w.in_plan;
  const { notes, sections } = w.structured ? parse(w.description) : { notes: [], sections: [] };
  const btn = "inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium ring-1 transition disabled:opacity-50";
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-[15px] font-semibold text-slate-100">
            <Icon size={16} className={color} />
            {w.name.replace(/ \(AI coach\)$/, "")}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11px] text-slate-500">
            {w.moving_time_sec ? <span className="text-slate-300">{Math.round(w.moving_time_sec / 60)} min</span> : null}
            <span>·</span>
            <StatusChip w={w} />
          </div>
        </div>
        <div className="flex shrink-0 gap-1.5">
          {(!pushed || (w.changed && w.in_plan)) && (
            <button className={`${btn} bg-orange-500/15 text-orange-200 ring-orange-400/30 hover:bg-orange-500/25`}
              disabled={busy !== null} onClick={() => act(w, "push")}>
              <CloudUpload size={13} />
              {busy === w.kind + "push" ? "Pushing…" : w.changed ? "Update" : "Push to Intervals"}
            </button>
          )}
          {!pushed && w.status !== "skipped" && (
            <button className={`${btn} text-slate-300 ring-white/10 hover:bg-white/[0.05]`} disabled={busy !== null}
              onClick={() => act(w, "skip")}>
              <X size={13} />
              Skip
            </button>
          )}
          {pushed && (
            <button className={`${btn} text-rose-200 ring-rose-400/25 hover:bg-rose-500/10`} disabled={busy !== null}
              onClick={() => act(w, "remove")}>
              <Trash2 size={13} />
              {busy === w.kind + "remove" ? "Removing…" : "Remove"}
            </button>
          )}
        </div>
      </div>

      {w.pinned && w.structured && (
        <p className="text-xs text-slate-400">
          Your session: <span className="text-slate-300">{w.plan_detail}</span>
        </p>
      )}
      {notes.map((n, i) => (
        <p key={i} className="text-xs italic text-amber-300/80">{n}</p>
      ))}
      {sections.length ? (
        <>
          <Profile sections={sections} />
          <Steps sections={sections} />
        </>
      ) : (
        <p className="rounded-lg border border-white/[0.05] bg-white/[0.02] px-3 py-2 text-sm leading-relaxed text-slate-300">
          {w.description}
        </p>
      )}
    </div>
  );
}

function dayLabel(d: WorkoutDay) {
  const dt = new Date(d.date + "T00:00:00");
  return `${d.weekday ?? ""} ${dt.getDate()}.${dt.getMonth() + 1}.`.trim();
}

/** The next training day's structured workouts (core/workouts.py); pushed to Intervals.icu only on a click. */
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

  return (
    <Card
      title={data ? `Workout · ${dayLabel(data)}` : "Workout"}
      icon={<FileText size={15} className="text-orange-400" />}
      action={<span className="hidden text-[11px] text-slate-500 sm:inline">Push puts it on your Intervals.icu calendar → watch</span>}
    >
      {failed ? (
        <p className="text-sm text-slate-500">The workouts couldn't be loaded.</p>
      ) : !data ? (
        <Skeleton className="h-40" />
      ) : data.workouts.length === 0 ? (
        <p className="text-sm text-slate-500">No training planned ({data.status}).</p>
      ) : (
        <div className="divide-y divide-white/[0.06] [&>*]:py-4 [&>*:first-child]:pt-0 [&>*:last-child]:pb-0">
          {data.workouts.map((w) => <WorkoutRow key={w.kind} w={w} busy={busy} act={act} />)}
        </div>
      )}
      {error && <p className="mt-2 text-xs text-rose-300">{error}</p>}
    </Card>
  );
}
