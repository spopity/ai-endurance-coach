import { useEffect, useState } from "react";
import { ShieldAlert, ShieldCheck, ChevronDown } from "lucide-react";
import { api } from "../api";
import type { InjuryReport, Severity } from "../types";
import { SEVERITY } from "../lib/colors";
import { Card, Skeleton } from "./Card";

const ORDER: Severity[] = ["high", "caution", "ok", "info"];

export function InjuryCard({ refreshKey, onAsk }: { refreshKey: number; onAsk: (q: string) => void }) {
  const [report, setReport] = useState<InjuryReport | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);

  useEffect(() => {
    api.injury().then(setReport).catch(() => setReport(null));
  }, [refreshKey]);

  if (!report) {
    return (
      <Card title="Injury & recovery" icon={<ShieldAlert size={15} className="text-emerald-400" />}>
        <Skeleton className="h-[260px]" />
      </Card>
    );
  }

  const overall = SEVERITY[report.overall];
  const flagged = report.findings.filter((f) => f.severity === "high" || f.severity === "caution");
  const good = report.findings.filter((f) => f.severity === "ok" || f.severity === "info");

  return (
    <Card
      title="Injury & recovery"
      icon={report.overall === "ok" ? <ShieldCheck size={15} className="text-emerald-400" /> : <ShieldAlert size={15} style={{ color: overall.color }} />}
      action={
        <button
          onClick={() => onAsk("Am I at risk of injury or overtraining right now? What should I change?")}
          className="rounded-lg bg-emerald-500/10 px-2.5 py-1 text-xs font-medium text-emerald-300 ring-1 ring-emerald-500/20 transition hover:bg-emerald-500/20"
        >
          Ask the agent
        </button>
      }
    >
      <div className={`mb-3 flex items-center justify-between rounded-xl px-3 py-2 ring-1 ${overall.bg} ${overall.ring}`}>
        <span className={`text-sm font-semibold ${overall.text}`}>Overall: {overall.label}</span>
        <span className="flex gap-2 text-xs">
          {ORDER.filter((s) => report.counts[s]).map((s) => (
            <span key={s} className="flex items-center gap-1 text-slate-300">
              <i className="h-2 w-2 rounded-full" style={{ background: SEVERITY[s].color }} />
              {report.counts[s]}
            </span>
          ))}
        </span>
      </div>

      <ul className="space-y-2">
        {flagged.length === 0 && <li className="text-sm text-slate-400">No warnings - everything checked is within your normal range.</li>}
        {flagged.map((f, i) => (
          <li
            key={i}
            onClick={() => setExpanded(expanded === i ? null : i)}
            className="cursor-pointer rounded-xl border border-white/[0.05] bg-white/[0.02] p-3 transition hover:bg-white/[0.04]"
          >
            <p className="text-sm font-medium leading-snug text-white">
              <i className="mr-2 inline-block h-2 w-2 rounded-full align-middle" style={{ background: SEVERITY[f.severity].color }} />
              <span className="mr-1.5 text-slate-400">{f.area}</span>
              {f.title}
            </p>
            <p className={`mt-1 text-xs leading-relaxed text-slate-400 ${expanded === i ? "" : "line-clamp-2"}`}>{f.detail}</p>
          </li>
        ))}
      </ul>

      {good.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {good.map((f, i) => (
            <span
              key={i}
              className="rounded-full px-2 py-0.5 text-[11px]"
              style={{
                color: SEVERITY[f.severity].color,
                background: `${SEVERITY[f.severity].color}14`,
                boxShadow: `inset 0 0 0 1px ${SEVERITY[f.severity].color}33`,
              }}
            >
              {f.area}: {f.title}
            </span>
          ))}
        </div>
      )}

      <button onClick={() => setShowAll((v) => !v)} className="mt-3 flex items-center gap-1 text-[11px] text-slate-500 hover:text-slate-300">
        <ChevronDown size={12} className={`transition ${showAll ? "rotate-180" : ""}`} />
        What was checked{report.data_gaps.length ? " · data gaps" : ""}
      </button>
      {showAll && (
        <div className="mt-2 space-y-1 text-[11px] text-slate-500">
          <p>Checked: {report.checked.join(", ")}.</p>
          {report.data_gaps.map((g, i) => (
            <p key={i}>· {g}</p>
          ))}
        </div>
      )}
    </Card>
  );
}
