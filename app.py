import json
import logging

import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from logging_config import setup_logging
from core.chat import SYNC_WINDOW_DAYS, run_sync, stream_reply
from dashboard_api import router as dashboard_router

logger = logging.getLogger("AI_COACH_V1.app")
app = FastAPI(title="Athlete Multi-Agent Core Platform (v1)")
app.include_router(dashboard_router)

# The dashboard is a React app built into web/dist (cd web && npm run build).
# The original single-page chat UI stays available at /classic.
DASHBOARD_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "dist")

html_content = """
<!DOCTYPE html>
<html>
<head>
    <title>AI Athlete Twin - v1</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root {
            --bg-color: #121212;
            --text-color: #e0e0e0;
            --user-msg-bg: #0d47a1;
            --agent-msg-bg: #1e1e1e;
            --accent-color: #2962ff;
            --border-color: #333;
        }
        body { font-family: "Google Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 800px; margin: auto; padding: 20px; background: var(--bg-color); color: var(--text-color); }
        h2 { color: var(--text-color); text-align: center; font-weight: 500; display: flex; align-items: center; justify-content: center; gap: 20px; }
        #chatbox { height: 70vh; overflow-y: auto; background: var(--bg-color); padding: 20px; border-radius: 16px; border: 1px solid var(--border-color); margin-bottom: 20px; display: flex; flex-direction: column; gap: 15px; }
        .msg { padding: 12px 18px; border-radius: 18px; max-width: 85%; line-height: 1.6; font-size: 16px; word-wrap: break-word; }
        #syncBtnUI { padding: 8px 16px; background: var(--accent-color); color: white; border: none; border-radius: 20px; font-size: 14px; font-weight: 500; cursor: pointer; transition: opacity 0.3s; }
        #syncBtnUI:hover { opacity: 0.9; }
        #syncBtnUI:disabled { opacity: 0.5; cursor: not-allowed; }
        .user { background: var(--user-msg-bg); color: white; align-self: flex-end; border-bottom-right-radius: 4px; }
        .agent { background: var(--agent-msg-bg); color: var(--text-color); align-self: flex-start; border-bottom-left-radius: 4px; white-space: pre-wrap; }
        .input-area { display: flex; gap: 10px; background: var(--agent-msg-bg); padding: 10px; border-radius: 28px; }
        input { flex-grow: 1; padding: 10px; border: none; border-radius: 24px; font-size: 16px; outline: none; background: transparent; color: var(--text-color); }
        button { padding: 10px 20px; background: var(--accent-color); color: white; border: none; border-radius: 20px; font-size: 16px; font-weight: 500; cursor: pointer; transition: opacity 0.3s; }
        button:hover { opacity: 0.9; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        .typing-indicator { display: inline-flex; align-items: center; gap: 4px; padding: 10px 0; }
        .typing-indicator span { height: 8px; width: 8px; background-color: #999; border-radius: 50%; display: inline-block; animation: bounce 1.2s infinite ease-in-out both; }
        .typing-indicator span:nth-child(1) { animation-delay: -0.32s; }
        .typing-indicator span:nth-child(2) { animation-delay: -0.16s; }
        @keyframes bounce { 0%, 80%, 100% { transform: scale(0); } 40% { transform: scale(1.0); } }
    </style>
</head>
<body>
    <h2>AI Athlete Twin <span style="font-size:12px; opacity:0.5;">v1</span> <button id="syncBtnUI" onclick="syncData()">Sync Latest Activities</button></h2>
    <div id="chatbox">
        <div class="msg agent">System online. Waiting for telemetry queries...</div>
    </div>
    <div class="input-area">
        <input type="text" id="userInput" placeholder="Ask your coach..." onkeypress="handleKeyPress(event)">
        <button onclick="sendMessage()" id="sendBtn">&#10148;</button>
    </div>

    <script>
        async function syncData() {
            const syncBtn = document.getElementById("syncBtnUI");
            const originalText = syncBtn.innerHTML;
            syncBtn.innerHTML = "Syncing...";
            syncBtn.disabled = true;

            try {
                const response = await fetch('/sync-data', { method: 'POST' });
                const result = await response.json();
                if (response.ok) {
                    addMessage(result.message, "agent");
                } else {
                    addMessage(`Sync failed: ${result.detail}`, "agent");
                }
            } catch (error) {
                console.error("Sync failed:", error);
                addMessage("Sync failed: Connection error.", "agent");
            } finally {
                syncBtn.innerHTML = originalText;
                syncBtn.disabled = false;
            }
        }
        async function sendMessage() {
            const input = document.getElementById("userInput");
            const btn = document.getElementById("sendBtn");
            const message = input.value.trim();
            if (!message) return;

            addMessage(message, "user");
            input.value = "";
            btn.innerHTML = "&#8987;";
            btn.disabled = true;

            const agentMsgDiv = createMessage("", "agent");
            agentMsgDiv.innerHTML = `
                <div class="status-updates" style="white-space: pre-wrap; font-size: 0.9em; color: #999;"></div>
                <div class="final-answer" style="display: none; white-space: pre-wrap;"></div>
                <div class="typing-indicator"><span></span><span></span><span></span></div>
            `;

            const eventSource = new EventSource(`/stream_chat?message=${encodeURIComponent(message)}`);

            eventSource.onmessage = function(event) {
                const data = JSON.parse(event.data);
                const statusDiv = agentMsgDiv.querySelector('.status-updates');
                const finalAnswerDiv = agentMsgDiv.querySelector('.final-answer');
                const indicator = agentMsgDiv.querySelector('.typing-indicator');
                if (data.type === 'status') {
                    if (statusDiv) statusDiv.textContent += data.content + '\\n';
                    chatbox.scrollTop = chatbox.scrollHeight;
                } else if (data.type === 'final') {
                    if (statusDiv) statusDiv.style.display = 'none';
                    if (indicator) indicator.style.display = 'none';
                    if (finalAnswerDiv) {
                        finalAnswerDiv.textContent = data.content;
                        finalAnswerDiv.style.display = 'block';
                    }
                } else if (data.type === 'end') {
                    if (indicator) indicator.style.display = 'none';
                    eventSource.close();
                    btn.innerHTML = "&#10148;";
                    btn.disabled = false;
                    input.focus();
                }
            };

            eventSource.onerror = function(err) {
                console.error("EventSource failed:", err);
                agentMsgDiv.textContent = "Connection error to core logic.";
                eventSource.close();
                btn.innerHTML = "&#10148;";
                btn.disabled = false;
            };
        }

        function createMessage(text, sender) {
            const chatbox = document.getElementById("chatbox");
            const div = document.createElement("div");
            div.className = "msg " + sender;
            div.innerText = text;
            chatbox.appendChild(div);
            chatbox.scrollTop = chatbox.scrollHeight;
            return div;
        }

        function addMessage(text, sender) { createMessage(text, sender); }
        function handleKeyPress(e) { if (e.key === "Enter") sendMessage(); }
    </script>
</body>
</html>
"""


