"""Package policy (docs/specs/execution-contracts.md §4). The Coder and the Planner only *request*
packages (invariant 3); this deterministic code decides, before anything is installed."""

import configparser
import hashlib
import re
import tomllib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import PackageReq
from app.storage.store import Store

PYPI = "https://pypi.org/pypi"
CACHE_TTL = timedelta(hours=24)
_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")  # PEP 508 name
PACKAGE_NAME = _NAME  # what a plan note may quote
_VERSION = r"[A-Za-z0-9.*+!_-]{1,40}"
_OP = r"(?:===|==|~=|!=|<=|>=|<|>)"
_SPEC = re.compile(rf"^ *{_OP} *{_VERSION}(?: *, *{_OP} *{_VERSION})* *$")  # plain spaces only
PACKAGE_SPECIFIER = _SPEC  # what the policy accepts as a version
TYPOSQUAT_DISTANCE = 2
SHORT_NAME = 5  # below this many characters a single edit is already a different name


class Reason(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT_SYNTAX = "REJECT_SYNTAX"
    REJECT_DENYLIST = "REJECT_DENYLIST"
    REJECT_NOT_FOUND = "REJECT_NOT_FOUND"
    REJECT_NO_WHEEL = "REJECT_NO_WHEEL"
    REJECT_TYPOSQUAT = "REJECT_TYPOSQUAT"
    REJECT_UNPOPULAR = "REJECT_UNPOPULAR"
    REJECT_QUOTA = "REJECT_QUOTA"


class PackageDecision(BaseModel):
    package: PackageReq
    reason: Reason
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.reason is Reason.ACCEPT


class PolicyDecision(BaseModel):
    decisions: list[PackageDecision]

    @property
    def accepted(self) -> list[PackageReq]:
        return [d.package for d in self.decisions if d.accepted]

    @property
    def rejected(self) -> list[PackageDecision]:
        return [d for d in self.decisions if not d.accepted]


class PolicyContext(BaseModel):
    repo_requirements: set[str] = Field(default_factory=set)  # normalized names
    requests_so_far: int = 0


class PolicyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_per_request: int
    max_requests_per_run: int
    min_releases: int
    min_age_days: int
    denylist: frozenset[str]
    popular: frozenset[str]


def normalize(name: str) -> str:
    """PEP 503 normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def load_policy_config(config_dir: Path) -> PolicyConfig:
    allow = yaml.safe_load((config_dir / "allowlist.yaml").read_text(encoding="utf-8"))["packages"]
    budgets = yaml.safe_load((config_dir / "budgets.yaml").read_text(encoding="utf-8"))
    popular_file = config_dir.parent / allow["popular_list"]
    popular = [
        line.strip()
        for line in popular_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    return PolicyConfig(
        max_per_request=int(allow["max_per_request"]),
        max_requests_per_run=int(budgets["per_run"]["package_requests"]),
        min_releases=int(allow["min_releases"]),
        min_age_days=int(allow["min_age_days"]),
        denylist=frozenset(normalize(n) for n in allow["denylist"]),
        popular=frozenset(normalize(n) for n in popular),
    )


def levenshtein(a: str, b: str, cap: int = 10) -> int:
    """Optimal string alignment distance: Levenshtein where swapping two adjacent letters is one
    edit (tqmd is one edit from tqdm, not two)."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    before: list[int] = []
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            cost = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cost = min(cost, before[j - 2] + 1)
            current.append(cost)
        before, previous = previous, current
    return previous[-1]


def typosquat_threshold(name: str, popular: str) -> int:
    """Edits within which `name` counts as a typosquat of `popular`: 1 when the shorter name has
    fewer than 5 characters (a fixed 2 flags most 2-4 letter names, e.g. mlx against jax)."""
    return 1 if min(len(name), len(popular)) < SHORT_NAME else TYPOSQUAT_DISTANCE


class PackageInfo(BaseModel):
    releases: int
    first_release: datetime | None
    has_wheel: bool | None = None  # None: cached before the wheel check, so fetched again


class PyPIClient:
    """PyPI JSON API through the 24 h cache. Read-only metadata; nothing is downloaded."""

    def __init__(self, store: Store, http: httpx.AsyncClient | None = None) -> None:
        self._store = store
        self._http = http

    async def info(self, name: str) -> PackageInfo | None:
        key = hashlib.sha256(name.encode()).hexdigest()
        hit = await self._store.cache_get("pypi", key, max_age=CACHE_TTL)
        cached = PackageInfo.model_validate(hit["info"]) if hit and hit.get("info") else None
        if hit is not None and (cached is None or cached.has_wheel is not None):
            return cached
        client = self._http or httpx.AsyncClient(timeout=30)
        try:
            response = await client.get(f"{PYPI}/{name}/json")
        finally:
            if self._http is None:
                await client.aclose()
        info = _parse_pypi(response.json()) if response.status_code == 200 else None
        await self._store.cache_put(
            "pypi", key, {"info": info.model_dump(mode="json") if info else None}
        )
        return info


def _parse_pypi(data: Mapping[str, Any]) -> PackageInfo:
    releases = {v: files for v, files in (data.get("releases") or {}).items() if files}
    times = [
        datetime.fromisoformat(f["upload_time_iso_8601"].replace("Z", "+00:00"))
        for files in releases.values()
        for f in files
        if f.get("upload_time_iso_8601")
    ]
    wheel = any(f.get("packagetype") == "bdist_wheel" for files in releases.values() for f in files)
    return PackageInfo(
        releases=len(releases), first_release=min(times) if times else None, has_wheel=wheel
    )


class PackagePolicy:
    def __init__(
        self,
        config: PolicyConfig,
        pypi: PyPIClient,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._pypi = pypi
        self._now = now

    @property
    def config(self) -> PolicyConfig:
        return self._config

    def _syntax_ok(self, req: PackageReq) -> bool:
        if not _NAME.fullmatch(req.name):
            return False
        return req.specifier is None or bool(_SPEC.fullmatch(req.specifier))

    async def _decide(self, req: PackageReq, context: PolicyContext) -> PackageDecision:
        cfg = self._config

        def decision(reason: Reason, detail: str = "") -> PackageDecision:
            return PackageDecision(package=req, reason=reason, detail=detail)

        # 1: a plain name and version specifier: no URLs, git+, paths, options, extras, markers
        if not self._syntax_ok(req):
            return decision(Reason.REJECT_SYNTAX, "not a plain package name/specifier")
        if req.specifier is not None:  # what goes into the manifest: no whitespace at all
            req = PackageReq(name=req.name, specifier=req.specifier.replace(" ", "") or None)
        name = normalize(req.name)
        if name in cfg.denylist:  # 2
            return decision(Reason.REJECT_DENYLIST)
        info = await self._pypi.info(name)  # 3
        if info is None or info.releases == 0:
            return decision(Reason.REJECT_NOT_FOUND)
        if not info.has_wheel:  # 3b: the provisioner never builds an sdist (only-binary)
            return decision(Reason.REJECT_NO_WHEEL, "no wheel on PyPI, only sdists")
        # 4: typosquats of popular names (packages the official repository declares are exempt:
        # e.g. torchao is 2 edits from torch)
        if name not in cfg.popular and name not in context.repo_requirements:
            close = sorted(
                p
                for p in cfg.popular
                if levenshtein(name, p, TYPOSQUAT_DISTANCE) <= typosquat_threshold(name, p)
            )
            if close:
                return decision(Reason.REJECT_TYPOSQUAT, f"looks like {close[0]}")
        if name in context.repo_requirements:  # 5
            return decision(Reason.ACCEPT, "in the repository requirements")
        cutoff = self._now() - timedelta(days=cfg.min_age_days)
        old_enough = info.first_release is not None and info.first_release <= cutoff
        if info.releases >= cfg.min_releases and old_enough:
            return decision(Reason.ACCEPT)
        return decision(Reason.REJECT_UNPOPULAR, "too new or too few releases")

    async def validate(self, requested: list[PackageReq], context: PolicyContext) -> PolicyDecision:
        decisions = []
        for index, req in enumerate(requested):
            over_run = context.requests_so_far >= self._config.max_requests_per_run
            if over_run or index >= self._config.max_per_request:  # 6
                decisions.append(PackageDecision(package=req, reason=Reason.REJECT_QUOTA))
            else:
                decisions.append(await self._decide(req, context))
        return PolicyDecision(decisions=decisions)


# --- repository requirements (never executed: parsed as text) ------------------------------

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _names(lines: list[str]) -> set[str]:
    names = set()
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")) or "://" in line:
            continue
        match = _REQ_NAME.match(line)
        if match:
            names.add(normalize(match.group(1)))
    return names


class _NoAliasLoader(yaml.SafeLoader):
    """Safe YAML without anchors/aliases: an untrusted file can't expand into a huge document."""

    def compose_node(self, parent: yaml.Node | None, index: int) -> yaml.Node | None:
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("aliases are not allowed")
        event = self.peek_event()  # type: ignore[no-untyped-call]
        if getattr(event, "anchor", None):
            raise yaml.YAMLError("anchors are not allowed")
        return super().compose_node(parent, index)


def _strings(value: object) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_one(base: str, text: str) -> set[str]:
    if base.startswith("requirements") and base.endswith(".txt"):
        return _names(text.splitlines())
    if base == "pyproject.toml":
        data = tomllib.loads(text)
        names = _names(_strings(_dict(data.get("project")).get("dependencies")))
        poetry = _dict(_dict(_dict(data.get("tool")).get("poetry")).get("dependencies"))
        declared = {n for n in poetry if n.lower() != "python" and _NAME.fullmatch(n)}
        return names | {normalize(n) for n in declared}
    if base == "setup.cfg":
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(text)
        return _names(parser.get("options", "install_requires", fallback="").splitlines())
    if base in ("environment.yml", "environment.yaml"):
        env = yaml.load(text, Loader=_NoAliasLoader)  # noqa: S506 - SafeLoader subclass
        found: set[str] = set()
        for dep in _dict(env).get("dependencies") or []:
            if isinstance(dep, dict):
                found |= _names(_strings(dep.get("pip")))
            elif isinstance(dep, str) and not dep.startswith("python"):
                found |= _names([dep.split("=")[0]])
        return found
    return set()


def parse_repo_requirements(files: Mapping[str, str]) -> set[str]:
    """Names declared by the repository's requirement files. CPU-bound on hostile input: call it
    through `RunContext.run_cpu` (invariant 12)."""
    names: set[str] = set()
    for path, text in files.items():
        base = path.rsplit("/", 1)[-1].lower()
        try:
            names |= _parse_one(base, text)
        except (
            ValueError,
            TypeError,
            AttributeError,
            RecursionError,
            configparser.Error,
            yaml.YAMLError,
            tomllib.TOMLDecodeError,
        ):
            continue  # a broken file in an untrusted repo just contributes nothing
    return names
