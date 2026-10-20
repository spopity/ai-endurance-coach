import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Bot, Check, Cloud, Cpu, Loader2, MessageSquarePlus, SendHorizontal, Sparkles, Square, User, X } from "lucide-react";
import { api, streamChat } from "../api";
import type { ChatMessage, SessionProposal } from "../types";
import { AGENT_META } from "../lib/colors";

const STORAGE_KEY = "ai-coach-chat-v1";
const LOCAL_ONLY_KEY = "ai-coach-local-only";
// Matches the server's history window (core/history.py) - older turns are kept for display only.
const HISTORY_SENT = 6;

const AGENT_OPTIONS = [
  { value: "", label: "Auto (supervisor)" },
  { value: "ai_coach", label: "AI Coach" },
  { value: "athlete_twin", label: "Athlete Twin" },
  { value: "racing_strategy", label: "Racing Strategy" },
  { value: "injury_prevention", label: "Injury & Recovery" },
];

const SUGGESTIONS = [
  "What should I do for training tomorrow?",
  "Am I recovered enough for intervals?",
  "Where will my CTL be after 4 weeks at 350 TSS/week?",
  "How did my last ride go?",
];

const MODEL_BADGE = {
  local: { label: "Local", icon: Cpu, className: "bg-emerald-500/10 text-emerald-300" },
  gemini: { label: "Gemini", icon: Cloud, className: "bg-sky-500/10 text-sky-300" },
} as const;

function loadLocalOnly(): boolean {
  try {
    return localStorage.getItem(LOCAL_ONLY_KEY) === "1";
  } catch {
    return false;
  }
}

function load(): ChatMessage[] {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "[]");
  } catch {
    return [];
  }
}

