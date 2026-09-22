"""The floor is prose, and prose is the easiest part of a bar to soften.

`stack_guard` watches the numbered tables for removed rows. Until T1 it watched the
Floor section for nothing at all, which made editing a floor bullet the one way to
lower the bar without anything saying so. This test is the reason that stays closed.
"""

from __future__ import annotations

import functools
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


@functools.cache
def _guard() -> ModuleType:
    """Load scripts/stack_guard.py, which is not an importable package."""
    spec = importlib.util.spec_from_file_location("stack_guard", ROOT / "scripts/stack_guard.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves its module through sys.modules; without this it sees None.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BEFORE = """# Constraints

## Floor (always enforced, no setup required)

- No new suppression comments
- No module outside `agentstack.storage` imports a DB driver

## Enforced with numbers

| A | B |
"""


def test_the_floor_bullets_are_extracted_from_that_section_only() -> None:
    bullets = _guard()._floor_bullets(BEFORE)

    assert len(bullets) == 2
    assert "- No new suppression comments" in bullets
    assert not any("| A | B |" in b for b in bullets)


def test_a_deleted_floor_rule_is_visible() -> None:
    guard = _guard()
    after = BEFORE.replace("- No new suppression comments\n", "")

    assert guard._floor_bullets(BEFORE) - guard._floor_bullets(after)


def test_a_reworded_floor_rule_is_visible_too() -> None:
    """'No module outside X' quietly becoming 'Prefer that modules outside X' is the
    move this exists to catch, and it is not a deletion."""
    guard = _guard()
    after = BEFORE.replace("- No module outside", "- Prefer that no module outside")

    assert guard._floor_bullets(BEFORE) - guard._floor_bullets(after)


def test_adding_a_floor_rule_is_silent() -> None:
    """Tightening the bar should never ask for attention."""
    guard = _guard()
    after = BEFORE.replace(
        "## Enforced with numbers", "- No secrets in source\n\n## Enforced with numbers"
    )

    assert guard._floor_bullets(BEFORE) - guard._floor_bullets(after) == set()


def test_the_real_constraints_file_has_a_floor_this_can_read() -> None:
    assert len(_guard()._floor_bullets((ROOT / "CONSTRAINTS.md").read_text())) >= 10


AMENDED = BEFORE.replace(
    "- No new suppression comments\n",
    "",
).replace(
    "## Enforced with numbers",
    "## Amendments to the floor\n\n"
    "| Date | Rule removed | Why |\n"
    "| 2026-09-22 | No new suppression comments | it was wrong |\n\n"
    "## Enforced with numbers",
)


def _weakened(before: str, after: str) -> set[str]:
    """What `state_findings` reports on, reproduced from its two helpers."""
    guard = _guard()
    amendments = guard._section(after, "Amendments to the floor")
    return {
        bullet
        for bullet in guard._floor_bullets(before) - guard._floor_bullets(after)
        if bullet.lstrip("- ").strip() not in amendments
    }


def test_a_removed_rule_written_into_the_amendments_log_is_acknowledged() -> None:
    """Loud has to be resolvable by recording the reason, not by silencing the check."""
    assert _weakened(BEFORE, AMENDED) == set()


def test_an_unrelated_amendment_row_does_not_acknowledge_this_removal() -> None:
    unrelated = AMENDED.replace("No new suppression comments | it was wrong", "Something else | x")

    assert _weakened(BEFORE, unrelated) == {"- No new suppression comments"}


def test_the_amendments_log_is_read_from_its_own_section() -> None:
    """A rule quoted under some other heading must not count as acknowledged."""
    elsewhere = BEFORE.replace("- No new suppression comments\n", "").replace(
        "## Enforced with numbers",
        "## Notes\n\nNo new suppression comments\n\n## Enforced with numbers",
    )

    assert _weakened(BEFORE, elsewhere) == {"- No new suppression comments"}


def test_the_current_floor_amendment_is_recorded() -> None:
    """T1 replaced a floor rule. The reason must be in the file, not in a commit message."""
    guard = _guard()
    amendments = guard._section((ROOT / "CONSTRAINTS.md").read_text(), "Amendments to the floor")

    assert "agentstack.storage" in amendments
    assert "docs/adr/0005" in amendments


def test_the_documents_that_name_the_patterns_are_not_scanned_for_them() -> None:
    """The guard, the document that forbids the patterns, and this file all contain
    them, because naming them is the job. Scanning any of the three finds a definition.

    This is an exclusion from the *pattern* scan, not from the guard: floor bullets,
    enforced rows and exceptions in CONSTRAINTS.md are all still checked, and that is
    where a real weakening of it would appear.
    """
    guard = _guard()
    prose = {"CONSTRAINTS.md": (["- No `# noqa` anywhere, and no `@pytest.mark.skipif`"], [])}
    code = {"src/agentstack/thing.py": (["value = compute()  # noqa"], [])}

    assert guard.diff_findings(prose) == []
    assert guard.diff_findings(code), "a suppression in real code must still be loud"


def test_constraints_is_still_checked_for_the_things_that_matter() -> None:
    """The exclusion above must not become a hole."""
    guard = _guard()

    assert guard.NAMES_THE_PATTERNS == (
        "scripts/stack_guard.py",
        "CONSTRAINTS.md",
        "tests/fitness/test_the_bar_guards_itself.py",
    ), "this list is an exemption; it grows only when a file's job is to name the patterns"
    # state_findings reads CONSTRAINTS.md directly, by name, for all three of these.
    source = (ROOT / "scripts/stack_guard.py").read_text()
    for check in ("Enforced with numbers", "Exceptions", "_floor_bullets"):
        assert check in source
