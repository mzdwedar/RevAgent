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

    def state(self, resource: str) -> tuple[str, ...] | None:
        """What this resource *is* right now, for binding an approval to it (Part 7).

        Never shown to the model: it's hashed into a state snapshot and nothing else.
        It describes the thing an approver decided about, not where it is in its
        lifecycle. The lifecycle is the surface's precondition to check at the act,
        and an effect must not move its own snapshot, or the retry that should
        deduplicate would read as stale. `None` when the surface can't describe it.
        """
        ...


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

    def state(self, resource: str) -> tuple[str, ...] | None:
        # A reference API with no resource model: the snapshot falls back to what the
        # run itself committed against the resource (runtime/snapshot.py).
        del resource
        return None


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
# Two resources, and anything else is refused rather than guessed at:
#
#   {tenant}/experiments/{id}           create the draft (commit) / the experiment (read)
#   {tenant}/experiments/{id}/rollout   expose the current version to a percentage
#
# Both clients below hold the same preconditions, and `tests/infra/test_registry_store.py`
# runs one contract suite over both. A precondition that does not hold is
# `SurfaceRefused`: the resource was not in the state the action needs, and nothing
# applied. That is not schema validation - the validator has already passed the
# arguments - it is "this resource, now", which only the surface can answer.

_REGISTRY_RESOURCE = re.compile(
    r"^(?P<tenant>[^/]+)/experiments/(?P<experiment>[^/]+)(?P<action>/rollout)?$"
)

# A rollout may widen a live experiment or launch a draft; from any other state it is
# refused. `halted` is terminal for a version in iteration 1 (SPEC-registry.md).
_ROLLOUT_FROM = ("draft", "live")


def _registry_resource(resource: str) -> tuple[str, str, str]:
    match = _REGISTRY_RESOURCE.match(resource)
    if match is None:
        raise SurfaceRefused(f"{resource}: not a resource the experiment registry serves")
    action = "rollout" if match["action"] else "draft"
    return match["tenant"], match["experiment"], action


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
    rollouts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    reads: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # Apply the effect and then lose the answer: the failure two-phase idempotency
    # exists for. Same switch as RecordingClient, for the same reason.
    fail_after_effect: bool = False
    _status: dict[str, str] = field(default_factory=dict)

    def read(self, resource: str, query: dict[str, Any]) -> dict[str, Any]:
        tenant, experiment, action = _registry_resource(resource)
        if action != "draft":
            raise SurfaceRefused(f"{resource}: not readable")
        self.reads.append((resource, dict(query)))
        draft = self.drafts.get(resource)
        if draft is None:
            return {}
        return {"experiment_id": experiment, "status": self._status[resource], **draft}

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        tenant, experiment, action = _registry_resource(resource)
        base = f"{tenant}/experiments/{experiment}"
        if action == "draft":
            if base in self.drafts:
                raise SurfaceRefused(f"{base} already exists; a draft never overwrites one")
            self.drafts[base] = dict(payload)
            self._status[base] = "draft"
            receipt = f"draft-{len(self.drafts)}"
        else:
            draft = self.drafts.get(base)
            if draft is None or draft["experiment_version"] != payload["experiment_version"]:
                raise SurfaceRefused(
                    f"{base} has no current version {payload['experiment_version']}"
                )
            if self._status[base] not in _ROLLOUT_FROM:
                raise SurfaceRefused(f"{base} is {self._status[base]}; it cannot be rolled out")
            if (resource, payload) in self.rollouts:
                raise SurfaceRefused(f"{resource}: this rollout already happened")
            self.rollouts.append((resource, dict(payload)))
            self._status[base] = "live"
            receipt = f"rollout-{len(self.rollouts)}"
        if self.fail_after_effect:
            raise SurfaceTimeout(f"{resource} applied, answer lost")
        return receipt

    def state(self, resource: str) -> tuple[str, ...] | None:
        tenant, experiment, _ = _registry_resource(resource)
        draft = self.drafts.get(f"{tenant}/experiments/{experiment}")
        if draft is None:
            return None
        return (draft["experiment_version"], draft["variant"], draft["hypothesis"])


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
        del query
        tenant, experiment, action = _registry_resource(resource)
        if action != "draft":
            raise SurfaceRefused(f"{resource}: not readable")
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

    def state(self, resource: str) -> tuple[str, ...] | None:
        """The experiment's current version, its variant and its latest hypothesis.

        Either resource names the same experiment: a rollout is approved against the
        experiment it exposes. Status is left out on purpose (see `SurfaceClient.state`):
        the rollout itself moves it to `live`.
        """
        tenant, experiment, _ = _registry_resource(resource)
        row = self.db.fetch_one(
            "SELECT e.current_version, v.variant,"
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
        return None if row is None else tuple(str(column) for column in row)

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        tenant, experiment, action = _registry_resource(resource)
        if action == "draft":
            return self._draft(tenant, experiment, payload)
        return self._rollout(tenant, experiment, payload)

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

    def _rollout(self, tenant: str, experiment: str, payload: dict[str, Any]) -> str:
        try:
            row = self.db.fetch_one(
                "WITH moved AS ("
                "  UPDATE experiments SET status = 'live', updated_at = now()"
                "  WHERE tenant = %s AND experiment_id = %s"
                "    AND current_version = %s AND status = ANY(%s)"
                "  RETURNING tenant, experiment_id, current_version)"
                " INSERT INTO registry_events"
                "  (tenant, experiment_id, experiment_version, kind, payload)"
                " SELECT tenant, experiment_id, current_version, 'rollout', %s::jsonb"
                " FROM moved RETURNING id",
                (
                    tenant,
                    experiment,
                    payload["experiment_version"],
                    list(_ROLLOUT_FROM),
                    json.dumps(payload),
                ),
            )
        except IntegrityViolation as exc:
            if exc.constraint != "one_row_per_effect":
                raise
            # The statement failed as a whole, so the status update went with it.
            raise SurfaceRefused(
                f"{tenant}/experiments/{experiment}/rollout: this rollout already happened"
            ) from exc
        if row is None:
            raise SurfaceRefused(
                f"{tenant}/experiments/{experiment} is not a draft or live experiment at "
                f"version {payload['experiment_version']}; nothing was rolled out"
            )
        return f"rollout-{row[0]}"
