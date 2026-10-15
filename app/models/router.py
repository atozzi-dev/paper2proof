"""Resolve each role to exactly one model ID from GET /v1/models (fail fast, invariant 5)."""

from collections.abc import Mapping, Sequence

import httpx
from pydantic import SecretStr

from app.models.client import ModelsConfig, ResolvedModel, Role

__all__ = ["ResolvedModel", "RouterError", "fetch_models", "resolve_roles"]


class RouterError(Exception):
    """A role matched zero or several models: startup must stop."""


def _context_window(entry: Mapping[str, object]) -> int | None:
    for key in ("context_length", "context_window", "max_model_len", "max_context_length"):
        value = entry.get(key)
        if isinstance(value, int):
            return value
    return None


def resolve_roles(
    models: Sequence[Mapping[str, object]], config: ModelsConfig
) -> dict[Role, ResolvedModel]:
    by_id = {str(m["id"]): m for m in models if "id" in m}
    resolved: dict[Role, ResolvedModel] = {}
    problems: list[str] = []
    for role, spec in config.roles.items():
        if spec.exact_id:
            hits = [spec.exact_id] if spec.exact_id in by_id else []
            wanted = f"exact_id {spec.exact_id!r}"
        else:
            needle = (spec.match or "").lower()
            hits = sorted(i for i in by_id if needle and needle in i.lower())
            wanted = f"match {spec.match!r}"
        if len(hits) == 1:
            resolved[role] = ResolvedModel(
                model_id=hits[0], context_window=_context_window(by_id[hits[0]])
            )
        else:
            problems.append(f"{role.value}: {wanted} hit {len(hits)} models {hits}")
    if problems:
        raise RouterError("model routing failed: " + "; ".join(problems))
    return resolved


async def fetch_models(
    http: httpx.AsyncClient, base_url: str, api_key: SecretStr
) -> list[dict[str, object]]:
    url = base_url.rstrip("/") + "/models"
    response = await http.get(
        url, headers={"Authorization": f"Bearer {api_key.get_secret_value()}"}, timeout=30
    )
    response.raise_for_status()
    data = response.json().get("data", [])
    return [m for m in data if isinstance(m, dict)]
