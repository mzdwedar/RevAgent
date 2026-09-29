"""Map changed paths to the Agent Stack layers they touch.

The layer -> owner-module table is read from the ledger in STACK.md, so there is one
source of truth. Deterministic on purpose: a model that misclassifies a path would hide
a layer from the audit, and the audit is the one place an error here is expensive.

    uv run python scripts/layer_of.py --base main            # JSON for the branch diff
    uv run python scripts/layer_of.py src/agentstack/policy/x.py
    uv run python scripts/layer_of.py --base main --sections # + the ledger rows to audit
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROW = re.compile(r"^\|\s*(\d+b?)\s*\|\s*([^|]+?)\s*\|\s*`agentstack\.(\w+)`")
DOC_OR_TEST = re.compile(r"(^|/)(tests|docs)/|\.md$")


def ledger() -> dict[str, tuple[str, str, str]]:
    """package -> (layer number, layer name, the full ledger row)."""
    table: dict[str, tuple[str, str, str]] = {}
    for line in (ROOT / "STACK.md").read_text().splitlines():
        m = ROW.match(line)
        if m:
            table[m.group(3)] = (m.group(1), m.group(2), line)
    return table


def _git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return out.stdout.split("\n")


def changed_files(base: str) -> list[str]:
    files = _git("diff", "--name-only", base) + _git("ls-files", "--others", "--exclude-standard")
    return sorted({f for f in files if f})


def classify(files: list[str]) -> dict:
    table = ledger()
    touched: dict[str, tuple[str, str]] = {}
    unmapped: list[str] = []
    src_changed = False
    for f in files:
        m = re.match(r"src/agentstack/(\w+)/", f)
        if m:
            src_changed = True
            if m.group(1) in table:
                num, name, row = table[m.group(1)]
                touched[num] = (name, row)
            else:
                unmapped.append(f)
        elif not DOC_OR_TEST.search(f):
            unmapped.append(f)
    order = sorted(touched)
    return {
        "src_changed": src_changed,
        "layers_touched": [f"{n} {touched[n][0]}" for n in order],
        "unmapped_non_doc_files": unmapped,
        "changed_files": len(files),
        "rows": [touched[n][1] for n in order],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--base", default="main")
    ap.add_argument("--sections", action="store_true")
    a = ap.parse_args()
    result = classify(a.paths or changed_files(a.base))
    rows = result.pop("rows")
    print(json.dumps(result))
    if a.sections:
        print("\n".join(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
