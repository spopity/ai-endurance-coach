/**
 * Demo chat: emits the same event stream as core/chat.py's stream_reply (supervisor routing,
 * model and tool status, final answer, optional next-session proposal). Answers are built
 * from the synthetic data with the same calculators and output formats the real agents use;
 * there is no language model behind them, so free-form questions outside these topics get
 * an honest "this is a demo" reply.
 */
import type { ChatEvent } from "../api";
import { ATHLETE, DATA, RACE, TODAY, addDays, hrvStatus, injuryReport, iso, latest, planWeek, readiness, type DemoActivity } from "./data";
import { estimateRideTime, formatHms, projectTrainingLoad, racePlan, raceWeather, avgWeeklyLoad4w } from "./calc";
import { createProposal, currentDecision } from "./endpoints";

type Agent = "ai_coach" | "athlete_twin" | "racing_strategy" | "injury_prevention" | "history_analyst";
const NAMES: Record<Agent, string> = {
  ai_coach: "AI Coach", athlete_twin: "Athlete Twin", racing_strategy: "Racing Strategy Agent",
  injury_prevention: "Injury Prevention Agent", history_analyst: "History Analyst",
};

interface Reply {
  content: string;
  tools?: string[];
  proposal?: { session: string; why: string | null };
  model?: "local" | "gemini" | "none";
}

const f1 = (v: number) => v.toFixed(1);
const signed = (v: number, d = 1) => `${v >= 0 ? "+" : ""}${v.toFixed(d)}`;

// --- Routing (what the local supervisor decides in graph.py) --------------------------------

function route(q: string): Agent {
  if (/\b(update|change|swap|save|replace|move)\b.*\b(plan|session|ride|workout|run|training)\b|\binstead\b/.test(q)) return "ai_coach";
  if (/\b(last (year|summer|winter|spring|autumn|month|season)|history|trend|compare|compared|ever|record|best|longest|built my fitness|boost|past races|in 20\d\d|this year|same time)\b/.test(q)) return "history_analyst";
  if (/\b(race|racing|pacing|fuel|fuelling|fueling|taper|gravel classic|race plan)\b/.test(q)) return "racing_strategy";
  if (/\b(ctl|tss\/week|tss per week|project|projection|what if|climb|alpe|ventoux|stelvio|tourmalet|galibier|how long would|how fast|kg lighter|lost \d|lose \d)\b|\d+ ?km at \d/.test(q)) return "athlete_twin";
  if (/\b(injur|overtrain|pain|hurt|sore|risk|recovered|recovery|hrv|sleep|tired|fatigue)/.test(q)) return "injury_prevention";
  return "ai_coach";
}

// --- AI Coach -----------------------------------------------------------------------------

function weekContext() {
  const pw = planWeek(0)!;
  const now = latest();
  return { pw, now, tsb: now.ctl - now.atl, hrv: hrvStatus(), ready: readiness() };
}

function nextSessionAnswer(): Reply {
  const d = currentDecision()!;
  const { pw, tsb, hrv, ready } = weekContext();
  return {
    tools: ["check_injury_risk"],
    content: [
      `**Your next session: ${d.session}**`,
      "",
      `Why this one: ${d.why}`,
      "",
      `- **Readiness ${ready.level}** - HRV 7-day average ${hrv.avg7.toFixed(0)} ms vs your ${hrv.baseline.toFixed(0)} ms baseline; form (TSB) ${signed(tsb)}.`,
      `- **This week** is ${pw.phase} (${pw.key === "build_2" ? "threshold progression" : "per plan"}), target ${pw.target_min}-${pw.target_max} TSS. Easy volume now protects the quality later in the week.`,
      "- The injury check flags nothing high-risk, so this is a fatigue call, not a red flag.",
      "",
      "**How to ride it:** keep it genuinely conversational - if HR drifts above ~145 bpm on the flats, back off. Fuel 40-60 g carbs/hour, and spin the cadence up rather than pushing gears.",
      "",
      "If your HRV bounces back toward baseline in the next day or two, the planned sweet spot stays on the calendar. If it doesn't, we trade it for more Zone 2 - the plan bends, it doesn't break.",
    ].join("\n"),
  };
}

