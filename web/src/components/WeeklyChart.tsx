import { useEffect, useState } from "react";
import { BarChart3 } from "lucide-react";
import type { EChartsOption } from "echarts";
import { api } from "../api";
import type { Overview, WeekPoint } from "../types";
import { C, SPORT_COLORS, chartBase } from "../lib/colors";
import { fmt, shortDate } from "../lib/format";
import { Card, Skeleton } from "./Card";
import { Chart } from "./Chart";

const SPORTS = ["Bike", "Run", "Strength", "Swim", "Other"];

export function WeeklyChart({ block, refreshKey, compact = false }: { block: Overview["block"]; refreshKey: number; compact?: boolean }) {
  const [data, setData] = useState<WeekPoint[] | null>(null);

  useEffect(() => {
    api.weekly(12).then(setData).catch(() => setData([]));
  }, [refreshKey]);

  const present = SPORTS.filter((s) => data?.some((w) => w.by_sport[s]));
  const option: EChartsOption = {
    ...chartBase,
    grid: { left: compact ? 34 : 40, right: 8, top: compact ? 52 : 34, bottom: 28 },
    legend: { ...chartBase.legend, ...(compact ? { left: 0, right: "auto", itemGap: 8, textStyle: { color: C.text, fontSize: 10 } } : {}) },
    tooltip: {
      ...chartBase.tooltip,
      formatter: (params: unknown) => {
        const ps = params as { dataIndex: number }[];
        const w = data?.[ps[0]?.dataIndex];
        if (!w) return "";
        const sports = Object.entries(w.by_sport).map(([k, v]) => `${k}: ${v}`).join("<br/>");
        const plan = w.target_min ? `<br/>Plan: ${w.target_min}–${w.target_max} (${w.phase})` : "";
        return `<b>Week of ${shortDate(w.week)}</b><br/>Load ${w.load} TSS · ${w.hours} h<br/>${sports}${plan}`;
      },
    },
    xAxis: {
      type: "category",
      data: data?.map((w) => shortDate(w.week)) ?? [],
      axisLine: { lineStyle: { color: C.grid } },
      axisTick: { show: false },
      axisLabel: { color: C.axis, fontSize: compact ? 10 : 11, interval: compact ? 1 : "auto" },
    },
    yAxis: { splitLine: { lineStyle: { color: C.grid } }, axisLabel: { color: C.axis, fontSize: 11 } },
    series: [
      ...present.map((sport, i) => ({
        name: sport,
        type: "bar" as const,
        stack: "load",
        barWidth: "55%",
        data: data?.map((w) => w.by_sport[sport] ?? 0) ?? [],
        itemStyle: { color: SPORT_COLORS[sport], borderRadius: i === present.length - 1 ? [4, 4, 0, 0] : 0 },
      })),
      {
        name: "Plan max",
        type: "line",
        step: "middle",
        data: data?.map((w) => w.target_max) ?? [],
        showSymbol: false,
        lineStyle: { color: "#f8fafc", width: 1.5, type: "dashed", opacity: 0.6 },
        itemStyle: { color: "#f8fafc" },
      },
      {
        name: "Plan min",
        type: "line",
        step: "middle",
        data: data?.map((w) => w.target_min) ?? [],
        showSymbol: false,
        lineStyle: { color: "#94a3b8", width: 1, type: "dotted", opacity: 0.6 },
        itemStyle: { color: "#94a3b8" },
      },
    ],
  };

  const progress = block ? Math.min(100, (block.load_so_far_this_block / block.target_weekly_load_max) * 100) : 0;

  return (
    <Card title="Weekly load vs plan" icon={<BarChart3 size={15} className="text-teal-400" />}>
      {block && (
        <div className="mb-3 rounded-xl bg-white/[0.03] p-3">
          <div className="flex items-center justify-between text-sm">
            <span className="font-semibold text-white">
              {block.phase}
              {block.is_deload && <span className="ml-2 rounded bg-sky-500/15 px-1.5 py-0.5 text-[11px] text-sky-300">deload</span>}
            </span>
            <span className="text-xs text-slate-400">
              {shortDate(block.start_date)} – {shortDate(block.end_date)}
            </span>
          </div>
          <p className="mt-1 text-xs text-slate-400">{block.primary_focus}</p>
          <div className="mt-2 flex items-center gap-2 text-xs text-slate-400">
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-white/5">
              <div className="h-full rounded-full bg-gradient-to-r from-teal-400 via-sky-400 to-violet-400" style={{ width: `${progress}%` }} />
            </div>
            <span className="tabular-nums">
              {fmt(block.load_so_far_this_block, 0)} / {fmt(block.target_weekly_load_min, 0)}–{fmt(block.target_weekly_load_max, 0)} TSS
            </span>
          </div>
        </div>
      )}
      {data ? <Chart option={option} height={230} /> : <Skeleton className="h-[230px]" />}
    </Card>
  );
}
