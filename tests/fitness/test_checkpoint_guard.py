"""Criterion 22: CI catches a stranding deploy.

T19 parks a run whose checkpoint this code cannot read. That is the net. This holds the
line before it: an incompatible change to the checkpoint shape that says nothing about
it fails the build, rather than being discovered by the runs it strands.
"""

from __future__ import annotations

import copy
import functools
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from agentstack.runtime import graph

ROOT = Path(__file__).resolve().parents[2]


@functools.cache
def _guard() -> ModuleType:
    """Load scripts/checkpoint_guard.py, which is not an importable package."""
    spec = importlib.util.spec_from_file_location(
        "checkpoint_guard", ROOT / "scripts/checkpoint_guard.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves its module through sys.modules; without this it sees None.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def released() -> dict[str, Any]:
    return graph.checkpoint_shape()


def changed(shape: dict[str, Any], **edits: Any) -> dict[str, Any]:
    new = copy.deepcopy(shape)
    for key, value in edits.items():
        new[key] = value
    return new


def without_field(shape: dict[str, Any], name: str) -> dict[str, Any]:
    fields = {k: v for k, v in shape["fields"].items() if k != name}
    return changed(shape, fields=fields)


def judge(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    compatible: frozenset[int] | None = None,
    notes: Path,
) -> list[Any]:
    return list(
        _guard().judge(
            released=before,
            recorded=after,
            current=after,
            compatible=compatible or frozenset({after["version"]}),
            notes=notes,
        )
    )


# --- what counts as incompatible ------------------------------------------------------


def test_the_recorded_shape_is_the_one_this_code_writes() -> None:
    """The baseline the next release is compared against has to be the truth."""
    recorded = json.loads((ROOT / "checkpoints" / "schema.json").read_text())

    assert recorded == graph.checkpoint_shape()


def test_the_shape_names_every_state_key_and_every_node() -> None:
    shape = graph.checkpoint_shape()

    assert set(shape["fields"]) == set(graph.TurnState.__annotations__)
    assert shape["fields"]["resumed_by"] == "str | None"
    assert "call_model" in shape["nodes"]
    assert not any(node.startswith("__") for node in shape["nodes"])


@pytest.mark.parametrize(
    ("edit", "named"),
    [
        (lambda s: without_field(s, "model_text"), "field 'model_text' removed"),
        (
            lambda s: changed(s, fields={**s["fields"], "receipts": "list[int]"}),
            "field 'receipts' changed type: list[str] -> list[int]",
        ),
        (
            lambda s: changed(s, nodes=[n if n != "act" else "act_v2" for n in s["nodes"]]),
            "node 'act' removed",
        ),
    ],
)
def test_removing_retyping_or_renaming_is_breaking(edit: Any, named: str) -> None:
    before = released()

    assert named in _guard().breaking_changes(before, edit(before))


def test_adding_a_key_or_a_node_is_not() -> None:
    """`TurnState` is total=False: a key an old checkpoint lacks reads as absent."""
    before = released()
    after = changed(
        before,
        fields={**before["fields"], "new_key": "str"},
        nodes=[*before["nodes"], "a_new_node"],
    )

    assert _guard().breaking_changes(before, after) == []


# --- the build fails unless the change is declared ------------------------------------


def test_a_breaking_change_without_a_bump_fails(tmp_path: Path) -> None:
    before = released()

    (finding,) = judge(before, without_field(before, "model_text"), notes=tmp_path)

    assert "without a version bump" in finding.what
    assert "model_text" in finding.why


def test_a_bump_that_still_claims_the_old_version_fails(tmp_path: Path) -> None:
    before = released()
    after = changed(without_field(before, "model_text"), version=before["version"] + 1)
    (tmp_path / f"v{after['version']}.md").write_text("migrate model_text")

    (finding,) = judge(
        before, after, compatible=frozenset({before["version"], after["version"]}), notes=tmp_path
    )

    assert "still claims to resume" in finding.what


def test_a_bump_with_no_migration_note_fails(tmp_path: Path) -> None:
    before = released()
    after = changed(without_field(before, "model_text"), version=before["version"] + 1)
    (tmp_path / f"v{after['version']}.md").write_text("   \n")

    (finding,) = judge(before, after, notes=tmp_path)

    assert f"no migration note at checkpoints/v{after['version']}.md" in finding.what


def test_a_declared_breaking_change_passes(tmp_path: Path) -> None:
    """Bumped, old version dropped, note written: the runs will park, and the operator
    knows what to do with them."""
    before = released()
    after = changed(without_field(before, "model_text"), version=before["version"] + 1)
    (tmp_path / f"v{after['version']}.md").write_text("rewrite model_text into proposals")

    assert judge(before, after, notes=tmp_path) == []


def test_a_compatible_change_passes_without_a_bump(tmp_path: Path) -> None:
    before = released()
    after = changed(before, fields={**before["fields"], "new_key": "str"})

    assert judge(before, after, notes=tmp_path) == []


def test_the_version_cannot_go_backwards(tmp_path: Path) -> None:
    before = changed(released(), version=5)

    (finding,) = judge(before, released(), notes=tmp_path)

    assert "went backwards" in finding.what


def test_a_stale_record_fails_even_when_nothing_was_released(tmp_path: Path) -> None:
    current = released()

    (finding,) = _guard().judge(
        released=None,
        recorded=without_field(current, "model_text"),
        current=current,
        compatible=frozenset({1}),
        notes=tmp_path,
    )

    assert "not the shape this code writes" in finding.what


# --- end to end, as CI runs it --------------------------------------------------------


def test_a_deliberate_incompatible_change_fails_the_build(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The criterion as written: the change is made, the record is even updated to
    match it, and the build still fails, because the release it ships over did not."""
    guard = _guard()
    before = graph.checkpoint_shape()
    after = without_field(before, "model_text")
    monkeypatch.setattr(graph, "checkpoint_shape", lambda: after)
    asked: list[str] = []

    def released_at(base: str) -> dict[str, Any]:
        asked.append(base)
        return before

    monkeypatch.setattr(guard, "released_at", released_at)
    record = tmp_path / "schema.json"
    record.write_text(json.dumps(after))
    monkeypatch.setattr(guard, "RECORD", record)

    code = guard.main(["--base", "origin/main"])

    out = capsys.readouterr().out
    assert code == 1
    assert asked == ["origin/main"], "compared against something other than the release"
    assert "incompatible change to checkpoint schema v1 without a version bump" in out
    assert "against origin/main" in out


def test_this_repository_is_safe_to_ship_over_its_last_commit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _guard().main(["--base", "HEAD"]) == 0
    assert "safe to ship" in capsys.readouterr().out


def test_an_unknown_base_cannot_run_rather_than_passing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A typo in CI's base ref must not read as "nothing released, all clear"."""
    assert _guard().main(["--base", "no-such-ref-anywhere"]) == 2
    assert "not a commit" in capsys.readouterr().err