function explainSessionAnswer(): Reply {
  const d = currentDecision()!;
  return {
    tools: ["check_injury_risk"],
    content: [
      `**${d.session}** - here's how to nail it:`,
      "",
      "1. **Warm-up (15 min):** start easy, build to the bottom of the target range, add 3 x 30 s at high cadence.",
      "2. **Main set:** stay inside the target - the effort should feel *comfortably controlled*, never a fight. Heart rate creeping up while power stays flat is fine early in the session; if it keeps climbing, cut a block.",
      "3. **Cool-down (10 min):** easy spin, then 20-30 g carbs and some protein within the hour.",
      "",
      `**Why this one:** ${d.why}`,
      "",
      "It's the session that moves the plan forward without spending energy you'll want later this week. Boring? Maybe. Effective? Very.",
    ].join("\n"),
  };
}

function findActivity(q: string): DemoActivity {
  const id = q.match(/\bi9\d{7}\b/)?.[0];
  const byId = id ? DATA.activities.find((a) => a.id === id) : undefined;
  if (byId) return byId;
  if (/\brun\b/.test(q)) return DATA.activities.find((a) => a.type === "Run")!;
  return DATA.activities.find((a) => a.group === "Bike")!;
}

function analysisAnswer(q: string): Reply {
  const a = findActivity(q);
  const focus: Record<string, [string, string]> = {
    endurance: ["Aerobic endurance (Zone 2)", "builds mitochondrial density and fat oxidation - the base every other session stands on"],
    sst: ["Sweet spot / threshold", "raises the power you can hold for an hour by stressing lactate clearance just under FTP"],
    vo2: ["VO2max", "pushes the aerobic ceiling up, which lifts everything underneath it"],
    run: ["Aerobic run", "adds durability and bone loading the bike can't give, at a low cost"],
    weights: ["Strength", "builds the force reserve that makes long climbs and sprints cheaper"],
    walk: ["Active recovery", "keeps you moving without adding real load"],
    race: ["Race effort", "the real test - specific, maximal, and the best data point of the season"],
  };
  const [label, why] = focus[a.kind] ?? focus.endurance;
  const numbers = a.np
    ? `Load: ${a.load} | NP/Avg: ${a.np}/${a.avg_power} W | IF: ${a.if?.toFixed(2)}`
    : `Load: ${a.load} | Avg HR: ${a.avg_hr} bpm | IF: ${a.if?.toFixed(2)}`;
  const decoupling = a.decoupling !== null ? ` Heart rate drift was ${a.decoupling.toFixed(1)}% - ${a.decoupling <= 5 ? "your aerobic engine held steady to the end" : "a hint of fatigue or heat late in the ride"}.` : "";
  return {
    tools: ["get_activity_details"],
    content: [
      `**Summary:** ${a.name} on ${new Date(a.start).toLocaleDateString("en", { weekday: "long", day: "numeric", month: "short" })} - ${a.hours! >= 2 ? "a proper chunk of work" : "short, sharp and on purpose"}${a.kind === "sst" ? ", and the intervals were paced like a metronome" : ""}.`,
      "",
      `**Training Focus:** ${label}${a.interval ? ` (${a.interval.replace(/(\d)w\b/, "$1 W")})` : ""}.`,
      "",
      `**Analysis:** This ${why}.${decoupling}`,
      "",
      `**Numbers:** ${numbers}`,
      "",
      `**Suggestion:** ${currentDecision()!.session}.`,
    ].join("\n"),
  };
}

function proposalAnswer(q: string, localOnly: boolean): Reply {
  if (localOnly) {
    return { content: "I can't save a change in Local only mode - it has no tools. Switch Local only off and ask again, and I'll draft it for you to approve." };
  }
  const session = /\brun\b/.test(q)
    ? "Easy run, 45 min conversational (HR under 150 bpm), then 5 x 20 s strides"
    : /\b(rest|off|day off)\b/.test(q)
      ? "Rest day - optional 20-30 min walk or mobility"
      : /\b(long|longer|outside|outdoor|gravel)\b/.test(q)
        ? "Long endurance ride outdoors, 2h30 Z2 (175-200 W), fuel 60 g carbs/hour"
        : /\b(interval|sweet ?spot|hard|threshold)\b/.test(q)
          ? "Controlled sweet spot, 75 min: 3x10 min at 225-235 W, 5 min easy between"
          : "Endurance ride, 75 min Z2 (180-200 W), cadence drills in the middle";
  const why = "Agreed in chat - fits this week's plan and today's readiness.";
  return {
    tools: ["propose_next_session_change"],
    proposal: { session, why },
    content: `Good call - **${session}** works with this week's plan and your readiness. I've saved it as a proposal: nothing changes until you tap **Apply** below, then the dashboard, morning briefing and Strava notes all switch to it.`,
  };
}

