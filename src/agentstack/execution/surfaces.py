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
#
# Both clients below hold the same preconditions, and `tests/infra/test_registry_store.py`
# runs one contract suite over both. A precondition that does not hold is
# `SurfaceRefused`: the resource was not in the state the action needs, and nothing
# applied. That is not schema validation - the validator has already passed the
# arguments - it is "this resource, now", which only the surface can answer.

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
}

# Every state an experiment can be in. An abstention is a record, not a transition
# (assumption 7), so it may be appended in any of them and moves none.
_ANY_STATE = ("draft", "live", "halted", "discarded")

# Each status change: the states it may start from, and the state it leaves. A rollout
# may widen a live experiment or launch a draft; only a draft may be discarded. From any
# other state the action is refused. `halted` and `discarded` are terminal for a version
# in iteration 1 (SPEC-registry.md).
_TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "rollout": (("draft", "live"), "live"),
    "discard": (("draft",), "discarded"),
}

# The constraints a revision can meet, and what each means. The statement failed whole,
# so each is provably nothing applied - a refusal, not an unknown outcome.
_REVISION_REFUSALS = {
    "one_row_per_revision": "this wording is already a revision",
    "draft_revisions_pkey": "another revision landed first",
    "revision_says_something": "a revision must say something",
}

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


def _list_limit(query: dict[str, Any]) -> int:
    return max(1, min(int(query.get("limit", LIST_DEFAULT)), LIST_LIMIT))


