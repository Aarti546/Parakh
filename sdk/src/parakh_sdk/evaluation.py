from typing import Any

import mmh3


def _enabled_value(config: dict[str, Any], default_value: Any) -> Any:
    value = config.get("on_value")
    if value is not None:
        return value
    return True if isinstance(default_value, bool) else default_value


def bucket_user(flag_key: str, salt: str, user_id: str) -> int:
    """Keep local SDK bucketing byte-for-byte compatible with the API evaluator."""
    return mmh3.hash(f"{flag_key}{salt}{user_id}", signed=False) % 10_000


def _condition_matches(condition: dict[str, Any], attributes: dict[str, Any]) -> bool:
    attribute = condition.get("attribute")
    if not isinstance(attribute, str):
        return False
    actual = attributes.get(attribute)
    expected = condition.get("value")
    operator = condition.get("operator")
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


def evaluate(config: dict[str, Any], user_id: str, attributes: dict[str, Any]) -> dict[str, Any]:
    default_value = config["default_value"]
    if config.get("kill_switch"):
        return {"value": default_value, "reason": "KILL_SWITCH"}
    if not config.get("enabled"):
        return {"value": default_value, "reason": "DISABLED"}
    rules = config.get("targeting_rules")
    if rules:
        conditions = rules.get("conditions", [])
        checks = [_condition_matches(item, attributes) for item in conditions]
        matched = bool(checks) and (
            all(checks) if rules.get("operator", "AND") == "AND" else any(checks)
        )
        if matched:
            return {
                "value": _enabled_value(config, default_value),
                "reason": "RULE_MATCH",
            }
    rollout = config.get("rollout_percentage", 0)
    if isinstance(rollout, (int, float)) and 0 <= rollout <= 100:
        bucket = bucket_user(config["key"], config.get("salt", config["key"]), str(user_id))
        if bucket < rollout * 100:
            experiment = config.get("experiment")
            if experiment:
                variant_bucket = (
                    bucket_user(
                        config["key"],
                        f"{config.get('salt', config['key'])}:variant",
                        str(user_id),
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