function helpAnswer(): Reply {
  return {
    model: "none",
    content: [
      "👋 This is the **demo** of AI Coach - the data is synthetic and there's no language model behind this chat, so I answer the same kinds of questions the real coaching team handles. Try:",
      "",
      "- *What should I do for training tomorrow?* (AI Coach)",
      "- *Am I recovered enough for intervals?* (Injury & Recovery)",
      "- *Build my race plan for the Demo Gravel Classic* (Racing Strategy)",
      "- *Where will my CTL be after 6 weeks at 450 TSS/week?* (Athlete Twin)",
      "- *How long would Alpe d'Huez take me at 250 W, and 3 kg lighter?* (Athlete Twin)",
      "- *How did my HRV look last summer?* or *What built my fitness this year?* (History Analyst)",
      "- *Swap my next session for an easy run* - then Apply or Discard the proposal",
      "",
      "In the real system each question goes to Gemini or a local model with the athlete's full history.",
    ].join("\n"),
  };
}

// --- Injury & recovery (local model narrating the computed report) ------------------------

function injuryMarkdown(): string {
  const r = injuryReport();
  const verdict = { high: "🔴 HIGH RISK", caution: "🟠 CAUTION", ok: "🟢 LOOKING GOOD" }[r.overall];
  const lines = [`**Overall: ${verdict}**`];
  const sections = [["high", "🔴 High risk"], ["caution", "🟠 Caution"], ["ok", "🟢 Looks good"], ["info", "📈 Positive trends"]] as const;
  for (const [sev, heading] of sections) {
    const items = r.findings.filter((f) => f.severity === sev);
    if (!items.length) continue;
    lines.push(`\n**${heading}**`);
    for (const f of items) lines.push(sev === "high" || sev === "caution" ? `- **${f.area} - ${f.title}**: ${f.detail}` : `- ${f.area}: ${f.title}`);
  }
  if (r.data_gaps.length) lines.push("\n**Data gaps**", ...r.data_gaps.map((g) => `- ${g}`));
  return lines.join("\n");
}

function injuryAnswer(q: string): Reply {
  const r = injuryReport();
  const hrv = hrvStatus();
  const intervals = /interval|hard|vo2|intense|intensity/.test(q);
  const pain = /pain|hurt|sore/.test(q);
  const narration = [
    `Overall you're at **${r.overall}**: nothing high-risk, but your HRV is running below normal (${hrv.avg7.toFixed(0)} vs ${hrv.baseline.toFixed(0)} ms).`,
    intervals
      ? "Hard intervals can wait a day or two - do steady Zone 2 today and save the quality session for when HRV is back near baseline."
      : "Keep the next day or two easy and protect your sleep; load and form are otherwise in a productive range.",
    pain ? "If the pain is sharp, getting worse or still there after a few easy days, see a physio or doctor - I can flag risk, not diagnose it." : "",
  ].filter(Boolean).join(" ");
  return { model: "local", content: `${narration}\n\n${injuryMarkdown()}` };
}

// --- Athlete Twin -------------------------------------------------------------------------

const CLIMBS: [RegExp, string, number, number][] = [
  [/alpe/, "Alpe d'Huez", 13.8, 8.1],
  [/ventoux/, "Mont Ventoux (Bédoin)", 21.5, 7.5],
  [/stelvio/, "Stelvio (Prato)", 24.3, 7.4],
  [/tourmalet/, "Col du Tourmalet (Luz)", 18.8, 7.4],
  [/galibier/, "Col du Galibier (Valloire)", 18.1, 6.9],
];

