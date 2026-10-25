/**
 * Demo build only (`npm run build:demo`): answers every /api call in the browser, so the
 * unchanged dashboard (api.ts and all components) runs as a static site with synthetic data.
 * Chat replies are sent as server-sent events, exactly what /api/chat streams in production.
 */
import { chatEvents } from "./chat";
import { HttpError, handle } from "./endpoints";

const realFetch = window.fetch.bind(window);
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, window.location.href);
  if (!url.pathname.includes("/api/")) return realFetch(input, init);
  const method = (init?.method ?? "GET").toUpperCase();
  const body = init?.body ? JSON.parse(String(init.body)) : {};

  if (url.pathname.endsWith("/api/chat")) {
    const encoder = new TextEncoder();
    const events = chatEvents(String(body.message ?? ""), body.agent ?? null, Boolean(body.local_only), init?.signal ?? undefined);
    const stream = new ReadableStream<Uint8Array>({
      async pull(controller) {
        try {
          const { value, done } = await events.next();
          if (done) controller.close();
          else controller.enqueue(encoder.encode(`data: ${JSON.stringify(value)}\n\n`));
        } catch (e) {
          controller.error(e);
        }
      },
    });
    return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
  }

  try {
    return json(await handle(method, url, body));
  } catch (e) {
    return json({ detail: (e as Error).message }, e instanceof HttpError ? e.status : 500);
  }
};

// A small, dismissible badge so nobody mistakes the demo for someone's real data.
const BACK_URL = import.meta.env.VITE_DEMO_BACK_URL || "https://popity.ro/projects/";
const REPO_URL = "https://github.com/spopity/ai-endurance-coach";

function badge() {
  const el = document.createElement("div");
  el.setAttribute("role", "note");
  el.style.cssText = [
    "position:fixed", "z-index:60", "left:12px", "bottom:calc(4.75rem + env(safe-area-inset-bottom))",
    "display:flex", "align-items:center", "gap:10px", "max-width:calc(100% - 24px)", "padding:7px 10px 7px 12px",
    "border-radius:12px", "background:rgba(15,23,42,0.92)", "border:1px solid rgba(167,139,250,0.35)",
    "box-shadow:0 10px 30px rgba(0,0,0,0.45)", "backdrop-filter:blur(8px)", "font:500 12px/1.35 Inter Variable,system-ui,sans-serif",
    "color:#cbd5e1",
  ].join(";");
  if (window.matchMedia("(min-width: 1024px)").matches) el.style.bottom = "16px";
  el.innerHTML = `
    <span style="padding:2px 7px;border-radius:7px;background:linear-gradient(90deg,#38bdf8,#a78bfa);color:#0b1020;font-weight:700;letter-spacing:.02em">DEMO</span>
    <span>Synthetic athlete, no real data - nothing leaves your browser.</span>
    <a href="${BACK_URL}" style="color:#7dd3fc;text-decoration:none;white-space:nowrap">About ↗</a>
    <a href="${REPO_URL}" target="_blank" rel="noopener" style="color:#7dd3fc;text-decoration:none;white-space:nowrap">Code ↗</a>
    <button aria-label="Hide" style="margin-left:2px;padding:0 4px;color:#64748b;background:none;border:0;font-size:16px;cursor:pointer">×</button>`;
  el.querySelector("button")!.addEventListener("click", () => el.remove());
  document.body.appendChild(el);
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", badge);
else badge();
