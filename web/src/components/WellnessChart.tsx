import { useEffect, useMemo, useState } from "react";
import { HeartPulse } from "lucide-react";
import type { EChartsOption } from "echarts";
import { api } from "../api";
import type { Overview, WellnessPoint } from "../types";
import { C, chartBase } from "../lib/colors";
import { fmt } from "../lib/format";
import { Card, Segmented, Skeleton } from "./Card";
import { Chart } from "./Chart";

type Metric = "hrv" | "resting_hr" | "sleep_h";
const METRICS: { label: string; value: Metric }[] = [
  { label: "HRV", value: "hrv" },
  { label: "Resting HR", value: "resting_hr" },
  { label: "Sleep", value: "sleep_h" },
];
const META: Record<Metric, { name: string; unit: string; color: string }> = {
  hrv: { name: "HRV", unit: "ms", color: C.hrv },
  resting_hr: { name: "Resting HR", unit: "bpm", color: C.rhr },
  sleep_h: { name: "Sleep", unit: "h", color: C.sleep },
};

function rolling(values: (number | null)[], window = 7): (number | null)[] {
  return values.map((_, i) => {
    const slice = values.slice(Math.max(0, i - window + 1), i + 1).filter((v): v is number => v !== null);
    return slice.length >= 3 ? +(slice.reduce((a, b) => a + b, 0) / slice.length).toFixed(1) : null;
  });
}

export function WellnessChart({ hrv, refreshKey, compact = false }: { hrv: Overview["hrv"]; refreshKey: number; compact?: boolean }) {
  const [metric, setMetric] = useState<Metric>("hrv");
  const [data, setData] = useState<WellnessPoint[] | null>(null);

  useEffect(() => {
    api.wellness(90).then(setData).catch(() => setData([]));
  }, [refreshKey]);

  const meta = META[metric];
  const values = useMemo(() => data?.map((d) => d[metric]) ?? [], [data, metric]);
  const coverage = values.filter((v) => v !== null).length;

  const option: EChartsOption = {
    ...chartBase,
    grid: { left: compact ? 34 : 40, right: 8, top: 34, bottom: 28 },
    xAxis: {
      type: "category",
      data: data?.map((d) => d.day) ?? [],
      axisLine: { lineStyle: { color: C.grid } },
      axisTick: { show: false },
      axisLabel: { color: C.axis, fontSize: 11, formatter: (v: string) => v.slice(5) },
    },
    yAxis: { scale: true, splitLine: { lineStyle: { color: C.grid } }, axisLabel: { color: C.axis, fontSize: 11 } },
    series: [
      {
        name: meta.name,
        type: "bar",
        data: values,
        barMaxWidth: 6,
        itemStyle: { color: meta.color, opacity: 0.45, borderRadius: [2, 2, 0, 0] },
        ...(metric === "hrv" && hrv
          ? {
              markArea: {
                silent: true,
                itemStyle: { color: "rgba(52,211,153,0.08)" },
                data: [[{ yAxis: hrv.baseline - hrv.sd / 2 }, { yAxis: hrv.baseline + hrv.sd / 2 }]],
              },
              markLine: {
                silent: true,
                symbol: "none",
                label: { show: false },
                lineStyle: { color: "rgba(52,211,153,0.5)", type: "dashed" },
                data: [{ yAxis: hrv.baseline }],
              },
            }
          : {}),
      },
      {
        name: "7-day avg",
        type: "line",
        data: rolling(values),
        smooth: true,
        showSymbol: false,
        connectNulls: true,
        lineStyle: { color: meta.color, width: 2.5 },
        itemStyle: { color: meta.color },
      },
    ],
  };

  return (
    <Card
      title="Recovery trends"
      icon={<HeartPulse size={15} className="text-violet-400" />}
      action={<Segmented options={METRICS} value={metric} onChange={setMetric} />}
    >
      {!data ? (
        <Skeleton className="h-[230px]" />
      ) : coverage < 5 ? (
        <div className="flex h-[230px] items-center justify-center text-center text-sm text-slate-500">
          Not enough {meta.name.toLowerCase()} data in the last 90 days
          <br />({coverage} readings) - wear the watch overnight to track this.
        </div>
      ) : (
        <>
          <Chart option={option} height={230} />
          {metric === "hrv" && hrv && (
            <p className="mt-1 text-[11px] text-slate-500">
              Green band = your normal range ({fmt(hrv.baseline - hrv.sd / 2, 0)}–{fmt(hrv.baseline + hrv.sd / 2, 0)} ms, 60-day baseline ±0.5 SD)
            </p>
          )}
        </>
      )}
    </Card>
  );
}
