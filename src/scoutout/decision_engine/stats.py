"""Small, dependency-free statistics used by the decision engine."""

from __future__ import annotations

import math
from statistics import NormalDist


def z_one_sided(confidence: float) -> float:
    return NormalDist().inv_cdf(confidence)


def wilson_lower(x: int, n: int, confidence: float) -> float:
    if n == 0:
        return 0.0
    z = z_one_sided(confidence)
    p = x / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def wilson_upper(x: int, n: int, confidence: float) -> float:
    if n == 0:
        return 1.0
    return 1.0 - wilson_lower(n - x, n, confidence)


def rogan_gladen(p_obs: float, se: float, sp: float) -> float:
    """Correct an observed prevalence for detector error: (p + Sp - 1) / (Se + Sp - 1), clipped to [0, 1]."""
    j = se + sp - 1
    if j <= 0:
        raise ValueError("detector is uninformative (Se + Sp <= 1)")
    return min(1.0, max(0.0, (p_obs + sp - 1) / j))


def sprt_llr(x: int, n: int, p0: float, p1: float) -> float:
    """Wald SPRT log-likelihood ratio log L(p1)/L(p0) for x hits in n draws."""
    p0 = min(max(p0, 1e-9), 1 - 1e-9)
    p1 = min(max(p1, 1e-9), 1 - 1e-9)
    return x * math.log(p1 / p0) + (n - x) * math.log((1 - p1) / (1 - p0))
