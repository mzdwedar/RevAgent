"""Named execution surfaces, and the containment around them (Part 7).

Do not reason about execution in the abstract. The same `call_api` label over a public
weather endpoint and over a production database are different risk classes, and only
the surface name distinguishes them.

**This is the only package permitted to import a real client library** - HTTP, DB,
shell, browser, mailer. `.importlinter` contract 3 enforces that; if you find yourself
adding `import httpx` anywhere else, the layer boundary is the thing that is wrong.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentstack.storage.database import Database, IntegrityViolation
from agentstack.tools.spec import Surface


class SandboxViolation(PermissionError):
    """The action was permitted, but it reached outside its containment."""


class SurfaceTimeout(RuntimeError):
    """The surface did not answer. Whether the effect applied is unknown."""


class SurfaceRefused(RuntimeError):
    """The surface answered, and the answer is that nothing applied.

    The opposite of `SurfaceTimeout`, and only a surface that can prove it may raise it:
    a guarded write that matched no row, a precondition checked in the same statement
    as the change. "The resource is not in the state this action needs" - a draft
    already discarded, an experiment no longer live - is this, not an error.

    The gateway releases the idempotency claim for it. Raising it for an effect that
    *might* have applied would turn an unknown outcome into a free slot, which is the
    double-refund the two-phase ledger exists to prevent.
    """


class SurfaceClient(Protocol):
    """A real thing you can ask about, and a real thing you can change.

    Two verbs, deliberately. One verb would force `commit` to branch GET-vs-POST off
    a resource string assembled from model-supplied arguments, and would leave a read
    with no way to return what it read - which is how tool functions start fetching
    things themselves.
    """

    def read(self, resource: str, query: dict[str, Any]) -> dict[str, Any]: ...

    def commit(self, resource: str, payload: dict[str, Any]) -> str: ...


@dataclass(slots=True)
class RecordingClient:
    """Reference client.

    Commits and reads are counted separately so a duplicated side effect is visible
    in tests and a cached read cannot hide behind the same number.
    """

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    reads: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail_after_effect: bool = False
    # Refuse before acting: the precondition did not hold, and nothing applied.
    refuse_before_effect: bool = False

    def read(self, resource: str, query: dict[str, Any]) -> dict[str, Any]:
        self.reads.append((resource, dict(query)))
        return {"resource": resource, "read_index": len(self.reads)}

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        if self.refuse_before_effect:
            raise SurfaceRefused(f"{resource}: refused, nothing applied")
        self.calls.append((resource, dict(payload)))
        if self.fail_after_effect:
            # The effect applied; the answer never came back. This is the failure the
            # whole idempotency design exists for, and a client that cannot produce it
            # makes every test about it vacuous.
            raise SurfaceTimeout(f"{resource}: applied, but the response was lost")
        return f"receipt-{len(self.calls)}"


@dataclass(frozen=True, slots=True)
class Sandbox:
    """Blast-radius limits, declared per dimension.

    Containment with no limit is theater - but so is a limit nothing reads. Every
    field here is enforced by `check()`, and `tests/fitness/test_containment.py`
    fails if a field is ever added without being enforced.

    Egress and filesystem containment are deliberately absent rather than declared
    and ignored: there is no real client to bound yet, and a configured allowlist
    that nothing consults stops the next reviewer from asking. They land with the
    first real surface client (ADR-0004).
    """

    tenant: str
    allowed_surfaces: frozenset[Surface]
    allowed_resource_prefixes: frozenset[str]

    def check(self, *, surface: Surface, resource: str) -> None:
        if surface not in self.allowed_surfaces:
            raise SandboxViolation(f"{surface.value} is outside this run's containment")
        if not any(resource.startswith(p) for p in self.allowed_resource_prefixes):
            raise SandboxViolation(f"{resource} is outside this run's allowed resources")
        if not resource.startswith(f"{self.tenant}/"):
            raise SandboxViolation(f"{resource} crosses the tenant boundary")


# --- the experiment registry ---
#
# A closed table of resources, each served by the verbs listed and no other. Anything
# else is refused rather than guessed at:
#
#   {tenant}/experiments/               read    the tenant's experiments, bounded
#   {tenant}/experiments/{id}           read    the experiment, at its current version
#                                       commit  create the draft
#   {tenant}/experiments/{id}/history   read    rollouts and halts, oldest first
#   {tenant}/experiments/{id}/rollout   commit  expose the current version to a percentage
#   {tenant}/experiments/{id}/revision  commit  reword a draft's hypothesis
#   {tenant}/experiments/{id}/discard   commit  a draft becomes discarded
#   {tenant}/experiments/{id}/abstention commit record why nothing was done; no status moves
#   {tenant}/experiments/{id}/halt      commit  a live experiment's exposure goes to zero
#
# Both clients below hold the same preconditions, and `tests/infra/test_registry_store.py`
# runs one contract suite over both. A precondition that does not hold is
# `SurfaceRefused`: the resource was not in the state the action needs, and nothing
# applied. That is not schema validation - the validator has already passed the
# arguments - it is "this resource, now", which only the surface can answer.
#
# A rollout, a revision and an abstention each name the state they move from (audit
# C2): the rollout event it follows, the revision it rewords, the last event it read.
# That is a compare-and-set - the named state must still be the latest, in the same
# statement as the write - so of two writes prepared against one state exactly one
# lands. When a write matches nothing and *this exact move* is already on record, that
# is the effect having happened, not a refusal: the surface answers with its receipt
# (audit M4). Anything else that matched nothing is `SurfaceRefused`.

_REGISTRY_RESOURCE = re.compile(
    r"^(?P<tenant>[^/]+)/experiments/(?:(?P<experiment>[^/]+)(?:/(?P<suffix>[a-z_]+))?)?$"
)

# Per verb, the action each suffix names. A resource valid for one verb is not thereby
# valid for the other: history is read and never written, a rollout written and never
# read. The collection (no experiment) is the list read and nothing else.
_READS: dict[str | None, str] = {None: "experiment", "history": "history"}
_COMMITS: dict[str | None, str] = {
    None: "draft",
    "rollout": "rollout",
    "revision": "revise",
    "discard": "discard",
    "abstention": "abstention",
    "halt": "halt",
}

# Every state an experiment can be in. An abstention is a record, not a transition
# (assumption 7), so it may be appended in any of them and moves none.
_ANY_STATE = ("draft", "live", "halted", "discarded")

# Each status change: the states it may start from, and the state it leaves. A rollout
# may widen a live experiment or launch a draft; only a draft may be discarded; only a
# live experiment may be halted. From any other state the action is refused. `halted`
# and `discarded` are terminal for a version in iteration 1 (SPEC-registry.md).
_TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "rollout": (("draft", "live"), "live"),
    "discard": (("draft",), "discarded"),
    "halt": (("live",), "halted"),
}

# The constraints a revision can meet, and what each means. The statement failed whole,
# so each is provably nothing applied - a refusal, not an unknown outcome.
_REVISION_REFUSALS = {
    # The key is (version, revision_no), and revision_no is the prior plus one: two
    # revisions of the same revision collide here (migrations/0015).
    "draft_revisions_pkey": "another revision of the same revision landed first",
    "revision_says_something": "a revision must say something",
}

# The same, for events. Each index is one "from here, once" (migrations/0015).
_EVENT_REFUSALS = {
    "one_rollout_per_prior": "another rollout from the same state landed first",
    "one_abstention_per_prior": "another abstention against the same record landed first",
    "one_ending_per_version": "this version has already ended",
}

# The argument each move names its prior state by, and where the surface reads it.
_PRIOR_OF = {"rollout": "prior_rollout_event", "abstention": "prior_event"}

# One read cannot flood the context window (SPEC-registry.md, Budgets). The tool schema
# refuses more; the surface clamps anyway, because the schema is not its only caller.
LIST_LIMIT = 50
LIST_DEFAULT = 20

# The events that change exposure. Discards and abstentions are history too, but they
# never put a variant in front of anyone, so they are not rollout history.
_EXPOSURE_KINDS = ("rollout", "halt")


def _registry_resource(resource: str, verb: dict[str | None, str]) -> tuple[str, str, str]:
    """(tenant, experiment, action) for this verb, or a refusal. The collection has no
    experiment, and only the list read serves it."""
    match = _REGISTRY_RESOURCE.match(resource)
    if match is not None and match["experiment"] is None and verb is _READS:
        return match["tenant"], "", "list"
    if match is None or match["experiment"] is None or match["suffix"] not in verb:
        raise SurfaceRefused(f"{resource}: not a resource the experiment registry serves")
    return match["tenant"], match["experiment"], verb[match["suffix"]]


def resource_verb(surface: Surface, resource: str, *, writes: bool) -> str | None:
    """The act `surface` would perform for `resource`, or None if it names none.

    The gateway asks this before policy, so a request is held to the verb its spec
    declares (`ToolSpec.verb`) by the *same* parse the client will use - two parsers
    would be two opinions about what a resource means, and the gap between them is where
    a draft becomes a rollout. Only the registry reads its act from the resource; other
    surfaces name none, and a spec on them declares none.
    """
    if surface is not Surface.REGISTRY:
        return None
    try:
        return _registry_resource(resource, _COMMITS if writes else _READS)[2]
    except SurfaceRefused:
        return None


# --- what each act's payload must be ---
#
# The resource says which act; the payload says what it is done with. The schema
# validator holds the model's proposals to shape, but the surface is not only reached
# through a validated proposal - a request built any other way, an operator's script,
# the next client - and a rollout with no percentage is a row `get_rollout_history`
# cannot read (C1). So the surface holds each act to its own shape, exactly: a key
# missing, a key extra or a value of the wrong kind is `SurfaceRefused`, and nothing
# applied. `migrations/0014` holds the percentages again in the store.


def _text(value: Any) -> bool:
    """A string that says something - the store's `revision_says_something`."""
    return isinstance(value, str) and bool(value.strip())


