from math import erf, exp, lgamma, log, sqrt
from statistics import NormalDist

import numpy as np


def _normal_cdf(value: float) -> float:
    return 0.5 * (1 + erf(value / sqrt(2)))


def two_proportion_z_test(
    control_conversions: int,
    control_users: int,
    treatment_conversions: int,
    treatment_users: int,
    alpha: float = 0.05,
) -> dict[str, float | bool]:
    if min(control_conversions, treatment_conversions) < 0:
        raise ValueError("conversion counts cannot be negative")
    if control_users <= 0 or treatment_users <= 0:
        raise ValueError("each variant needs at least one user")
    if control_conversions > control_users or treatment_conversions > treatment_users:
        raise ValueError("conversions cannot exceed users")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between zero and one")
    p_control = control_conversions / control_users
    p_treatment = treatment_conversions / treatment_users
    pooled = (control_conversions + treatment_conversions) / (control_users + treatment_users)
    pooled_se = sqrt(pooled * (1 - pooled) * (1 / control_users + 1 / treatment_users))
    difference = p_treatment - p_control
    z_score = difference / pooled_se if pooled_se else 0.0
    p_value = 2 * (1 - _normal_cdf(abs(z_score)))
    confidence_z = NormalDist().inv_cdf(1 - alpha / 2)
    ci_se = sqrt(
        p_control * (1 - p_control) / control_users
        + p_treatment * (1 - p_treatment) / treatment_users
    )
    return {
        "control_rate": p_control,
        "treatment_rate": p_treatment,
        "relative_uplift": (p_treatment / p_control - 1) if p_control else 0.0,
        "z_score": z_score,
        "p_value": p_value,
        "ci_low": difference - confidence_z * ci_se,
        "ci_high": difference + confidence_z * ci_se,
        "significant": p_value < alpha,
    }


def sample_size_per_variant(
    baseline_rate: float,
    minimum_detectable_effect: float,
    power: float = 0.8,
    alpha: float = 0.05,
) -> int:
    if not 0 < baseline_rate < 1:
        raise ValueError("baseline_rate must be between zero and one")
    if not 0 < minimum_detectable_effect < 1 - baseline_rate:
        raise ValueError("minimum_detectable_effect is too large for this baseline rate")
    if not 0 < power < 1 or not 0 < alpha < 1:
        raise ValueError("power and alpha must be between zero and one")
    treatment_rate = baseline_rate + minimum_detectable_effect
    average_rate = (baseline_rate + treatment_rate) / 2
    z_alpha = NormalDist().inv_cdf(1 - alpha / 2)
    z_power = NormalDist().inv_cdf(power)
    numerator = (
        z_alpha * sqrt(2 * average_rate * (1 - average_rate))
        + z_power
        * sqrt(baseline_rate * (1 - baseline_rate) + treatment_rate * (1 - treatment_rate))
    ) ** 2
    return int(numerator / minimum_detectable_effect**2) + 1


def _regularized_gamma_q(shape: float, value: float) -> float:
    if shape <= 0 or value < 0:
        raise ValueError("gamma arguments are out of range")
    if value == 0:
        return 1.0
    if value < shape + 1:
        term = total = 1 / shape
        current_shape = shape
        for _ in range(10_000):
            current_shape += 1
            term *= value / current_shape
            total += term
            if abs(term) < abs(total) * 1e-14:
                break
        lower = total * exp(-value + shape * log(value) - lgamma(shape))
        return min(1.0, max(0.0, 1 - lower))
    b = value + 1 - shape
    c = 1e300
    d = 1 / b
    result = d
    for iteration in range(1, 10_001):
        coefficient = -iteration * (iteration - shape)
        b += 2
        d = coefficient * d + b
        if abs(d) < 1e-300:
            d = 1e-300
        c = b + coefficient / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1 / d
        fraction = d * c
        result *= fraction
        if abs(fraction - 1) < 1e-14:
            break
    return min(1.0, max(0.0, exp(-value + shape * log(value) - lgamma(shape)) * result))


def sample_ratio_mismatch(
    observed: list[int], expected_shares: list[float]
) -> dict[str, float | bool]:
    if len(observed) != len(expected_shares) or len(observed) < 2:
        raise ValueError("observed counts and expected shares need equal length >= 2")
    if any(count < 0 for count in observed) or any(share <= 0 for share in expected_shares):
        raise ValueError("counts must be non-negative and expected shares positive")
    if abs(sum(expected_shares) - 1) > 1e-6:
        raise ValueError("expected shares must sum to 1")
    observed_values = np.asarray(observed, dtype=np.float64)
    shares = np.asarray(expected_shares, dtype=np.float64)
    total = float(observed_values.sum())
    if total == 0:
        raise ValueError("at least one observation is required")
    expected_counts = total * shares
    statistic = float(np.sum(np.square(observed_values - expected_counts) / expected_counts))
    p_value = _regularized_gamma_q((len(observed) - 1) / 2, statistic / 2)
    return {"chi_square": statistic, "p_value": p_value, "mismatch": p_value < 0.001}