export function ChatPanel({
  ask,
  mobile = false,
  onAnswer,
  onPlanChanged,
}: {
  ask: { text: string; id: number } | null;
  mobile?: boolean;
  /** Called when an answer arrives (the phone layout shows an unread dot). */
  onAnswer?: () => void;
  /** Called after a proposed next-session change is applied (the dashboard reloads it). */
  onPlanChanged?: () => void;
}) {
  const [messages, setMessages] = useState<ChatMessage[]>(load);
  const [input, setInput] = useState("");
  const [agent, setAgent] = useState("");
  const [localOnly, setLocalOnly] = useState(loadLocalOnly);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const lastAsk = useRef<number | null>(null);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(messages.slice(-50)));
    } catch {
      /* storage unavailable - chat still works for this session */
    }
  }, [messages]);

  useEffect(() => {
    try {
      localStorage.setItem(LOCAL_ONLY_KEY, localOnly ? "1" : "0");
    } catch {
      /* storage unavailable - setting lasts for this session */
    }
  }, [localOnly]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, status]);

  useEffect(() => {
    if (ask && ask.id !== lastAsk.current && !busy) {
      lastAsk.current = ask.id;
      void send(ask.text);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ask]);

  async function send(text: string) {
    const question = text.trim();
    if (!question || busy) return;
    const history = messages.slice(-HISTORY_SENT);
    setMessages((m) => [...m, { role: "user", content: question }]);
    setInput("");
    setBusy(true);
    setStatus(["Supervisor is routing your question..."]);
    const controller = new AbortController();
    abortRef.current = controller;
    let answered = false;
    try {
      await streamChat(question, history, agent || null, localOnly, (e) => {
        if (e.type === "status" || e.type === "route") {
          setStatus((s) => (s[s.length - 1] === e.content ? s : [...s.slice(-3), e.content]));
        } else if (e.type === "final") {
          answered = true;
          onAnswer?.();
          setMessages((m) => [...m, { role: "assistant", content: e.content, agent: e.agent, model: e.model, error: e.agent === "error" }]);
        } else if (e.type === "proposal") {
          // Attach to the answer that proposed it; the athlete applies or discards it there.
          setMessages((m) => m.map((msg, i) => (i === m.length - 1 && msg.role === "assistant" ? { ...msg, proposal: e.proposal } : msg)));
        }
      }, controller.signal);
      if (!answered) {
        setMessages((m) => [...m, { role: "assistant", content: "No answer came back - please try again.", agent: "error", error: true }]);
      }
    } catch (err) {
      const aborted = (err as Error).name === "AbortError";
      setMessages((m) => [...m, {
        role: "assistant", agent: "error", error: true,
        content: aborted ? "Stopped." : `Something went wrong: ${(err as Error).message}`,
      }]);
    } finally {
      setBusy(false);
      setStatus([]);
      abortRef.current = null;
    }
  }

  async function resolve(index: number, p: SessionProposal, action: "apply" | "discard") {
    const update = (patch: Partial<SessionProposal>) =>
      setMessages((m) => m.map((msg, i) => (i === index && msg.proposal ? { ...msg, proposal: { ...msg.proposal, ...patch } } : msg)));
    try {
      await api.resolveProposal(p.id, action);
      update({ status: action === "apply" ? "applied" : "discarded", note: undefined });
      if (action === "apply") onPlanChanged?.();
    } catch (err) {
      update({ status: "failed", note: (err as Error).message });
    }
  }

  return (
    <aside className={`flex h-full flex-col bg-ink-900/70 backdrop-blur-xl ${mobile ? "bg-ink-950" : "border-l border-white/[0.06]"}`}>
      <header className={`flex items-center justify-between gap-2 border-b border-white/[0.06] px-4 pb-3 ${mobile ? "pt-[max(0.75rem,env(safe-area-inset-top))]" : "pt-3"}`}>
        <div className="flex items-center gap-2">
          <div className="rounded-xl bg-gradient-to-br from-sky-500 to-violet-500 p-1.5 shadow-lg shadow-sky-900/40">
            <Sparkles size={16} className="text-white" />
          </div>
          <div>
            <h2 className="text-sm font-semibold text-white">Coach chat</h2>
            <p className="text-[11px] text-slate-500">Remembers the last {HISTORY_SENT / 2} exchanges</p>
          </div>
        </div>
        <button
          onClick={() => !busy && setMessages([])}
          title="New conversation"
          className="rounded-lg p-2 text-slate-400 transition hover:bg-white/5 hover:text-white disabled:opacity-40"
          disabled={busy || messages.length === 0}
        >
          <MessageSquarePlus size={17} />
        </button>
      </header>

      <div className="flex-1 space-y-4 overflow-y-auto px-4 py-4">
        {messages.length === 0 && (
          <div className="mt-6 text-center">
            <p className="text-sm text-slate-300">Ask your coaching team anything.</p>
            <p className="mt-1 text-xs text-slate-500">The supervisor picks the right specialist.</p>
            <div className="mt-5 space-y-2">
              {SUGGESTIONS.map((s) => (
                <button key={s} onClick={() => send(s)}
                  className="block w-full rounded-xl border border-white/[0.06] bg-white/[0.02] px-3 py-2 text-left text-xs text-slate-300 transition hover:border-sky-500/30 hover:bg-sky-500/5">
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m, i) =>
          m.role === "user" ? (
            <div key={i} className="flex justify-end gap-2">
              <div className="max-w-[85%] break-words rounded-2xl rounded-tr-sm bg-gradient-to-br from-sky-600 to-blue-700 px-3.5 py-2 text-[13.5px] text-white shadow-lg shadow-blue-950/40">
                {m.content}
              </div>
              <div className="mt-1 rounded-full bg-white/5 p-1 text-slate-400"><User size={13} /></div>
            </div>
          ) : (
            <div key={i} className="flex gap-2">
              <div className="mt-1 h-fit rounded-full p-1" style={{ background: `${AGENT_META[m.agent ?? "ai_coach"]?.color ?? "#64748b"}22`, color: AGENT_META[m.agent ?? "ai_coach"]?.color }}>
                <Bot size={13} />
              </div>
              <div className="min-w-0 max-w-[92%]">
                {m.agent && AGENT_META[m.agent] && (
                  <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold" style={{ color: AGENT_META[m.agent].color }}>
                    {AGENT_META[m.agent].label}
                    {m.model && m.model !== "none" && <ModelBadge model={m.model} />}
                  </div>
                )}
                <div className={`prose-chat rounded-2xl rounded-tl-sm border px-3.5 py-2 ${m.error ? "border-red-500/20 bg-red-500/5" : "border-white/[0.06] bg-white/[0.03]"}`}>
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
                </div>
                {m.proposal && <ProposalCard proposal={m.proposal} onResolve={(action) => resolve(i, m.proposal!, action)} />}
              </div>
            </div>
          ),
        )}

        {busy && (
          <div className="flex gap-2">
            <div className="mt-1 h-fit rounded-full bg-sky-500/15 p-1 text-sky-300"><Loader2 size={13} className="animate-spin" /></div>
            <div className="space-y-1 rounded-2xl rounded-tl-sm border border-white/[0.06] bg-white/[0.02] px-3.5 py-2">
              {status.map((s, i) => (
                <p key={i} className={`text-xs ${i === status.length - 1 ? "text-slate-200" : "text-slate-500"}`}>{s}</p>
              ))}
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <footer className="border-t border-white/[0.06] p-3">
        <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[11px] text-slate-500">
          <label className="flex items-center gap-2">
            Route to
            <select value={agent} onChange={(e) => setAgent(e.target.value)}
              className="rounded-md border border-white/10 bg-ink-800 px-1.5 py-0.5 text-[11px] text-slate-200 outline-none focus:border-sky-500/50">
              {AGENT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </label>
          <button
            type="button"
            role="switch"
            aria-checked={localOnly}
            onClick={() => setLocalOnly((v) => !v)}
            title={localOnly
              ? "Answers come from the local model on your GPU - free, but less thorough. Click to allow Gemini."
              : "Coach, Twin and Racing use Gemini. Click to answer everything with the local model."}
            className="flex items-center gap-1.5"
          >
            <span className={`relative h-4 w-7 rounded-full transition ${localOnly ? "bg-emerald-500/70" : "bg-white/10"}`}>
              <span className={`absolute top-0.5 h-3 w-3 rounded-full bg-white shadow transition-all ${localOnly ? "left-3.5" : "left-0.5"}`} />
            </span>
            <span className={localOnly ? "font-medium text-emerald-300" : ""}>Local only</span>
          </button>
        </div>
        <div className="flex items-end gap-2 rounded-2xl border border-white/10 bg-ink-800/80 p-2 focus-within:border-sky-500/40">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send(input);
              }
            }}
            rows={Math.min(5, Math.max(1, input.split("\n").length))}
            placeholder="Ask about training, recovery, races..."
            className={`max-h-32 flex-1 resize-none bg-transparent px-1.5 py-1 text-white placeholder:text-slate-500 focus:outline-none ${mobile ? "text-base" : "text-[13.5px]"}`}
          />
          {busy ? (
            <button onClick={() => abortRef.current?.abort()} title="Stop"
              className="rounded-xl bg-white/10 p-2 text-slate-200 transition hover:bg-white/15">
              <Square size={16} />
            </button>
          ) : (
            <button onClick={() => send(input)} disabled={!input.trim()} title="Send (Enter)"
              className="rounded-xl bg-gradient-to-br from-sky-500 to-violet-500 p-2 text-white shadow-lg shadow-sky-900/40 transition hover:brightness-110 disabled:opacity-30">
              <SendHorizontal size={16} />
            </button>
          )}
        </div>
      </footer>
    </aside>
  );
}

function ProposalCard({ proposal: p, onResolve }: { proposal: SessionProposal; onResolve: (action: "apply" | "discard") => Promise<void> }) {
  const [working, setWorking] = useState(false);
  const run = (action: "apply" | "discard") => {
    setWorking(true);
    void onResolve(action).finally(() => setWorking(false));
  };
  return (
    <div className="mt-2 rounded-xl border border-violet-500/25 bg-violet-500/[0.07] px-3 py-2.5">
      <p className="text-[11px] font-semibold uppercase tracking-wide text-violet-300">Change next session?</p>
      <p className="mt-1 text-[13px] font-medium text-white">{p.session}</p>
      {p.why && <p className="mt-0.5 text-xs text-slate-400">{p.why}</p>}
      {p.status === "applied" ? (
        <p className="mt-2 flex items-center gap-1 text-xs text-emerald-300"><Check size={13} /> Applied - dashboard, briefing and Strava use it now.</p>
      ) : p.status === "discarded" ? (
        <p className="mt-2 flex items-center gap-1 text-xs text-slate-400"><X size={13} /> Discarded - next session unchanged.</p>
      ) : (
        <>
          {p.note && <p className="mt-2 text-xs text-rose-400">{p.note}</p>}
          <div className="mt-2 flex gap-2">
            <button onClick={() => run("apply")} disabled={working}
              className="inline-flex items-center gap-1.5 rounded-lg bg-violet-500/20 px-2.5 py-1 text-xs font-medium text-violet-100 ring-1 ring-violet-500/30 transition hover:bg-violet-500/30 disabled:opacity-40">
              {working ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />} Apply
            </button>
            <button onClick={() => run("discard")} disabled={working}
              className="inline-flex items-center gap-1.5 rounded-lg bg-white/[0.05] px-2.5 py-1 text-xs font-medium text-slate-300 ring-1 ring-white/10 transition hover:bg-white/10 disabled:opacity-40">
              <X size={13} /> Discard
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function ModelBadge({ model }: { model: "local" | "gemini" }) {
  const { label, icon: Icon, className } = MODEL_BADGE[model];
  return (
    <span className={`inline-flex items-center gap-1 rounded px-1.5 py-px text-[10px] font-medium ${className}`}>
      <Icon size={10} />
      {label}
    </span>
  );
}
