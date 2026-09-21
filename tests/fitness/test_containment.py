"""Part 7: containment is bounded on every dimension it claims to bound.

`Sandbox` documents itself as naming every dimension "so an empty one is an obvious
omission". That only holds if a declared dimension is actually read. A configured
allowlist that nothing consults is worse than an absent one, because a reviewer sees
it and stops looking.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pathlib
import re

import pytest

from agentstack.execution.surfaces import Sandbox, SandboxViolation
from agentstack.tools.spec import Surface

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "agentstack"


def _attributes_read_by(func_name: str) -> set[str]:
    tree = ast.parse((SRC / "execution" / "surfaces.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return {
                child.attr
                for child in ast.walk(node)
                if isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id == "self"
            }
    raise AssertionError(f"{func_name} not found")


def test_no_declared_containment_dimension_is_dead() -> None:
    declared = {f.name for f in dataclasses.fields(Sandbox)}
    enforced = _attributes_read_by("check")
    dead = declared - enforced
    assert not dead, (
        f"Sandbox declares {sorted(dead)} and check() never reads them. A declared "
        "control that nothing enforces stops the next person asking. Enforce it or "
        "delete the field."
    )


def test_check_still_bounds_surface_resource_and_tenant() -> None:
    sandbox = Sandbox(
        tenant="acme",
        allowed_surfaces=frozenset({Surface.API}),
        allowed_resource_prefixes=frozenset({"acme/customers/"}),
    )
    sandbox.check(surface=Surface.API, resource="acme/customers/c-1")
    with pytest.raises(SandboxViolation, match="containment"):
        sandbox.check(surface=Surface.SHELL, resource="acme/customers/c-1")
    with pytest.raises(SandboxViolation, match="allowed resources"):
        sandbox.check(surface=Surface.API, resource="acme/invoices/i-1")
    # The tenant check is a backstop behind the prefix check, so it needs a
    # misconfigured prefix list to be reachable at all - which is exactly the
    # misconfiguration it exists to catch.
    misconfigured = Sandbox(
        tenant="acme",
        allowed_surfaces=frozenset({Surface.API}),
        allowed_resource_prefixes=frozenset({"acme/customers/", "globex/customers/"}),
    )
    with pytest.raises(SandboxViolation, match="tenant boundary"):
        misconfigured.check(surface=Surface.API, resource="globex/customers/c-1")


def test_an_unenforced_dimension_is_explained_not_implied() -> None:
    """Naming a dimension the code does not enforce is fine - implying it is not.

    The earlier version of this test flagged any mention of "network" or
    "filesystem", which cannot tell a promise from a disclaimer. What is actually
    checkable: if the docstring raises a dimension `check()` does not enforce, it
    must point at the decision that says why.
    """
    doc = inspect.getdoc(Sandbox) or ""
    enforced = _attributes_read_by("check")
    for dimension in ("network", "filesystem"):
        mentioned = dimension in doc.lower()
        is_enforced = any(dimension in attr for attr in enforced)
        if mentioned and not is_enforced:
            assert re.search(r"ADR-\d{4}", doc), (
                f"the docstring raises {dimension} containment that check() does not "
                "enforce, without pointing at the ADR that explains the absence"
            )
