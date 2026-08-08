from pathlib import Path
import sqlite3
import threading

import pytest

import server.metadata as metadata
from server.metadata import MetadataStore, NewSession


class RecordConstructionInterrupted(BaseException):
    pass


def test_session_rows_round_trip_without_message_content(tmp_path: Path):
    with MetadataStore(tmp_path / "ui.sqlite3") as store:
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
        assert store.raw_session_columns() == (
            "session_id",
            "workspace",
            "title",
            "mode",
            "context_mode",
            "created_at",
            "updated_at",
            "last_opened_at",
            "archived_at",
        )
        assert store._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store._connection.execute("SELECT version FROM schema_version").fetchone()[0] == 1


def test_archived_sessions_are_excluded_unless_requested(tmp_path: Path):
    with MetadataStore(tmp_path / "ui.sqlite3") as store:
        store.create_session(NewSession.defaults("s1", tmp_path))

        archived = store.archive_session("s1")

        assert archived.archived_at is not None
        assert store.list_sessions() == []
        assert [row.session_id for row in store.list_sessions(include_archived=True)] == ["s1"]


def test_updates_and_preferences_survive_reopening_the_database(tmp_path: Path):
    database = tmp_path / "ui.sqlite3"
    with MetadataStore(database) as store:
        store.create_session(NewSession.defaults("s1", tmp_path / "workspace"))
        renamed = store.rename_session("s1", "Useful title")
        touched = store.touch_session("s1")
        saved_preference = store.set_preference("theme", {"name": "dark"})

        with MetadataStore(database) as reopened:
            assert reopened.get_session("s1") == touched
            assert touched.title == "Useful title"
            assert touched.updated_at >= renamed.updated_at
            assert touched.last_opened_at is not None
            assert reopened.get_preference("theme") == saved_preference


def test_session_mode_update_is_validated_transactional_and_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database = tmp_path / "ui.sqlite3"
    with MetadataStore(database) as store:
        created = store.create_session(
            NewSession.defaults("s1", tmp_path / "workspace")
        )
        monkeypatch.setattr(store, "_now", lambda: "9999-12-31T23:59:59.999999")

        updated = store.set_session_mode("s1", "readOnly")

        assert updated.mode == "readOnly"
        assert updated.updated_at == "9999-12-31T23:59:59.999999"
        assert updated.updated_at > created.updated_at
        with pytest.raises(ValueError):
            store.set_session_mode("s1", "plan")
        assert store.get_session("s1") == updated
        with pytest.raises(KeyError):
            store.set_session_mode("missing", "default")

    with MetadataStore(database) as reopened:
        assert reopened.get_session("s1") == updated


def test_discovery_adds_new_sessions_without_overwriting_user_title(tmp_path: Path):
    with MetadataStore(tmp_path / "ui.sqlite3") as store:
        discovered = NewSession.defaults("s1", tmp_path / "workspace")

        first = store.upsert_discovered_session(discovered)
        store.rename_session("s1", "Pinned title")
        second = store.upsert_discovered_session(discovered)

        assert first.session_id == "s1"
        assert second.title == "Pinned title"


def test_discovery_preserves_existing_interaction_recency_and_list_order(tmp_path: Path):
    with MetadataStore(tmp_path / "ui.sqlite3") as store:
        store.create_session(NewSession.defaults("s1", tmp_path / "one"))
        store.create_session(NewSession.defaults("s2", tmp_path / "two"))
        first_before = store.touch_session("s1")
        store.touch_session("s2")

        discovered = store.upsert_discovered_session(
            NewSession(
                session_id="s1",
                workspace=tmp_path / "moved",
                title="Discovered title",
                mode="acceptAll",
                context_mode="folding",
            )
        )

        assert discovered.workspace == (tmp_path / "moved").resolve()
        assert discovered.mode == "acceptAll"
        assert discovered.context_mode == "folding"
        assert discovered.last_opened_at == first_before.last_opened_at
        assert discovered.updated_at == first_before.updated_at
        assert [row.session_id for row in store.list_sessions()] == ["s2", "s1"]


def test_store_close_is_idempotent_and_context_manager_releases_connection(tmp_path: Path):
    database = tmp_path / "ui.sqlite3"
    with MetadataStore(database) as managed:
        managed.create_session(NewSession.defaults("s1", tmp_path))

    with pytest.raises(sqlite3.ProgrammingError):
        managed.get_session("s1")

    managed.close()


