import pytest
from app.services.statistics import (
    sample_ratio_mismatch,
    sample_size_per_variant,
    two_proportion_z_test,
)
from scipy.stats import norm


def test_two_proportion_z_test_matches_scipy() -> None:
    result = two_proportion_z_test(120, 1000, 150, 1000)
    control_rate = 0.12
    treatment_rate = 0.15
    pooled = (120 + 150) / 2000
    standard_error = (pooled * (1 - pooled) * (1 / 1000 + 1 / 1000)) ** 0.5
    expected_z = (treatment_rate - control_rate) / standard_error
    assert result["z_score"] == pytest.approx(expected_z)
    assert result["p_value"] == pytest.approx(2 * norm.sf(abs(expected_z)))
    assert result["ci_low"] < 0.03 < result["ci_high"]


def test_sample_size_and_srm() -> None:
    assert sample_size_per_variant(0.1, 0.02) > 1000
    assert sample_ratio_mismatch([500, 500], [0.5, 0.5])["mismatch"] is False
    assert sample_ratio_mismatch([900, 100], [0.5, 0.5])["mismatch"] is True


def test_statistics_reject_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        two_proportion_z_test(2, 1, 0, 1)
    with pytest.raises(ValueError):
        sample_size_per_variant(0.1, 0)
    with pytest.raises(ValueError):
        sample_ratio_mismatch([1, 1], [0.4, 0.4])
    with pytest.raises(ValueError):
        sample_ratio_mismatch([1], [1])
    with pytest.raises(ValueError):
        sample_ratio_mismatch([-1, 2], [0.5, 0.5])
    with pytest.raises(ValueError):
        sample_ratio_mismatch([0, 0], [0.5, 0.5])
    with pytest.raises(ValueError):
        two_proportion_z_test(0, 0, 0, 1)
    with pytest.raises(ValueError):
        two_proportion_z_test(0, 1, 0, 1, alpha=1)
    with pytest.raises(ValueError):
        sample_size_per_variant(0, 0.1)
    with pytest.raises(ValueError):
        sample_size_per_variant(0.1, 0.1, power=1)


def test_srm_series_branch_and_zero_standard_error() -> None:
    near_even = sample_ratio_mismatch([51, 49], [0.5, 0.5])
    assert near_even["p_value"] == pytest.approx(0.8414805811, rel=1e-6)
    equal_zero = two_proportion_z_test(0, 100, 0, 100)
    assert equal_zero["z_score"] == 0
    assert equal_zero["relative_uplift"] == 0
