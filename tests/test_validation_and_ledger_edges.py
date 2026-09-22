"""Edge coverage for the two modules this audit introduced.

Both sit on a path where being wrong is expensive - one reads untrusted model output,
the other decides whether money moves twice - so their branches get exercised rather
than inferred.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentstack.execution.idempotency import ClaimState, IdempotencyLedger
from agentstack.policy.prompt import ApprovalPrompt
from agentstack.storage.database import Database
from agentstack.tools.catalog import LOOKUP, REFUND
from agentstack.tools.spec import ActsAs, Approval, Idempotency, Surface, ToolSpec
from agentstack.tools.validation import InvalidToolArguments, validate_arguments

GOOD = {"tenant": "acme", "customer_id": "c-42", "charge_id": "ch-7", "amount_cents": 1999}


def test_a_non_object_schema_is_refused() -> None:
    spec = ToolSpec(
        name="odd",
        description="declares an array at the top level",
        input_schema={"type": "array"},
        acts_as=ActsAs.SERVICE,
        scope="x:y",
        surface=Surface.API,
        side_effecting=False,
        reversible=True,
        approval=Approval.NONE,
        idempotency=Idempotency.NATURAL,
    )
    with pytest.raises(InvalidToolArguments, match="object schemas"):
        validate_arguments(spec, {})


def test_stringly_typed_transport_is_accepted_when_it_is_the_declared_type() -> None:
    """A model emits text; "1999" is an integer, "quite a lot" is not."""
    assert validate_arguments(REFUND, {**GOOD, "amount_cents": "1999"})["amount_cents"] == 1999
    with pytest.raises(InvalidToolArguments, match="must be integer"):
        validate_arguments(REFUND, {**GOOD, "amount_cents": "1,999"})


def test_a_boolean_is_not_an_integer() -> None:
    """Python says True == 1. An approval prompt showing "True dollars" says otherwise."""
    with pytest.raises(InvalidToolArguments, match="boolean"):
        validate_arguments(REFUND, {**GOOD, "amount_cents": True})


def test_a_wrongly_typed_non_string_value_is_refused() -> None:
    with pytest.raises(InvalidToolArguments, match="must be string"):
        validate_arguments(REFUND, {**GOOD, "charge_id": 7})


def test_an_undeclared_property_type_passes_through_unchanged() -> None:
    assert validate_arguments(LOOKUP, {"tenant": "acme", "customer_id": "c-1"})["tenant"] == "acme"


def test_a_number_and_a_boolean_arrive_from_text() -> None:
    spec = ToolSpec(
        name="tune",
        description="takes a float and a flag",
        input_schema={
            "type": "object",
            "properties": {"ratio": {"type": "number"}, "dry_run": {"type": "boolean"}},
            "required": ["ratio"],
        },
        acts_as=ActsAs.SERVICE,
        scope="x:y",
        surface=Surface.API,
        side_effecting=False,
        reversible=True,
        approval=Approval.NONE,
        idempotency=Idempotency.NATURAL,
    )
    assert validate_arguments(spec, {"ratio": "0.5", "dry_run": "true"}) == {
        "ratio": 0.5,
        "dry_run": True,
    }
    with pytest.raises(InvalidToolArguments, match="must be boolean"):
        validate_arguments(spec, {"ratio": "0.5", "dry_run": "perhaps"})


def test_abandon_releases_a_claim_that_provably_did_not_apply(app_database: Database) -> None:
    ledger = IdempotencyLedger(db=app_database)
    ledger.claim("k")
    assert ledger.unresolved_keys() == ("k",)
    ledger.abandon("k")
    assert ledger.unresolved_keys() == ()
    assert ledger.claim("k").state is ClaimState.FRESH


def test_recorded_returns_only_a_settled_receipt(app_database: Database) -> None:
    ledger = IdempotencyLedger(db=app_database)
    ledger.claim("k", now=datetime.now(UTC) - timedelta(minutes=5))
    assert ledger.recorded("k") is None, "a claim is not a receipt"
    ledger.finalize("k", "receipt-9")
    assert ledger.recorded("k") == "receipt-9"


def test_the_prompt_renders_ids_plainly_and_truncates_long_text() -> None:
    rendered = ApprovalPrompt(
        spec=REFUND,
        resource="acme/customers/c-42/charges/ch-7",
        # "q" appears in none of the prompt's own labels, so counting it counts
        # only the payload text.
        payload={"charge_id": "ch-7", "amount_cents": 1999, "note": "q" * 400},
        principal="p",
        acts_as=ActsAs.DELEGATED,
        requested_by="r",
        channel="cli",
    ).render()
    assert "ch-7" in rendered
    note_line = next(line for line in rendered.splitlines() if line.startswith("  note"))
    assert note_line.endswith("[untrusted text, from the model's proposal]")
    assert "…" in note_line, "an approver should not have to scroll past 400 characters"
    assert note_line.count("q") == 160, "truncated to the declared maximum, not to taste"
