"""The KOM agent node: keyword routing and forwarding to the separate /opt/kom service."""
from langchain_core.messages import AIMessage, HumanMessage

import agents.kom as kom


def test_kom_words_route_segment_questions():
    for q in ("how many KOMs do I have?", "Did I lose a kom?", "QOM on Húšť", "who is the local legend there",
              "my time on the Strmina segment", "which segments are close"):
        assert kom.KOM_WORDS.search(q), q
    for q in ("what should I do tomorrow?", "komoot route for Sunday", "am I recovered?", "how did my race go"):
        assert not kom.KOM_WORDS.search(q), q


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.headers = {"content-type": "application/json"}
        self.text = str(body)

    def json(self):
        return self._body


def test_call_kom_forwards_question_and_history(monkeypatch):
    sent = {}

    def post(url, json, headers, timeout):
        sent.update(url=url, body=json, headers=headers)
        return _Resp(200, {"reply": "You hold 39 KOMs."})

    monkeypatch.setattr(kom.requests, "post", post)
    monkeypatch.setattr(kom, "record", lambda *a, **k: None)
    monkeypatch.setitem(kom.env, "KOM_API_TOKEN", "t0ken")
    state = {"messages": [HumanMessage(content="hi"), AIMessage(content="hello", name="ai_coach"),
                          HumanMessage(content="how many KOMs?")]}
    out = kom.call_kom(state)
    assert out["messages"][0].content == "You hold 39 KOMs."
    assert sent["url"].endswith("/ask") and sent["headers"]["X-Kom-Token"] == "t0ken"
    assert sent["body"]["message"] == "how many KOMs?"
    assert sent["body"]["history"] == [{"role": "user", "content": "hi", "agent": None},
                                       {"role": "assistant", "content": "hello", "agent": "ai_coach"}]


def test_call_kom_reports_failure_instead_of_raising(monkeypatch):
    monkeypatch.setattr(kom.requests, "post", lambda *a, **k: _Resp(502, {"detail": "Claude failed: boom"}))
    monkeypatch.setattr(kom, "record", lambda *a, **k: None)
    monkeypatch.setitem(kom.env, "KOM_API_TOKEN", "t0ken")
    out = kom.call_kom({"messages": [HumanMessage(content="my KOMs?")]})
    assert "couldn't answer" in out["messages"][0].content and "boom" in out["messages"][0].content


def test_call_kom_without_token(monkeypatch):
    monkeypatch.setitem(kom.env, "KOM_API_TOKEN", "")
    out = kom.call_kom({"messages": [HumanMessage(content="my KOMs?")]})
    assert "isn't set up" in out["messages"][0].content