def test_schema_version_rejection_closes_partially_initialized_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database = tmp_path / "ui.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_version (version) VALUES (2)")
    connection.commit()
    connection.close()

    real_connect = sqlite3.connect
    opened_connections: list[sqlite3.Connection] = []

    def tracking_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        opened = real_connect(*args, **kwargs)
        opened_connections.append(opened)
        return opened

    monkeypatch.setattr(metadata.sqlite3, "connect", tracking_connect)

    with pytest.raises(RuntimeError, match="unsupported metadata schema version: 2"):
        MetadataStore(database)

    assert len(opened_connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened_connections[0].execute("SELECT 1")


def test_store_documents_and_enforces_single_thread_confinement(tmp_path: Path):
    with MetadataStore(tmp_path / "ui.sqlite3") as store:
        errors: list[BaseException] = []

        def read_from_other_thread() -> None:
            try:
                store.get_session("s1")
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=read_from_other_thread)
        thread.start()
        thread.join()

        assert "single-thread" in MetadataStore.__doc__
        assert len(errors) == 1
        assert isinstance(errors[0], sqlite3.ProgrammingError)


@pytest.mark.parametrize(
    ("mode", "context_mode"),
    [("plan", "folding"), ("default", "unknown")],
)
def test_session_creation_rejects_modes_outside_public_contract(
    tmp_path: Path, mode: str, context_mode: str
):
    with MetadataStore(tmp_path / "ui.sqlite3") as store:

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


def test_create_session_record_construction_interruption_rolls_back_insert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    with MetadataStore(tmp_path / "ui.sqlite3") as store:
        interruption = RecordConstructionInterrupted(
            "record construction interrupted"
        )

        def fail_record(_row):
            raise interruption

        monkeypatch.setattr(store, "_session_record", fail_record)

        with pytest.raises(RecordConstructionInterrupted) as raised:
            store.create_session(NewSession.defaults("ghost", tmp_path))

        assert raised.value is interruption
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE session_id = 'ghost'"
            ).fetchone()[0]
            == 0
        )
        assert not store._connection.in_transaction
        monkeypatch.undo()
        assert store.create_session(
            NewSession.defaults("recovered", tmp_path)
        ).session_id == "recovered"


@pytest.mark.parametrize(
    "operation",
    [
        "rename_session",
        "set_session_mode",
        "touch_session",
        "archive_session",
        "upsert_discovered_session",
    ],
)
def test_session_mutation_record_construction_interruption_is_transaction_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
):
    with MetadataStore(tmp_path / "ui.sqlite3") as store:
        original = store.create_session(NewSession.defaults("s1", tmp_path / "one"))
        interruption = RecordConstructionInterrupted(
            "record construction interrupted"
        )

        def fail_record(_row):
            raise interruption

        monkeypatch.setattr(store, "_session_record", fail_record)
        with pytest.raises(RecordConstructionInterrupted) as raised:
            if operation == "rename_session":
                store.rename_session("s1", "Changed")
            elif operation == "set_session_mode":
                store.set_session_mode("s1", "readOnly")
            elif operation == "touch_session":
                store.touch_session("s1")
            elif operation == "archive_session":
                store.archive_session("s1")
            else:
                store.upsert_discovered_session(
                    NewSession(
                        session_id="s1",
                        workspace=tmp_path / "moved",
                        title="Ignored",
                        mode="readOnly",
                        context_mode="folding",
                    )
                )

        assert raised.value is interruption
        assert not store._connection.in_transaction
        monkeypatch.undo()
        assert store.get_session("s1") == original
        assert store.rename_session("s1", "Recovered").title == "Recovered"


def test_preference_record_construction_interruption_is_transaction_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    with MetadataStore(tmp_path / "ui.sqlite3") as store:
        original = store.set_preference("theme", {"name": "light"})
        interruption = RecordConstructionInterrupted(
            "record construction interrupted"
        )

        def fail_record(**_fields):
            raise interruption

        monkeypatch.setattr(metadata, "PreferenceRecord", fail_record)
        with pytest.raises(RecordConstructionInterrupted) as raised:
            store.set_preference("theme", {"name": "dark"})

        assert raised.value is interruption
        assert not store._connection.in_transaction
        monkeypatch.undo()
        assert store.get_preference("theme") == original
        assert store.set_preference("theme", {"name": "recovered"}).value == {
            "name": "recovered"
        }
