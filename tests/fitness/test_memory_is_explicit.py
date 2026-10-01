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

from .conftest import drive_to_completion

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "agentstack"
ME = Scope(tenant="acme", user="u-1")


def test_an_implicit_write_is_refused() -> None:
    store = MemoryStore()
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


def test_a_write_needs_provenance_and_a_positive_ttl() -> None:
    store = MemoryStore()
    with pytest.raises(ValueError, match="provenance"):
        write(
            store, key="k", value="v", scope=ME, provenance="", ttl=timedelta(days=1), explicit=True
        )
    with pytest.raises(ValueError, match="positive TTL"):
        write(store, key="k", value="v", scope=ME, provenance="p", ttl=timedelta(0), explicit=True)


def test_recall_is_scoped() -> None:
    store = MemoryStore()
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


def test_memory_carries_its_trust_label_into_context() -> None:
    store = MemoryStore()
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
