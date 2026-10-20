import { useEffect, useState } from "react";
import { MessageCircle, Navigation, RefreshCw, Sparkles } from "lucide-react";
import { api } from "../api";
import type { NextSession, ReadinessLevel } from "../types";
import { timeAgo } from "../lib/format";
import { Card, Skeleton } from "./Card";

const READINESS: Record<ReadinessLevel, { dot: string; text: string; bg: string; ring: string; glow: string }> = {
  green: { dot: "bg-emerald-400", text: "text-emerald-300", bg: "bg-emerald-500/10", ring: "ring-emerald-500/25", glow: "from-emerald-400/70" },
  amber: { dot: "bg-amber-400", text: "text-amber-300", bg: "bg-amber-500/10", ring: "ring-amber-500/25", glow: "from-amber-400/70" },
  red: { dot: "bg-rose-400", text: "text-rose-300", bg: "bg-rose-500/10", ring: "ring-rose-500/25", glow: "from-rose-400/70" },
};

export function NextSessionCard({ refreshKey, onAsk }: { refreshKey: number; onAsk: (q: string) => void }) {
  const [data, setData] = useState<NextSession | null>(null);
  const [failed, setFailed] = useState(false);
  const [deciding, setDeciding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.nextSession().then(setData).catch(() => setFailed(true));
  }, [refreshKey]);

  const decide = (redo: boolean) => {
    setDeciding(true);
    setError(null);
    api
      .decideNextSession(redo)
      .then(setData)
      .catch((e: Error) => setError(e.message))
      .finally(() => setDeciding(false));
  };

  const r = data?.readiness ? READINESS[data.readiness.level] : null;
  const d = data?.decision;

  return (
    <Card
      title="Next training"
      icon={<Navigation size={15} className="text-violet-400" />}
      className="relative overflow-hidden"
      action={
        r && (
          <span className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium ring-1 ${r.bg} ${r.ring} ${r.text}`}>
            <span className={`h-2 w-2 rounded-full ${r.dot}`} />
            {data!.readiness!.verdict}
          </span>
        )
      }
    >
      {r && <div className={`pointer-events-none absolute inset-y-0 left-0 w-1 bg-gradient-to-b ${r.glow} to-transparent`} />}

      {data === null ? (
        failed ? <p className="text-sm text-slate-500">Could not load the next session.</p> : <Skeleton className="h-[72px]" />
      ) : deciding ? (
        <div className="flex items-center gap-2 py-3 text-sm text-slate-300">
          <RefreshCw size={15} className="animate-spin text-violet-400" />
          The coach is deciding your next session...
        </div>
      ) : d ? (
        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div className="min-w-0">
            <p className="bg-gradient-to-r from-white to-violet-200 bg-clip-text text-lg font-semibold leading-snug text-transparent sm:text-xl">
              {d.session}
            </p>
            {d.why && <p className="mt-1 text-sm text-slate-400">{d.why}</p>}
            <p className="mt-2 truncate text-[11px] text-slate-500">
              {d.decided_by === "athlete" ? "Your change, applied" : "Decided"} {timeAgo(d.decided_at)} · after {d.after_activity}
            </p>
          </div>
          <div className="flex shrink-0 gap-2">
            <button
              onClick={() => onAsk(`My next session is: ${d.session}. How exactly should I do it, and why this one?`)}
              className="inline-flex items-center gap-1.5 rounded-lg bg-violet-500/15 px-2.5 py-1.5 text-xs font-medium text-violet-200 ring-1 ring-violet-500/25 transition hover:bg-violet-500/25"
            >
              <MessageCircle size={13} /> Ask coach
            </button>
            <button
              onClick={() => decide(true)}
              title="Decide again with the latest data (e.g. after this morning's HRV)"
              className="inline-flex items-center gap-1.5 rounded-lg bg-white/[0.05] px-2.5 py-1.5 text-xs font-medium text-slate-300 ring-1 ring-white/10 transition hover:bg-white/10"
            >
              <RefreshCw size={13} /> Re-decide
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-sm text-slate-400">
            No current decision - it's made automatically after each ride or run.
          </p>
          <button
            onClick={() => decide(false)}
            className="inline-flex shrink-0 items-center gap-1.5 self-start rounded-lg bg-violet-500/15 px-2.5 py-1.5 text-xs font-medium text-violet-200 ring-1 ring-violet-500/25 transition hover:bg-violet-500/25"
          >
            <Sparkles size={13} /> Decide now
          </button>
        </div>
      )}
      {error && <p className="mt-2 text-xs text-rose-400">{error}</p>}
    </Card>
  );
}
