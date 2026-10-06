"""The arithmetic behind a behaviour report: small, pure, and checked by hand."""

from __future__ import annotations

from collections.abc import Sequence
from math import sqrt


def pass_at_1(passed: Sequence[bool]) -> float:
    """The chance one trial passes, estimated as the share that did."""
    return sum(passed) / len(passed) if passed else 0.0


def pass_hat_k(passed: Sequence[bool]) -> bool:
    """Every one of the k trials passed. The bar a user relies on is "every time", not
    "usually", so this is reported beside the mean and never replaced by it."""
    return bool(passed) and all(passed)


def wilson(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval for a proportion (95% by default).

    Used instead of the normal approximation because trial counts here are small and
    rates sit near 0 or 1, where the normal interval leaves [0, 1].
    """
    if trials == 0:
        return (0.0, 1.0)
    p = successes / trials
    scale = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / scale
    half = z * sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / scale
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == trials else min(1.0, centre + half)
    return (low, high)


def cohens_kappa(a: Sequence[str], b: Sequence[str]) -> float | None:
    """Agreement between two raters beyond what chance would give.

    1 is perfect, 0 is no better than chance, below 0 is worse. None when it is
    undefined: no pairs, or both raters used one label throughout, so chance agreement
    is already 1 and there is nothing to measure.
    """
    if len(a) != len(b):
        raise ValueError("raters must label the same items")
    n = len(a)
    if n == 0:
        return None
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    expected = sum((a.count(label) / n) * (b.count(label) / n) for label in set(a) | set(b))
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)
