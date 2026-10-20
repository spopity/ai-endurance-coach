import { useEffect, useState } from "react";
import { TrendingUp } from "lucide-react";
import type { EChartsOption } from "echarts";
import { api } from "../api";
import type { PmcPoint } from "../types";
import { C, chartBase } from "../lib/colors";
import { Card, Segmented, Skeleton } from "./Card";
import { Chart } from "./Chart";

const RANGES = [
  { label: "90d", value: 90 },
  { label: "6m", value: 180 },
  { label: "1y", value: 365 },
  { label: "3y", value: 1095 },
  { label: "All", value: 6000 },
];

// Form (TSB) zones, as on Intervals.icu.
const ZONES: [number, number, string][] = [
  [25, 60, "rgba(148,163,184,0.06)"],
  [5, 25, "rgba(56,189,248,0.08)"],
  [-10, 5, "rgba(163,163,163,0.05)"],
  [-30, -10, "rgba(52,211,153,0.08)"],
  [-80, -30, "rgba(248,113,113,0.09)"],
];

export function PmcChart({ refreshKey, compact = false }: { refreshKey: number; compact?: boolean }) {
  const [days, setDays] = useState(180);
  const [data, setData] = useState<PmcPoint[] | null>(null);

  useEffect(() => {
    api.pmc(days).then(setData).catch(() => setData([]));
  }, [days, refreshKey]);

  const days_ = data?.map((d) => d.day) ?? [];
  const option: EChartsOption = {
    ...chartBase,
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: compact
      ? [
          { left: 30, right: 30, top: 44, height: "50%" },
          { left: 30, right: 30, top: "74%", bottom: 22 },
        ]
      : [
          { left: 44, right: 44, top: 34, height: "52%" },
          { left: 44, right: 44, top: "72%", bottom: 24 },
        ],
    legend: { ...chartBase.legend, ...(compact ? { left: 0, right: "auto", itemGap: 8, textStyle: { color: C.text, fontSize: 10 } } : {}) },
    xAxis: [0, 1].map((i) => ({
      type: "category" as const,
      gridIndex: i,
      data: days_,
      boundaryGap: false,
      axisLine: { lineStyle: { color: C.grid } },
      axisTick: { show: false },
      axisLabel: { show: i === 1, color: C.axis, fontSize: 11, formatter: (v: string) => v.slice(5) },
    })),
    yAxis: [
      { gridIndex: 0, splitLine: { lineStyle: { color: C.grid } }, axisLabel: { color: C.axis, fontSize: 11 } },
      { gridIndex: 0, position: "right", splitLine: { show: false }, axisLabel: { color: C.axis, fontSize: 11 } },
      { gridIndex: 1, splitNumber: 2, splitLine: { lineStyle: { color: C.grid } }, axisLabel: { color: C.axis, fontSize: 11 } },
    ],
    series: [
      {
        name: "Daily load", type: "bar", xAxisIndex: 0, yAxisIndex: 1, data: data?.map((d) => d.load) ?? [],
        itemStyle: { color: "rgba(100,116,139,0.35)", borderRadius: [2, 2, 0, 0] }, barMaxWidth: 6,
      },
      {
        name: "Fitness (CTL)", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: data?.map((d) => d.ctl) ?? [],
        showSymbol: false, smooth: true, lineStyle: { width: 2.5, color: C.ctl }, itemStyle: { color: C.ctl },
        areaStyle: {
          color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [{ offset: 0, color: "rgba(56,189,248,0.35)" }, { offset: 1, color: "rgba(56,189,248,0)" }] },
        },
      },
      {
        name: "Fatigue (ATL)", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: data?.map((d) => d.atl) ?? [],
        showSymbol: false, smooth: true, lineStyle: { width: 1.5, color: C.atl }, itemStyle: { color: C.atl },
      },
      {
        name: "Form (TSB)", type: "line", xAxisIndex: 1, yAxisIndex: 2, data: data?.map((d) => d.tsb) ?? [],
        showSymbol: false, smooth: true, lineStyle: { width: 2, color: C.tsb }, itemStyle: { color: C.tsb },
        markArea: { silent: true, data: ZONES.map(([lo, hi, color]) => [{ yAxis: lo, itemStyle: { color } }, { yAxis: hi }]) as never },
        markLine: { silent: true, symbol: "none", lineStyle: { color: "rgba(148,163,184,0.3)", type: "dashed" }, data: [{ yAxis: 0 }], label: { show: false } },
      },
    ],
    dataZoom: [{ type: "inside", xAxisIndex: [0, 1] }],
  };

  return (
    <Card
      title="Fitness · Fatigue · Form"
      icon={<TrendingUp size={15} className="text-sky-400" />}
      action={<Segmented options={RANGES} value={days} onChange={setDays} />}
    >
      {data ? <Chart option={option} height={compact ? 300 : 360} /> : <Skeleton className="h-[300px] lg:h-[360px]" />}
      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500">
        <span><i className="mr-1 inline-block h-2 w-2 rounded-sm bg-sky-400/50" />Fresh +5…+25</span>
        <span><i className="mr-1 inline-block h-2 w-2 rounded-sm bg-neutral-400/40" />Grey zone −10…+5</span>
        <span><i className="mr-1 inline-block h-2 w-2 rounded-sm bg-emerald-400/50" />Optimal −30…−10</span>
        <span><i className="mr-1 inline-block h-2 w-2 rounded-sm bg-red-400/50" />High risk &lt; −30</span>
      </div>
    </Card>
  );
}
