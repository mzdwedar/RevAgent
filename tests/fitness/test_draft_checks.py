"""The deterministic C3 (currency) and C4 (numbers) checks.

These are veto checks: a fail has to be exact, so each rule is pinned by a case, and the
known gaps are pinned too, so a change that closes or widens one is a decision and not an
accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from evals.checks import (
    FAIL,
    PASS,
    check_currency,
    check_item,
    check_numbers,
    numbers_in,
    run_checks,
    veto_stats,
)
from evals.grader import UNGRADED, Grade
from evals.labelling import CRITERIA, write_jsonl

COHORT = {
    "data_as_of": "fixture:2026-09",
    "targeting_model_version": "tabpfn-3.5",
    "risk_threshold": 0.61,
    "size": 1200,
    "annual_value_at_risk_cents": 7200000,
    "risk_quantiles": [0.61, 0.7, 0.8],
}


def draft(hypothesis: str, variant: str = "a discount", *, context: list[str] | None = None,
          **cohort: Any) -> dict[str, Any]:  # fmt: skip
    return {
        "item": "d01",
        "cohort": COHORT | cohort,
        "context": context or [],
        "draft": {"hypothesis": hypothesis, "variant": variant},
        "labels": dict.fromkeys(CRITERIA, "yes"),
        "notes": {},
    }


# --- C3: currency ---


@pytest.mark.parametrize(
    ("currency", "text", "verdict"),
    [
        (None, "protects $900 of revenue", PASS),  # no cohort currency: nothing to contradict
        ("EUR", "protects $720 of revenue", FAIL),
        ("EUR", "protects EUR 720 or €720 of revenue", PASS),
        ("EUR", "protects 720 euros", PASS),
        ("NTD", "protects $10,000", FAIL),  # the labels fail a bare $ on an NTD cohort
        ("NTD", "protects NT$10,000", PASS),  # NT$ is NTD, not USD
        ("NTD", "protects TWD 10,000", PASS),
        ("JPY", "protects ¥320,000", PASS),
        ("JPY", "protects $3,200", FAIL),
        ("EUR", "no money is mentioned here", PASS),
    ],
)
def test_currency_conflicts_are_exact(currency: str | None, text: str, verdict: str) -> None:
    assert check_currency(draft(text, currency=currency)).verdict == verdict


def test_the_currency_check_names_what_it_found() -> None:
    result = check_currency(draft("a $5 credit", currency="EUR"))
    assert result.reasons == ("states USD; the cohort is in EUR",)


def test_the_variant_is_checked_for_currency_too() -> None:
    assert check_currency(draft("a retention test", "$50 credit", currency="EUR")).verdict == FAIL


# --- C4: numbers ---


def test_the_cohort_s_own_figures_are_supported_in_any_written_form() -> None:
    text = (
        "1,200 customers at 0.61 or above, so 61% risk, hold $72,000 of revenue. "
        "The 70% band, scored on 2026-09 data by tabpfn-3.5."
    )
    assert check_numbers(draft(text)).verdict == PASS


def test_a_k_or_m_suffix_is_read_as_a_magnitude() -> None:
    assert check_numbers(draft("protecting $72k of revenue")).verdict == PASS
    assert check_numbers(draft("protecting $1.2k customers")).verdict == PASS  # 1,200
    assert check_numbers(draft("protecting $5M of revenue")).verdict == FAIL


def test_an_invented_figure_is_flagged_and_named() -> None:
    result = check_numbers(draft("The offer should retain 25% of the cohort."))
    assert result.verdict == FAIL and "25%" in result.reasons[0]


def test_the_offer_s_own_number_is_the_proposal_wherever_it_is_stated() -> None:
    assert check_numbers(draft("a 20% discount", "Discount offer")).verdict == PASS
    assert check_numbers(draft("a $50 credit", "credit")).verdict == PASS
    assert check_numbers(draft("retain 40% of them", "40% off")).verdict == PASS  # in the variant
    assert check_numbers(draft("retain 40% of them", "a discount")).verdict == FAIL


def test_a_measurement_window_is_a_design_choice_not_a_claim() -> None:
    assert check_numbers(draft("90-day retention over 3 months and 2 weeks")).verdict == PASS
    assert check_numbers(draft("retention lifted 12% in 30 days")).verdict == FAIL  # the 12


def test_numbers_inside_words_and_identifiers_are_not_numbers() -> None:
    assert numbers_in("our Q2 plan and discount_15 and v2") == []
    assert [shown for shown, _ in numbers_in("4,987 customers at 0.24 and 15%")] == [
        "4,987",
        "0.24",
        "15%",
    ]


def test_a_number_in_the_retrieved_context_is_supported() -> None:
    assert check_numbers(draft("the playbook's 18 months", context=["Playbook: 18 months"])
                         ).verdict == PASS  # fmt: skip


def test_known_gaps_are_pinned() -> None:
    # A target is flagged: it cannot be told from a predicted effect.
    assert check_numbers(draft("reduces churn to below 50%")).verdict == FAIL
    # A number the variant itself invents is allowed: variant numbers are the proposal.
    assert check_numbers(draft("customers above the 0.50 quantile",
                               "10% off above 0.50 quantile")).verdict == PASS  # fmt: skip


# --- as a grade, and against labels ---


def test_only_the_covered_criteria_are_graded() -> None:
    grade = check_item(draft("a $5 credit", currency="EUR"))
    assert grade.verdicts == {
        "C1": UNGRADED, "C2": UNGRADED, "C3": FAIL, "C4": PASS, "C5": UNGRADED,
    }  # fmt: skip


def test_veto_stats_are_precision_and_recall_of_the_flags() -> None:
    labels = {"d01": "no", "d02": "yes", "d03": "no", "d04": "no"}
    items = [{"item": k, "labels": dict.fromkeys(CRITERIA, v)} for k, v in labels.items()]
    flags = {"d01": FAIL, "d02": FAIL, "d03": PASS, "d04": FAIL}  # 3 flagged, 2 of them real
    grades = {
        k: Grade(dict.fromkeys(CRITERIA, v), dict.fromkeys(CRITERIA, "r")) for k, v in flags.items()
    }
    stats = veto_stats(items, grades)["C4"]
    assert stats["flagged"] == 3 and stats["labelled_fail"] == 3
    assert stats["precision"] == pytest.approx(2 / 3) and stats["recall"] == pytest.approx(2 / 3)


def test_run_checks_reports_only_c3_and_c4_and_who_labelled(tmp_path: Path) -> None:
    labels, key = tmp_path / "drafts.jsonl", tmp_path / "key.jsonl"
    bad = draft("retain 25% of them", currency=None) | {"item": "d01"}
    bad["labels"] = dict.fromkeys(CRITERIA, "yes") | {"C4": "no"}
    blank = draft("x") | {"item": "d02", "labels": dict.fromkeys(CRITERIA)}
    write_jsonl(labels, [bad, blank])
    write_jsonl(key, [{"item": "d01", "engine": "e"}])
    report = run_checks(labels=labels, key=key, labeller="someone")
    assert report["items_checked"] == 1 and report["labeller"] == "someone"
    assert set(report["criteria"]) == {"C3", "C4"}
    assert report["flagged"] == {"d01": {"C4": "25%: not in the evidence, context or variant"}}
    with pytest.raises(ValueError, match="no fully labelled"):
        run_checks(labels=_empty(tmp_path), key=key)


def _empty(tmp_path: Path) -> Path:
    path = tmp_path / "empty.jsonl"
    write_jsonl(path, [draft("x") | {"labels": dict.fromkeys(CRITERIA)}])
    return path
