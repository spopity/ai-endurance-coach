import { useCallback, useEffect, useState } from "react";
import { LayoutDashboard, MessageCircle, PanelRightClose, PanelRightOpen, Radio, RefreshCw, Zap } from "lucide-react";
import { api } from "./api";
import type { Overview } from "./types";
import { timeAgo, weekday } from "./lib/format";
import { useIsMobile } from "./hooks/useMediaQuery";
import { KpiRow } from "./components/KpiRow";
import { PmcChart } from "./components/PmcChart";
import { WeeklyChart } from "./components/WeeklyChart";
import { WellnessChart } from "./components/WellnessChart";
import { InjuryCard } from "./components/InjuryCard";
import { RaceCard } from "./components/RaceCard";
import { ActivitiesTable } from "./components/ActivitiesTable";
import { PlanCard } from "./components/PlanCard";
import { NextSessionCard } from "./components/NextSessionCard";
import { ChatPanel } from "./components/ChatPanel";
import { SystemPage } from "./components/SystemPage";
import { Skeleton } from "./components/Card";

type MobileTab = "dashboard" | "chat";
type Page = "dashboard" | "system";

// The page lives in the URL hash (#system) so it survives reloads and can be bookmarked.
const pageFromHash = (): Page => (window.location.hash === "#system" ? "system" : "dashboard");

