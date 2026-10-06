"""L8: experiment B is drafted with what experiment A taught.

Real KKBox cohort and recorded scores, the real workflow, approval, readout and memory;
outcomes are simulated and the model is a scripted stand-in (see the script). Needs the
Kaggle files on disk, like the rest of `tests/live`, and the Temporal time-skipping server.

    uv run pytest tests/live/test_learning_loop.py -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

from agentstack.storage.database import Database

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "learning_loop_demo", ROOT / "scripts/learning_loop_demo.py"
)
assert _spec and _spec.loader
demo = importlib.util.module_from_spec(_spec)
sys.modules["learning_loop_demo"] = demo
_spec.loader.exec_module(demo)


def test_b_is_drafted_with_a_readout_a_produced(
    app_database: Database, checkpointer: Any, tmp_path: Path
) -> None:
    result = asyncio.run(demo.run_loop(app_database, checkpointer, tmp_path))

    a_id = result.readout_ids["exp-a"]
    assert a_id.startswith("readout:acme/exp-a/")
    assert a_id in result.contexts["exp-b"], "B's assembled context carries A's readout id"
    assert a_id not in result.contexts["exp-a"], "A had nothing to recall"
    assert result.actions["exp-a"] == "stop", "the planted effects make A's offer a loser"
    assert result.offers["exp-b"] != result.offers["exp-a"], "B chose differently"
