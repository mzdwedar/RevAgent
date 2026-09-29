"""The turn-end cache may skip a run; it may never let a red tree ride on a green one.

`check_task.sh` remembers a pass per tree key so the Stop hook, the gatekeeper and CI do
not verify the same bytes three times. That is only safe if the key moves whenever
anything a check can read moves, and if only a pass is ever remembered.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _key() -> str:
    out = subprocess.run(
        ["bash", "scripts/tree_key.sh"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def test_key_is_stable_for_an_unchanged_tree() -> None:
    assert _key() == _key()


def test_key_moves_when_a_tracked_file_changes() -> None:
    target = ROOT / "scripts" / "layer_of.py"
    original = target.read_bytes()
    before = _key()
    try:
        target.write_bytes(original + b"\n# cache probe\n")
        assert _key() != before
    finally:
        target.write_bytes(original)
    assert _key() == before


def test_key_moves_when_an_untracked_file_appears() -> None:
    probe = ROOT / "scripts" / "_untracked_cache_probe.txt"
    before = _key()
    try:
        probe.write_text("x")
        assert _key() != before
    finally:
        probe.unlink(missing_ok=True)


def test_only_a_pass_writes_the_marker() -> None:
    """The marker is written after the last gate and guarded by `fail`, never before."""
    text = (ROOT / "scripts" / "check_task.sh").read_text()
    write = ': > "$marker"'
    assert text.count(write) == 1
    at = text.index(write)
    assert 'if [ "$fail" -eq 0 ]' in text[at - 80 : at]
    assert at > text.index("replay_guard.py"), "marker written before the last gate ran"
