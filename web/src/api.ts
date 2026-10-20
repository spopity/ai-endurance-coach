import type { Activity, ChatMessage, SessionProposal, FocusMode, InjuryReport, NextSession, Overview, PlanWeek, PmcPoint, StravaNoteStatus, TrainingFocus, Usage, WeekPoint, WellnessPoint } from "./types";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: ${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data as { detail?: string }).detail ?? `${res.status} ${res.statusText}`);
  return data as T;
}

export const api = {
  overview: () => get<Overview>("/api/overview"),
  pmc: (days: number) => get<PmcPoint[]>(`/api/pmc?days=${days}`),
  wellness: (days: number) => get<WellnessPoint[]>(`/api/wellness?days=${days}`),
  weekly: (weeks: number) => get<WeekPoint[]>(`/api/weekly?weeks=${weeks}`),
  plan: (weeks: number) => get<PlanWeek[]>(`/api/plan?weeks=${weeks}`),
  nextSession: () => get<NextSession>("/api/next-session"),
  decideNextSession: (redo: boolean) => post<NextSession>("/api/next-session", { redo }),
  resolveProposal: (id: number, action: "apply" | "discard") =>
    post<NextSession>(`/api/next-session/proposals/${id}/${action}`, {}),
  focus: () => get<TrainingFocus>("/api/focus"),
  setFocus: (mode: FocusMode) => post<TrainingFocus>("/api/focus", { mode }),
  activities: (limit: number) => get<Activity[]>(`/api/activities?limit=${limit}`),
  injury: () => get<InjuryReport>("/api/injury"),
  usage: (days: number) => get<Usage>(`/api/usage?days=${days}`),
  stravaNote: (id: string) => get<StravaNoteStatus>(`/api/strava-note/${id}`),
  stravaNotePreview: (id: string, context: string) =>
    post<{ preview_id: string; note: string }>(`/api/strava-note/${id}/preview`, { context }),
  stravaNotePost: (id: string, previewId: string) =>
    post<{ strava_url: string; posted_at: string }>(`/api/strava-note/${id}`, { preview_id: previewId }),
  sync: async (): Promise<string> => {
    const res = await fetch("/api/sync", { method: "POST" });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail ?? "Sync failed");
    return body.message as string;
  },
};

export type ChatEvent =
  | { type: "status"; content: string }
  | { type: "route"; agent: string; content: string }
  | { type: "final"; agent: string; model?: "local" | "gemini" | "none"; content: string }
  | { type: "proposal"; proposal: SessionProposal }
  | { type: "end" };

/** Streams one chat turn (server-sent events over a POST response). */
export async function streamChat(
  message: string,
  history: ChatMessage[],
  agent: string | null,
  localOnly: boolean,
  onEvent: (e: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      agent,
      local_only: localOnly,
      history: history.filter((m) => !m.error).map(({ role, content, agent }) => ({ role, content, agent })),
    }),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`Chat failed: ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const line = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (line) onEvent(JSON.parse(line.slice(6)) as ChatEvent);
    }
  }
}