def _string(value: Any) -> bool:
    return isinstance(value, str)


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _percentage(value: Any) -> bool:
    """A whole percentage, 0 to 100. `True` is an int in Python and is not one."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100


def _zero(value: Any) -> bool:
    return _percentage(value) and value == 0


def _position(value: Any) -> bool:
    """A place in the registry's history a move starts from (C2): 0 is "before any"."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _revision(value: Any) -> bool:
    """A revision a rewording starts from. A draft is born at revision 1."""
    return _position(value) and value >= 1


_SHAPES: dict[str, dict[str, Callable[[Any], bool]]] = {
    "draft": {"experiment_version": _text, "hypothesis": _text, "variant": _text},
    "rollout": {
        "experiment_version": _text,
        "percentage": _percentage,
        "targeting_model_version": _text,
        "risk_threshold": _number,
        "prior_rollout_event": _position,
    },
    # A blank hypothesis is a string here and refused below with its own reason, the
    # same one the store's CHECK gives.
    "revise": {"experiment_version": _text, "hypothesis": _string, "prior_revision": _revision},
    "discard": {"experiment_version": _text, "reason": _string},
    "abstention": {"experiment_version": _text, "explanation": _string, "prior_event": _position},
    "halt": {"experiment_version": _text, "percentage": _zero, "reason": _string},
}


