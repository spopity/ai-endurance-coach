/**
 * TypeScript ports of the deterministic calculators in core/simulators.py, so the demo
 * chat computes its numbers the same way the real agents' tools do.
 */
import { ATHLETE, ATL_DECAY, CTL_DECAY, DATA, RACE, TODAY, addDays, iso, latest, planWeek } from "./data";

const r1 = (v: number) => v.toFixed(1);
const signed = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(1)}`;

export function currentWeight(): number {
  for (let i = DATA.days.length - 1; i >= 0; i--) if (DATA.days[i].weight_kg) return DATA.days[i].weight_kg!;
  return 72;
}

export function avgWeeklyLoad4w(): number {
  return Math.round((DATA.days.slice(-28).reduce((s, d) => s + d.load, 0) / 4) * 10) / 10;
}

// --- project_training_load -----------------------------------------------------------

export interface Projection {
  text: string;
  end: { ctl: number; atl: number; tsb: number };
  peakCtl: number;
  onDate: { ctl: number; atl: number; tsb: number } | null;
}

export function projectTrainingLoad(plan: number[], daysPerWeek = 5, reportDate?: string): Projection {
  const start = latest();
  let ctl = start.ctl;
  let atl = start.atl;
  let day = new Date(start.day + "T00:00:00");
  const offsets = new Set(Array.from({ length: daysPerWeek }, (_, i) => Math.round((i * 7) / daysPerWeek)));
  let peak = ctl;
  let minTsb = ctl - atl;
  let maxTsb = ctl - atl;
  let onDate: Projection["onDate"] = null;
  const lines = [
    `Starting point (${start.day}, from Intervals.icu): CTL ${r1(ctl)}, ATL ${r1(atl)}, TSB ${signed(ctl - atl)}`,
    `Actual load over the last 4 weeks: avg ${avgWeeklyLoad4w()} TSS/week`,
    `Projection (each week's TSS spread over ${daysPerWeek} evenly spaced training days; exponential model, CTL 42d / ATL 7d - same as Intervals.icu):`,
  ];
  for (const [w, weekly] of plan.entries()) {
    const daily = weekly / daysPerWeek;
    for (let d = 0; d < 7; d++) {
      day = addDays(day, 1);
      const load = offsets.has(d) ? daily : 0;
      ctl = ctl * CTL_DECAY + load * (1 - CTL_DECAY);
      atl = atl * ATL_DECAY + load * (1 - ATL_DECAY);
      peak = Math.max(peak, ctl);
      minTsb = Math.min(minTsb, ctl - atl);
      maxTsb = Math.max(maxTsb, ctl - atl);
      if (reportDate && iso(day) === reportDate) onDate = { ctl, atl, tsb: ctl - atl };
    }
    lines.push(`- Week ${w + 1} (to ${iso(day)}): ${weekly} TSS -> CTL ${r1(ctl)}, ATL ${r1(atl)}, TSB ${signed(ctl - atl)}`);
  }
  lines.push(`End of projection (${iso(day)}): CTL ${r1(ctl)}, ATL ${r1(atl)}, TSB ${signed(ctl - atl)}. Peak CTL ${r1(peak)}; TSB range ${signed(minTsb)} to ${signed(maxTsb)}.`);
  if (onDate) lines.push(`On ${reportDate}: CTL ${r1(onDate.ctl)}, ATL ${r1(onDate.atl)}, TSB ${signed(onDate.tsb)}.`);
  lines.push("Rule of thumb: TSB below about -30 means high overreaching risk; +5 to +25 is a typical fresh race-day range.");
  return { text: lines.join("\n"), end: { ctl, atl, tsb: ctl - atl }, peakCtl: peak, onDate };
}

// --- estimate_ride_time --------------------------------------------------------------

const G = 9.81;
const EFFICIENCY = 0.97;
const POSITIONS = { hoods: [0.36, 0.004], drops: [0.31, 0.004], tt: [0.24, 0.004], gravel: [0.38, 0.008], mtb: [0.45, 0.012] } as const;
export type Position = keyof typeof POSITIONS;

function powerRequired(v: number, mass: number, slope: number, cda: number, crr: number, rho: number) {
  const theta = Math.atan(slope);
  return v * mass * G * (Math.sin(theta) + crr * Math.cos(theta)) + 0.5 * rho * cda * v ** 3;
}

function solveSpeed(power: number, mass: number, slope: number, cda: number, crr: number, rho: number) {
  let lo = 0.05;
  let hi = 40;
  if (powerRequired(hi, mass, slope, cda, crr, rho) < power) return hi;
  for (let i = 0; i < 100; i++) {
    const mid = (lo + hi) / 2;
    if (powerRequired(mid, mass, slope, cda, crr, rho) < power) lo = mid;
    else hi = mid;
  }
  return (lo + hi) / 2;
}

