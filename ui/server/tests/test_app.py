import asyncio

from fastapi.testclient import TestClient

from ui.server.app import EventSink, HarnessDeps, create_app
from ui.server.tests.fake_llm import FakeLLM, text_reply


def make_deps(replies=None, tools=None):
    return HarnessDeps(
        llm=FakeLLM(replies or [text_reply("hi")]),
        tools=tools or {},
        policy_factory=lambda: None,
        system_prompt=lambda: "the system prompt",
        subagent_system_prompt=None,
        mode="default",
        workspace="/tmp/workspace",
    )


def test_sessions_create_and_list():
    client = TestClient(create_app(make_deps()))
    assert client.get("/api/sessions").json() == []
    created = client.post("/api/sessions").json()
    assert set(created) == {"id", "created_at", "updated_at"}
    listed = client.get("/api/sessions").json()
    assert [s["id"] for s in listed] == [created["id"]]


def test_meta_reports_mode_workspace_and_system_prompt():
    client = TestClient(create_app(make_deps()))
    meta = client.get("/api/meta").json()
    assert meta == {
        "mode": "default",
        "workspace": "/tmp/workspace",
        "system_prompt": "the system prompt",
    }


def test_event_sink_delivers_and_drops():
    async def scenario():
        sink = EventSink()
        sink.push({"type": "dropped"})  # detached: silently dropped
        q = asyncio.Queue()
        sink.attach(asyncio.get_running_loop(), q)
        await asyncio.to_thread(sink.push, {"type": "delivered"})
        assert await asyncio.wait_for(q.get(), 1) == {"type": "delivered"}
        sink.detach()
        sink.push({"type": "dropped again"})
        assert q.empty()

    asyncio.run(scenario())
