"""LLM factories: the local Ollama model and Gemini, each call logged to api_calls
(core/api_log.py) under the `component` that made it.

Local: every local call uses the same model.

The Ollama box has an 8 GB GTX 1070, which holds one 8B model at a time - mixing
models would reload VRAM on every switch, so everything local shares one model.
qwen3:8b beat llama3.1 on narration quality at the same speed (2026-10-04
comparison); its "thinking" mode is turned off because it multiplies latency
without helping short routing/narration jobs. Override with OLLAMA_MODEL in .env.
"""
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ollama import ChatOllama

from core.api_log import UsageCallback

from core.db import load_env

env = load_env()

LOCAL_MODEL = env.get("OLLAMA_MODEL", "qwen3:8b")
# Keep the model in VRAM between chats instead of Ollama's default 5 minutes.
KEEP_ALIVE = "30m"
# One context size for every call: Ollama reloads the model whenever num_ctx changes.
# 8k fits local-only prompts (metrics + plan + reports, ~3-4k tokens) and, with the
# 8B weights, still fits the 8 GB card (~6.6 GB).
NUM_CTX = 8192


GEMINI_MODEL = "gemini-pro-latest"


def local_llm(component: str, **kwargs) -> ChatOllama:
    reasoning = {"reasoning": False} if LOCAL_MODEL.startswith("qwen3") else {}
    return ChatOllama(
        callbacks=[UsageCallback("ollama", component)],
        base_url=env.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        model=LOCAL_MODEL,
        keep_alive=KEEP_ALIVE,
        num_ctx=NUM_CTX,
        **reasoning,
        **kwargs,
    )


def gemini_llm(component: str, temperature: float) -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model=GEMINI_MODEL,
        google_api_key=env.get("GEMINI_API_KEY"),
        temperature=temperature,
        callbacks=[UsageCallback("gemini", component)],
    )
