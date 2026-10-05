"""The committed KKBox exploration is what the real files say (needs data/kkbox-raw).

Slow (minutes: it reads a 1.7 GB transaction file) and local only, like the rest of
`tests/live`. It fails, never skips, when the files are missing, with the command that
fixes it: a conditional skip would trip the floor.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import ModuleType

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _explore() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "explore_kkbox", ROOT / "scripts" / "explore_kkbox.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_report_is_what_the_real_files_say() -> None:
    explore = _explore()
    absent = [
        name
        for name in ("train_v2.csv", "transactions_v2.csv", "transactions.csv", "members_v3.csv")
        if not (explore.RAW / name).exists()
    ]
    if absent:
        pytest.fail(
            f"{absent} not found under {explore.RAW}. Fetch them: "
            "uv run python scripts/fetch_datasets.py --kkbox"
        )
    regenerated = explore.render(explore.explore(explore.RAW))
    committed = explore.REPORT.read_text()
    assert regenerated == committed, (
        "docs/evidence/kkbox-exploration.md no longer matches the data. Regenerate it with "
        "`uv run python scripts/explore_kkbox.py` and re-read kkbox-exploration-reading.md."
    )