def _history(experiment: str, events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Current exposure is derived from the latest rollout or halt, never stored as a
    second truth beside the events it would have to agree with."""
    return {
        "experiment_id": experiment,
        "events": [{"kind": kind, **payload} for kind, payload in events],
        "current_exposure": events[-1][1]["percentage"] if events else 0,
    }


@dataclass(slots=True)
class RegistryClient:
    """The experiment registry, in process.

    The fake the fitness suite can inspect without a database, held to the same
    contract as `PostgresRegistryClient`. It still does not validate arguments - a
    surface that rejected a malformed draft would make the schema validator look
    unnecessary, and criterion 19 is that nothing malformed gets this far. It *does*
    hold the state preconditions, because those are the surface's to answer, and a fake
    that accepted a rollout of nothing would let a test pass that production fails.
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
            return _history(
                experiment,
                [(k, p) for b, k, p in self.events if b == base and k in _EXPOSURE_KINDS],
            )
        return {
            "experiment_id": experiment,
            "status": self._status[base],
            **draft,
            "hypothesis": self.revisions[base][-1],
        }

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
        base = f"{tenant}/experiments/{experiment}"
        if action == "draft":
            if base in self.drafts:
                raise SurfaceRefused(f"{base} already exists; a draft never overwrites one")
            self.drafts[base] = dict(payload)
            self.revisions[base] = [payload["hypothesis"]]
            self._status[base] = "draft"
            receipt = f"draft-{len(self.drafts)}"
        elif action == "revise":
            self._current(base, payload, ("draft",))
            if not payload["hypothesis"].strip():
                raise SurfaceRefused(f"{base}: a revision must say something")
            if payload["hypothesis"] in self.revisions[base]:
                raise SurfaceRefused(f"{base}: this wording is already a revision")
            self.revisions[base].append(payload["hypothesis"])
            receipt = f"revision-{len(self.revisions[base])}"
        elif action == "abstention":
            self._current(base, payload, _ANY_STATE)
            if (base, action, payload) in self.events:
                raise SurfaceRefused(f"{resource}: this abstention is already recorded")
            self.events.append((base, action, dict(payload)))
            receipt = f"abstention-{len(self.events)}"
        else:
            allowed, becomes = _TRANSITIONS[action]
            self._current(base, payload, allowed)
            if (base, action, payload) in self.events:
                raise SurfaceRefused(f"{resource}: this {action} already happened")
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


@dataclass(frozen=True, slots=True)
class PostgresRegistryClient:
    """The experiment registry, on the tables `migrations/0012` created.

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
            "SELECT e.status, e.current_version, v.variant,"
            " (SELECT r.hypothesis FROM draft_revisions r"
            "   WHERE (r.tenant, r.experiment_id, r.experiment_version)"
            "       = (e.tenant, e.experiment_id, e.current_version)"
            "   ORDER BY r.revision_no DESC LIMIT 1)"
            " FROM experiments e"
            " JOIN experiment_versions v"
            "   ON (v.tenant, v.experiment_id, v.experiment_version)"
            "    = (e.tenant, e.experiment_id, e.current_version)"
            " WHERE e.tenant = %s AND e.experiment_id = %s",
            (tenant, experiment),
        )
        if row is None:
            return {}
        status, version, variant, hypothesis = row
        return {
            "experiment_id": experiment,
            "status": status,
            "experiment_version": version,
            "hypothesis": hypothesis,
            "variant": variant,
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
        rows = self.db.fetch_all(
            "SELECT kind, payload FROM registry_events"
            " WHERE tenant = %s AND experiment_id = %s AND kind = ANY(%s) ORDER BY id",
            (tenant, experiment, list(_EXPOSURE_KINDS)),
        )
        return _history(experiment, [(kind, dict(payload)) for kind, payload in rows])

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        tenant, experiment, action = _registry_resource(resource, _COMMITS)
        if action == "draft":
            return self._draft(tenant, experiment, payload)
        if action == "revise":
            return self._revise(tenant, experiment, payload)
        if action == "abstention":
            return self._abstain(tenant, experiment, payload)
        return self._transition(tenant, experiment, action, payload)

    def _abstain(self, tenant: str, experiment: str, payload: dict[str, Any]) -> str:
        """Append the explanation to the named version, whatever its state. No `UPDATE`:
        a run stops because the policy said abstain, not because a row was written."""
        try:
            row = self.db.fetch_one(
                "INSERT INTO registry_events"
                "  (tenant, experiment_id, experiment_version, kind, payload)"
                " SELECT tenant, experiment_id, experiment_version, 'abstention', %s::jsonb"
                " FROM experiment_versions"
                " WHERE tenant = %s AND experiment_id = %s AND experiment_version = %s"
                " RETURNING id",
                (json.dumps(payload), tenant, experiment, payload["experiment_version"]),
            )
        except IntegrityViolation as exc:
            if exc.constraint != "one_row_per_effect":
                raise
            raise SurfaceRefused(
                f"{tenant}/experiments/{experiment}: this abstention is already recorded"
            ) from exc
        if row is None:
            raise SurfaceRefused(
                f"{tenant}/experiments/{experiment} has no version "
                f"{payload['experiment_version']}; nothing was recorded"
            )
        return f"abstention-{row[0]}"

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
            if exc.constraint != "one_row_per_effect":
                raise
            # The statement failed as a whole, so the status update went with it.
            raise SurfaceRefused(
                f"{tenant}/experiments/{experiment}/{kind}: this {kind} already happened"
            ) from exc
        if row is None:
            raise SurfaceRefused(
                f"{tenant}/experiments/{experiment} is not in {allowed} at version "
                f"{payload['experiment_version']}; nothing was changed"
            )
        return f"{kind}-{row[0]}"

    def _revise(self, tenant: str, experiment: str, payload: dict[str, Any]) -> str:
        """Append a wording to the current version of a draft - never a new version.

        `FOR UPDATE` holds the experiment row for the statement: a discard racing this
        revise either lands first, and the re-checked `status = 'draft'` matches nothing,
        or waits until the revision is in. Two revises racing for the same `revision_no`
        collide on the key, and the loser's statement applied nothing.
        """
        try:
            row = self.db.fetch_one(
                "WITH e AS ("
                "  SELECT tenant, experiment_id, current_version FROM experiments"
                "  WHERE tenant = %s AND experiment_id = %s"
                "    AND current_version = %s AND status = 'draft'"
                "  FOR UPDATE)"
                " INSERT INTO draft_revisions"
                "  (tenant, experiment_id, experiment_version, revision_no, hypothesis)"
                " SELECT e.tenant, e.experiment_id, e.current_version,"
                "  (SELECT max(r.revision_no) + 1 FROM draft_revisions r"
                "    WHERE (r.tenant, r.experiment_id, r.experiment_version)"
                "        = (e.tenant, e.experiment_id, e.current_version)),"
                "  %s"
                " FROM e RETURNING revision_no",
                (tenant, experiment, payload["experiment_version"], payload["hypothesis"]),
            )
        except IntegrityViolation as exc:
            if exc.constraint not in _REVISION_REFUSALS:
                raise
            raise SurfaceRefused(
                f"{tenant}/experiments/{experiment}: {_REVISION_REFUSALS[exc.constraint]}; "
                "nothing was changed"
            ) from exc
        if row is None:
            raise SurfaceRefused(
                f"{tenant}/experiments/{experiment} is not a draft at version "
                f"{payload['experiment_version']}; nothing was revised"
            )
        return f"revision-{row[0]}"
