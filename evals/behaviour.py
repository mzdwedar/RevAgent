"""Behaviour cases: what a real model does when it is told the same thing the worker tells it.

A gate case scripts the proposal and tests the harness (`runner.py`). A behaviour case
scripts nothing: it gives a trigger's frozen cohort, a stage and optionally some injected
context, runs the turn the worker runs (`draft_instruction` / `rollout_instruction`, the
real registry and admission), and grades what the engine proposed. The result is a rate,
not a gate, so nothing here blocks CI; the deterministic gates stay the release bar.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentstack.interfaces.wiring import ExperimentTurns
from agentstack.model.contract import ModelAsset, ModelRequest, ModelResponse, ToolCallProposal
from agentstack.model.engine import EchoEngine, ModelEngine
from agentstack.model.ollama_engine import OllamaEngine
from agentstack.policy.triggers import Outcome, TriggerKind
from agentstack.runtime import drafting
from agentstack.runtime.cycles import Cycle
from agentstack.runtime.drafting import (
    PROPOSAL_DEVIATES,
    PROPOSAL_OUT_OF_BOUNDS,
    draft_admission,
    draft_instruction,
    rollout_admission,
    rollout_instruction,
)
from agentstack.runtime.loop import run_turn
from agentstack.runtime.run import new_run
from agentstack.storage.database import Database
from agentstack.tools.experiments import DRAFT_STAGE, ROLLOUT, ROLLOUT_STAGE
from agentstack.tools.spec import Surface
from evals.runner import TENANT, USER, freeze, stack_with
from evals.stats import pass_at_1, pass_hat_k, wilson

BEHAVIOUR_DIR = Path(__file__).resolve().parent / "behaviour"

STAGES = (DRAFT_STAGE, ROLLOUT_STAGE)

# What became of a trial's proposal, read from the system's record of it.
ADMITTED = "admitted"
DEVIATES = "deviates"  # audited as proposal.deviates: well-formed, but not the record
OUT_OF_BOUNDS = "out_of_bounds"  # audited as proposal.out_of_bounds: a choice outside policy
MALFORMED = "malformed"  # refused by validation or exposure, before admission
WRONG_TOOL = "wrong_tool"
NO_PROPOSAL = "no_proposal"
OUTCOMES = (ADMITTED, DEVIATES, OUT_OF_BOUNDS, MALFORMED, WRONG_TOOL, NO_PROPOSAL)
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
    outcome: str = ""
    # For a deviation, the fields of the proposal that differed from the record.
    fields: list[str] = field(default_factory=list)


def deviating_fields(reasons: Sequence[str]) -> list[str]:
    """The fields a refusal says differed. The reasons are the system's own text
    (`rollout_deviation`, `draft_deviation`), not anything the model wrote."""
    found: list[str] = []
    for reason in reasons:
        match = re.search(r"it differs in ([\w, ]+?)\. A proposal", reason)
        if match:
            found.extend(name.strip() for name in match.group(1).split(","))
        elif "names experiment_version" in reason:
            found.append("experiment_version")
        elif "is not the experiment frozen cohort" in reason:
            found.append("resource")
    return found


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
    deviated = [
        r for r in stack.audit.for_run(run.run_id) if r.policy_decision == PROPOSAL_DEVIATES
    ]
    out_of_bounds = [
        r for r in stack.audit.for_run(run.run_id) if r.policy_decision == PROPOSAL_OUT_OF_BOUNDS
    ]
    fields: list[str] = []
    if proposal is None:
        outcome = NO_PROPOSAL
        failures.append("no proposal: the model proposed no tool call")
    elif proposal.tool != expect["tool"]:
        outcome = WRONG_TOOL
        failures.append(f"chose {proposal.tool}, expected {expect['tool']}")
    elif deviated:
        outcome = DEVIATES
        fields = deviating_fields(result.refusals)
    elif out_of_bounds:
        outcome = OUT_OF_BOUNDS
        fields = ["percentage" if proposal.tool == ROLLOUT.name else "offer"]
    elif result.refusals:
        outcome = MALFORMED
    else:
        outcome = ADMITTED
    if outcome in (DEVIATES, OUT_OF_BOUNDS, MALFORMED) and expect.get("admitted", True):
        reasons = result.refusals or ["it differs from the record"]
        failures.append(f"not admitted: {'; '.join(reasons)}")
    if proposal is not None and proposal.tool == expect["tool"]:
        for name in expect.get("non_empty", []):
            value = proposal.arguments.get(name)
            if not isinstance(value, str) or not value.strip():
                failures.append(f"{name} is missing or empty")
    return BehaviourOutcome(
        case=case,
        passed=not failures,
        failures=failures,
        seconds=time.perf_counter() - started,
        tool=None if proposal is None else proposal.tool,
        outcome=outcome,
        fields=fields,
    )


def engine_for(name: str, *, seed: int, temperature: float | None = None) -> ModelEngine:
    """`echo`, or `ollama:<model>` (the model name may itself contain a colon)."""
    if name == "echo":
        return EchoEngine()
    kind, _, model = name.partition(":")
    if kind == "ollama" and model:
        settings = {} if temperature is None else {"temperature": temperature}
        return OllamaEngine(model=model, seed=seed, **settings)
    raise ValueError(f"unknown engine {name!r}; use 'echo' or 'ollama:<model>'")


def prompt_hash(case: BehaviourCase) -> str:
    """What the model is shown, as far as it can be known without running a turn: the
    case, and the source of the functions that turn it into a prompt. A reworded
    instruction therefore invalidates the cache instead of being served from it."""
    shown = [
        drafting.draft_instruction,
        drafting.rollout_instruction,
        drafting.cohort_profile,
        drafting.intended_rollout,
    ]
    material = json.dumps(asdict(case), sort_keys=True) + "".join(
        inspect.getsource(f) for f in shown
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Trial:
    """One run of one case: one row of the JSONL log."""

    case: str
    engine: str
    model: str
    seed: int
    prompt_hash: str
    passed: bool
    failures: list[str]
    seconds: float
    # Rows logged before E3 carry neither.
    outcome: str = ""
    fields: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, int, str]:
        return (self.case, self.engine, self.seed, self.prompt_hash)


def read_trials(log: Path) -> list[Trial]:
    if not log.exists():
        return []
    return [Trial(**json.loads(line)) for line in log.read_text().splitlines() if line.strip()]


def run_trials(
    cases: Sequence[BehaviourCase],
    factory: Callable[[int], ModelEngine],
    *,
    engine: str,
    k: int,
    db: Database,
    checkpointer: Any,
    log: Path,
) -> list[Trial]:
    """Seeds 0..k-1 for every case, appending each trial to `log` as it finishes.

    A trial already in the log for the same (case, engine, seed, prompt hash) is not run
    again, so an interrupted run resumes and a longer k only pays for the new seeds.
    """
    done = {t.key: t for t in read_trials(log)}
    wanted: list[Trial] = []
    for case in cases:
        digest = prompt_hash(case)
        for seed in range(k):
            key = (case.id, engine, seed, digest)
            if key not in done:
                model = factory(seed)
                outcome = run_behaviour_case(case, db, checkpointer, model)
                done[key] = Trial(
                    case=case.id,
                    engine=engine,
                    model=model.asset.name,
                    seed=seed,
                    prompt_hash=digest,
                    passed=outcome.passed,
                    failures=outcome.failures,
                    seconds=round(outcome.seconds, 3),
                    outcome=outcome.outcome,
                    fields=outcome.fields,
                )
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open("a") as handle:
                    handle.write(json.dumps(asdict(done[key])) + "\n")
            wanted.append(done[key])
    return wanted


@dataclass(frozen=True)
class CaseSummary:
    trials: int
    pass_at_1: float
    pass_hat_k: bool


@dataclass(frozen=True)
class Summary:
    per_case: dict[str, CaseSummary]
    trials: int
    pass_at_1: float
    interval: tuple[float, float]
    # The share of cases every one of whose trials passed.
    pass_hat_k: float


def summarise(trials: Sequence[Trial]) -> Summary:
    by_case: dict[str, list[bool]] = {}
    for trial in trials:
        by_case.setdefault(trial.case, []).append(trial.passed)
    per_case = {
        case: CaseSummary(len(p), pass_at_1(p), pass_hat_k(p))
        for case, p in sorted(by_case.items())
    }
    passed = sum(trial.passed for trial in trials)
    return Summary(
        per_case=per_case,
        trials=len(trials),
        pass_at_1=passed / len(trials) if trials else 0.0,
        interval=wilson(passed, len(trials)),
        pass_hat_k=sum(c.pass_hat_k for c in per_case.values()) / len(per_case)
        if per_case
        else 0.0,
    )


def distribution(trials: Sequence[Trial]) -> dict[str, dict[str, int]]:
    """How many trials ended each way, per engine. Rows from before the outcome was
    recorded are left out rather than counted as something they were not."""
    counts: dict[str, dict[str, int]] = {}
    for trial in trials:
        if trial.outcome:
            per = counts.setdefault(trial.engine, {})
            per[trial.outcome] = per.get(trial.outcome, 0) + 1
    return counts
