import pytest
from app.services.evaluation import bucket_user, evaluate_all, evaluate_flag


def test_bucket_is_stable_across_user_inputs() -> None:
    first = bucket_user("new-checkout", "salt", "user-42")
    assert first == bucket_user("new-checkout", "salt", "user-42")
    assert 0 <= first < 10_000
    assert first != bucket_user("new-checkout", "salt", "user-43")
    assert first != bucket_user("other-flag", "salt", "user-42")


def test_kill_switch_disabled_targeting_rollout_order() -> None:
    config = {
        "key": "checkout",
        "default_value": False,
        "enabled": True,
        "kill_switch": True,
        "rollout_percentage": 100,
        "targeting_rules": {
            "operator": "AND",
            "conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}],
        },
    }
    assert evaluate_flag(config, "u1", {"plan": "pro"})["reason"] == "KILL_SWITCH"
    config["kill_switch"] = False
    assert evaluate_flag(config, "u1", {"plan": "pro"}) == {
        "value": True,
        "reason": "RULE_MATCH",
    }
    config["targeting_rules"] = None
    config["rollout_percentage"] = 0
    assert evaluate_flag(config, "u1") == {"value": False, "reason": "DEFAULT"}
    config["enabled"] = False
    config["rollout_percentage"] = 100
    assert evaluate_flag(config, "u1") == {"value": False, "reason": "DISABLED"}


@pytest.mark.parametrize(
    ("operator", "value", "expected"),
    [
        ("equals", "CA", True),
        ("not_equals", "US", True),
        ("in", ["US", "CA"], True),
        ("contains", "C", True),
        ("gt", 2, True),
        ("lt", 4, True),
        ("gt", "not-a-number", False),
    ],
)
def test_targeting_operators(operator: str, value: object, expected: bool) -> None:
    config = {
        "key": "flag",
        "default_value": False,
        "enabled": True,
        "rollout_percentage": 0,
        "targeting_rules": {
            "operator": "AND",
            "conditions": [
                {
                    "attribute": "country" if operator not in {"gt", "lt"} else "age",
                    "operator": operator,
                    "value": value,
                }
            ],
        },
    }
    attrs = {"country": "CA", "age": 3}
    result = evaluate_flag(config, "user", attrs)
    assert result["reason"] == ("RULE_MATCH" if expected else "DEFAULT")


def test_evaluate_all_returns_each_flag() -> None:
    configs = [
        {"key": "a", "default_value": False, "enabled": False},
        {"key": "b", "default_value": "x", "enabled": False},
    ]
    assert set(evaluate_all(configs, "u")) == {"a", "b"}