function rideTimeAnswer(q: string): Reply {
  const climb = CLIMBS.find(([re]) => re.test(q));
  const seg = q.match(/(\d+(?:[.,]\d+)?)\s*km\s*(?:at|@)\s*(\d+(?:[.,]\d+)?)\s*%/);
  const name = climb?.[1] ?? (seg ? `a ${seg[1]} km climb at ${seg[2]}%` : "Alpe d'Huez");
  const distanceKm = climb?.[2] ?? (seg ? parseFloat(seg[1].replace(",", ".")) : 13.8);
  const gradientPct = climb?.[3] ?? (seg ? parseFloat(seg[2].replace(",", ".")) : 8.1);
  const watts = q.match(/(\d{2,3})\s*(?:w\b|watts)/);
  const pct = q.match(/(\d{2,3})\s*%\s*(?:of\s*)?ftp/);
  const powerW = watts ? Number(watts[1]) : undefined;
  const pctFtp = pct ? Number(pct[1]) : undefined;
  const lighter = q.match(/(\d+(?:[.,]\d+)?)\s*kg\s*(?:lighter|less)|(?:lost|lose|drop)\s*(\d+(?:[.,]\d+)?)\s*kg/);
  const now = estimateRideTime({ distanceKm, gradientPct, powerW, pctFtp });
  const lines = [
    `**${name}** - ${distanceKm} km at ${gradientPct}% average, physics model (gravity + rolling resistance + drag):`,
    "",
    `- **At ${now.rider} kg, ${now.power.toFixed(0)} W** (${(now.power / now.rider).toFixed(2)} W/kg): **${formatHms(now.seconds)}** at ${now.speed.toFixed(1)} km/h.`,
  ];
  const tools = ["estimate_ride_time"];
  if (lighter) {
    const kg = parseFloat((lighter[1] ?? lighter[2]).replace(",", "."));
    const light = estimateRideTime({ distanceKm, gradientPct, powerW: now.power, riderKg: Math.round((now.rider - kg) * 10) / 10 });
    tools.push("estimate_ride_time");
    lines.push(`- **${kg} kg lighter** (${light.rider} kg, same watts, ${(light.power / light.rider).toFixed(2)} W/kg): **${formatHms(light.seconds)}** - ${formatHms(now.seconds - light.seconds)} faster.`);
  }
  lines.push(
    "",
    `For scale: the pros go up in under 40 minutes, so there's still a gap - but ${now.power >= ATHLETE.ftp * 0.9 ? "holding that power for this long is a serious effort, pace the first 3 km conservatively" : "that power is sustainable for you, and the time is realistic with good pacing"}.`,
    "",
    "_Assumptions: steady power, no wind, 8.5 kg bike, hoods position. Real times vary with pacing, wind and gradient changes._",
  );
  return { tools, content: lines.join("\n") };
}

function projectionAnswer(q: string): Reply {
  const weeksM = q.match(/(\d+)\s*weeks?/);
  const tssM = q.match(/(\d{2,4})\s*tss/);
  const restM = q.match(/rest\s*(\d+)\s*days?|(\d+)\s*days?\s*(?:off|rest)/);
  let plan: number[];
  let scenario: string;
  if (restM) {
    const days = Number(restM[1] ?? restM[2]);
    plan = [Math.round(avgWeeklyLoad4w() * Math.max(0, 7 - days) / 7)];
    scenario = `${days} days off, then normal training for the rest of that week`;
  } else {
    const weeks = Math.min(16, Number(weeksM?.[1] ?? 4));
    const weekly = Number(tssM?.[1] ?? Math.round(avgWeeklyLoad4w()));
    plan = Array(weeks).fill(weekly);
    scenario = `${weeks} weeks at ${weekly} TSS/week`;
  }
  const p = projectTrainingLoad(plan);
  const start = latest();
  const change = p.end.ctl - start.ctl;
  const zone = p.end.tsb < -30 ? "deep in the overreaching zone - that block needs a recovery week in it" : p.end.tsb < -10 ? "productive fatigue - you'd want a recovery week right after" : p.end.tsb > 5 ? "fresh - race-ready territory" : "balanced";
  const weekLines = p.text.split("\n").filter((l) => l.startsWith("- Week"));
  return {
    tools: ["project_training_load"],
    content: [
      `**Simulation: ${scenario}** (spread over 5 training days a week, same 42/7-day model Intervals.icu uses).`,
      "",
      `- Today: CTL **${f1(start.ctl)}**, ATL ${f1(start.atl)}, TSB ${signed(start.ctl - start.atl)}`,
      `- End: CTL **${f1(p.end.ctl)}** (${signed(change)}), ATL ${f1(p.end.atl)}, TSB **${signed(p.end.tsb)}**`,
      `- Your actual average over the last 4 weeks: ${avgWeeklyLoad4w()} TSS/week`,
      "",
      `That ends ${zone}.${change > 0 ? ` Fitness grows by about ${f1(change / plan.length)} CTL per week.` : " Fitness drops - CTL decays slowly, so a short break costs less than it feels like."}`,
      "",
      "**Week by week**",
      ...(weekLines.length > 8 ? [...weekLines.slice(0, 3), "- ...", ...weekLines.slice(-3)] : weekLines),
      "",
      "_Assumptions: the load is completed as planned; real CTL also depends on how you hit the TSS (intensity vs volume)._",
    ].join("\n"),
  };
}

