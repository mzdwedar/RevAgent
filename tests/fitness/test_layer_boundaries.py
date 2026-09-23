"""Part 1: layers only depend downward, and only one of them touches the world.

`lint-imports` is the verdict named in CONSTRAINTS.md; this test runs it so a single
`pytest tests/fitness` is enough to know whether the architecture still holds.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "agentstack"
CLIENT_MODULES = {
    "httpx",
    "requests",
    "urllib.request",
    "subprocess",
    "socket",
    "sqlite3",
    "smtplib",
    "boto3",
    "psycopg",
    "psycopg_pool",
}

# Which layer may hold which real client, and nothing beyond it.
#
# `execution` is the gateway (Part 7): the systems the agent acts upon. `storage` is
# the substrate (layer 10, ADR-0005): the agent's own state. They are exempt from
# different things, which is the whole reason they are two entries and not one list -
# storage holding an HTTP client would be exactly as wrong as runtime holding a driver.
ALLOWED_CLIENTS = {
    "execution": CLIENT_MODULES,
    "storage": {"psycopg", "psycopg_pool"},
}


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_import_linter_contracts_hold() -> None:
    lint_imports = pathlib.Path(sys.executable).parent / "lint-imports"
    assert lint_imports.exists(), "import-linter is a declared dev dependency; run `uv sync`"
    result = subprocess.run(
        [str(lint_imports)],
        cwd=SRC.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"layer contracts broken:\n{result.stdout}\n{result.stderr}"


def test_only_the_named_layers_import_a_real_client() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        layer = path.relative_to(SRC).parts[0]
        allowed = ALLOWED_CLIENTS.get(layer, set())
        for module in _imported_modules(path):
            root = module.split(".")[0]
            if (module in CLIENT_MODULES or root in CLIENT_MODULES) and not (
                module in allowed or root in allowed
            ):
                offenders.append(f"{path.relative_to(SRC)} imports {module}")
    assert not offenders, (
        "capability exposure is not execution authority (Part 7), and the state "
        "substrate is not an execution surface (ADR-0005).\n" + "\n".join(offenders)
    )


def test_the_storage_exemption_covers_the_driver_and_nothing_else() -> None:
    """`storage` is exempt for the database, not for the network.

    A blanket "storage may import anything" would quietly re-create the god-module the
    layering exists to prevent - egress would just move one package down.
    """
    assert ALLOWED_CLIENTS["storage"] == {"psycopg", "psycopg_pool"}
    assert {"httpx", "requests", "subprocess", "socket"}.isdisjoint(ALLOWED_CLIENTS["storage"])


def test_every_layer_directory_is_declared_in_the_ledger() -> None:
    ledger = (SRC.parents[1] / "STACK.md").read_text()
    packages = {p.name for p in SRC.iterdir() if p.is_dir() and not p.name.startswith("_")}
    missing = {p for p in packages if f"agentstack.{p}" not in ledger}
    assert not missing, f"layers not recorded in STACK.md: {sorted(missing)}"


def test_the_readme_counts_match_what_is_actually_here() -> None:
    """The README claimed 22 fitness tests and nine layers while there were 26 and
    eleven. A count in prose is a fact with no verdict attached, so here is the verdict."""
    root = SRC.parents[1]
    readme = (root / "README.md").read_text()

    fitness = len(list((root / "tests" / "fitness").glob("test_*.py")))
    packages = len([p for p in SRC.iterdir() if p.is_dir() and not p.name.startswith("_")])

    assert f"{fitness} tests" in readme, f"README does not say there are {fitness} fitness tests"
    assert f"{packages} packages" in readme, f"README does not say there are {packages} packages"
