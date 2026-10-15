import pytest
from pydantic import ValidationError

from app.settings import Settings, get_settings

_ENV_VARS = (
    "P2P_ENV",
    "NEBIUS_API_KEY",
    "TAVILY_API_KEY",
    "TF_PROJECT_ID",
    "DB_URL",
    "S3_ENDPOINT",
    "S3_REGION",
    "SESSION_KEY",
    "ADMIN_TOKEN",
    "CONFIG_DIR",
)


@pytest.fixture(autouse=True)
def empty_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


def test_settings_defaults_to_test_env() -> None:
    assert Settings().p2p_env == "test"


def test_secrets_not_in_repr() -> None:
    assert "x" not in repr(Settings(nebius_api_key="x"))
    secret = "tf-secret-value-0123456789"
    rendered = repr(Settings(nebius_api_key=secret, admin_token=secret, session_key=secret))
    assert secret not in rendered


def test_prod_requires_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("P2P_ENV", "prod")
    monkeypatch.setenv("SESSION_KEY", "s" * 64)
    monkeypatch.setenv("ADMIN_TOKEN", "a" * 64)
    with pytest.raises(ValidationError, match="NEBIUS_API_KEY"):
        Settings()


def test_prod_requires_explicit_session_key_and_admin_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("P2P_ENV", "prod")
    monkeypatch.setenv("NEBIUS_API_KEY", "k" * 32)
    with pytest.raises(ValidationError, match="SESSION_KEY") as excinfo:
        Settings()
    assert "ADMIN_TOKEN" in str(excinfo.value)


def test_prod_treats_empty_key_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("P2P_ENV", "prod")
    monkeypatch.setenv("NEBIUS_API_KEY", "")
    monkeypatch.setenv("SESSION_KEY", "s" * 64)
    monkeypatch.setenv("ADMIN_TOKEN", "a" * 64)
    with pytest.raises(ValidationError, match="NEBIUS_API_KEY"):
        Settings()


def test_prod_with_all_keys_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("P2P_ENV", "prod")
    monkeypatch.setenv("NEBIUS_API_KEY", "k" * 32)
    monkeypatch.setenv("SESSION_KEY", "s" * 64)
    monkeypatch.setenv("ADMIN_TOKEN", "a" * 64)
    settings = Settings()
    assert settings.p2p_env == "prod"
    assert settings.nebius_api_key is not None
    assert settings.nebius_api_key.get_secret_value() == "k" * 32


def test_prod_validation_error_does_not_leak_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    api_key = "nebius-key-" + "n" * 30
    session_key = "session-key-" + "s" * 52
    monkeypatch.setenv("P2P_ENV", "prod")
    monkeypatch.setenv("NEBIUS_API_KEY", api_key)
    monkeypatch.setenv("SESSION_KEY", session_key)
    monkeypatch.setenv("ADMIN_TOKEN", "")
    with pytest.raises(ValidationError, match="ADMIN_TOKEN") as excinfo:
        Settings()
    rendered = str(excinfo.value) + repr(excinfo.value.errors())
    for secret in (api_key, session_key, api_key[-8:], session_key[-8:]):
        assert secret not in rendered


@pytest.mark.parametrize("name", ["SESSION_KEY", "ADMIN_TOKEN"])
def test_prod_rejects_short_secrets(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("P2P_ENV", "prod")
    monkeypatch.setenv("NEBIUS_API_KEY", "k" * 32)
    monkeypatch.setenv("SESSION_KEY", "s" * 64)
    monkeypatch.setenv("ADMIN_TOKEN", "a" * 64)
    monkeypatch.setenv(name, "x" * 31)
    with pytest.raises(ValidationError, match=f"{name} must be at least 32 characters"):
        Settings()


def test_prod_rejects_identical_session_key_and_admin_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("P2P_ENV", "prod")
    monkeypatch.setenv("NEBIUS_API_KEY", "k" * 32)
    monkeypatch.setenv("SESSION_KEY", "z" * 64)
    monkeypatch.setenv("ADMIN_TOKEN", "z" * 64)
    with pytest.raises(ValidationError, match="must differ"):
        Settings()


def test_non_prod_generates_ephemeral_session_key_and_admin_token() -> None:
    first, second = Settings(), Settings()
    assert len(first.session_key.get_secret_value()) >= 32
    assert first.session_key.get_secret_value() != second.session_key.get_secret_value()
    assert first.admin_token.get_secret_value() != first.session_key.get_secret_value()


def test_invalid_env_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("P2P_ENV", "staging")
    with pytest.raises(ValidationError):
        Settings()


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