// --- Racing Strategy -------------------------------------------------------------------

function raceAnswer(): Reply {
  const p = racePlan();
  const date = new Date(RACE.date + "T00:00:00").toLocaleDateString("en", { weekday: "long", day: "numeric", month: "long" });
  return {
    tools: ["list_upcoming_races", "build_race_plan"],
    content: [
      `**Race summary:** ${RACE.name} (${RACE.priority} race), ${date} - in ${p.days} days. ${RACE.distance_km} km, ${RACE.elevation_m} m of climbing. Predicted time **${formatHms(p.hours * 3600).replace(/ \d+s$/, "")}** - your goal of "${RACE.goal}" is realistic.`,
      "",
      "**Pacing plan**",
      `- **Start (first 20 min):** don't exceed ~${Math.round(ATHLETE.ftp * 0.95)} W apart from the start surge - early over-pacing is the #1 cause of fading.`,
      `- **Climbs:** ${p.climbW - 10}-${p.climbW + 10} W (≈${Math.round((p.climbW / ATHLETE.ftp) * 100)}% FTP), HR under ${p.climbHrCap} bpm. Short kickers can go higher, sit down on the long ones.`,
      `- **Flats and rolling gravel:** ${p.flatW - 10}-${p.flatW + 10} W, sit in wheels wherever possible.`,
      "- **Descents:** recover and use them to eat and drink.",
      `- **Final 25%:** if HR is under ${p.hrCap} bpm and the legs feel good, lift the power 5-10%.`,
      `- Whole race: target NP ≈ **${p.np} W** (IF ${p.IF.toFixed(2)}).`,
      "",
      "**Fuelling & hydration**",
      `- Day before: 8-10 g carbs/kg. Race breakfast ${p.breakfast[0]}-${p.breakfast[1]} g carbs, 3 h before the start.`,
      `- During: **${p.carbs} g carbs/hour** from the first 20-30 min, little and often.`,
      `- Fluids: ~${p.fluid} ml/hour with ~${p.sodium} mg sodium/hour (heavy sweater) - plan bottle refills.`,
      `- Caffeine: ~${p.caffeinePre} mg (3 mg/kg) 45-60 min before the start, another dose after ~2/3 of the race; stay under ${p.caffeineMax} mg in total.`,
      "",
      "**Taper & race week**",
      `- The next weeks build as planned (${p.plan.slice(0, 3).join(", ")} TSS), then race week drops to ~${p.plan[3]} TSS (about 50% volume, keep short intensity).`,
      `- Projected race-morning form: **TSB ${signed(p.tsb)}** (target +5 to +15 for an A race).`,
      "- D-3: 60 min with 3x5 min at race pace. D-1: openers - 45 min with 3x1 min at race pace + 2 short sprints.",
      "",
      "**Conditions**",
      `- ${raceWeather()} Mild temperatures - no heat adjustment to the pacing needed.`,
      "",
      "**Key risks**",
      "- Going with the front group on the first climb - let them go, you'll catch some on the gravel.",
      "- Under-fuelling in the second half: set a 20-minute eating timer.",
      "- Punctures on the gravel sectors: tubeless, plugs and a mini-pump within reach.",
    ].join("\n"),
  };
}

// --- History Analyst ----------------------------------------------------------------------

function seasonRange(q: string): [string, string, string] {
  const y = TODAY.getFullYear();
  const seasons: [RegExp, number, number][] = [[/summer/, 5, 7], [/winter/, 11, 1], [/spring/, 2, 4], [/autumn|fall/, 8, 10]];
  for (const [re, from, to] of seasons) {
    if (re.test(q)) {
      const startYear = new Date(y, from, 1) > TODAY ? y - 1 : y;
      const start = new Date(startYear, from, 1);
      const end = new Date(to < from ? startYear + 1 : startYear, to + 1, 0);
      return [iso(start), iso(end < TODAY ? end : addDays(TODAY, -1)), q.match(re)![0]];
    }
  }
  return [iso(addDays(TODAY, -90)), iso(addDays(TODAY, -1)), "the last 90 days"];
}