export default function App() {
  const isMobile = useIsMobile();
  const [overview, setOverview] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [syncing, setSyncing] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [chatOpen, setChatOpen] = useState(true);
  const [tab, setTab] = useState<MobileTab>("dashboard");
  const [ask, setAsk] = useState<{ text: string; id: number } | null>(null);
  const [unread, setUnread] = useState(false);
  const [page, setPageState] = useState<Page>(pageFromHash);

  useEffect(() => {
    const onHash = () => setPageState(pageFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const setPage = useCallback((p: Page) => {
    window.location.hash = p === "system" ? "system" : "";
    setPageState(p);
  }, []);

  useEffect(() => {
    api.overview().then((o) => { setOverview(o); setError(null); }).catch((e) => setError(String(e)));
  }, [refreshKey]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 6000);
    return () => clearTimeout(t);
  }, [toast]);

  useEffect(() => {
    if (tab === "chat") setUnread(false);
  }, [tab]);

  const askCoach = useCallback((text: string) => {
    setChatOpen(true);
    setTab("chat");
    setAsk({ text, id: Date.now() });
  }, []);

  const onAnswer = useCallback(() => {
    if (isMobile && tab !== "chat") setUnread(true);
  }, [isMobile, tab]);

  async function sync() {
    setSyncing(true);
    try {
      setToast(await api.sync());
      setRefreshKey((k) => k + 1);
    } catch (e) {
      setToast(`Sync failed: ${(e as Error).message}`);
    } finally {
      setSyncing(false);
    }
  }

  const showDashboard = !isMobile || tab === "dashboard";
  const showChat = isMobile ? tab === "chat" : chatOpen;
  const raceCard = overview ? <RaceCard race={overview.next_race} onAsk={askCoach} /> : <Skeleton className="h-[300px]" />;

  return (
    <div className="flex h-full">
      <div className={`min-w-0 flex-1 flex-col ${showDashboard ? "flex" : "hidden"}`}>
        <header className="sticky top-0 z-10 flex items-center justify-between gap-3 border-b border-white/[0.06] bg-ink-950/70 px-4 pb-3 pt-[max(0.75rem,env(safe-area-inset-top))] backdrop-blur-xl lg:px-6">
          <div className="flex items-center gap-3">
            <div className="rounded-xl bg-gradient-to-br from-sky-400 via-violet-500 to-pink-500 p-2 shadow-lg shadow-violet-900/40">
              <Zap size={18} className="text-white" fill="white" />
            </div>
            <div>
              <h1 className="text-[15px] font-semibold leading-tight text-white">AI Coach</h1>
              <p className="text-[11px] text-slate-500">
                {overview ? weekday(overview.today) : "Loading..."}
                {isMobile && overview && <> · synced {timeAgo(overview.last_sync)}</>}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2 lg:gap-3">
            <span className="hidden text-xs text-slate-500 lg:inline">Last sync {timeAgo(overview?.last_sync ?? null)}</span>
            {!isMobile && (
              <button onClick={() => setPage(page === "system" ? "dashboard" : "system")}
                title={page === "system" ? "Back to the dashboard" : "System activity: API calls and Gemini cost"}
                className={`flex items-center gap-2 rounded-xl px-3 py-1.5 text-xs font-medium ring-1 transition ${
                  page === "system"
                    ? "bg-violet-500/15 text-violet-200 ring-violet-400/30 hover:bg-violet-500/25"
                    : "bg-white/[0.06] text-slate-200 ring-white/10 hover:bg-white/10"}`}>
                {page === "system" ? <LayoutDashboard size={15} /> : <Radio size={15} />}
                {page === "system" ? "Dashboard" : "System"}
              </button>
            )}
            <button onClick={sync} disabled={syncing} title="Sync Intervals.icu"
              className="flex items-center gap-2 rounded-xl bg-white/[0.06] p-2 text-xs font-medium text-slate-200 ring-1 ring-white/10 transition hover:bg-white/10 disabled:opacity-50 lg:px-3 lg:py-1.5">
              <RefreshCw size={15} className={syncing ? "animate-spin" : ""} />
              <span className="hidden lg:inline">{syncing ? "Syncing..." : "Sync Intervals.icu"}</span>
            </button>
            {!isMobile && (
              <button onClick={() => setChatOpen((v) => !v)} title={chatOpen ? "Hide chat" : "Show chat"}
                className="rounded-xl p-2 text-slate-400 ring-1 ring-white/10 transition hover:bg-white/5 hover:text-white">
                {chatOpen ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}
              </button>
            )}
          </div>
        </header>

        <main className="flex-1 overflow-y-auto px-3 pb-[calc(5.5rem+env(safe-area-inset-bottom))] pt-3 lg:px-6 lg:py-5">
          {error && (
            <div className="mb-4 rounded-xl border border-red-500/20 bg-red-500/10 p-3 text-sm text-red-200">
              Could not load the dashboard: {error}
            </div>
          )}
          {page === "system" ? <SystemPage refreshKey={refreshKey} compact={isMobile} /> : (
          <div className="mx-auto max-w-[1600px] space-y-3 lg:space-y-4">
            {overview ? <KpiRow data={overview} /> : <Skeleton className="h-[116px]" />}

            <NextSessionCard refreshKey={refreshKey} onAsk={askCoach} />

            <div className="grid grid-cols-1 gap-3 lg:gap-4 xl:grid-cols-3">
              {!isMobile && <div className="xl:order-2 [&>section]:h-full">{raceCard}</div>}
              <div className="xl:order-1 xl:col-span-2"><PmcChart refreshKey={refreshKey} compact={isMobile} /></div>
            </div>

            <div className="grid grid-cols-1 gap-3 lg:gap-4 xl:grid-cols-3">
              {/* Phones: warnings first, plan after weekly load. Desktop keeps weekly | recovery | injury, plan below. */}
              <div className="xl:order-3 [&>section]:h-full"><InjuryCard refreshKey={refreshKey} onAsk={askCoach} /></div>
              <div className="xl:order-1 [&>section]:h-full"><WeeklyChart block={overview?.block ?? null} refreshKey={refreshKey} compact={isMobile} /></div>
              <div className="xl:order-4 xl:col-span-3"><PlanCard refreshKey={refreshKey} /></div>
              <div className="xl:order-2 [&>section]:h-full"><WellnessChart hrv={overview?.hrv ?? null} refreshKey={refreshKey} compact={isMobile} /></div>
            </div>

            {/* Phones: race countdown sits after recovery, just before activities. */}
            {isMobile && raceCard}

            <ActivitiesTable refreshKey={refreshKey} onAsk={askCoach} compact={isMobile} />
          </div>
          )}
        </main>
      </div>

      {/* Chat stays mounted on every layout so the conversation survives tab switches. */}
      <div
        className={
          isMobile
            ? `${showChat ? "flex" : "hidden"} fixed inset-x-0 top-0 bottom-[calc(4rem+env(safe-area-inset-bottom))] z-20 flex-col`
            : `${showChat ? "w-[420px]" : "w-0"} shrink-0 overflow-hidden transition-[width] duration-300`
        }
      >
        <div className={isMobile ? "h-full w-full" : "h-full w-[420px]"}>
          <ChatPanel ask={ask} mobile={isMobile} onAnswer={onAnswer} onPlanChanged={() => setRefreshKey((k) => k + 1)} />
        </div>
      </div>

      {isMobile && (
        <nav className="fixed inset-x-0 bottom-0 z-30 border-t border-white/[0.08] bg-ink-900/90 pb-[env(safe-area-inset-bottom)] backdrop-blur-xl">
          <div className="grid h-16 grid-cols-3">
            {([
              { id: "dashboard", label: "Dashboard", icon: <LayoutDashboard size={20} /> },
              { id: "system", label: "System", icon: <Radio size={20} /> },
              { id: "chat", label: "Coach", icon: <MessageCircle size={20} /> },
            ] as const).map((t) => {
              const active = t.id === "chat" ? tab === "chat" : tab !== "chat" && page === t.id;
              return (
              <button key={t.id} onClick={() => { if (t.id === "chat") setTab("chat"); else { setPage(t.id); setTab("dashboard"); } }}
                className={`relative flex flex-col items-center justify-center gap-1 text-[11px] font-medium transition ${
                  active ? "text-white" : "text-slate-500"}`}>
                {active && <span className="absolute top-0 h-0.5 w-12 rounded-full bg-gradient-to-r from-sky-400 to-violet-500" />}
                <span className={`relative ${active ? "text-sky-300" : ""}`}>
                  {t.icon}
                  {t.id === "chat" && unread && <span className="absolute -right-1 -top-0.5 h-2.5 w-2.5 rounded-full bg-pink-500 ring-2 ring-ink-900" />}
                </span>
                {t.label}
              </button>
              );
            })}
          </div>
        </nav>
      )}

      {toast && (
        <div className="fixed bottom-[calc(5rem+env(safe-area-inset-bottom))] left-1/2 z-50 w-[calc(100%-2rem)] max-w-md -translate-x-1/2 rounded-xl border border-white/10 bg-ink-800/95 px-4 py-2.5 text-center text-sm text-slate-200 shadow-2xl backdrop-blur lg:bottom-5 lg:w-auto">
          {toast}
        </div>
      )}
    </div>
  );
}
