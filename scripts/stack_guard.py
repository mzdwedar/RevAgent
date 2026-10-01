"""Watch the diff for a weakened bar.

An agent that hits a red check takes the cheapest road to green. It does not craft
clever loopholes - it adds a suppression, skips a test, drops an assertion, deletes a
contract, or edits a threshold down. None of that needs sophisticated tooling to
catch; it needs someone looking.

Tightening the bar should be silent. Loosening it should be loud.

    uv run python scripts/stack_guard.py                # worktree vs HEAD
    uv run python scripts/stack_guard.py --base main    # branch vs main

Exit codes: 0 clean, 1 findings, 2 could not run.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SUPPRESSIONS = re.compile(
    r"#\s*(type:\s*ignore|noqa|nosec|pragma:\s*no\s*cover)"
    r"|@ts-ignore|eslint-disable|nosemgrep|gitleaks:allow|istanbul ignore"
)
STUBS = re.compile(r"raise NotImplementedError|except[^:]*:\s*pass\s*$|except\s*:\s*$")
SKIPS = re.compile(r"@pytest\.mark\.(skip|xfail)|pytest\.skip\(|@unittest\.skip")

# The guard names the forbidden patterns, and so does the document that forbids them.
# Scanning either for those patterns only ever finds the definition. CONSTRAINTS.md is
# still fully checked by `state_findings` - floor bullets, enforced rows, exceptions -
# which is where a real weakening of it would show up.
# Everywhere a ToolSpec is declared. A tier downgrade in any of them is an
# Identity, trust, policy, approvals finding, and hardcoding one filename meant the next
# catalog was unguarded.
TOOL_SOURCES = tuple(sorted((ROOT / "src" / "agentstack" / "tools").glob("*.py")))

NAMES_THE_PATTERNS = (
    "scripts/stack_guard.py",
    "CONSTRAINTS.md",
    "tests/fitness/test_the_bar_guards_itself.py",
)

REQUIRED_SETS = {
    "REQUIRED_SPANS": "src/agentstack/observability/spans.py",
    "REQUIRED_TOOL_FIELDS": "src/agentstack/tools/spec.py",
    "REQUIRED_ITEM_FIELDS": "src/agentstack/context/items.py",
    "REQUIRED_ENVELOPE_FIELDS": "src/agentstack/policy/envelope.py",
}


@dataclass(frozen=True)
class Finding:
    where: str
    what: str
    why: str


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return result.stdout


def _has_commits() -> bool:
    return bool(_git("rev-parse", "--verify", "HEAD").strip())


# Which new files are worth reading. A new PNG has no suppressions in it, and a
# lockfile would drown the scan in noise.
SCANNED_SUFFIXES = frozenset({".py", ".md", ".sh", ".sql", ".toml", ".yml", ".yaml", ".json"})


def _untracked() -> dict[str, tuple[list[str], list[str]]]:
    """New files, as if every line of them were an addition.

    `git diff HEAD` does not mention untracked files, so until a change was staged the
    guard could not see it at all - a brand-new module full of suppressions passed the
    loop's own gate, which is the gate that exists to catch exactly that. CI compared
    commits and would have caught it later, but "later" is after the hook said yes.
    """
    per_file: dict[str, tuple[list[str], list[str]]] = {}
    for path in _git("ls-files", "--others", "--exclude-standard").split("\n"):
        name = path.strip()
        if not name:
            continue
        target = ROOT / name
        if target.is_file() and target.suffix in SCANNED_SUFFIXES:
            per_file[name] = (target.read_text(errors="replace").splitlines(), [])
    return per_file


def _diff(base: str | None) -> str:
    if base:
        return _git("diff", f"{base}...", "--unified=0")
    if _has_commits():
        return _git("diff", "HEAD", "--unified=0")
    return _git("diff", "--cached", "--unified=0")


def _file_at(base: str | None, path: str) -> str | None:
    if base:
        out = _git("show", f"{base}:{path}")
        return out or None
    if _has_commits():
        out = _git("show", f"HEAD:{path}")
        return out or None
    return None


def _current(path: str) -> str:
    target = ROOT / path
    return target.read_text() if target.exists() else ""


def _parse_diff(diff: str) -> dict[str, tuple[list[str], list[str]]]:
    """path -> (added lines, removed lines)."""
    per_file: dict[str, tuple[list[str], list[str]]] = {}
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            per_file.setdefault(path, ([], []))
        elif path and line.startswith("+") and not line.startswith("+++"):
            per_file[path][0].append(line[1:])
        elif path and line.startswith("-") and not line.startswith("---"):
            per_file[path][1].append(line[1:])
    return per_file


def _set_members(source: str, name: str) -> set[str]:
    match = re.search(rf"{name}[^=]*=\s*frozenset\(\s*\{{(.*?)\}}\s*\)", source, re.S)
    if not match:
        return set()
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _tool_approvals(source: str) -> dict[str, str]:
    approvals: dict[str, str] = {}
    for block in source.split("ToolSpec(")[1:]:
        name = re.search(r'name="([^"]+)"', block)
        approval = re.search(r"approval=Approval\.(\w+)", block)
        if name and approval:
            approvals[name.group(1)] = approval.group(1)
    return approvals


def _contract_names(source: str) -> set[str]:
    return set(re.findall(r"^name = (.+)$", source, re.M))


def _table_rows(source: str, heading: str) -> set[str]:
    rows: set[str] = set()
    inside = False
    for line in source.splitlines():
        if line.startswith("## "):
            inside = line.strip().lower().startswith(f"## {heading.lower()}")
            continue
        if inside and line.startswith("|") and not re.match(r"^\|[\s:|-]+\|$", line):
            rows.add(line.strip())
    return rows


def _section(source: str, heading: str) -> str:
    """The raw text under one `##` heading, used to look for an acknowledgement."""
    lines: list[str] = []
    inside = False
    for line in source.splitlines():
        if line.startswith("## "):
            inside = line.strip().lower() == f"## {heading.lower()}"
            continue
        if inside:
            lines.append(line)
    return "\n".join(lines)


def _floor_bullets(source: str) -> set[str]:
    """The bullets under `## Floor` in CONSTRAINTS.md.

    The table checks below watch for *removed* rows. The floor is prose bullets, and
    until T1 nothing watched it at all - so amending a floor rule was the one way to
    lower the bar silently. Rewording shows up here as a removal plus an addition,
    which is correct: a reworded floor rule deserves the same look as a deleted one.
    """
    bullets: set[str] = set()
    inside = False
    for line in source.splitlines():
        if line.startswith("## "):
            inside = line.strip().lower().startswith("## floor")
            continue
        if inside and line.lstrip().startswith("- "):
            bullets.add(line.strip())
    return bullets


def diff_findings(per_file: dict[str, tuple[list[str], list[str]]]) -> list[Finding]:
    found: list[Finding] = []
    for path, (added, removed) in per_file.items():
        if path.startswith(NAMES_THE_PATTERNS):
            continue  # these files exist to name the patterns, so they contain them
        for line in added:
            if SUPPRESSIONS.search(line):
                found.append(Finding(path, f"new suppression: {line.strip()[:80]}", "the floor"))
            if STUBS.search(line):
                found.append(Finding(path, f"unfinished work: {line.strip()[:80]}", "the floor"))
            if SKIPS.search(line):
                found.append(Finding(path, f"test switched off: {line.strip()[:80]}", "the floor"))
        if path.startswith("tests/"):
            deleted_tests = [line for line in removed if re.match(r"\s*def test_", line)]
            if deleted_tests and len(deleted_tests) > len(
                [line for line in added if re.match(r"\s*def test_", line)]
            ):
                found.append(Finding(path, f"{len(deleted_tests)} test(s) removed", "the floor"))
            lost = len([x for x in removed if x.strip().startswith("assert ")]) - len(
                [x for x in added if x.strip().startswith("assert ")]
            )
            if lost > 0:
                found.append(Finding(path, f"{lost} assertion(s) removed", "assertion quality"))
    return found


def state_findings(base: str | None) -> list[Finding]:
    found: list[Finding] = []

    for name, path in REQUIRED_SETS.items():
        before = _file_at(base, path)
        if before is None:
            continue
        lost = _set_members(before, name) - _set_members(_current(path), name)
        if lost:
            found.append(
                Finding(path, f"{name} shrank: {sorted(lost)}", "an invariant was narrowed")
            )

    before = _file_at(base, ".importlinter")
    if before is not None:
        lost = _contract_names(before) - _contract_names(_current(".importlinter"))
        if lost:
            found.append(
                Finding(
                    ".importlinter", f"layer contract removed: {sorted(lost)}", "the layer model"
                )
            )

    # Every file that declares tools, not just the first one there was. T13 added
    # `experiments.py`, and until this was a glob a rollout could be downgraded from
    # ALWAYS to PRE_COMMIT without the guard saying anything.
    rank = {"NONE": 0, "PRE_COMMIT": 1, "ALWAYS": 2}
    for path in sorted(p.relative_to(ROOT).as_posix() for p in TOOL_SOURCES):
        before = _file_at(base, path)
        if before is None:
            continue
        old = _tool_approvals(before)
        new = _tool_approvals(_current(path))
        for tool, tier in new.items():
            if tool in old and rank.get(tier, 0) < rank.get(old[tool], 0):
                found.append(
                    Finding(
                        path,
                        f"{tool}: approval downgraded {old[tool]} -> {tier}",
                        "Identity, trust, policy, approvals",
                    )
                )

    before = _file_at(base, "CONSTRAINTS.md")
    if before is not None:
        current = _current("CONSTRAINTS.md")
        lost = _table_rows(before, "Enforced with numbers") - _table_rows(
            current, "Enforced with numbers"
        )
        if lost:
            found.append(
                Finding("CONSTRAINTS.md", f"{len(lost)} enforced row(s) removed", "the bar")
            )
        amendments = _section(current, "Amendments to the floor")
        weakened = {
            bullet
            for bullet in _floor_bullets(before) - _floor_bullets(current)
            if bullet.lstrip("- ").strip() not in amendments
        }
        for bullet in sorted(weakened):
            found.append(
                Finding(
                    "CONSTRAINTS.md",
                    f"floor rule removed or reworded: {bullet[:90]}",
                    "the floor is the part nothing else re-checks",
                )
            )
        gained = _table_rows(current, "Exceptions") - _table_rows(before, "Exceptions")
        if gained:
            found.append(
                Finding(
                    "CONSTRAINTS.md",
                    f"{len(gained)} new exception(s) - who owns them, and when do they expire?",
                    "the bar",
                )
            )

    fitness_now = len(list((ROOT / "tests" / "fitness").glob("test_*.py")))
    fitness_declared = re.search(r"\| Fitness test count \| (\d+) \|", _current("CONSTRAINTS.md"))
    if fitness_declared and fitness_now < int(fitness_declared.group(1)):
        found.append(
            Finding(
                "tests/fitness",
                f"{fitness_now} fitness tests, CONSTRAINTS.md records {fitness_declared.group(1)}",
                "a ratchet went backwards",
            )
        )

    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="git ref to compare against (e.g. main)")
    args = parser.parse_args()

    if not (ROOT / ".git").exists():
        print("stack_guard: not a git repository", file=sys.stderr)
        return 2

    changed = _parse_diff(_diff(args.base))
    # Untracked files are not in any diff, and a new file is where a new
    # suppression is most likely to be.
    changed.update(_untracked())
    findings = diff_findings(changed) + state_findings(args.base)

    if not findings:
        print("stack_guard: the bar is intact")
        return 0

    print(f"stack_guard: {len(findings)} finding(s) - the bar moved\n")
    for finding in findings:
        print(f"  {finding.where}")
        print(f"      {finding.what}")
        print(f"      ({finding.why})")
    print(
        "\nTightening the bar is silent; loosening it is loud. If one of these is "
        "deliberate, record it in CONSTRAINTS.md - an exception needs an owner and an "
        "expiry, an amended floor rule needs a row under 'Amendments to the floor'."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