def _shaped(resource: str, action: str, payload: dict[str, Any]) -> None:
    """Refuse a payload that is not exactly this act's shape. Checked before any state
    is read or written, so a refusal here provably applied nothing."""
    shape = _SHAPES[action]
    if set(payload) != set(shape):
        raise SurfaceRefused(
            f"{resource}: a {action} carries exactly {sorted(shape)}, "
            f"not {sorted(payload)}; nothing applied"
        )
    wrong = sorted(key for key, holds in shape.items() if not holds(payload[key]))
    if wrong:
        raise SurfaceRefused(
            f"{resource}: a {action} with this {wrong} is malformed; nothing applied"
        )


def _list_limit(query: dict[str, Any]) -> int:
    return max(1, min(int(query.get("limit", LIST_DEFAULT)), LIST_LIMIT))


def _history(experiment: str, events: list[tuple[int, str, dict[str, Any]]]) -> dict[str, Any]:
    """Every event this experiment has, oldest first, as (id, kind, payload).

    Current exposure is derived from the latest rollout or halt, never stored as a
    second truth beside the events it would have to agree with. The two positions are
    what a next move names as its prior: `latest_rollout_event` for a rollout,
    `latest_event` for an abstention - which is why abstentions and discards, not listed
    as rollout history, still move the second.
    """
    exposure = [(i, kind, p) for i, kind, p in events if kind in _EXPOSURE_KINDS]
    return {
        "experiment_id": experiment,
        "events": [{"event_id": i, "kind": kind, **p} for i, kind, p in exposure],
        "current_exposure": exposure[-1][2]["percentage"] if exposure else 0,
        "latest_rollout_event": max((i for i, k, _ in events if k == "rollout"), default=0),
        "latest_event": max((i for i, _, _ in events), default=0),
    }


