from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FlagType = Literal["boolean", "string", "number"]


def value_matches_type(value: Any, value_type: FlagType) -> bool:
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "string":
        return isinstance(value, str)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return not isinstance(value, float) or isfinite(value)


class FlagCreate(BaseModel):
    key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", max_length=120)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    value_type: FlagType
    default_value: Any

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @model_validator(mode="after")
    def default_value_has_declared_type(self) -> "FlagCreate":
        if not value_matches_type(self.default_value, self.value_type):
            raise ValueError(f"default_value must be a {self.value_type}")
        return self


class FlagUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    value_type: FlagType | None = None
    default_value: Any = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @model_validator(mode="after")
    def patch_values_cannot_be_null(self) -> "FlagUpdate":
        if any(
            field in self.model_fields_set and getattr(self, field) is None
            for field in ("name", "description", "value_type", "default_value")
        ):
            raise ValueError("Flag fields cannot be set to null.")
        return self


class FlagRead(BaseModel):
    id: int
    project_id: int
    key: str
    name: str
    description: str
    value_type: FlagType
    default_value: Any
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class EnvironmentFlagUpdate(BaseModel):
    enabled: bool | None = None
    kill_switch: bool | None = None
    rollout_percentage: float | None = Field(default=None, ge=0, le=100)
    targeting_rules: dict[str, Any] | None = None
    on_value: Any = None

    @model_validator(mode="after")
    def validate_targeting_rules(self) -> "EnvironmentFlagUpdate":
        if self.targeting_rules is None:
            return self
        operator = self.targeting_rules.get("operator", "AND")
        conditions = self.targeting_rules.get("conditions")
        allowed = {"equals", "not_equals", "in", "contains", "gt", "lt"}
        if operator not in {"AND", "OR"} or not isinstance(conditions, list) or not conditions:
            raise ValueError("targeting_rules must contain AND/OR and a non-empty conditions list")
        for condition in conditions:
            if (
                not isinstance(condition, dict)
                or not isinstance(condition.get("attribute"), str)
                or condition.get("operator") not in allowed
                or "value" not in condition
            ):
                raise ValueError(
                    "Each targeting condition needs attribute, supported operator, and value"
                )
        return self


class EnvironmentFlagRead(BaseModel):
    environment_id: int
    flag_id: int
    enabled: bool
    kill_switch: bool
    rollout_percentage: float
    targeting_rules: dict[str, Any] | None
    on_value: Any | None
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
