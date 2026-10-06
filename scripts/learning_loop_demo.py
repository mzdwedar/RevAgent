"""Two experiments in a row, the second one drafted with what the first taught (L8).

    bash scripts/dev_up.sh
    uv run python scripts/learning_loop_demo.py --out docs/evidence/learning_loop.txt

Experiment A runs on the real KKBox cohort (recorded TabPFN scores) through trigger,
draft, approval, rollout, outcomes, readout and conclusion. Its outcomes are SIMULATED
(ADR-0014): the effect table in `simulate_outcomes.py` plants a loser for the offer A's
draft picks, and the truth is printed beside what the readout measured. When A concludes
it leaves a learning in memory (L6). Experiment B then runs on the same tenant, and its
drafting turn is shown that learning (L7).

Two things here are stand-ins, and the transcript says so. The model is `LoopEngine`, a
scripted rule ("pick free-month with no history; with history, never repeat an offer that
lost"), not a language model: what is under test is that the recalled evidence reaches the
draft's context and that a draft can act on it, not that any model does. And time is
Temporal's time-skipping server, so the 30-day outcome window takes a moment.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from tests.temporal_support import (
    DraftingEngine,
    asker_for,
    progress_until,
    time_skipping,
    turns_for,
    worker_on,
)

from agentstack.context import datasets
from agentstack.context.assignment import Assignment
from agentstack.context.conclusion_store import ConclusionStore
from agentstack.context.frozen_cohorts import FrozenCohortStore
from agentstack.context.learnings import Learnings
from agentstack.context.outcomes import outcomes_path
from agentstack.context.targeting import TargetingRule, select
from agentstack.interfaces.wiring import ChannelAnnouncer, Stack, build_stack, deliver
from agentstack.model.contract import ModelRequest, ModelResponse, ToolCallProposal
from agentstack.policy.approvers import ApprovalReply
from agentstack.prediction.churn import RecordedScorer, RecordedScorers
from agentstack.runtime.drafting import ROLLOUT_PERCENTAGE
from agentstack.runtime.temporal.contracts import OUTCOME_WINDOW, RunProgress, workflow_id
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.storage.database import Database, IntegrityViolation
from agentstack.tools.experiments import VARIANTS

ROOT = Path(__file__).resolve().parents[1]
DATASET = "kkbox-churn"
TENANT = "acme"
PROFILE = "dev"
FIRST_OFFER = "free-month"  # what the scripted engine drafts with no history

_spec = importlib.util.spec_from_file_location("sim", ROOT / "scripts" / "simulate_outcomes.py")
assert _spec and _spec.loader
sim = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("sim", sim)
_spec.loader.exec_module(sim)

LEARNING = re.compile(
    r"\[learning\|first_party\|(?P<id>readout:[^\]]+)\].*?Offer tried: (?P<offer>[\w-]+) at "
    r".*?decision: (?P<action>\w+)",
    re.S,
)


class LoopEngine(DraftingEngine):
    """A scripted stand-in for the model. Never repeats an offer a recalled learning says
    lost (stop), and cites the readout it relied on; with no history it drafts FIRST_OFFER."""

    def __init__(self) -> None:
        super().__init__()
        self.drafting_contexts: list[str] = []

    def _answer(self, request: ModelRequest) -> ModelResponse:
        if "create_experiment_draft" not in request.tool_names:
            return super()._answer(request)
        context = request.rendered_context
        self.drafting_contexts.append(context)
        told = dict(token.split("=", 1) for token in context.split() if "=" in token)
        lost = [(m["offer"], m["id"]) for m in LEARNING.finditer(context) if m["action"] == "stop"]
        offer = next((v for v in VARIANTS if v not in {o for o, _ in lost}), FIRST_OFFER)
        if not lost:
            offer = FIRST_OFFER
            hypothesis = f"a {offer} has the strongest pull on the customers most at risk"
        else:
            hypothesis = (
                f"{offer} instead: {lost[0][0]} lost money in {lost[0][1]}, so a cheaper "
                "offer should retain these customers without costing more than it saves"
            )
        arguments = {k: told[k] for k in ("tenant", "experiment_id", "experiment_version")}
        arguments |= {"hypothesis": hypothesis, "variant": offer}
        return ModelResponse(
            text=f"drafted {offer}",
            proposals=(ToolCallProposal(tool="create_experiment_draft", arguments=arguments),),
        )


@dataclass
class LoopResult:
    transcript: list[str] = field(default_factory=list)
    readout_ids: dict[str, str] = field(default_factory=dict)  # experiment -> readout id
    offers: dict[str, str] = field(default_factory=dict)
    actions: dict[str, str] = field(default_factory=dict)
    contexts: dict[str, str] = field(default_factory=dict)  # experiment -> drafting context


def approve(stack: Stack, parked: RunProgress) -> None:
    with contextlib.suppress(IntegrityViolation):  # the second approval of the same run
        stack.approver_directory.add(
            tenant=TENANT, slack_user_id="UANA", principal="ana@acme", added_by="demo"
        )
    cycle = parked.cycles[0]
    stack.coordinator.apply(
        ApprovalReply(
            run_id=parked.run_id, wait_id=parked.awaiting_approval or "",
            experiment_version=cycle.experiment_version or "", data_as_of=cycle.data_as_of,
            approved=True, slack_user_id="UANA", channel="C1",
        )
    )  # fmt: skip


def _plant_outcomes(
    root: Path, experiment: str, version: str, offer: str, rollout_on: Any
) -> dict[str, Any]:
    snap = datasets.load(DATASET)
    cohort = select(snap, RecordedScorer.load(DATASET).scores, rule=TargetingRule.load(PROFILE))
    payload, truth = sim.simulate(
        snap.frame, cohort, Assignment(cohort, ROLLOUT_PERCENTAGE),
        offer=offer, seed=0, rollout_on=rollout_on,
    )  # fmt: skip
    path = outcomes_path(root, experiment, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return truth


async def _one(
    env: Any, stack: Stack, db: Database, experiment: str, engine: LoopEngine,
    root: Path, result: LoopResult,
) -> None:  # fmt: skip
    say = result.transcript.append
    snap = datasets.load(DATASET)
    handle = None
    say(f"\n=== experiment {experiment} ===")
    trigger = {
        "kind": "data_arrival", "experiment_id": experiment,
        "data_as_of": snap.data_as_of, "tenant": TENANT,
    }  # fmt: skip
    say(f"trigger  : {trigger}")
    run_id = await deliver(stack, env.client, trigger, source="learning-loop", task_queue="loop")
    handle = env.client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
    parked = await progress_until(handle, lambda p: p.awaiting_approval is not None, timeout=120)
    (cycle,) = parked.cycles
    version = cycle.experiment_version or ""
    frozen = FrozenCohortStore(db).get(
        tenant=TENANT, experiment_id=experiment, experiment_version=version
    )
    assert frozen is not None
    say(
        f"cohort   : {frozen.size} customers frozen as {version}, "
        f"scored by {frozen.targeting_model_version}"
    )
    draft = stack.registry_client.read(f"{TENANT}/experiments/{experiment}", {})
    offer = str(draft["variant"])
    result.offers[experiment] = offer
    context = engine.drafting_contexts[-1]
    start = context.find("Prior learnings for this tenant")
    recalled = ""
    if start >= 0:
        lines = context[start:].splitlines()
        recalled = "\n".join(
            [lines[0]] + [x for x in lines[1:] if x.startswith(("[learning|", "  Its hypothesis"))]
        )
    result.contexts[experiment] = context
    say("drafting context, prior learnings: " + (f"\n{recalled}" if recalled else "none"))
    say(f"draft    : variant={offer}\n           hypothesis={draft['hypothesis']!r}")

    approve(stack, parked)
    await handle.signal(ExperimentWorkflow.answered, parked.awaiting_approval)
    await progress_until(handle, lambda p: bool(p.answered))
    rollout_on = (await env.get_current_time()).date()
    say(f"approval : a person approved the {ROLLOUT_PERCENTAGE}% rollout; rolled out {rollout_on}")
    truth = _plant_outcomes(root, experiment, version, offer, rollout_on)
    say(
        f"outcomes : SIMULATED, planted lift {truth['lift']:.2f} for {offer}; true INSV "
        f"{truth['true_insv_cents']:.0f}c (the readout never sees this)"
    )

    for _ in range(3):  # an extend measures again after one more window
        seen = len((await handle.query(ExperimentWorkflow.progress)).conclusions)
        await env.sleep(OUTCOME_WINDOW + timedelta(hours=1))
        where = await progress_until(handle, lambda p, s=seen: len(p.conclusions) > s, timeout=120)
        if where.conclusions[-1].action != "extend":
            break
    final = where.conclusions[-1]
    stored = ConclusionStore(db).get(
        tenant=TENANT, experiment_id=experiment, experiment_version=version, endpoint=final.endpoint
    )
    assert stored is not None
    r = stored.readout
    say(
        f"readout  : SIMULATED, INSV {r['insv_cents']:.0f}c, 95% CI "
        f"[{r['insv_lower_cents']:.0f}c, {r['insv_upper_cents']:.0f}c], offer cost "
        f"{r['offer_cost_cents']}c per customer"
    )
    say(f"conclude : {stored.action.upper()} ({stored.reason})")
    result.actions[experiment] = stored.action
    result.readout_ids[experiment] = f"readout:{TENANT}/{experiment}/{version}/{final.endpoint}"
    if stored.action == "promote":
        parked = await progress_until(handle, lambda p: p.awaiting_approval is not None, timeout=60)
        approve(stack, parked)
        await handle.signal(ExperimentWorkflow.answered, parked.awaiting_approval)
        await progress_until(handle, lambda p: len(p.commits) == 2, timeout=60)
        say("promote  : a person approved the promotion; committed")
    else:
        await progress_until(handle, lambda p: len(p.turns) >= 3, timeout=60)
        say("stop     : the rollout was halted; nobody was asked")
    say(f"remembered: {sorted(r.key for r in stack.memory.all_records() if experiment in r.key)}")


async def run_loop(db: Database, checkpointer: Any, outcomes_root: Path) -> LoopResult:
    """Experiment A, then experiment B, on one tenant, one worker and one database."""
    stack = build_stack(db, checkpointer)
    engine = LoopEngine()
    result = LoopResult()
    options: dict[str, Any] = dict(
        scorer=RecordedScorers.load(), rule=TargetingRule.load(PROFILE),
        turns=turns_for(stack, engine), asker=asker_for(stack),
        conclusions=ConclusionStore(db), outcomes_root=outcomes_root,
        announcer=ChannelAnnouncer(stack.notifier), learnings=Learnings(db),
    )  # fmt: skip
    result.transcript.append(
        "Simulated outcomes (ADR-0014) and a scripted stand-in for the model; real KKBox cohort, "
        "recorded TabPFN scores, real workflow, approval, readout and memory."
    )
    async with time_skipping() as env, worker_on(env.client, "loop", db, **options):
        await _one(env, stack, db, "exp-a", engine, outcomes_root, result)
        await _one(env, stack, db, "exp-b", engine, outcomes_root, result)
    return result


def main() -> None:
    import os
    import tempfile

    from agentstack.storage import migrate
    from agentstack.storage.checkpoints import open_checkpointer
    from agentstack.storage.pool import DEV_DATABASE_URL, open_pool
    from agentstack.storage.provision import drop_database, rebuild_database, run_scoped

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    admin = os.environ.get("DATABASE_URL") or DEV_DATABASE_URL
    name = run_scoped("agentstack_learning_demo_test")
    url = rebuild_database(admin, name)
    try:
        with open_pool(url, min_size=1, max_size=8) as pool:
            migrate.apply(pool, ROOT / "migrations")
            checkpoints, saver = open_checkpointer(url)  # after the migration that makes its schema
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    result = asyncio.run(run_loop(Database(pool=pool), saver, Path(tmp)))
            finally:
                checkpoints.close()
    finally:
        drop_database(admin, name)
    text = "\n".join(result.transcript) + "\n"
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)


if __name__ == "__main__":
    main()