@dataclass(slots=True)
class RegistryClient:
    """The experiment registry, in process.

    The fake the fitness suite can inspect without a database, held to the same
    contract as `PostgresRegistryClient`. It does not validate *arguments* - that is the
    schema validator's job, and criterion 19 is that nothing malformed gets this far on
    the proposal path. It does hold each act's payload to its shape (`_shaped`), because
    the proposal path is not the only one that reaches a surface, and it holds the state
    preconditions, because those are the surface's to answer: a fake that accepted a
    rollout of nothing would let a test pass that production fails.
    """

    drafts: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Every effect after the draft, in order: (experiment resource, kind, payload) - the
    # fake's `registry_events`.
    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    # Each experiment's hypotheses, in revision order. `drafts` keeps what was drafted.
    revisions: dict[str, list[str]] = field(default_factory=dict)
    reads: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # Apply the effect and then lose the answer: the failure two-phase idempotency
    # exists for. Same switch as RecordingClient, for the same reason.
    fail_after_effect: bool = False
    _status: dict[str, str] = field(default_factory=dict)

    @property
    def rollouts(self) -> list[tuple[str, dict[str, Any]]]:
        return [(f"{base}/rollout", p) for base, kind, p in self.events if kind == "rollout"]

    def read(self, resource: str, query: dict[str, Any]) -> dict[str, Any]:
        tenant, experiment, action = _registry_resource(resource, _READS)
        self.reads.append((resource, dict(query)))
        if action == "list":
            return self._list(tenant, query)
        base = f"{tenant}/experiments/{experiment}"
        draft = self.drafts.get(base)
        if draft is None:
            return {}
        if action == "history":
            return _history(experiment, self._events_of(base))
        return {
            "experiment_id": experiment,
            "status": self._status[base],
            **draft,
            "hypothesis": self.revisions[base][-1],
            "revision_no": len(self.revisions[base]),
        }

    def _events_of(self, base: str) -> list[tuple[int, str, dict[str, Any]]]:
        """This experiment's events with their ids. An event's id is its position in the
        whole registry, counted from 1, as a `bigserial` counts."""
        return [(n, k, p) for n, (b, k, p) in enumerate(self.events, start=1) if b == base]

    def _list(self, tenant: str, query: dict[str, Any]) -> dict[str, Any]:
        prefix = f"{tenant}/experiments/"
        rows = [
            {
                "experiment_id": base.removeprefix(prefix),
                "status": self._status[base],
                "experiment_version": draft["experiment_version"],
            }
            for base, draft in sorted(self.drafts.items())
            if base.startswith(prefix) and query.get("status") in (None, self._status[base])
        ]
        return {"experiments": rows[: _list_limit(query)]}

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        tenant, experiment, action = _registry_resource(resource, _COMMITS)
        _shaped(resource, action, payload)
        base = f"{tenant}/experiments/{experiment}"
        if action == "draft":
            if base in self.drafts:
                raise SurfaceRefused(f"{base} already exists; a draft never overwrites one")
            self.drafts[base] = dict(payload)
            self.revisions[base] = [payload["hypothesis"]]
            self._status[base] = "draft"
            receipt = f"draft-{len(self.drafts)}"
        elif action == "revise":
            if not payload["hypothesis"].strip():
                raise SurfaceRefused(f"{base}: a revision must say something")
            try:
                self._current(base, payload, ("draft",))
                if len(self.revisions[base]) != payload["prior_revision"]:
                    raise SurfaceRefused(
                        f"{base} is past revision {payload['prior_revision']}; nothing was revised"
                    )
            except SurfaceRefused:
                return self._already_revised(base, payload)
            self.revisions[base].append(payload["hypothesis"])
            receipt = f"revision-{len(self.revisions[base])}"
        elif action in _PRIOR_OF:
            # A rollout moves status and an abstention moves none; both are moves from a
            # named prior, and both are the same compare-and-set.
            allowed, becomes = _TRANSITIONS.get(action, (_ANY_STATE, ""))
            try:
                self._current(base, payload, allowed)
                self._still_latest(base, action, payload)
            except SurfaceRefused:
                return self._already_moved(base, action, payload)
            self.events.append((base, action, dict(payload)))
            self._status[base] = becomes or self._status[base]
            receipt = f"{action}-{len(self.events)}"
        else:
            allowed, becomes = _TRANSITIONS[action]
            self._current(base, payload, allowed)
            if any(b == base and k == action for b, k, _ in self.events):
                raise SurfaceRefused(f"{resource}: this version has already ended")
            self.events.append((base, action, dict(payload)))
            self._status[base] = becomes
            receipt = f"{action}-{len(self.events)}"
        if self.fail_after_effect:
            raise SurfaceTimeout(f"{resource} applied, answer lost")
        return receipt

    def _current(self, base: str, payload: dict[str, Any], allowed: tuple[str, ...]) -> None:
        """The same `WHERE` the Postgres statements carry: this version, in these states."""
        draft = self.drafts.get(base)
        if draft is None or draft["experiment_version"] != payload["experiment_version"]:
            raise SurfaceRefused(f"{base} has no current version {payload['experiment_version']}")
        if self._status[base] not in allowed:
            raise SurfaceRefused(f"{base} is {self._status[base]}; this action needs {allowed}")

    def _still_latest(self, base: str, kind: str, payload: dict[str, Any]) -> None:
        """The compare in compare-and-set: the prior this move names is still the latest.
        A rollout follows rollouts; an abstention follows whatever was recorded last."""
        prior = payload[_PRIOR_OF[kind]]
        latest = max(
            (n for n, k, _ in self._events_of(base) if kind != "rollout" or k == "rollout"),
            default=0,
        )
        if latest != prior:
            raise SurfaceRefused(
                f"{base} has moved on from {prior} (now {latest}); nothing applied"
            )

    def _already_moved(self, base: str, kind: str, payload: dict[str, Any]) -> str:
        """Matched nothing: if this exact move from this exact state is on record, it
        happened, and its receipt is the answer. Otherwise nothing applied."""
        for n, k, p in self._events_of(base):
            if k == kind and p == payload:
                return f"{kind}-{n}"
        raise SurfaceRefused(
            f"{base}: this {kind} does not follow the latest state, and nothing applied"
        )

    def _already_revised(self, base: str, payload: dict[str, Any]) -> str:
        """The revision's own prior is `revision_no - 1`, so the exact move is a wording
        at `prior + 1`."""
        wordings = self.revisions.get(base, [])
        draft = self.drafts.get(base, {})
        at = payload["prior_revision"]
        if (
            draft.get("experiment_version") == payload["experiment_version"]
            and 0 < at < len(wordings)
            and wordings[at] == payload["hypothesis"]
        ):
            return f"revision-{at + 1}"
        raise SurfaceRefused(
            f"{base} is not a draft at revision {at} of version "
            f"{payload['experiment_version']}; nothing was revised"
        )


