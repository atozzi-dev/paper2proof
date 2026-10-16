"""Refuse a Terraform plan CI should not apply on its own.

CI applies infra/terraform/envs/prod unattended: the `prod` reviewer approves the deploy before
any plan exists. So CI applies only routine changes and refuses:
  - anything that deletes, replaces or forgets a resource (buckets, networks, the allocation);
  - any change to resources that decide isolation, access or durability: network (routes,
    subnets, security groups and rules, allocations), IAM, compute (the egress gateway VM and
    its cloud-init), storage buckets, secrets, registries and the database.
A human reviews and applies those by hand (infra/README.md).

    terraform show -json tfplan > tfplan.json
    python3 .github/scripts/tfplan_guard.py tfplan.json
"""

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

DESTRUCTIVE = {"delete", "forget"}
HARMLESS = {"no-op", "read"}
# Resource types whose every change shapes isolation, access or durability.
SENSITIVE_PREFIXES = (
    "nebius_vpc_",
    "nebius_iam_",
    "nebius_compute_",
    "nebius_storage_",
    "nebius_mysterybox_",
    "nebius_registry_",
    "nebius_msp_",
)


def refused_changes(plan: dict[str, Any]) -> list[str]:
    # Terraform omits resource_changes when the plan has no changes.
    found = []
    for change in plan.get("resource_changes", []):
        actions = list(change["change"]["actions"])
        label = f"{change['address']} ({', '.join(actions)})"
        if DESTRUCTIVE & set(actions):
            found.append(f"destructive: {label}")
        elif str(change.get("type", "")).startswith(SENSITIVE_PREFIXES) and not (
            set(actions) <= HARMLESS
        ):
            found.append(f"needs review: {label}")
    return sorted(found)


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1:
        print("usage: tfplan_guard.py <terraform show -json output>")
        return 2
    plan = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    if not isinstance(plan, dict) or not {"format_version", "terraform_version"} <= plan.keys():
        print("tfplan guard: not a `terraform show -json` plan")
        return 2
    found = refused_changes(plan)
    for line in found:
        print(f"tfplan guard: {line}")
    if found:
        print("tfplan guard: REFUSED. A human plans and applies these changes (infra/README.md).")
        return 1
    total = len(plan.get("resource_changes", []))
    print(f"tfplan guard: OK ({total} resource changes, none refused)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
