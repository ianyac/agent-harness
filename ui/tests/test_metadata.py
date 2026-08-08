from pathlib import Path

import pytest

from server.metadata import MetadataStore, NewSession


def test_session_rows_round_trip_without_message_content(tmp_path: Path):
    store = MetadataStore(tmp_path / "ui.sqlite3")
    created = store.create_session(
        NewSession(
            session_id="s1",
            workspace=tmp_path / "project",
            title="Streaming retries",
            mode="default",
            context_mode="folding",
        )
    )

    assert store.get_session("s1") == created
    assert created.workspace == (tmp_path / "project").resolve()
    assert "message" not in store.raw_session_columns()


def test_archived_sessions_are_excluded_unless_requested(tmp_path: Path):
    store = MetadataStore(tmp_path / "ui.sqlite3")
    store.create_session(NewSession.defaults("s1", tmp_path))

    archived = store.archive_session("s1")

    assert archived.archived_at is not None
    assert store.list_sessions() == []
    assert [row.session_id for row in store.list_sessions(include_archived=True)] == ["s1"]


def test_updates_and_preferences_survive_reopening_the_database(tmp_path: Path):
    database = tmp_path / "ui.sqlite3"
    store = MetadataStore(database)
    store.create_session(NewSession.defaults("s1", tmp_path / "workspace"))
    renamed = store.rename_session("s1", "Useful title")
    touched = store.touch_session("s1")
    saved_preference = store.set_preference("theme", {"name": "dark"})

    reopened = MetadataStore(database)

    assert reopened.get_session("s1") == touched
    assert touched.title == "Useful title"
    assert touched.updated_at >= renamed.updated_at
    assert touched.last_opened_at is not None
    assert reopened.get_preference("theme") == saved_preference


def test_discovery_adds_new_sessions_without_overwriting_user_title(tmp_path: Path):
    store = MetadataStore(tmp_path / "ui.sqlite3")
    discovered = NewSession.defaults("s1", tmp_path / "workspace")

    first = store.upsert_discovered_session(discovered)
    store.rename_session("s1", "Pinned title")
    second = store.upsert_discovered_session(discovered)

    assert first.session_id == "s1"
    assert second.title == "Pinned title"


@pytest.mark.parametrize(
    ("mode", "context_mode"),
    [("plan", "folding"), ("default", "unknown")],
)
def test_session_creation_rejects_modes_outside_public_contract(
    tmp_path: Path, mode: str, context_mode: str
):
    store = MetadataStore(tmp_path / "ui.sqlite3")

    with pytest.raises(ValueError):
        store.create_session(
            NewSession(
                session_id="s1",
                workspace=tmp_path,
                title="Invalid mode",
                mode=mode,
                context_mode=context_mode,
            )
        )
