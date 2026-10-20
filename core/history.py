"""
Short conversation memory shared by the graph and its front-ends.

The graph state's `messages` is [...prior turns..., HumanMessage(current question)].
Prior AI turns carry the answering agent's node name in `AIMessage.name`, so the
supervisor can see who handled the previous turn and keep follow-ups on track.
"""
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

# How many prior messages (user + assistant) are kept and passed to the agents.
# 6 = the last 3 exchanges - enough for "and tomorrow?" style follow-ups without
# bloating every prompt (the metrics context is already large).
MAX_HISTORY_MESSAGES = 6

# The supervisor only needs the gist of earlier turns to route, not full answers.
SUPERVISOR_SNIPPET_CHARS = 300


def split_state(state) -> tuple[list[BaseMessage], str]:
    """Returns (prior_messages, current_question) from a graph state."""
    messages = state["messages"]
    return list(messages[:-1])[-MAX_HISTORY_MESSAGES:], messages[-1].content


def to_messages(turns: list[dict]) -> list[BaseMessage]:
    """Plain-dict turns ({role, content, agent}) -> LangChain messages."""
    out = []
    for t in turns[-MAX_HISTORY_MESSAGES:]:
        if t["role"] == "user":
            out.append(HumanMessage(content=t["content"]))
        else:
            out.append(AIMessage(content=t["content"], name=t.get("agent")))
    return out


def format_for_supervisor(prior: list[BaseMessage]) -> str:
    if not prior:
        return "(no earlier messages - this is the start of the conversation)"
    lines = []
    for m in prior:
        text = str(m.content).replace("\n", " ")
        if len(text) > SUPERVISOR_SNIPPET_CHARS:
            text = text[:SUPERVISOR_SNIPPET_CHARS] + "..."
        who = "User" if isinstance(m, HumanMessage) else f"Assistant [{m.name or 'unknown'}]"
        lines.append(f"{who}: {text}")
    return "\n".join(lines)
