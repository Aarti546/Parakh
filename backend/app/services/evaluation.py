from __future__ import annotations

from typing import Any

import mmh3

SUPPORTED_OPERATORS = {"equals", "not_equals", "in", "contains", "gt", "lt"}


def _enabled_value(config: dict[str, Any], default_value: Any) -> Any:
    value = config.get("on_value")
    if value is not None:
        return value
    return True if isinstance(default_value, bool) else default_value


def bucket_user(flag_key: str, salt: str, user_id: str) -> int:
    """Return a stable integer in [0, 10000) for deterministic rollout assignment."""
    digest = mmh3.hash(f"{flag_key}{salt}{user_id}", signed=False)
    return digest % 10_000


def _condition_matches(condition: dict[str, Any], attributes: dict[str, Any]) -> bool:
    attribute = condition.get("attribute")
    operator = condition.get("operator")
    expected = condition.get("value")
    if not isinstance(attribute, str) or operator not in SUPPORTED_OPERATORS:
        return False
    actual = attributes.get(attribute)
    if actual is None:
        return False
    try:
        if operator == "equals":
            return actual == expected
        if operator == "not_equals":
            return actual != expected
        if operator == "in":
            return isinstance(expected, list) and actual in expected
        if operator == "contains":
            return isinstance(actual, (str, list)) and expected in actual
        if operator == "gt":
            return actual > expected
        if operator == "lt":
            return actual < expected
    except (TypeError, ValueError):
        return False
    return False


def targeting_matches(rules: dict[str, Any] | None, attributes: dict[str, Any]) -> bool:
    if not rules:
        return False
    operator = rules.get("operator", "AND")
    conditions = rules.get("conditions", [])
    if operator not in {"AND", "OR"} or not isinstance(conditions, list) or not conditions:
        return False
    results = [
        _condition_matches(condition, attributes)
        for condition in conditions
        if isinstance(condition, dict)
    ]
    if len(results) != len(conditions):
        return False
    return all(results) if operator == "AND" else any(results)


def evaluate_flag(
    config: dict[str, Any],
    user_id: str,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attributes = attributes or {}
    default_value = config["default_value"]
    if config.get("kill_switch", False):
        return {"value": default_value, "reason": "KILL_SWITCH"}
    if not config.get("enabled", False):
        return {"value": default_value, "reason": "DISABLED"}
    if targeting_matches(config.get("targeting_rules"), attributes):
        return {
            "value": _enabled_value(config, default_value),
            "reason": "RULE_MATCH",
        }
    rollout = config.get("rollout_percentage", 0.0)
    if not isinstance(rollout, (int, float)) or not 0 <= rollout <= 100:
        return {"value": default_value, "reason": "DEFAULT"}
    bucket = bucket_user(config["key"], config.get("salt", config["key"]), str(user_id))
    if bucket < rollout * 100:
        experiment = config.get("experiment")
        if experiment:
            variant_bucket = (
                bucket_user(
                    config["key"], f"{config.get('salt', config['key'])}:variant", str(user_id)
                )
                / 100
            )
            cumulative = 0.0
            for variant in experiment["variants"]:
                cumulative += variant["traffic_percentage"]
                if variant_bucket < cumulative:
                    return {
                        "value": variant["value"],
                        "reason": "EXPERIMENT",
                        "variant": variant["key"],
                    }
        return {
            "value": _enabled_value(config, default_value),
            "reason": "ROLLOUT",
        }
    return {"value": default_value, "reason": "DEFAULT"}


def evaluate_all(
    configs: list[dict[str, Any]],
    user_id: str,
    attributes: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    return {config["key"]: evaluate_flag(config, user_id, attributes) for config in configs}
