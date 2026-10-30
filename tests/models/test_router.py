import pytest

from app.models.client import Role
from app.models.router import RouterError, resolve_roles
from tests.models.conftest import NANO, SUPER, ULTRA, config, models_list


def test_one_match_per_role_resolves() -> None:
    resolved = resolve_roles(models_list(), config())
    assert resolved[Role.READER].model_id == SUPER
    assert resolved[Role.READER].context_window == 1048576
    assert resolved[Role.PLANNER].model_id == ULTRA
    assert resolved[Role.UTILITY].model_id == NANO
    assert resolved[Role.VISION].model_id == "nvidia/Nemotron-3.5-Super-VL"  # spike S-2
    assert resolved[Role.CODER].model_id == ULTRA  # Super VL ran no smoke test on set B
    assert resolved[Role.UTILITY].context_window is None
    assert set(resolved) == set(Role)


def test_two_matches_fail_listing_both() -> None:
    data = [*models_list(), {"id": "nvidia/Nemotron-3.5-Super-VL-FP8", "object": "model"}]
    with pytest.raises(RouterError) as err:
        resolve_roles(data, config())
    assert SUPER in str(err.value)
    assert "nvidia/Nemotron-3.5-Super-VL-FP8" in str(err.value)


def test_no_match_fails() -> None:
    data = [m for m in models_list() if "Ultra" not in m["id"]]
    with pytest.raises(RouterError, match="planner"):
        resolve_roles(data, config())


def test_exact_id_overrides_match() -> None:
    cfg = config()
    data = [*models_list(), {"id": "nvidia/Nemotron-3.5-Super-VL-FP8", "object": "model"}]
    roles = dict(cfg.roles)
    for role in (Role.READER, Role.VISION):  # one model since the retirement
        roles[role] = roles[role].model_copy(update={"exact_id": SUPER})
    resolved = resolve_roles(data, cfg.model_copy(update={"roles": roles}))
    assert resolved[Role.READER].model_id == SUPER


def test_exact_id_must_exist() -> None:
    cfg = config()
    roles = dict(cfg.roles)
    roles[Role.JUDGE] = roles[Role.JUDGE].model_copy(update={"exact_id": "nope/missing"})
    with pytest.raises(RouterError, match="nope/missing"):
        resolve_roles(models_list(), cfg.model_copy(update={"roles": roles}))
