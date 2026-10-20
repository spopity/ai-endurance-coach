import { useEffect, useState } from "react";
import { Activity, BarChart3, Bot, Clock, Cpu, DollarSign, Network, Radio, ScrollText, Sparkles } from "lucide-react";
import type { EChartsOption } from "echarts";
import { api } from "../api";
import type { Usage, UsageCall } from "../types";
import { C, SERVICE_META, chartBase } from "../lib/colors";
import { fmt, shortDate, timeAgo } from "../lib/format";
import { Card, Segmented, Skeleton } from "./Card";
import { Chart } from "./Chart";
import { Kpi } from "./KpiRow";

const PROCESS_LABELS: Record<string, string> = {
  app: "Web app",
  telegram_bot: "Telegram bot",
  post_strava_notes: "Strava notes timer",
  check_intervals_api: "API health timer",
  check_intervals_sync: "Nightly sync check",
  sync_strava_names: "Nightly name sync",
};

const TRIGGER_LABELS: Record<string, string> = {
  chat: "Chat",
  briefing: "Morning briefing",
  next_session: "Next-session decision",
  strava_note: "Strava note",
  sync: "Data sync",
  health_check: "Health check",
  sync_check: "Sync check",
  name_sync: "Name sync",
  other: "Other",
};

const COMPONENT_LABELS: Record<string, string> = {
  ai_coach: "AI Coach",
  athlete_twin: "Athlete Twin",
  racing_strategy: "Racing Strategy",
  supervisor: "Router (supervisor)",
  injury_prevention: "Injury & Recovery",
  local_writer: "Local agents · writer",
  local_extractor: "Local agents · extractor",
  strava_note_writer: "Strava note writer",
};

const TRIGGER_COLORS = ["#38bdf8", "#a78bfa", "#f472b6", "#fb923c", "#34d399", "#fbbf24", "#2dd4bf", "#fb7185", "#94a3b8"];

const svc = (s: string) => SERVICE_META[s] ?? { label: s, color: "#64748b" };
const label = (map: Record<string, string>, k: string | null) => (k ? map[k] ?? k : "–");

function usd(v: number | null | undefined): string {
  if (v === null || v === undefined) return "–";
  return v > 0 && v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
}