export function steadySpeedKmh(power: number, mass: number, gradientPct: number, position: Position = "hoods", cdaFactor = 1) {
  const [cda, crr] = POSITIONS[position];
  return solveSpeed(power * EFFICIENCY, mass, gradientPct / 100, cda * cdaFactor, crr, 1.225) * 3.6;
}

export const formatHms = (seconds: number) => {
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  return h ? `${h}h ${String(m).padStart(2, "0")}m ${String(ss).padStart(2, "0")}s` : `${m}m ${String(ss).padStart(2, "0")}s`;
};

export function estimateRideTime(opts: { distanceKm: number; gradientPct: number; powerW?: number; pctFtp?: number; riderKg?: number; position?: Position }) {
  const ftp = ATHLETE.ftp;
  const rider = opts.riderKg ?? currentWeight();
  const power = opts.powerW ?? (ftp * (opts.pctFtp ?? 85)) / 100;
  const position = opts.position ?? "hoods";
  const [cda, crr] = POSITIONS[position];
  const mass = rider + 8.5;
  const v = solveSpeed(power * EFFICIENCY, mass, opts.gradientPct / 100, cda, crr, 1.225);
  const seconds = (opts.distanceKm * 1000) / v;
  const lines = [
    `Segment: ${opts.distanceKm} km at ${opts.gradientPct >= 0 ? "+" : ""}${opts.gradientPct}% (${position}, altitude 0 m)`,
    `Power: ${power.toFixed(0)} W (${((power / ftp) * 100).toFixed(0)}% of FTP ${ftp} W), ${(power / rider).toFixed(2)} W/kg at ${rider} kg rider + 8.5 kg bike`,
    `Estimated time: ${formatHms(seconds)} at ${(v * 3.6).toFixed(1)} km/h average`,
  ];
  if (opts.gradientPct > 0) {
    const gain = opts.distanceKm * 1000 * (opts.gradientPct / 100);
    lines.push(`Elevation gain: ~${gain.toFixed(0)} m, VAM ~${(gain / (seconds / 3600)).toFixed(0)} m/h`);
  }
  lines.push(`Assumptions: steady power, no wind, CdA ${cda} m², Crr ${crr}, drivetrain efficiency 97%. Real-world times vary with pacing, wind, surface and gradient changes.`);
  return { text: lines.join("\n"), seconds, speed: v * 3.6, power, rider };
}

// --- Race plan (core/race_planner.py, simplified) -------------------------------------

export function racePlan() {
  const weight = currentWeight();
  const raceDate = new Date(RACE.date + "T00:00:00");
  const days = Math.round((raceDate.getTime() - TODAY.getTime()) / 86400e3);
  // Course split: ~45% climbing at 5%, the rest flat/rolling and descents.
  const climbKm = (RACE.elevation_m / 0.05 / 1000) * 0.9;
  const descentKm = climbKm * 0.8;
  const flatKm = RACE.distance_km - climbKm - descentKm;
  const IF = 0.74;
  const np = Math.round(ATHLETE.ftp * IF);
  const climbW = Math.round(ATHLETE.ftp * 0.84);
  const flatW = Math.round(ATHLETE.ftp * 0.7);
  const mass = weight + 9.5;
  const climbSpeed = steadySpeedKmh(climbW, mass, 5, "gravel");
  const flatSpeed = steadySpeedKmh(flatW, mass, 0, "gravel", 0.85);
  const descentSpeed = 33;
  const hours = climbKm / climbSpeed + flatKm / flatSpeed + descentKm / descentSpeed;
  // Taper: the planned weeks up to race week, then ~50% volume.
  const plan = [1, 2, 3, 4].map((w) => {
    const pw = planWeek(w);
    const mid = pw?.target_min && pw.target_max ? (pw.target_min + pw.target_max) / 2 : 300;
    return Math.round(w === 4 ? mid * 0.5 : mid);
  });
  const projection = projectTrainingLoad(plan.slice(0, Math.ceil(days / 7) || 1), 5, RACE.date);
  const tsb = projection.onDate?.tsb ?? projection.end.tsb;
  return {
    days, hours, np, climbW, flatW, IF, weight, plan, tsb,
    carbs: hours > 2.5 ? 80 : 60,
    fluid: 750,
    sodium: 900,
    caffeinePre: Math.round(3 * weight),
    caffeineMax: Math.round(6 * weight),
    breakfast: [Math.round(2 * weight), Math.round(3 * weight)],
    hrCap: Math.round(ATHLETE.lthr * 0.93),
    climbHrCap: Math.round(ATHLETE.lthr * 0.98),
  };
}

export const raceWeather = () => {
  const d = new Date(RACE.date + "T00:00:00");
  return `Typical for ${d.toLocaleDateString("en", { day: "numeric", month: "short" })} (5-year climatology, not a forecast yet): 11-21 °C, rain on 20% of days, wind gusts typically up to 28 km/h.`;
};
