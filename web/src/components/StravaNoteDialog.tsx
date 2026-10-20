import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ExternalLink, Loader2, NotebookPen, RefreshCw, Send, X } from "lucide-react";
import { api } from "../api";
import type { Activity, StravaNoteStatus } from "../types";
import { weekday } from "../lib/format";

const STRAVA_ORANGE = "#fc4c02";

/** Preview-then-post editor for the coach note in an activity's Strava description. */
export function StravaNoteDialog({ activity, onClose, onPosted }: {
  activity: Activity;
  onClose: () => void;
  onPosted: () => void;
}) {
  const [status, setStatus] = useState<StravaNoteStatus | null>(null);
  const [context, setContext] = useState("");
  const [preview, setPreview] = useState<{ id: string; note: string } | null>(null);
  const [busy, setBusy] = useState<"preview" | "post" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [posted, setPosted] = useState<string | null>(null);

  useEffect(() => {
    api.stravaNote(activity.id)
      .then((s) => { setStatus(s); setContext(s.context ?? ""); })
      .catch((e) => setError((e as Error).message));
  }, [activity.id]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  async function makePreview() {
    setBusy("preview");
    setError(null);
    try {
      const p = await api.stravaNotePreview(activity.id, context);
      setPreview({ id: p.preview_id, note: p.note });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function post() {
    if (!preview) return;
    setBusy("post");
    setError(null);
    try {
      const r = await api.stravaNotePost(activity.id, preview.id);
      setPosted(r.strava_url);
      onPosted();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const stravaLink = posted ?? status?.strava_url ?? activity.strava_url;

  // Portal: the Card it opens from has backdrop-blur, which would trap a fixed overlay inside the card.
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 backdrop-blur-sm sm:items-center sm:p-4"
      onClick={() => !busy && onClose()}>
      <div onClick={(e) => e.stopPropagation()}
        className="flex max-h-[92vh] w-full flex-col overflow-hidden rounded-t-2xl border border-white/10 bg-ink-900 shadow-2xl sm:max-w-lg sm:rounded-2xl">
        <header className="flex items-start justify-between gap-3 border-b border-white/[0.06] px-4 py-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-sm font-semibold text-white">
              <NotebookPen size={15} style={{ color: STRAVA_ORANGE }} /> Strava coach note
            </h2>
            <p className="mt-0.5 truncate text-xs text-slate-400">{activity.name} · {weekday(activity.start)}</p>
          </div>
          <button onClick={onClose} disabled={!!busy} className="rounded-lg p-1.5 text-slate-400 hover:bg-white/5 hover:text-white disabled:opacity-40">
            <X size={16} />
          </button>
        </header>

        <div className="space-y-3 overflow-y-auto px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
          <div className="flex items-center justify-between gap-2 text-xs text-slate-400">
            <span>
              {posted ? <span className="text-emerald-300">Posted just now</span>
                : status?.posted_at ? `Note posted ${weekday(status.posted_at)}`
                : status ? "No coach note on Strava yet" : "Loading..."}
            </span>
            {stravaLink && (
              <a href={stravaLink} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:text-white" style={{ color: STRAVA_ORANGE }}>
                Open on Strava <ExternalLink size={11} />
              </a>
            )}
          </div>

          <label className="block">
            <span className="text-[11px] font-medium uppercase tracking-wider text-slate-500">Extra context (optional)</span>
            <textarea value={context} onChange={(e) => setContext(e.target.value)} rows={3} disabled={!!busy || !!posted}
              placeholder="e.g. rode with friends, strong headwind, legs heavy from yesterday's gym"
              className="mt-1 w-full resize-none rounded-xl border border-white/10 bg-ink-800/80 px-3 py-2 text-sm text-white placeholder:text-slate-500 focus:border-sky-500/40 focus:outline-none" />
            <span className="text-[11px] text-slate-500">Shapes the coach's sentences. It's public via the note - health details are kept out.</span>
          </label>

          {preview && (
            <div>
              <span className="text-[11px] font-medium uppercase tracking-wider text-slate-500">Preview</span>
              <pre className="mt-1 whitespace-pre-wrap rounded-xl border border-white/[0.06] bg-white/[0.03] px-3 py-2 font-sans text-[13px] leading-relaxed text-slate-200">{preview.note}</pre>
            </div>
          )}

          {error && <p className="rounded-lg bg-red-500/10 px-3 py-2 text-xs text-red-300">{error}</p>}

          <div className="flex flex-wrap justify-end gap-2 pt-1">
            {posted ? (
              <button onClick={onClose} className="inline-flex items-center gap-1.5 rounded-xl bg-emerald-500/15 px-3.5 py-2 text-sm font-medium text-emerald-300">
                <Check size={15} /> Done
              </button>
            ) : (
              <>
                <button onClick={makePreview} disabled={!!busy || !status}
                  className="inline-flex items-center gap-1.5 rounded-xl bg-white/[0.06] px-3.5 py-2 text-sm font-medium text-slate-200 transition hover:bg-white/10 disabled:opacity-40">
                  {busy === "preview" ? <Loader2 size={15} className="animate-spin" /> : <RefreshCw size={15} />}
                  {preview ? "Rewrite" : "Preview"}
                </button>
                <button onClick={post} disabled={!!busy || !preview}
                  className="inline-flex items-center gap-1.5 rounded-xl px-3.5 py-2 text-sm font-semibold text-white shadow-lg transition hover:brightness-110 disabled:opacity-30"
                  style={{ background: STRAVA_ORANGE }}>
                  {busy === "post" ? <Loader2 size={15} className="animate-spin" /> : <Send size={15} />}
                  Post to Strava
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
