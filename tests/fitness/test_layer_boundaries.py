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


def test_only_the_execution_layer_imports_a_real_client() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        layer = path.relative_to(SRC).parts[0]
        if layer == "execution":
            continue
        for module in _imported_modules(path):
            root = module.split(".")[0]
            if module in CLIENT_MODULES or root in CLIENT_MODULES:
                offenders.append(f"{path.relative_to(SRC)} imports {module}")
    assert not offenders, (
        "capability exposure is not execution authority (Part 7): only "
        "agentstack.execution may touch a real client.\n" + "\n".join(offenders)
    )


def test_every_layer_directory_is_declared_in_the_ledger() -> None:
    ledger = (SRC.parents[1] / "STACK.md").read_text()
    packages = {p.name for p in SRC.iterdir() if p.is_dir() and not p.name.startswith("_")}
    missing = {p for p in packages if f"agentstack.{p}" not in ledger}
    assert not missing, f"layers not recorded in STACK.md: {sorted(missing)}"
