"""The draft tool's `offer_kind` enum is the policy catalog (ADR-0015).

Layer 6 cannot import layer 8, so the tool spells the kinds out. This is the one place
the two are held equal: a kind added to `offers.toml` and not to the tool is unchoosable,
and one in the tool and not in the config would be refused at admission every time.
"""

from __future__ import annotations

from agentstack.policy.offers import Bounds
from agentstack.tools.experiments import DRAFT


def test_the_offer_kind_enum_is_the_catalog_in_config() -> None:
    schema = DRAFT.input_schema["properties"]["offer_kind"]

    for profile in ("default", "dev"):
        assert set(schema["enum"]) == set(Bounds.load(profile).kinds)


def test_the_offer_arguments_are_flat_scalars_the_model_must_supply() -> None:
    properties = DRAFT.input_schema["properties"]

    assert properties["offer_amount"]["type"] == "integer"
    assert properties["offer_months"]["type"] == "integer"
    assert {"offer_kind", "offer_amount", "offer_months"} <= set(DRAFT.input_schema["required"])
