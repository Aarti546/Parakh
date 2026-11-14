from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    jwt_secret: SecretStr = Field(min_length=32)
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    access_token_minutes: int = Field(default=30, gt=0)
    cookie_secure: bool = False
    cors_origins: list[str] = ["http://localhost:8000"]
    environment: str = "development"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("cors_origins")
    @classmethod
    def production_cors_must_be_explicit(cls, origins: list[str]) -> list[str]:
        if "*" in origins:
            raise ValueError("CORS_ORIGINS must not contain '*'")
        return origins

    @model_validator(mode="after")
    def production_cookies_must_be_secure(self) -> "Settings":
        if self.environment.lower() == "production":
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true in production.")
            if any(not origin.startswith("https://") for origin in self.cors_origins):
                raise ValueError("Production CORS_ORIGINS must use HTTPS.")
        return self

    @field_validator("database_url")
    @classmethod
    def use_psycopg_driver(cls, database_url: str) -> str:
        if database_url.startswith("postgres://"):
            return database_url.replace("postgres://", "postgresql+psycopg://", 1)
        if database_url.startswith("postgresql://"):
            return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        return database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
