"""Deterministic checks for two draft criteria: C3's currency and C4's numbers (plan E6).

These are *veto* checks. A fail is exact: the draft states a currency the cohort does not
use, or a number that appears nowhere it could have come from. A pass says only that no
such violation was found. C3 is wider than currency (does the offer suit the cohort at
all?) and C4 is wider than numbers (an invented campaign with no figure in it), so a pass
is never evidence of "fits the cohort" or "no invented facts". Those stay with a person.

Known gaps: a number the variant itself invents ("above the 0.50 quantile") is allowed,
because variant numbers are the proposal; and a target the hypothesis sets ("churn below
50%") is flagged, because a target cannot be told from a predicted effect.

Strict on purpose. A derived or rounded figure ("about 1.5M" for 1,560,000) is flagged;
loosen a rule only where labels show it is wrong, never to make a number look better.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.grader import UNGRADED, Grade, calibrate
from evals.labelling import CRITERIA, KEY, read_jsonl

PASS, FAIL = "yes", "no"  # in the labels' vocabulary: yes = nothing violated

# Longest first, so NT$ is read as NTD before its "$" is read as USD.
_CURRENCY = re.compile(
    r"NT\$|\bTWD\b|\bNTD\b|\bEUR\b|\bUSD\b|\bJPY\b|\bGBP\b|[€¥£$]"
    r"|\b(?:dollars?|euros?|yen)\b",
    re.IGNORECASE,
)
_CANON = {
    "nt$": "NTD", "twd": "NTD", "ntd": "NTD", "eur": "EUR", "€": "EUR", "euro": "EUR",
    "euros": "EUR", "usd": "USD", "$": "USD", "dollar": "USD", "dollars": "USD",
    "jpy": "JPY", "¥": "JPY", "yen": "JPY", "gbp": "GBP", "£": "GBP",
}  # fmt: skip
# A number that does not sit inside a word or an identifier ("Q2", "discount_15").
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(\d[\d,]*(?:\.\d+)?)(%|[kKmM](?![A-Za-z]))?")
_LOOSE_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# "90-day retention" names the test's window, a choice the draft makes, not a claim.
_WINDOW = re.compile(r"\s?-?\s?(?:day|week|month|year)s?\b", re.IGNORECASE)
# "a 20% discount", "a $50 credit": the amount that defines the offer is the proposal,
# wherever the draft states it, including when the variant is only a label.
_OFFER = re.compile(r"\s?(?:discount|off\b|credit|free\b|trial)", re.IGNORECASE)
_SUFFIX = {"k": 1_000.0, "m": 1_000_000.0}


@dataclass(frozen=True)
class CheckResult:
    verdict: str  # PASS or FAIL
    reasons: tuple[str, ...]


def _text(draft: dict[str, str]) -> str:
    return f"{draft['hypothesis']}\n{draft['variant']}"


def currencies_in(text: str) -> set[str]:
    return {_CANON[m.group(0).lower()] for m in _CURRENCY.finditer(text)}


def check_currency(item: dict[str, Any]) -> CheckResult:
    """C3, currency only. A cohort that names no currency cannot be contradicted."""
    expected = item["cohort"].get("currency")
    if not expected:
        return CheckResult(PASS, ())
    expected = _CANON.get(str(expected).lower(), str(expected).upper())
    wrong = sorted(currencies_in(_text(item["draft"])) - {expected})
    if wrong:
        return CheckResult(FAIL, (f"states {', '.join(wrong)}; the cohort is in {expected}",))
    return CheckResult(PASS, ())


def _supported(item: dict[str, Any]) -> set[float]:
    """Every number the draft may legitimately state, rounded for comparison."""
    cohort = item["cohort"]
    values: set[float] = set()

    def add(number: float) -> None:
        values.add(round(number, 6))
        if 0 < number <= 1:  # a probability may be written as a percentage
            values.add(round(number * 100, 6))

    for key in ("size", "risk_threshold"):
        if isinstance(cohort.get(key), int | float):
            add(float(cohort[key]))
    for quantile in cohort.get("risk_quantiles", []):
        add(float(quantile))
    if isinstance(cohort.get("annual_value_at_risk_cents"), int | float):
        add(cohort["annual_value_at_risk_cents"] / 100)
    for value in cohort.values():  # "fixture:2026-09", "tabpfn-3.5"
        if isinstance(value, str):
            values.update(round(float(n), 6) for n in _LOOSE_NUMBER.findall(value))
    for text in item["context"]:
        values.update(round(float(n), 6) for n in _LOOSE_NUMBER.findall(text))
    # The offer's own parameters are the proposal, not a claim about the world.
    values.update(round(float(n), 6) for n in _LOOSE_NUMBER.findall(item["draft"]["variant"]))
    return values


def numbers_in(text: str) -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    for match in _NUMBER.finditer(text):
        if _WINDOW.match(text, match.end()) or _OFFER.match(text, match.end()):
            continue
        value = float(match.group(1).replace(",", ""))
        value *= _SUFFIX.get((match.group(2) or "").lower(), 1.0)
        found.append((match.group(0), round(value, 6)))
    return found


def check_numbers(item: dict[str, Any]) -> CheckResult:
    """C4, numbers only. Every figure in the hypothesis must trace to the cohort evidence,
    the retrieved context, or the variant it proposes."""
    supported = _supported(item)
    unsupported = [
        shown for shown, value in numbers_in(item["draft"]["hypothesis"]) if value not in supported
    ]
    if unsupported:
        return CheckResult(
            FAIL,
            (f"{', '.join(dict.fromkeys(unsupported))}: not in the evidence, context or variant",),
        )
    return CheckResult(PASS, ())


def check_item(item: dict[str, Any]) -> Grade:
    """The two checks as a Grade, so calibration treats them like any grader. Criteria
    these checks do not cover are ungraded, never passed."""
    verdicts = dict.fromkeys(CRITERIA, UNGRADED)
    reasons = dict.fromkeys(CRITERIA, "not covered by a deterministic check")
    for criterion, result in (("C3", check_currency(item)), ("C4", check_numbers(item))):
        verdicts[criterion] = result.verdict
        reasons[criterion] = "; ".join(result.reasons) or "no violation found"
    return Grade(verdicts, reasons)


def veto_stats(items: Sequence[dict[str, Any]], grades: dict[str, Grade]) -> dict[str, Any]:
    """For a check that only vetoes: of the drafts it flagged, how many does the labeller
    also fail (precision), and of the labeller's fails, how many did it find (recall)."""
    stats: dict[str, Any] = {}
    for criterion in ("C3", "C4"):
        rows = [
            (grades[i["item"]].verdicts[criterion], i["labels"][criterion])
            for i in items
            if i["labels"][criterion] in ("yes", "no")
        ]
        flagged = [label for verdict, label in rows if verdict == FAIL]
        failed = [verdict for verdict, label in rows if label == "no"]
        stats[criterion] = {
            "flagged": len(flagged),
            "labelled_fail": len(failed),
            "precision": sum(label == "no" for label in flagged) / len(flagged)
            if flagged
            else None,
            "recall": sum(verdict == FAIL for verdict in failed) / len(failed) if failed else None,
        }
    return stats


def run_checks(*, labels: Path, key: Path = KEY, labeller: str = "owner") -> dict[str, Any]:
    """Run the checks over every fully labelled draft and report them against the labels."""
    items = [i for i in read_jsonl(labels) if all(v is not None for v in i["labels"].values())]
    if not items:
        raise ValueError(f"{labels} has no fully labelled items")
    grades = {item["item"]: check_item(item) for item in items}
    injections = {k["item"] for k in read_jsonl(key) if k.get("injection")}
    report = calibrate(items, grades, injections)
    # Only C3 and C4 are covered; the rest of the table is not a result.
    report["criteria"] = {c: report["criteria"][c] for c in ("C3", "C4")}
    return {
        "checker": "deterministic: C3 currency, C4 numbers",
        "labeller": labeller,
        "items_checked": len(items),
        "criteria": report["criteria"],
        "veto": veto_stats(items, grades),
        "flagged": {
            i["item"]: {
                c: grades[i["item"]].reasons[c]
                for c in ("C3", "C4")
                if grades[i["item"]].verdicts[c] == FAIL
            }  # fmt: skip
            for i in items
            if FAIL in (grades[i["item"]].verdicts["C3"], grades[i["item"]].verdicts["C4"])
        },
    }