@dataclass(frozen=True, slots=True)
class PostgresRegistryClient:
    """The experiment registry, on the tables `migrations/0012` created and `0015` re-keyed.

    Every write is **one statement**: the precondition is the `WHERE` of the same
    statement that changes the row, so "checked" and "changed" are the same instant, and
    a statement that matched nothing provably applied nothing - the one case the gateway
    may release a claim for. `Database` has no transaction scope, deliberately, and none
    is needed.

    `drafts` and `rollouts` answer what reached the registry, in the fake's shape, so a
    test asks the same question of either client.
    """

    db: Database

    @property
    def drafts(self) -> dict[str, dict[str, Any]]:
        rows = self.db.fetch_all(
            "SELECT e.tenant, e.experiment_id, v.experiment_version, r.hypothesis, v.variant"
            " FROM experiments e"
            " JOIN experiment_versions v USING (tenant, experiment_id)"
            " JOIN draft_revisions r USING (tenant, experiment_id, experiment_version)"
            " WHERE v.experiment_version = e.current_version AND r.revision_no = 1"
        )
        return {
            f"{t}/experiments/{e}": {"experiment_version": v, "hypothesis": h, "variant": var}
            for t, e, v, h, var in rows
        }

    @property
    def rollouts(self) -> list[tuple[str, dict[str, Any]]]:
        rows = self.db.fetch_all(
            "SELECT tenant, experiment_id, payload FROM registry_events"
            " WHERE kind = 'rollout' ORDER BY id"
        )
        return [(f"{t}/experiments/{e}/rollout", dict(p)) for t, e, p in rows]

    def read(self, resource: str, query: dict[str, Any]) -> dict[str, Any]:
        tenant, experiment, action = _registry_resource(resource, _READS)
        if action == "list":
            return self._list(tenant, query)
        if action == "history":
            return self._history(tenant, experiment)
        row = self.db.fetch_one(
            "SELECT e.status, e.current_version, v.variant, r.hypothesis, r.revision_no"
            " FROM experiments e"
            " JOIN experiment_versions v"
            "   ON (v.tenant, v.experiment_id, v.experiment_version)"
            "    = (e.tenant, e.experiment_id, e.current_version)"
            " JOIN LATERAL (SELECT hypothesis, revision_no FROM draft_revisions r"
            "   WHERE (r.tenant, r.experiment_id, r.experiment_version)"
            "       = (e.tenant, e.experiment_id, e.current_version)"
            "   ORDER BY r.revision_no DESC LIMIT 1) r ON true"
            " WHERE e.tenant = %s AND e.experiment_id = %s",
            (tenant, experiment),
        )
        if row is None:
            return {}
        status, version, variant, hypothesis, revision_no = row
        return {
            "experiment_id": experiment,
            "status": status,
            "experiment_version": version,
            "hypothesis": hypothesis,
            "variant": variant,
            "revision_no": revision_no,
        }

    def _list(self, tenant: str, query: dict[str, Any]) -> dict[str, Any]:
        status = query.get("status")
        rows = self.db.fetch_all(
            "SELECT experiment_id, status, current_version FROM experiments"
            " WHERE tenant = %s AND (%s::text IS NULL OR status = %s)"
            " ORDER BY experiment_id LIMIT %s",
            (tenant, status, status, _list_limit(query)),
        )
        return {
            "experiments": [
                {"experiment_id": e, "status": s, "experiment_version": v} for e, s, v in rows
            ]
        }

    def _history(self, tenant: str, experiment: str) -> dict[str, Any]:
        if (
            self.db.fetch_one(
                "SELECT 1 FROM experiments WHERE tenant = %s AND experiment_id = %s",
                (tenant, experiment),
            )
            is None
        ):
            return {}
        # Every kind, because `latest_event` counts abstentions and discards too; the
        # rollout history itself is still only what changed exposure.
        rows = self.db.fetch_all(
            "SELECT id, kind, payload FROM registry_events"
            " WHERE tenant = %s AND experiment_id = %s ORDER BY id",
            (tenant, experiment),
        )
        return _history(experiment, [(int(i), kind, dict(p)) for i, kind, p in rows])

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        tenant, experiment, action = _registry_resource(resource, _COMMITS)
        _shaped(resource, action, payload)
        if action == "draft":
            return self._draft(tenant, experiment, payload)
        if action == "revise":
            return self._revise(tenant, experiment, payload)
        if action == "abstention":
            return self._abstain(tenant, experiment, payload)
        if action == "rollout":
            return self._rollout(tenant, experiment, payload)
        return self._transition(tenant, experiment, action, payload)

    def _move(
        self,
        tenant: str,
        experiment: str,
        kind: str,
        payload: dict[str, Any],
        statement: tuple[str, tuple[Any, ...]],
    ) -> str:
        """Run one move's statement. If it matched nothing, or collided with a move from
        the same prior, the exact move already on record is the answer (audit M4) - the
        prior is in the payload, so "the same payload" is "the same move from the same
        state". Anything else is a refusal: the statement failed whole, nothing applied.

        The lookup is a read after the write, not a second write: history is
        insert-only, so a row it finds cannot stop being true.
        """
        try:
            row = self.db.fetch_one(*statement)
        except IntegrityViolation as exc:
            if exc.constraint not in _EVENT_REFUSALS:
                raise
            row = None
        if row is not None:
            return f"{kind}-{row[0]}"
        existing = self.db.fetch_one(
            "SELECT id FROM registry_events"
            " WHERE tenant = %s AND experiment_id = %s AND kind = %s AND payload = %s::jsonb"
            " ORDER BY id LIMIT 1",
            (tenant, experiment, kind, json.dumps(payload)),
        )
        if existing is not None:
            return f"{kind}-{existing[0]}"
        raise SurfaceRefused(
            f"{tenant}/experiments/{experiment}: this {kind} does not follow the latest "
            "state, or the experiment is not in a state it can move from; nothing applied"
        )

    def _abstain(self, tenant: str, experiment: str, payload: dict[str, Any]) -> str:
        """Append the explanation to the named version, whatever its state, if nothing has
        been recorded since the event the evaluation read. No `UPDATE`: a run stops
        because the policy said abstain, not because a row was written."""
        prior = payload["prior_event"]
        return self._move(
            tenant,
            experiment,
            "abstention",
            payload,
            (
                "INSERT INTO registry_events"
                "  (tenant, experiment_id, experiment_version, kind, follows, payload)"
                " SELECT v.tenant, v.experiment_id, v.experiment_version, 'abstention',"
                "  %s, %s::jsonb"
                " FROM experiment_versions v"
                " WHERE v.tenant = %s AND v.experiment_id = %s AND v.experiment_version = %s"
                "   AND %s = (SELECT coalesce(max(r.id), 0) FROM registry_events r"
                "     WHERE (r.tenant, r.experiment_id) = (v.tenant, v.experiment_id))"
                " RETURNING id",
                (
                    prior,
                    json.dumps(payload),
                    tenant,
                    experiment,
                    payload["experiment_version"],
                    prior,
                ),
            ),
        )

    def _rollout(self, tenant: str, experiment: str, payload: dict[str, Any]) -> str:
        """The rollout is `_transition` with one more clause in the same `WHERE`: the
        rollout it names as its prior is still the latest. Two rollouts approved against
        one state can both pass that under read committed - neither sees the other's
        uncommitted event - and `one_rollout_per_prior` refuses the second."""
        allowed, becomes = _TRANSITIONS["rollout"]
        prior = payload["prior_rollout_event"]
        return self._move(
            tenant,
            experiment,
            "rollout",
            payload,
            (
                "WITH moved AS ("
                "  UPDATE experiments e SET status = %s, updated_at = now()"
                "  WHERE e.tenant = %s AND e.experiment_id = %s"
                "    AND e.current_version = %s AND e.status = ANY(%s)"
                "    AND %s = (SELECT coalesce(max(r.id), 0) FROM registry_events r"
                "      WHERE (r.tenant, r.experiment_id) = (e.tenant, e.experiment_id)"
                "        AND r.kind = 'rollout')"
                "  RETURNING tenant, experiment_id, current_version)"
                " INSERT INTO registry_events"
                "  (tenant, experiment_id, experiment_version, kind, follows, payload)"
                " SELECT tenant, experiment_id, current_version, 'rollout', %s, %s::jsonb"
                " FROM moved RETURNING id",
                (
                    becomes,
                    tenant,
                    experiment,
                    payload["experiment_version"],
                    list(allowed),
                    prior,
                    prior,
                    json.dumps(payload),
                ),
            ),
        )

    def _draft(self, tenant: str, experiment: str, payload: dict[str, Any]) -> str:
        # `ON CONFLICT DO NOTHING` on the experiment, and the version and revision are
        # selected from what that insert returned: an experiment that already exists
        # yields no row, so neither does anything after it.
        row = self.db.fetch_one(
            "WITH e AS ("
            "  INSERT INTO experiments (tenant, experiment_id, status, current_version)"
            "  VALUES (%s, %s, 'draft', %s)"
            "  ON CONFLICT (tenant, experiment_id) DO NOTHING"
            "  RETURNING tenant, experiment_id, current_version),"
            " v AS ("
            "  INSERT INTO experiment_versions (tenant, experiment_id, experiment_version, variant)"
            "  SELECT tenant, experiment_id, current_version, %s FROM e"
            "  RETURNING tenant, experiment_id, experiment_version)"
            " INSERT INTO draft_revisions"
            "  (tenant, experiment_id, experiment_version, revision_no, hypothesis)"
            " SELECT tenant, experiment_id, experiment_version, 1, %s FROM v"
            " RETURNING experiment_version",
            (
                tenant,
                experiment,
                payload["experiment_version"],
                payload["variant"],
                payload["hypothesis"],
            ),
        )
        if row is None:
            raise SurfaceRefused(
                f"{tenant}/experiments/{experiment} already exists; a draft never overwrites one"
            )
        return f"draft:{tenant}/{experiment}@{row[0]}"

    def _transition(self, tenant: str, experiment: str, kind: str, payload: dict[str, Any]) -> str:
        """A status change and the event that records it, as one statement: the guarded
        `UPDATE` feeds the `INSERT`, so no event is written for a move that did not
        happen and no move happens without its event."""
        allowed, becomes = _TRANSITIONS[kind]
        try:
            row = self.db.fetch_one(
                "WITH moved AS ("
                "  UPDATE experiments SET status = %s, updated_at = now()"
                "  WHERE tenant = %s AND experiment_id = %s"
                "    AND current_version = %s AND status = ANY(%s)"
                "  RETURNING tenant, experiment_id, current_version)"
                " INSERT INTO registry_events"
                "  (tenant, experiment_id, experiment_version, kind, payload)"
                " SELECT tenant, experiment_id, current_version, %s, %s::jsonb"
                " FROM moved RETURNING id",
                (
                    becomes,
                    tenant,
                    experiment,
                    payload["experiment_version"],
                    list(allowed),
                    kind,
                    json.dumps(payload),
                ),
            )
        except IntegrityViolation as exc:
            if exc.constraint != "one_ending_per_version":
                raise
            # The statement failed as a whole, so the status update went with it.
            raise SurfaceRefused(
                f"{tenant}/experiments/{experiment}/{kind}: {_EVENT_REFUSALS[exc.constraint]}"
            ) from exc
        if row is None:
            raise SurfaceRefused(
                f"{tenant}/experiments/{experiment} is not in {allowed} at version "
                f"{payload['experiment_version']}; nothing was changed"
            )
        return f"{kind}-{row[0]}"

    def _revise(self, tenant: str, experiment: str, payload: dict[str, Any]) -> str:
        """Append a wording to the current version of a draft - never a new version - if
        the draft is still at the revision the wording was written against.

        `FOR UPDATE` holds the experiment row for the statement: a discard racing this
        revise either lands first, and the re-checked `status = 'draft'` matches nothing,
        or waits until the revision is in. The new `revision_no` is the prior plus one,
        so two revises of the same revision collide on the key, and the loser's
        statement applied nothing.
        """
        prior = payload["prior_revision"]
        try:
            row = self.db.fetch_one(
                "WITH e AS ("
                "  SELECT tenant, experiment_id, current_version FROM experiments"
                "  WHERE tenant = %s AND experiment_id = %s"
                "    AND current_version = %s AND status = 'draft'"
                "  FOR UPDATE)"
                " INSERT INTO draft_revisions"
                "  (tenant, experiment_id, experiment_version, revision_no, hypothesis)"
                " SELECT e.tenant, e.experiment_id, e.current_version, %s + 1, %s"
                " FROM e"
                " WHERE %s = (SELECT max(r.revision_no) FROM draft_revisions r"
                "    WHERE (r.tenant, r.experiment_id, r.experiment_version)"
                "        = (e.tenant, e.experiment_id, e.current_version))"
                " RETURNING revision_no",
                (
                    tenant,
                    experiment,
                    payload["experiment_version"],
                    prior,
                    payload["hypothesis"],
                    prior,
                ),
            )
        except IntegrityViolation as exc:
            if exc.constraint not in _REVISION_REFUSALS:
                raise
            if exc.constraint != "draft_revisions_pkey":
                raise SurfaceRefused(
                    f"{tenant}/experiments/{experiment}: {_REVISION_REFUSALS[exc.constraint]}; "
                    "nothing was changed"
                ) from exc
            row = None
        if row is not None:
            return f"revision-{row[0]}"
        # The exact move - this wording, from this revision - already on record is the
        # effect having happened (audit M4). Anything else applied nothing.
        existing = self.db.fetch_one(
            "SELECT revision_no FROM draft_revisions"
            " WHERE tenant = %s AND experiment_id = %s AND experiment_version = %s"
            "   AND revision_no = %s + 1 AND hypothesis = %s",
            (tenant, experiment, payload["experiment_version"], prior, payload["hypothesis"]),
        )
        if existing is not None:
            return f"revision-{existing[0]}"
        raise SurfaceRefused(
            f"{tenant}/experiments/{experiment} is not a draft at revision {prior} of version "
            f"{payload['experiment_version']}; nothing was revised"
        )
