"""Part 6 -> 7: preparing an action is not committing one."""

from __future__ import annotations

import ast
import pathlib

from agentstack.tools.action import ActionRequest
from agentstack.tools.catalog import build_registry
from agentstack.tools.experiments import ROLLOUT, ROLLOUT_STAGE

from .conftest import ROLLOUT_ARGS

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "agentstack"


def test_preparing_a_tool_call_changes_nothing() -> None:
    registry = build_registry()
    exposed = registry.expose_for(tenant="acme", stage=ROLLOUT_STAGE)
    request = registry.prepare(ROLLOUT.name, ROLLOUT_ARGS, exposed=exposed)
    assert isinstance(request, ActionRequest)
    assert request.fingerprint(), "a prepared action is inspectable before it is committed"


def test_the_tools_package_never_calls_commit() -> None:
    offenders: list[str] = []
    for path in (SRC / "tools").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"commit", "execute"}
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"a tool performed its own side effect: {offenders}"


def test_the_gateway_is_the_only_caller_of_a_surface_client() -> None:
    callers: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "commit"
                and getattr(node.func.value, "id", "") == "client"
            ):
                callers.add(str(path.relative_to(SRC)))
    assert callers <= {"execution/gateway.py"}, (
        f"a surface was committed outside the gateway: {callers}"
    )
