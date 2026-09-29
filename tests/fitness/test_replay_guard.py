"""Criterion 41: a deploy that would strand live runs fails the build.

`scripts/replay_guard.py` replays the recorded histories in `tests/fixtures/histories/`
through the workflow code. These hold it to its job: the real code replays every one;
a step inserted in front of an existing one fails every one, because each run it
recorded evaluated a trigger; and the same step behind `workflow.patched()` passes.
The mutants are deploys built in `replay_mutants.py`. The real workflow is untouched.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests import temporal_support
from tests.fitness.replay_mutants import StepInsertedBehindPatch, StepInsertedUnguarded

ROOT = Path(__file__).resolve().parents[2]

# The paths the histories cover. Deleting one is ask-first (SPEC-durable-runtime.md).
RECORDED = {
    "trigger_cycles",
    "approved_commit",
    "refused_at_the_act",
    "unresolved_reconciled",
    "draft_unresolved_reconciled",
}


@functools.cache
def _guard() -> ModuleType:
    """Load scripts/replay_guard.py, which is not an importable package."""
    spec = importlib.util.spec_from_file_location("replay_guard", ROOT / "scripts/replay_guard.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves its module through sys.modules; without this it sees None.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def replayed(workflows: list[type] | None = None) -> list[str]:
    guard = _guard()
    findings = asyncio.run(guard.replay(guard.load(), workflows))
    return [f"{finding.what}: {finding.why}" for finding in findings]


def test_every_path_is_recorded() -> None:
    assert set(_guard().load()) == RECORDED


def test_the_recorded_histories_replay_against_this_code() -> None:
    assert replayed() == []


def test_a_step_inserted_unguarded_fails_every_history() -> None:
    findings = replayed([StepInsertedUnguarded])

    assert len(findings) == len(RECORDED)
    for name in RECORDED:
        (finding,) = [f for f in findings if f.startswith(f"{name}.json")]
        assert "NondeterminismError" in finding and "TMPRL1100" in finding


def test_the_same_step_behind_patched_passes() -> None:
    assert replayed([StepInsertedBehindPatch]) == []


def test_the_guard_exits_one_on_a_stranding_change(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Through `main`, as `check_task.sh` runs it: a finding is exit 1, and it says which
    history and what to do."""
    guard = _guard()
    unguarded = functools.partial(guard.replay, workflows=[StepInsertedUnguarded])
    monkeypatch.setattr(guard, "replay", unguarded)

    assert guard.main([]) == 1
    out = capsys.readouterr().out
    assert "approved_commit.json no longer replays" in out
    assert "workflow.patched()" in out


def test_no_histories_is_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A guard with nothing to replay would pass every change."""
    guard = _guard()
    assert guard.load(tmp_path) == {}
    monkeypatch.setattr(guard, "load", lambda: {})

    assert guard.main([]) == 2
    assert "--record" in capsys.readouterr().err


def test_the_guard_records_where_the_suite_writes() -> None:
    guard = _guard()
    assert guard.RECORD_HISTORIES == temporal_support.RECORD_HISTORIES
    assert guard.HISTORIES == temporal_support.HISTORIES
    assert guard.RECORDER.is_file()
