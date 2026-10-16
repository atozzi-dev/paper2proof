"""CI applies envs/prod only for routine changes (.github/scripts/tfplan_guard.py)."""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.ci import load_script

guard = load_script(".github/scripts/tfplan_guard.py")


def _plan(*changes: tuple[str, list[str]]) -> dict[str, Any]:
    return {
        "format_version": "1.2",
        "terraform_version": "1.16.4",
        "resource_changes": [
            {
                "address": address,
                "type": address.split(".")[-2],
                "change": {"actions": actions},
            }
            for address, actions in changes
        ],
    }


def test_routine_changes_pass() -> None:
    plan = _plan(
        ("module.logs.nebius_logging_v1_sink.x", ["update"]),
        ("module.other.nebius_kms_v1_thing.y", ["create"]),
        ("module.net.nebius_vpc_v1_subnet.exec", ["no-op"]),
        ("data.nebius_vpc_v1_network.default", ["read"]),
    )
    assert guard.refused_changes(plan) == []


def test_deletes_and_replacements_are_refused() -> None:
    plan = _plan(
        ("module.storage.nebius_storage_v1_bucket.state", ["delete"]),
        ("module.reg.nebius_registry_v1_registry.x", ["delete", "create"]),
        ("module.reg.nebius_registry_v1_registry.y", ["create", "delete"]),
        ("module.reg.nebius_registry_v1_registry.z", ["forget"]),
    )
    assert guard.refused_changes(plan) == [
        "destructive: module.reg.nebius_registry_v1_registry.x (delete, create)",
        "destructive: module.reg.nebius_registry_v1_registry.y (create, delete)",
        "destructive: module.reg.nebius_registry_v1_registry.z (forget)",
        "destructive: module.storage.nebius_storage_v1_bucket.state (delete)",
    ]


@pytest.mark.parametrize(
    "address",
    [
        "module.net.nebius_vpc_v1_route.default_egress",
        "module.net.nebius_vpc_v1_security_rule.gw_in",
        "module.net.nebius_vpc_v1_subnet.exec",
        "module.iam.nebius_iam_v1_access_permit.ci",
        "module.gw.nebius_compute_v1_instance.egress_gateway",
        "module.storage.nebius_storage_v1_bucket.state",
        "module.secrets.nebius_mysterybox_v1_secret.p2p",
        "module.registry.nebius_registry_v1_registry.control",
        "module.pg.nebius_msp_postgresql_v1alpha1_cluster.db",
    ],
)
@pytest.mark.parametrize("action", ["create", "update"])
def test_any_change_to_a_sensitive_type_is_refused(address: str, action: str) -> None:
    assert guard.refused_changes(_plan((address, [action]))) == [
        f"needs review: {address} ({action})"
    ]


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_plan(("a.nebius_x.b", ["create"]))), encoding="utf-8")
    assert guard.main([str(good)]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_plan(("a.nebius_x.b", ["delete"]))), encoding="utf-8")
    assert guard.main([str(bad)]) == 1
    assert "a.nebius_x.b (delete)" in capsys.readouterr().out
    # Terraform leaves resource_changes out of a plan with no changes.
    empty = tmp_path / "empty.json"
    empty.write_text('{"format_version": "1.2", "terraform_version": "1.16.4"}', encoding="utf-8")
    assert guard.main([str(empty)]) == 0
    broken = tmp_path / "broken.json"
    broken.write_text('{"resource_changes": []}', encoding="utf-8")
    assert guard.main([str(broken)]) == 2
