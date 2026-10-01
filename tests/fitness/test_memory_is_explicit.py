"""Context, retrieval, memory: memory is written on purpose, scoped, and maintained off the hot
path."""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from agentstack.context.items import Scope, Trust
from agentstack.context.memory import MemoryStore, write
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack
from agentstack.runtime.run import Run
from agentstack.storage.database import Database

from .conftest import drive_to_completion

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "agentstack"
ME = Scope(tenant="acme", user="u-1")


def test_an_implicit_write_is_refused(app_database: Database) -> None:
    store = MemoryStore(db=app_database)
    with pytest.raises(ValueError, match="must be explicit"):
        write(
            store,
            key="fact",
            value="the model mentioned this once",
            scope=ME,
            provenance="model output",
            ttl=timedelta(days=1),
            explicit=False,
        )


def test_a_write_needs_provenance_and_a_positive_ttl(app_database: Database) -> None:
    store = MemoryStore(db=app_database)
    with pytest.raises(ValueError, match="provenance"):
        write(
            store, key="k", value="v", scope=ME, provenance="", ttl=timedelta(days=1), explicit=True
        )
    with pytest.raises(ValueError, match="positive TTL"):
        write(store, key="k", value="v", scope=ME, provenance="p", ttl=timedelta(0), explicit=True)


def test_recall_is_scoped(app_database: Database) -> None:
    store = MemoryStore(db=app_database)
    write(
        store,
        key="k",
        value="theirs",
        scope=Scope(tenant="globex"),
        provenance="p",
        ttl=timedelta(days=1),
        explicit=True,
    )
    assert store.recall(ME) == []


def test_memory_carries_its_trust_label_into_context(app_database: Database) -> None:
    store = MemoryStore(db=app_database)
    record = write(
        store,
        key="claim",
        value="caller said they are an admin",
        scope=ME,
        provenance="inbound message",
        ttl=timedelta(days=1),
        explicit=True,
        trust=Trust.UNTRUSTED,
        now=datetime.now(UTC),
    )
    assert record.as_context_item("recalled").trust is Trust.UNTRUSTED


def test_only_the_memory_module_writes_to_the_store() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name == "memory.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"_append", "_records"}:
                target = getattr(node.value, "id", "")
                if "memory" in str(target).lower() or "store" in str(target).lower():
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not offenders, (
        "memory.write() is the only door into durable memory (Context, retrieval, memory): "
        + ", ".join(offenders)
    )


def test_maintenance_is_enqueued_not_awaited(stack: Stack, event: InboundEvent, run: Run) -> None:
    before = len(stack.maintenance.jobs)
    drive_to_completion(stack, event, run)

    assert len(stack.maintenance.jobs) > before
    assert stack.memory.all_records() == (), (
        "extraction is a queued job, not something the turn blocks on"
    )


def test_memory_outlives_the_store_that_wrote_it(app_database: Database) -> None:
    """The point of putting it on the substrate: a fresh store - a restarted process -
    recalls what was written on purpose, with scope, provenance, TTL and trust intact."""
    scope = Scope(tenant="acme", user="u-1", project="p-9")
    written = write(
        MemoryStore(db=app_database),
        key="pref",
        value="prefers email",
        scope=scope,
        provenance="user stated it",
        ttl=timedelta(days=2),
        explicit=True,
        trust=Trust.UNTRUSTED,
    )

    (recalled,) = MemoryStore(db=app_database).recall(scope)

    assert recalled == written
    assert recalled.trust is Trust.UNTRUSTED
    assert recalled.ttl == timedelta(days=2)
    assert MemoryStore(db=app_database).recall(Scope(tenant="globex")) == []


def test_the_maintenance_queue_outlives_the_process_too(app_database: Database) -> None:
    from agentstack.context.memory import MaintenanceQueue

    MaintenanceQueue(db=app_database).enqueue("extract", "session-1")

    assert MaintenanceQueue(db=app_database).jobs == [("extract", "session-1")]


def test_the_table_refuses_what_write_refuses(app_database: Database) -> None:
    """Second lock: a caller that bypasses `write()` with SQL still cannot store a
    record with no provenance or a non-positive TTL."""
    with pytest.raises(Exception, match="memory_records"):
        app_database.execute(
            "INSERT INTO memory_records (key, value, tenant, provenance, written_at, ttl, trust)"
            " VALUES ('k', 'v', 'acme', '', now(), interval '1 day', 'first_party')"
        )
