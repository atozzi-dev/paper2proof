"""Process configuration, read from environment variables only.

`.env` is never read implicitly: `make dev` and the deploy scripts load it into the
environment, so tests always see a clean, explicit configuration.
"""

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["test", "dev", "prod"]
MIN_SECRET_LEN = 32


def _ephemeral_secret() -> SecretStr:
    """Random per-process secret, used only outside prod (sessions reset on restart)."""
    return SecretStr(secrets.token_hex(32))


class Settings(BaseSettings):
    # hide_input_in_errors: a failed validation must never echo secret values into logs.
    model_config = SettingsConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)

    p2p_env: Env = "test"
    nebius_api_key: SecretStr | None = None
    tavily_api_key: SecretStr | None = None
    tf_project_id: str | None = None
    smoke_executor: Literal["sandboxes", "job", "local"] = "job"  # "sandboxes" once S-3 passes
    db_url: str = "sqlite+aiosqlite:///./var/p2p.db"
    s3_endpoint: str | None = None
    s3_region: str = "eu-north1"
    session_key: SecretStr = Field(default_factory=_ephemeral_secret)
    admin_token: SecretStr = Field(default_factory=_ephemeral_secret)
    config_dir: Path = Path("config")

    def model_post_init(self, context: object, /) -> None:
        # Checked after validation and raised with a redacted input: an `after` validator
        # would carry every raw value (including secrets) in ValidationError.errors().
        if self.p2p_env != "prod":
            return
        problems: list[str] = []
        if self.nebius_api_key is None or not self.nebius_api_key.get_secret_value():
            problems.append("NEBIUS_API_KEY is required")
        for name in ("session_key", "admin_token"):
            value: str = getattr(self, name).get_secret_value()
            if name not in self.model_fields_set or not value:
                problems.append(f"{name.upper()} is required")
            elif len(value) < MIN_SECRET_LEN:
                problems.append(f"{name.upper()} must be at least {MIN_SECRET_LEN} characters")
        if self.session_key.get_secret_value() == self.admin_token.get_secret_value():
            problems.append("SESSION_KEY and ADMIN_TOKEN must differ")
        if problems:
            error = InitErrorDetails(
                type=PydanticCustomError(
                    "prod_secrets", "P2P_ENV=prod: {problems}", {"problems": "; ".join(problems)}
                ),
                loc=(),
                input="<redacted>",
            )
            raise ValidationError.from_exception_data(type(self).__name__, [error])


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