@app.get("/")
async def serve_dashboard():
    index = os.path.join(DASHBOARD_DIST, "index.html")
    if os.path.exists(index):
        # Always revalidate: index.html points at the hashed bundle of the current build,
        # and phones (especially home-screen web apps) otherwise keep showing an old layout.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return HTMLResponse(content=html_content)


@app.get("/classic")
async def serve_classic_ui():
    return HTMLResponse(content=html_content)


async def stream_generator(message: str):
    logger.info(f"Received streaming request from web UI: '{message}'")
    async for event in stream_reply(message):
        yield f"data: {json.dumps(event)}\n\n"


@app.get("/stream_chat")
async def stream_chat(message: str):
    return StreamingResponse(stream_generator(message), media_type="text/event-stream")


@app.post("/sync-data")
async def sync_data():
    """
    Pulls the last SYNC_WINDOW_DAYS of activities + wellness from Intervals.icu
    directly into athlete_twin_v2. No separate data-pipeline service involved -
    the old container isn't part of this path anymore.
    """
    logger.info(f"Sync triggered: pulling last {SYNC_WINDOW_DAYS} days from Intervals.icu.")
    try:
        message = await run_sync(SYNC_WINDOW_DAYS)
        logger.info(f"Sync complete: {message}")
        return JSONResponse(content={"message": message})

    except Exception as e:
        logger.error(f"Sync failed: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"detail": str(e)})


# Home-screen / PWA files that Vite copies from web/public into web/dist.
# Registered last so this catch-all path never shadows the routes above.
PUBLIC_FILES = {"apple-touch-icon.png", "icon-192.png", "icon-512.png", "manifest.webmanifest"}


@app.get("/{name}", include_in_schema=False)
async def serve_public_file(name: str):
    path = os.path.join(DASHBOARD_DIST, name)
    if name not in PUBLIC_FILES or not os.path.exists(path):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    media = "application/manifest+json" if name.endswith(".webmanifest") else None
    return FileResponse(path, media_type=media)


if os.path.isdir(os.path.join(DASHBOARD_DIST, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(DASHBOARD_DIST, "assets")), name="assets")


if __name__ == "__main__":
    import uvicorn
    setup_logging()
    uvicorn.run(app, host="0.0.0.0", port=8000)
