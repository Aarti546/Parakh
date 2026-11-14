import pytest
from parakh_sdk.evaluation import _condition_matches, bucket_user, evaluate


def test_bucket_is_deterministic_and_in_range() -> None:
    assert bucket_user("checkout", "salt", "user-1") == bucket_user("checkout", "salt", "user-1")
    assert 0 <= bucket_user("checkout", "salt", "user-1") < 10_000
    assert bucket_user("checkout", "salt", "user-1") != bucket_user("checkout", "salt", "user-2")


def test_evaluation_order_and_targeting() -> None:
    config = {
        "key": "checkout",
        "salt": "checkout",
        "enabled": True,
        "kill_switch": True,
        "default_value": False,
        "rollout_percentage": 100,
        "targeting_rules": {
            "operator": "AND",
            "conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}],
        },
    }
    assert evaluate(config, "one", {"plan": "pro"}) == {
        "value": False,
        "reason": "KILL_SWITCH",
    }
    config["kill_switch"] = False
    assert evaluate(config, "one", {"plan": "pro"}) == {
        "value": True,
        "reason": "RULE_MATCH",
    }
    config["targeting_rules"] = None
    config["rollout_percentage"] = 0
    assert evaluate(config, "one", {}) == {"value": False, "reason": "DEFAULT"}


def test_or_targeting_and_invalid_rules_fall_back_safely() -> None:
    config = {
        "key": "checkout",
        "default_value": False,
        "enabled": True,
        "rollout_percentage": 0,
        "targeting_rules": {
            "operator": "OR",
            "conditions": [
                {"attribute": "country", "operator": "equals", "value": "IN"},
                {"attribute": "plan", "operator": "equals", "value": "pro"},
            ],
        },
    }
    assert evaluate(config, "u1", {"plan": "pro"})["reason"] == "RULE_MATCH"
    config["targeting_rules"] = {"operator": "XOR", "conditions": []}
    assert evaluate(config, "u1", {}) == {"value": False, "reason": "DEFAULT"}
    config["enabled"] = False
    assert evaluate(config, "u1", {})["reason"] == "DISABLED"


def test_non_boolean_enabled_value_and_rollout_reasons() -> None:
    config = {
        "key": "banner",
        "default_value": "control",
        "on_value": "treatment",
        "enabled": True,
        "kill_switch": False,
        "rollout_percentage": 100,
    }
    assert evaluate(config, "u1", {}) == {"value": "treatment", "reason": "ROLLOUT"}
    config["kill_switch"] = True
    assert evaluate(config, "u1", {}) == {"value": "control", "reason": "KILL_SWITCH"}


@pytest.mark.parametrize(
    ("condition", "attributes", "expected"),
    [
        ({"attribute": "plan", "operator": "not_equals", "value": "free"}, {"plan": "pro"}, True),
        ({"attribute": "plan", "operator": "in", "value": ["pro", "team"]}, {"plan": "pro"}, True),
        (
            {"attribute": "email", "operator": "contains", "value": "@example.com"},
            {"email": "a@example.com"},
            True,
        ),
        (
            {"attribute": "roles", "operator": "contains", "value": "admin"},
            {"roles": ["admin"]},
            True,
        ),
        ({"attribute": "age", "operator": "gt", "value": 18}, {"age": 20}, True),
        ({"attribute": "age", "operator": "lt", "value": 18}, {"age": 16}, True),
        ({"attribute": "age", "operator": "gt", "value": 18}, {"age": "unknown"}, False),
        ({"attribute": "missing", "operator": "equals", "value": 1}, {}, False),
        ({"attribute": 1, "operator": "equals", "value": 1}, {"1": 1}, False),
        ({"attribute": "plan", "operator": "unsupported", "value": "pro"}, {"plan": "pro"}, False),
    ],
)
def test_targeting_conditions_match_expected_attributes(condition, attributes, expected) -> None:
    assert _condition_matches(condition, attributes) is expected
