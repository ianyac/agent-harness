from ui.server.store import InMemorySessionStore


def make_clock(start=1000.0):
    state = {"now": start}

    def clock():
        state["now"] += 1.0
        return state["now"]

    return clock


def test_create_get_and_shared_messages_list():
    store = InMemorySessionStore(clock=make_clock())
    session = store.create()
    assert store.get(session.id) is session
    assert session.messages == []
    session.messages.append({"role": "user", "content": "hi"})
    assert store.get(session.id).messages == [{"role": "user", "content": "hi"}]
    assert store.get("nope") is None


def test_list_orders_by_recent_update():
    store = InMemorySessionStore(clock=make_clock())
    first = store.create()
    second = store.create()
    assert [s.id for s in store.list_sessions()] == [second.id, first.id]
    store.touch(first.id)
    assert [s.id for s in store.list_sessions()] == [first.id, second.id]
    assert store.get(first.id).updated_at > store.get(second.id).updated_at
