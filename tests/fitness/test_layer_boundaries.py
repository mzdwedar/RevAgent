"""The layer model: layers only depend downward, and only one of them touches the world.

`lint-imports` is the verdict named in CONSTRAINTS.md; this test runs it so a single
`pytest tests/fitness` is enough to know whether the architecture still holds.
"""

from __future__ import annotations

import ast
import configparser
import os
import pathlib
import shutil
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
    "tabpfn_client",
}

# Which layer may hold which real client, and nothing beyond it.
#
# `execution` is the gateway (Execution surfaces): the systems the agent acts upon. `storage` is
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
        "capability exposure is not execution authority (Execution surfaces), and the state "
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

    # It said "five" for as long as there were six: contract 6 arrived in T33 unannounced.
    contracts = (root / ".importlinter").read_text().count("[importlinter:contract:")
    spelled = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
    said = f"{spelled[contracts]} `.importlinter` contracts"
    assert said in readme, f"README does not say there are {said}"


def _contract_3() -> tuple[set[str], set[str]]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(SRC.parents[1] / ".importlinter")
    section = parser["importlinter:contract:3"]
    return (
        {m for m in section["source_modules"].split() if m},
        {m for m in section["forbidden_modules"].split() if m},
    )


def test_the_hosted_scorer_client_is_a_surface_client_only_execution_may_hold() -> None:
    """`tabpfn_client` sends rows to a third party, so it is egress: layer 7 only (ADR-0012)."""
    sources, forbidden = _contract_3()
    assert "tabpfn_client" in forbidden
    assert "agentstack.execution" not in sources
    assert "tabpfn_client" in ALLOWED_CLIENTS["execution"]
    assert "tabpfn_client" not in ALLOWED_CLIENTS["storage"]


def _lint_with_probe(tmp_path: pathlib.Path, layer: str) -> subprocess.CompletedProcess[str]:
    """Run the real contracts over a copy of the tree with one probe module added."""
    root = SRC.parents[1]
    tmp_path.mkdir(parents=True, exist_ok=True)
    shutil.copy(root / ".importlinter", tmp_path / ".importlinter")
    shutil.copytree(
        SRC, tmp_path / "src" / "agentstack", ignore=shutil.ignore_patterns("__pycache__")
    )
    (tmp_path / "src" / "agentstack" / layer / "probe_egress.py").write_text(
        "import tabpfn_client\n\nCLIENT = tabpfn_client\n"
    )
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "src")}
    lint_imports = pathlib.Path(sys.executable).parent / "lint-imports"
    return subprocess.run(
        [str(lint_imports)], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
    )


def test_a_probe_importing_tabpfn_client_fails_outside_execution(tmp_path: pathlib.Path) -> None:
    for layer in ("prediction", "context"):
        result = _lint_with_probe(tmp_path / layer, layer)
        assert result.returncode != 0, f"{layer} imported tabpfn_client and the contracts held"
        assert "tabpfn_client" in result.stdout


def test_a_probe_importing_tabpfn_client_is_allowed_in_execution(tmp_path: pathlib.Path) -> None:
    result = _lint_with_probe(tmp_path, "execution")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_contract_3_ignores_exactly_the_two_startup_edges_to_the_hosted_scorer() -> None:
    """The exception for K9 is two edges, so it grows only deliberately."""
    import configparser

    parser = configparser.ConfigParser()
    parser.read(pathlib.Path(__file__).resolve().parents[2] / ".importlinter")
    ignored = parser["importlinter:contract:3"]["ignore_imports"].split()
    edges = {
        (ignored[i], ignored[i + 2]) for i in range(0, len(ignored), 3) if ignored[i + 1] == "->"
    }
    assert edges == {
        ("agentstack.interfaces.worker_cli", "agentstack.execution.hosted_scorer"),
        ("agentstack.interfaces.preflight_cli", "agentstack.execution.hosted_scorer"),
    }
