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
    p2p_var_dir: Path = Path("var")  # dev/test: local artifacts and smoke work dirs
    # dev only: "docker" runs the real job images locally, for canary papers (A-25)
    p2p_job_backend: Literal["fake", "docker"] = "fake"
    # S3 credentials of sa-api-runtime (control-plane buckets); loaded by deploy/secrets.sh
    runtime_s3_key_id: SecretStr | None = None
    runtime_s3_secret: SecretStr | None = None
    p2p_control_bucket: str | None = None  # control-plane artifacts and Proof Bundles
    nebius_sandbox_project_id: str | None = None
    # Job launching (sa-job-launcher): SDK authorized key (JSON, or a file in dev/WSL) and the
    # S3 key for the per-run buckets
    job_launcher_auth_key: SecretStr | None = None
    job_launcher_auth_key_file: Path | None = None
    job_launcher_s3_key_id: SecretStr | None = None
    job_launcher_s3_secret: SecretStr | None = None
    gateway_instance_id: str | None = None  # None: the gateway is always on (not managed)
    # Bundle signing in KMS as sa-api-runtime: SDK authorized key (JSON, or a file)
    runtime_auth_key: SecretStr | None = None
    runtime_auth_key_file: Path | None = None
    # Execution plane (infra outputs and image digests from the deploy, required in prod;
    # checked by app.main.execution_config)
    p2p_provisioner_image: str | None = None
    p2p_runner_image: str | None = None
    p2p_runner_cpu_image: str | None = None
    p2p_subnet_provision: str | None = None
    p2p_subnet_exec: str | None = None
    p2p_gpu_preemptible: bool | None = None  # explicit in prod: never preemptible for demos
    p2p_bundle_key_id: str | None = None  # KMS key that signs Proof Bundles
    p2p_build_commit: str | None = None  # recorded in Proof Bundles

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
