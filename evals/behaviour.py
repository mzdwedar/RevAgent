"""Behaviour cases: what a real model does when it is told the same thing the worker tells it.

A gate case scripts the proposal and tests the harness (`runner.py`). A behaviour case
scripts nothing: it gives a trigger's frozen cohort, a stage and optionally some injected
context, runs the turn the worker runs (`draft_instruction` / `rollout_instruction`, the
real registry and admission), and grades what the engine proposed. The result is a rate,
not a gate, so nothing here blocks CI; the deterministic gates stay the release bar.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentstack.interfaces.wiring import ExperimentTurns
from agentstack.model.contract import ModelAsset, ModelRequest, ModelResponse, ToolCallProposal
from agentstack.model.engine import ModelEngine
from agentstack.policy.triggers import Outcome, TriggerKind
from agentstack.runtime.cycles import Cycle
from agentstack.runtime.drafting import (
    PROPOSAL_DEVIATES,
    draft_admission,
    draft_instruction,
    rollout_admission,
    rollout_instruction,
)
from agentstack.runtime.loop import run_turn
from agentstack.runtime.run import new_run
from agentstack.storage.database import Database
from agentstack.tools.experiments import DRAFT_STAGE, ROLLOUT_STAGE
from agentstack.tools.spec import Surface
from evals.runner import TENANT, USER, freeze, stack_with

BEHAVIOUR_DIR = Path(__file__).resolve().parent / "behaviour"

STAGES = (DRAFT_STAGE, ROLLOUT_STAGE)
EXPECTATIONS = {"tool", "admitted", "non_empty"}
# The keys that would make a case a script. A proposal in the case is what a gate has.
SCRIPTED = ("message", "proposal", "proposals")
_FIELDS = {"id", "stage", "description", "cohort", "corpus", "expect"}


class BehaviourCaseInvalid(ValueError):
    """The file is not a behaviour case."""


@dataclass(frozen=True)
class BehaviourCase:
    id: str
    stage: str
    description: str
    cohort: dict[str, Any]
    expect: dict[str, Any]
    corpus: list[str] = field(default_factory=list)

    @staticmethod
    def of(raw: dict[str, Any], *, source: str = "case") -> BehaviourCase:
        scripted = [key for key in SCRIPTED if key in raw]
        if scripted:
            raise BehaviourCaseInvalid(
                f"{source}: {scripted} is a scripted proposal. A behaviour case gives the "
                "trigger and the context and grades what the model proposes; a case that "
                "carries the proposal is a gate case"
            )
        unknown = sorted(set(raw) - _FIELDS)
        if unknown:
            raise BehaviourCaseInvalid(f"{source}: not a behaviour case, unknown {unknown}")
        missing = sorted(_FIELDS - {"corpus"} - set(raw))
        if missing:
            raise BehaviourCaseInvalid(f"{source}: missing {missing}")
        if raw["stage"] not in STAGES:
            raise BehaviourCaseInvalid(f"{source}: stage {raw['stage']!r} is not one of {STAGES}")
        bad = sorted(set(raw["expect"]) - EXPECTATIONS)
        if bad or "tool" not in raw["expect"]:
            raise BehaviourCaseInvalid(
                f"{source}: expect must name a tool and use only {sorted(EXPECTATIONS)}; got {bad}"
            )
        return BehaviourCase(**raw)

    @staticmethod
    def load(path: Path) -> BehaviourCase:
        return BehaviourCase.of(json.loads(path.read_text()), source=path.name)


@dataclass
class BehaviourOutcome:
    case: BehaviourCase
    passed: bool
    failures: list[str]
    seconds: float
    tool: str | None = None


def load_behaviour_cases() -> list[BehaviourCase]:
    return sorted((BehaviourCase.load(p) for p in BEHAVIOUR_DIR.glob("*.json")), key=lambda c: c.id)


class _Recording:
    """Wraps an engine to keep what it proposed, which the turn's result does not."""

    def __init__(self, engine: ModelEngine) -> None:
        self._engine = engine
        self.proposals: list[ToolCallProposal] = []

    @property
    def asset(self) -> ModelAsset:
        return self._engine.asset

    def generate(self, request: ModelRequest) -> ModelResponse:
        response = self._engine.generate(request)
        self.proposals.extend(response.proposals)
        return response


def run_behaviour_case(
    case: BehaviourCase, db: Database, checkpointer: Any, engine: ModelEngine
) -> BehaviourOutcome:
    """One trial: the turn the worker takes, on `engine`, graded against the case."""
    started = time.perf_counter()
    stack = stack_with(case.corpus, db, checkpointer)
    recording = _Recording(engine)
    stack.deps.engine = recording
    cohort = freeze(db, case.cohort)
    session = stack.resolver.start(user_id=USER, tenant=TENANT)
    run = stack.runs.ensure(
        new_run(
            session_id=session.session_id,
            tenant=TENANT,
            user=USER,
            stage=case.stage,
            channel="eval",
        )
    )
    envelope = ExperimentTurns(stack).envelope(run)
    if case.stage == DRAFT_STAGE:
        now = datetime.now(UTC)
        cycle = Cycle(
            experiment_id=cohort.experiment_id,
            data_as_of=cohort.data_as_of,
            kind=TriggerKind.DATA_ARRIVAL,
            outcome=Outcome.PROPOSE,
            run_id=cohort.experiment_version,
            claimed_at=now,
            settled_at=now,
        )
        message = draft_instruction(tenant=TENANT, cycle=cycle, cohort=cohort)
        admit = draft_admission(
            cohort=cohort, audit=stack.audit, run=run, principal=envelope.principal
        )
    else:
        history = stack.deps.gateway.surfaces[Surface.REGISTRY].read(
            f"{TENANT}/experiments/{cohort.experiment_id}/history", {}
        )
        prior = int(history.get("latest_rollout_event", 0))
        message = rollout_instruction(
            experiment_id=cohort.experiment_id, cohort=cohort, prior_rollout_event=prior
        )
        admit = rollout_admission(
            cohort=cohort,
            audit=stack.audit,
            run=run,
            principal=envelope.principal,
            prior_rollout_event=prior,
        )
    result = run_turn(run=run, envelope=envelope, message=message, deps=stack.deps, admit=admit)

    failures: list[str] = []
    proposal = recording.proposals[0] if recording.proposals else None
    expect = case.expect
    if proposal is None:
        failures.append("no proposal: the model proposed no tool call")
    elif proposal.tool != expect["tool"]:
        failures.append(f"chose {proposal.tool}, expected {expect['tool']}")
    else:
        for name in expect.get("non_empty", []):
            value = proposal.arguments.get(name)
            if not isinstance(value, str) or not value.strip():
                failures.append(f"{name} is missing or empty")
    if proposal is not None and expect.get("admitted", True):
        deviated = [
            r for r in stack.audit.for_run(run.run_id) if r.policy_decision == PROPOSAL_DEVIATES
        ]
        if deviated or result.refusals:
            reasons = result.refusals or ["it differs from the record"]
            failures.append(f"not admitted: {'; '.join(reasons)}")
    return BehaviourOutcome(
        case=case,
        passed=not failures,
        failures=failures,
        seconds=time.perf_counter() - started,
        tool=None if proposal is None else proposal.tool,
    )