function stats(vals: number[]) {
  const mean = vals.reduce((s, v) => s + v, 0) / vals.length;
  return { mean, min: Math.min(...vals), max: Math.max(...vals) };
}

function historyAnswer(q: string, localOnly: boolean): Reply {
  if (localOnly) {
    return { model: "none", content: "History reports need the cloud model - it writes database queries over your full history, which the local model can't do reliably. Turn off 'Local only' and ask again." };
  }
  if (/built|boost|fitness gain|ctl gain|what worked/.test(q)) {
    // Biggest 28-day CTL rise in the data.
    let best = { i: 28, gain: -Infinity };
    for (let i = 28; i < DATA.days.length; i++) {
      const gain = DATA.days[i].ctl - DATA.days[i - 28].ctl;
      if (gain > best.gain) best = { i, gain };
    }
    const from = DATA.days[best.i - 28];
    const to = DATA.days[best.i];
    const acts = DATA.activities.filter((a) => a.day > from.day && a.day <= to.day);
    const hours = acts.reduce((s, a) => s + (a.hours ?? 0), 0);
    const kinds = (k: string) => acts.filter((a) => a.kind === k).length;
    return {
      tools: ["fitness_gain_windows", "training_summary"],
      content: [
        `Your biggest fitness gain came between **${from.day} and ${to.day}**: CTL ${f1(from.ctl)} → ${f1(to.ctl)} (**${signed(best.gain)}** in 4 weeks).`,
        "",
        `- **Volume:** ${hours.toFixed(1)} h in 4 weeks (${(hours / 4).toFixed(1)} h/week), ${acts.reduce((s, a) => s + (a.load ?? 0), 0)} TSS.`,
        `- **Mix:** ${kinds("endurance")} endurance rides, ${kinds("sst")} sweet spot/threshold sessions, ${kinds("vo2")} VO2max, ${kinds("run")} runs, ${kinds("weights")} gym sessions.`,
        `- **Long rides:** ${acts.filter((a) => (a.hours ?? 0) >= 3).length} rides over 3 h - the steady volume did most of the lifting.`,
        "",
        "What it means now: consistency beat intensity - the gain coincided with steady weekly volume plus 1-2 quality sessions, not with extra hard days.",
        "",
        "_Period: rolling 28-day windows over the full history; correlation, not causation._",
      ].join("\n"),
    };
  }
  if (/race|record|best|longest|ever/.test(q)) {
    const races = DATA.activities.filter((a) => a.race);
    const longest = [...DATA.activities].filter((a) => a.group === "Bike").sort((a, b) => (b.distance_km ?? 0) - (a.distance_km ?? 0))[0];
    return {
      tools: ["race_history", "query_database"],
      content: [
        `You've raced **${races.length} times** in the last 14 months; the longest ride on record is **${longest.name}** (${longest.distance_km} km, ${longest.day}).`,
        "",
        ...races.map((r) => `- **${r.day} ${r.name}** - ${r.distance_km} km, ${r.elevation_m} m, ${formatHms((r.hours ?? 0) * 3600).replace(/ \d+s$/, "")}, NP ${r.np} W (IF ${r.if?.toFixed(2)})`),
        "",
        `What it means now: your best-paced race was the one with the lowest IF for its length - useful for the ${RACE.name}: start controlled.`,
        "",
        "_Period: all history; races = Intervals.icu race flag or race-like names._",
      ].join("\n"),
    };
  }
  const [start, end, label] = seasonRange(q);
  const rows = DATA.days.filter((d) => d.day >= start && d.day <= end);
  const metric = /rest(ing)? ?hr|heart rate/.test(q) ? "resting_hr" : /sleep/.test(q) ? "sleep_h" : "hrv";
  const unit = { hrv: "ms", resting_hr: "bpm", sleep_h: "h" }[metric];
  const title = { hrv: "HRV", resting_hr: "Resting HR", sleep_h: "Sleep" }[metric];
  const vals = rows.map((r) => r[metric]).filter((v): v is number => v !== null);
  if (!vals.length) {
    const first = DATA.days.find((d) => d.hrv !== null)!.day;
    return {
      tools: ["wellness_summary"],
      content: `HRV is only recorded since **${first}**, so ${label} has no HRV data - I won't present missing data as a flat trend. Resting HR does cover that period: ask me for that instead, or for the same season after ${first}.\n\n_Period: ${start} to ${end}; source: daily wellness from Intervals.icu._`,
    };
  }
  const s = stats(vals);
  const all = DATA.days.slice(-60).map((r) => r[metric]).filter((v): v is number => v !== null);
  const recent = stats(all);
  const fmt = (v: number) => (metric === "sleep_h" ? v.toFixed(1) : v.toFixed(0));
  return {
    tools: ["wellness_summary"],
    content: [
      `${title} during ${label} averaged **${fmt(s.mean)} ${unit}** (range ${fmt(s.min)}-${fmt(s.max)}), versus **${fmt(recent.mean)} ${unit}** over the last 60 days.`,
      "",
      `- **Period:** ${start} to ${end}, ${vals.length} days with data.`,
      `- **Difference:** ${signed(recent.mean - s.mean, metric === "sleep_h" ? 1 : 0)} ${unit} now vs then.`,
      metric === "hrv" ? "- **Shape:** dips line up with the hardest training weeks and recover within 2-3 easy days - a healthy response." : "- **Shape:** stable, with short spikes after the heaviest weeks.",
      "",
      metric === "hrv" && recent.mean < s.mean ? "What it means now: you're slightly below that level - consistent with the current build block. Nothing alarming, but sleep is the cheapest lever." : "What it means now: you're in a similar place - no change to the plan needed.",
      "",
      "_Period and source: daily wellness from Intervals.icu; calculations done locally, not by the model._",
    ].join("\n"),
  };
}

