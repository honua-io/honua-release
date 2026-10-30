"""One-sided 95% Clopper-Pearson intervals. No third-party math library."""

from __future__ import annotations

import math


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function."""
    max_iter = 200
    epsilon = 3e-12
    am = 1.0
    bm = 1.0
    az = 1.0
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    bz = 1.0 - qab * x / qap
    for m in range(1, max_iter + 1):
        em = float(m)
        tem = em + em
        d = em * (b - em) * x / ((qam + tem) * (a + tem))
        ap = az + d * am
        bp = bz + d * bm
        d = -(a + em) * (qab + em) * x / ((a + tem) * (qap + tem))
        app = ap + d * az
        bpp = bp + d * bz
        am, bm, az, bz = ap, bp, app, bpp
        if abs(az) < 1e-30:
            continue
        am /= az
        bm /= az
        bz /= az
        az = 1.0
        if abs(bpp) < 1e-30 or abs(bp) < 1e-30:
            continue
        if abs(az - bm / bz) < epsilon * abs(az):
            break
    return az / bz


def regularized_beta(x: float, a: float, b: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(a * math.log(x) + b * math.log(1.0 - x) - log_beta) / a
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x)
    return 1.0 - math.exp((b * math.log(1.0 - x) + a * math.log(x)) - log_beta) / b * _betacf(b, a, 1.0 - x)


def beta_quantile(p: float, a: float, b: float) -> float:
    """Inverse of the regularized incomplete beta, by bisection."""
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    lo = 0.0
    hi = 1.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if regularized_beta(mid, a, b) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def clopper_pearson_lower(successes: int, trials: int, alpha: float = 0.05):
    """One-sided lower bound. Zero successes is 0. A perfect run is alpha ** (1/n)."""
    if trials <= 0:
        return None
    if successes <= 0:
        return 0.0
    if successes >= trials:
        return alpha ** (1.0 / trials)
    return beta_quantile(alpha, successes, trials - successes + 1)


def clopper_pearson_upper(events: int, trials: int, alpha: float = 0.05):
    """One-sided upper bound. Zero events is 1 - alpha ** (1/n)."""
    if trials <= 0:
        return None
    if events >= trials:
        return 1.0
    if events <= 0:
        return 1.0 - alpha ** (1.0 / trials)
    return beta_quantile(1.0 - alpha, events + 1, trials - events)