function tokens(v: number | null | undefined): string {
  if (!v) return "–";
  return v >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}k` : String(v);
}

function ServiceBadge({ service }: { service: string }) {
  const m = svc(service);
  return (
    <span className="rounded-md px-1.5 py-0.5 text-[11px] font-medium" style={{ color: m.color, background: `${m.color}1a` }}>
      {m.label}
    </span>
  );
}

function TimelineChart({ data, compact }: { data: Usage; compact: boolean }) {
  const services = Object.keys(SERVICE_META).filter((s) => data.timeline.some((p) => p.calls[s]));
  const xLabel = (t: string) => (data.bucket === "hour" ? t.slice(11, 16) : shortDate(t));
  const option: EChartsOption = {
    ...chartBase,
    grid: { left: 40, right: 48, top: compact ? 56 : 36, bottom: 28 },
    legend: { ...chartBase.legend, ...(compact ? { left: 0, right: "auto", itemGap: 8, textStyle: { color: C.text, fontSize: 10 } } : {}) },
    tooltip: {
      ...chartBase.tooltip,
      formatter: (params: unknown) => {
        const p = data.timeline[(params as { dataIndex: number }[])[0]?.dataIndex];
        if (!p) return "";
        const rows = Object.entries(p.calls).map(([s, n]) => `${svc(s).label}: ${n}`).join("<br/>") || "No calls";
        return `<b>${data.bucket === "hour" ? `${shortDate(p.t)} ${xLabel(p.t)}` : shortDate(p.t)}</b><br/>${rows}<br/>Gemini cost: ${usd(p.cost)}`;
      },
    },
    xAxis: {
      type: "category",
      data: data.timeline.map((p) => xLabel(p.t)),
      axisLine: { lineStyle: { color: C.grid } },
      axisTick: { show: false },
      axisLabel: { color: C.axis, fontSize: compact ? 10 : 11 },
    },
    yAxis: [
      { name: "calls", nameTextStyle: { color: C.axis, fontSize: 10 }, splitLine: { lineStyle: { color: C.grid } }, axisLabel: { color: C.axis, fontSize: 11 } },
      { name: "$", nameTextStyle: { color: C.axis, fontSize: 10 }, splitLine: { show: false }, axisLabel: { color: C.axis, fontSize: 11, formatter: (v: number) => `$${v === 0 ? "0" : v < 0.1 ? v.toFixed(3) : v < 1 ? v.toFixed(2) : v.toFixed(0)}` } },
    ],
    series: [
      ...services.map((s, i) => ({
        name: svc(s).label,
        type: "bar" as const,
        stack: "calls",
        barWidth: "60%",
        data: data.timeline.map((p) => p.calls[s] ?? 0),
        itemStyle: { color: svc(s).color, borderRadius: i === services.length - 1 ? [4, 4, 0, 0] : 0 },
      })),
      {
        name: "Gemini $",
        type: "line",
        yAxisIndex: 1,
        showSymbol: data.timeline.length <= 31,
        symbolSize: 5,
        data: data.timeline.map((p) => p.cost),
        lineStyle: { color: "#f8fafc", width: 2 },
        itemStyle: { color: "#f8fafc" },
        areaStyle: { color: "rgba(167,139,250,0.08)" },
      },
    ],
  };
  return <Chart option={option} height={compact ? 260 : 290} />;
}

function FlowChart({ data, compact }: { data: Usage; compact: boolean }) {
  const triggers = [...new Set(data.flows.map((f) => f.trigger))];
  const links = new Map<string, number>();
  const add = (a: string, b: string, n: number) => links.set(`${a}→${b}`, (links.get(`${a}→${b}`) ?? 0) + n);
  for (const f of data.flows) {
    add(`p:${f.process}`, `t:${f.trigger}`, f.calls);
    add(`t:${f.trigger}`, `s:${f.service}`, f.calls);
  }
  const nodes = new Set<string>();
  for (const k of links.keys()) k.split("→").forEach((n) => nodes.add(n));
  const nodeLabel = (n: string) => {
    const [kind, key] = [n.slice(0, 1), n.slice(2)];
    return kind === "p" ? label(PROCESS_LABELS, key) : kind === "t" ? label(TRIGGER_LABELS, key) : svc(key).label;
  };
  const nodeColor = (n: string) => {
    const [kind, key] = [n.slice(0, 1), n.slice(2)];
    if (kind === "s") return svc(key).color;
    if (kind === "t") return TRIGGER_COLORS[triggers.indexOf(key) % TRIGGER_COLORS.length];
    return "#64748b";
  };
  const option: EChartsOption = {
    ...chartBase,
    tooltip: {
      ...chartBase.tooltip,
      trigger: "item",
      formatter: (p: unknown) => {
        const d = (p as { data: { source?: string; target?: string; name?: string; value?: number }; value: number }).data;
        return d.source ? `${nodeLabel(d.source)} → ${nodeLabel(d.target!)}: <b>${(p as { value: number }).value}</b> calls` : nodeLabel(d.name!);
      },
    },
    series: [{
      type: "sankey",
      left: 4,
      right: compact ? 70 : 110,
      top: 8,
      bottom: 8,
      nodeWidth: 12,
      nodeGap: 12,
      emphasis: { focus: "adjacency" },
      data: [...nodes].map((n) => ({ name: n, itemStyle: { color: nodeColor(n), borderWidth: 0 } })),
      links: [...links].map(([k, v]) => { const [source, target] = k.split("→"); return { source, target, value: v }; }),
      lineStyle: { color: "gradient", opacity: 0.35, curveness: 0.5 },
      label: { color: C.text, fontSize: compact ? 10 : 12, formatter: (p: { name: string }) => nodeLabel(p.name) },
    }],
  };
  return <Chart option={option} height={compact ? 320 : 340} />;
}

function SpendPie({ data }: { data: Usage }) {
  const rows = data.components.filter((c) => c.service === "gemini" && c.cost > 0);
  const colors = ["#a78bfa", "#38bdf8", "#fb923c", "#f472b6", "#34d399", "#fbbf24"];
  const option: EChartsOption = {
    ...chartBase,
    tooltip: { ...chartBase.tooltip, trigger: "item", formatter: (p: unknown) => {
      const { name, value, percent } = p as { name: string; value: number; percent: number };
      return `${name}<br/><b>${usd(value)}</b> (${percent.toFixed(0)}%)`;
    } },
    legend: { ...chartBase.legend, top: "auto", bottom: 0, left: "center", right: "auto" },
    series: [{
      type: "pie",
      radius: ["48%", "72%"],
      center: ["50%", "44%"],
      itemStyle: { borderColor: "#0b1020", borderWidth: 2, borderRadius: 4 },
      label: { show: false },
      data: rows.map((r, i) => ({ name: label(COMPONENT_LABELS, r.component), value: r.cost, itemStyle: { color: colors[i % colors.length] } })),
    }],
  };
  return rows.length ? <Chart option={option} height={290} /> : <Empty text="No Gemini calls in this period." h={290} />;
}

function HourChart({ data }: { data: Usage }) {
  const services = Object.keys(SERVICE_META).filter((s) => data.by_hour.some((h) => h.calls[s]));
  const option: EChartsOption = {
    ...chartBase,
    grid: { left: 36, right: 8, top: 36, bottom: 28 },
    xAxis: { type: "category", data: data.by_hour.map((h) => String(h.hour).padStart(2, "0")), axisLine: { lineStyle: { color: C.grid } }, axisTick: { show: false }, axisLabel: { color: C.axis, fontSize: 10 } },
    yAxis: { splitLine: { lineStyle: { color: C.grid } }, axisLabel: { color: C.axis, fontSize: 11 } },
    series: services.map((s, i) => ({
      name: svc(s).label,
      type: "bar" as const,
      stack: "h",
      data: data.by_hour.map((h) => h.calls[s] ?? 0),
      itemStyle: { color: svc(s).color, borderRadius: i === services.length - 1 ? [3, 3, 0, 0] : 0 },
    })),
  };
  return <Chart option={option} height={250} />;
}

function Empty({ text, h }: { text: string; h: number }) {
  return <div className="flex items-center justify-center rounded-xl bg-white/[0.02] text-sm text-slate-500" style={{ height: h }}>{text}</div>;
}

function ComponentsTable({ data }: { data: Usage }) {
  if (!data.components.length) return <Empty text="No model calls in this period." h={120} />;
  return (
    <div className="-mx-4 overflow-x-auto">
      <table className="w-full min-w-[560px] text-sm">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wider text-slate-500">
            <th className="px-4 py-2 font-medium">Component</th>
            <th className="px-2 py-2 text-right font-medium">Calls</th>
            <th className="px-2 py-2 text-right font-medium">Tokens in</th>
            <th className="px-2 py-2 text-right font-medium">Tokens out</th>
            <th className="px-2 py-2 text-right font-medium">Avg time</th>
            <th className="px-4 py-2 text-right font-medium">Est. cost</th>
          </tr>
        </thead>
        <tbody>
          {data.components.map((c) => (
            <tr key={`${c.service}-${c.component}`} className="border-t border-white/[0.04]">
              <td className="px-4 py-2">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-slate-100">{label(COMPONENT_LABELS, c.component)}</span>
                  <ServiceBadge service={c.service} />
                </div>
              </td>
              <td className="px-2 py-2 text-right tabular-nums text-slate-300">
                {c.calls}{c.errors > 0 && <span className="ml-1 text-red-300">({c.errors}✕)</span>}
              </td>
              <td className="px-2 py-2 text-right tabular-nums text-slate-400">{tokens(c.input_tokens)}</td>
              <td className="px-2 py-2 text-right tabular-nums text-slate-400">{tokens(c.output_tokens)}</td>
              <td className="px-2 py-2 text-right tabular-nums text-slate-400">{c.avg_ms ? `${fmt(c.avg_ms / 1000, 1)} s` : "–"}</td>
              <td className="px-4 py-2 text-right tabular-nums font-medium text-white">{c.service === "gemini" ? usd(c.cost) : <span className="text-emerald-300">free</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function callDetail(c: UsageCall): string {
  if (c.input_tokens !== null) {
    const think = c.thinking_tokens ? ` (${tokens(c.thinking_tokens)} thinking)` : "";
    return `${tokens(c.input_tokens)} in · ${tokens(c.output_tokens)} out${think}`;
  }
  return c.operation ?? "";
}

function RecentCalls({ data, compact }: { data: Usage; compact: boolean }) {
  if (!data.recent.length) return <Empty text="No calls logged yet." h={120} />;
  if (compact) {
    return (
      <ul className="-mx-1 divide-y divide-white/[0.04]">
        {data.recent.slice(0, 40).map((c, i) => (
          <li key={i} className="flex items-center gap-3 px-1 py-2">
            <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: c.ok ? svc(c.service).color : "#f87171" }} />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 text-sm text-slate-100">
                <ServiceBadge service={c.service} />
                <span className="truncate">{c.component ? label(COMPONENT_LABELS, c.component) : label(TRIGGER_LABELS, c.trigger)}</span>
              </div>
              <div className="mt-0.5 truncate text-[11px] text-slate-500">{timeAgo(c.ts)} · {callDetail(c)}</div>
            </div>
            <div className="shrink-0 text-right text-[11px] tabular-nums text-slate-400">
              {c.cost_usd ? <div className="text-sm font-medium text-white">{usd(c.cost_usd)}</div> : null}
              {c.duration_ms !== null && `${fmt(c.duration_ms / 1000, 1)} s`}
            </div>
          </li>
        ))}
      </ul>
    );
  }
  return (
    <div className="-mx-4 max-h-[460px] overflow-auto">
      <table className="w-full min-w-[900px] text-sm">
        <thead className="sticky top-0 bg-ink-900">
          <tr className="text-left text-[11px] uppercase tracking-wider text-slate-500">
            <th className="px-4 py-2 font-medium">When</th>
            <th className="px-2 py-2 font-medium">Service</th>
            <th className="px-2 py-2 font-medium">Who / why</th>
            <th className="px-2 py-2 font-medium">Detail</th>
            <th className="px-2 py-2 text-right font-medium">Time</th>
            <th className="px-2 py-2 text-right font-medium">Status</th>
            <th className="px-4 py-2 text-right font-medium">Cost</th>
          </tr>
        </thead>
        <tbody>
          {data.recent.map((c, i) => (
            <tr key={i} className="border-t border-white/[0.04]" title={c.error ?? undefined}>
              <td className="whitespace-nowrap px-4 py-1.5 tabular-nums text-slate-400">
                {new Date(c.ts).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" })}
              </td>
              <td className="px-2 py-1.5"><ServiceBadge service={c.service} /></td>
              <td className="px-2 py-1.5">
                <div className="text-slate-200">{c.component ? label(COMPONENT_LABELS, c.component) : label(PROCESS_LABELS, c.process)}</div>
                <div className="text-[11px] text-slate-500">{label(TRIGGER_LABELS, c.trigger ?? "other")}</div>
              </td>
              <td className="max-w-[320px] truncate px-2 py-1.5 font-mono text-[12px] text-slate-400">{callDetail(c)}</td>
              <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">{c.duration_ms !== null ? `${fmt(c.duration_ms / 1000, 2)} s` : "–"}</td>
              <td className={`px-2 py-1.5 text-right tabular-nums ${c.ok ? "text-emerald-300" : "text-red-300"}`}>{c.status ?? (c.ok ? "ok" : "error")}</td>
              <td className="px-4 py-1.5 text-right tabular-nums font-medium text-white">{c.service === "gemini" ? usd(c.cost_usd) : "–"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SystemPage({ refreshKey, compact = false }: { refreshKey: number; compact?: boolean }) {
  const [days, setDays] = useState(7);
  const [data, setData] = useState<Usage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    api.usage(days).then((d) => { setData(d); setError(null); }).catch((e) => setError(String(e)));
  }, [days, refreshKey]);

  const count = (s: string) => data?.totals.find((t) => t.service === s);
  const g = data?.gemini;
  const rangeLabel = days === 1 ? "last 24 h" : `last ${days} days`;

  return (
    <div className="mx-auto max-w-[1600px] space-y-3 lg:space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="flex items-center gap-2 text-lg font-semibold text-white">
            <Radio size={18} className="text-violet-400" /> System activity
          </h2>
          <p className="text-xs text-slate-500">
            Every outgoing call to Gemini, the local model, Intervals.icu, Strava and the rest
            {data?.logging_since && <> · logging since {new Date(data.logging_since).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</>}
          </p>
        </div>
        <Segmented options={[{ label: "24 h", value: 1 }, { label: "7 days", value: 7 }, { label: "30 days", value: 30 }]} value={days} onChange={setDays} />
      </div>

      {error && <div className="rounded-xl border border-red-500/20 bg-red-500/10 p-3 text-sm text-red-200">Could not load system activity: {error}</div>}

      {data && g ? (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 2xl:grid-cols-6">
          <Kpi label="Gemini cost" value={usd(g.cost)} accent="#a78bfa" icon={<DollarSign size={16} />} sub={<>estimated, {rangeLabel}</>} />
          <Kpi label="Per month" value={g.projected_month !== null ? usd(g.projected_month) : "–"} accent="#f472b6" icon={<Sparkles size={16} />}
            sub={<>projected from {data.logging_since && Date.now() - new Date(data.logging_since).getTime() < 86400000 ? "today so far" : "this period"}</>} />
          <Kpi label="Gemini calls" value={String(g.calls)} accent="#a78bfa" icon={<Bot size={16} />}
            sub={<>{g.avg_cost_per_call !== null ? `${usd(g.avg_cost_per_call)} per call` : "no calls yet"}</>} />
          <Kpi label="Gemini tokens" value={tokens(g.input_tokens + g.output_tokens)} accent="#38bdf8" icon={<Activity size={16} />}
            sub={<>{tokens(g.input_tokens)} in · {tokens(g.output_tokens)} out{g.thinking_tokens ? ` · ${Math.round((g.thinking_tokens / Math.max(1, g.output_tokens)) * 100)}% thinking` : ""}</>} />
          <Kpi label="Local model" value={String(count("ollama")?.calls ?? 0)} unit="calls" accent="#34d399" icon={<Cpu size={16} />}
            sub={<>free · avg {count("ollama")?.avg_ms ? `${fmt((count("ollama")!.avg_ms ?? 0) / 1000, 1)} s` : "–"}</>} />
          <Kpi label="Data APIs" value={String((count("intervals")?.calls ?? 0) + (count("strava")?.calls ?? 0))} unit="calls" accent="#fb923c" icon={<Network size={16} />}
            sub={<>Intervals {count("intervals")?.calls ?? 0} · Strava {count("strava")?.calls ?? 0}
              {((count("intervals")?.errors ?? 0) + (count("strava")?.errors ?? 0)) > 0 &&
                <span className="text-red-300"> · {(count("intervals")?.errors ?? 0) + (count("strava")?.errors ?? 0)} failed</span>}</>} />
        </div>
      ) : (
        <Skeleton className="h-[116px]" />
      )}

      <div className="grid grid-cols-1 gap-3 lg:gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2 [&>section]:h-full">
          <Card title="Calls over time" icon={<BarChart3 size={15} className="text-sky-400" />}>
            {data ? <TimelineChart data={data} compact={compact} /> : <Skeleton className="h-[290px]" />}
          </Card>
        </div>
        <div className="[&>section]:h-full">
          <Card title="Gemini spend by component" icon={<DollarSign size={15} className="text-violet-400" />}>
            {data ? <SpendPie data={data} /> : <Skeleton className="h-[290px]" />}
          </Card>
        </div>
      </div>

      <Card title="How the system runs" icon={<Network size={15} className="text-pink-400" />}
        action={<span className="text-[11px] text-slate-500">process → reason → service · line width = calls</span>}>
        {data ? (data.flows.length ? <FlowChart data={data} compact={compact} /> : <Empty text="No calls in this period." h={200} />) : <Skeleton className="h-[340px]" />}
      </Card>

      <div className="grid grid-cols-1 gap-3 lg:gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2 [&>section]:h-full">
          <Card title="Models by component" icon={<Bot size={15} className="text-violet-400" />}
            action={data && <span className="text-[11px] text-slate-500">
              Gemini {data.gemini.price.model}: ${data.gemini.price.input_per_m}/M in · ${data.gemini.price.output_per_m}/M out (thinking billed as out)
            </span>}>
            {data ? <ComponentsTable data={data} /> : <Skeleton className="h-40" />}
          </Card>
        </div>
        <div className="[&>section]:h-full">
          <Card title="Busy hours" icon={<Clock size={15} className="text-amber-400" />}>
            {data ? <HourChart data={data} /> : <Skeleton className="h-[250px]" />}
          </Card>
        </div>
      </div>

      <Card title="Recent calls" icon={<ScrollText size={15} className="text-teal-400" />}
        action={<span className="text-[11px] text-slate-500">latest {data?.recent.length ?? 0}, all time</span>}>
        {data ? <RecentCalls data={data} compact={compact} /> : <Skeleton className="h-64" />}
      </Card>
    </div>
  );
}