// --- Orchestration --------------------------------------------------------------------------

function answer(agent: Agent, q: string, localOnly: boolean): Reply {
  if (agent === "injury_prevention") return injuryAnswer(q);
  if (agent === "history_analyst") return historyAnswer(q, localOnly);
  if (agent === "racing_strategy") return raceAnswer();
  if (agent === "athlete_twin") {
    return /climb|alpe|ventoux|stelvio|tourmalet|galibier|how long would|how fast|km at|kg/.test(q) && !/ctl|tss/.test(q) ? rideTimeAnswer(q) : projectionAnswer(q);
  }
  if (/\b(update|change|swap|save|replace|move)\b|\binstead\b/.test(q)) return proposalAnswer(q, localOnly);
  if (/^my next session is/.test(q)) return explainSessionAnswer();
  if (/analy[sz]e|last ride|last run|how did my|yesterday/.test(q)) return analysisAnswer(q);
  if (/\b(train|training|workout|session|ride|today|tomorrow|next|should i)\b/.test(q)) return nextSessionAnswer();
  return helpAnswer();
}

const wait = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => { clearTimeout(t); reject(new DOMException("Aborted", "AbortError")); });
  });

/** Yields the event stream for one chat turn, timed like the real system. */
export async function* chatEvents(message: string, forced: string | null, localOnly: boolean, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
  const q = message.toLowerCase().trim();
  const agent = (forced as Agent | null) ?? route(q);
  if (!forced) {
    yield { type: "status", content: "Engaging Supervisor..." };
    yield { type: "status", content: "Engaging local model (Ollama)..." };
    await wait(900 + Math.random() * 500, signal);
    yield { type: "route", agent, content: `Supervisor routed to: ${NAMES[agent]}` };
  }
  const reply = answer(agent, q, localOnly);
  yield { type: "status", content: `Engaging ${NAMES[agent]}...` };
  const cloud = reply.model !== "local" && reply.model !== "none" && !localOnly && agent !== "injury_prevention";
  if (reply.model !== "none") {
    yield { type: "status", content: cloud ? "Engaging cloud model (Gemini)..." : "Engaging local model (Ollama)..." };
    await wait(cloud ? 1400 : 900, signal);
    if (cloud) {
      for (const tool of reply.tools ?? []) {
        yield { type: "status", content: `Running tool: ${tool}` };
        await wait(500 + Math.random() * 400, signal);
      }
      yield { type: "status", content: "Engaging cloud model (Gemini)..." };
    }
    await wait(cloud ? 1600 + Math.random() * 1200 : 1000, signal);
  }
  const model = reply.model ?? (cloud ? "gemini" : "local");
  yield { type: "final", agent, model, content: reply.content };
  if (reply.proposal && !localOnly) {
    yield { type: "proposal", proposal: createProposal(reply.proposal.session, reply.proposal.why) };
  }
  yield { type: "end" };
}
