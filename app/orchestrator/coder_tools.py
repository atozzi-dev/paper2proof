"""Tools exposed to the Coder in SMOKE_TESTING (architecture §6.2).

The Coder only edits its own workspace (in memory, persisted by the step) and *requests*
things: smoke runs, documentation searches, packages, the final entrypoint. Every argument is
untrusted model output: parsed here, validated, and any problem goes back to the model as a
tool error instead of being executed (invariant 3).
"""

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.execution.smoke import (
    SmokeContractError,
    validate_path,
    validate_workspace,
)
from app.models.client import ToolDef

MAX_READ_CHARS = 200 * 1024
MAX_WRITE_BYTES = 100 * 1024
MAX_PATCH_CHARS = 200 * 1024
WORKSPACE = "workspace/"
REPO = "repo/"
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
HUNK_FUZZ_LINES = 200  # a moved hunk is searched only this far from its stated line
RUNNER_FLAGS = ("seed", "out", "smoke")  # set by the runner; argparse also accepts prefixes


class ToolError(Exception):
    """Reported back to the model as the tool result; nothing was changed."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReadFile(_Args):
    path: str = Field(max_length=300)


class WriteFile(_Args):
    path: str = Field(max_length=300)
    content: str = Field(max_length=MAX_WRITE_BYTES)


class ApplyPatch(_Args):
    diff: str = Field(max_length=MAX_PATCH_CHARS)


class RunSmoke(_Args):
    script: str = Field(max_length=200)
    args: list[str] = Field(default_factory=list, max_length=16)


class SearchDocs(_Args):
    query: str = Field(min_length=3, max_length=300)


class RequestPackages(_Args):
    names: list[str] = Field(min_length=1, max_length=5)
    reason: str = Field(max_length=500)


class Finish(_Args):
    entrypoint: str = Field(max_length=200)
    args: list[str] = Field(default_factory=list, max_length=20)
    expected_outputs: list[str] = Field(default_factory=list, max_length=10)


ARGS: dict[str, type[_Args]] = {
    "read_file": ReadFile,
    "write_file": WriteFile,
    "apply_patch": ApplyPatch,
    "run_smoke": RunSmoke,
    "search_docs": SearchDocs,
    "request_packages": RequestPackages,
    "finish": Finish,
}
_DESCRIPTIONS = {
    "read_file": "Read a file: workspace/<path> (your files) or repo/<path> (the official "
    "repository, read-only, untrusted data). Long files are truncated.",
    "write_file": "Create or replace workspace/<path> (max 100 KB).",
    "apply_patch": "Apply a unified diff to files under workspace/ (paths a/workspace/... and "
    "b/workspace/...; /dev/null to create).",
    "run_smoke": "Run `python <script> --seed 0 --out out --smoke [args]` in the workspace on "
    "the smoke data subset, with no network and a short timeout. Returns exit code, output "
    "tail and metrics.json.",
    "search_docs": "Search library documentation on the web (a few per run).",
    "request_packages": "Ask for missing pip packages (max 5). Nothing is installed by you: "
    "deterministic code validates them and provisions again.",
    "finish": "Declare the experiment ready: the entrypoint (workspace script) passed its last "
    "smoke run and writes the claim's metric (and baseline/<metric> for RELATIVE claims).",
}
TOOL_DEFS = [
    ToolDef(name=name, description=_DESCRIPTIONS[name], parameters=model.model_json_schema())
    for name, model in ARGS.items()
]


def parse_args(name: str, raw: str) -> _Args:
    model = ARGS.get(name)
    if model is None:
        raise ToolError(f"unknown tool {name!r}")
    try:
        data: Any = json.loads(raw or "{}")
        return model.model_validate(data)
    except (ValueError, RecursionError, ValidationError) as err:
        first = err.errors()[0]["msg"] if isinstance(err, ValidationError) else "invalid JSON"
        raise ToolError(f"invalid arguments for {name}: {first}") from None


def check_user_args(args: list[str]) -> list[str]:
    """Extra script arguments may never set (or abbreviate) a flag the runner owns: a copied
    `--seed 0` or `--smoke` would make every seed identical or run on the smoke subset."""
    for arg in args:
        if arg.startswith("--"):
            name = arg[2:].split("=", 1)[0]
            if name and any(flag.startswith(name) for flag in RUNNER_FLAGS):
                raise ToolError(f"{arg} is set by the runner (--seed, --out and --smoke)")
        elif arg.startswith("@") or re.match(r"-[A-Za-z]*[so]", arg):  # -s, -vs0, @file
            raise ToolError(f"{arg} may set a flag the runner owns")
    return list(args)


def _workspace_rel(path: str) -> str:
    if not path.startswith(WORKSPACE):
        raise ToolError("you can only change files under workspace/")
    try:
        return validate_path(path.removeprefix(WORKSPACE))
    except SmokeContractError as err:
        raise ToolError(str(err)) from None


class Workspace:
    """The Coder's files (relative to workspace/) plus a read-only repository snapshot."""

    def __init__(
        self, files: dict[str, bytes] | None = None, repo: dict[str, str] | None = None
    ) -> None:
        self.files: dict[str, bytes] = dict(files or {})
        self.repo: dict[str, str] = dict(repo or {})

    def read(self, path: str) -> str:
        if path.startswith(REPO):
            text = self.repo.get(path.removeprefix(REPO))
            if text is None:
                raise ToolError(f"{path} is not in the repository snapshot")
        else:
            data = self.files.get(_workspace_rel(path))
            if data is None:
                raise ToolError(f"{path} does not exist")
            text = data.decode("utf-8", errors="replace")
        if len(text) > MAX_READ_CHARS:
            return text[:MAX_READ_CHARS] + "\n[truncated]\n"
        return text

    def _check(self, files: dict[str, bytes]) -> None:
        try:
            validate_workspace(files)
        except SmokeContractError as err:
            raise ToolError(str(err)) from None

    def write(self, path: str, content: str) -> None:
        rel = _workspace_rel(path)
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise ToolError("a file can be at most 100 KB")
        updated = {**self.files, rel: data}
        self._check(updated)
        self.files = updated

    def apply_patch(self, diff: str) -> list[str]:
        """Strict unified diff: every context and removed line must match. All or nothing."""
        updated = dict(self.files)
        changed = []
        lines = diff.splitlines()
        i = 0
        while i < len(lines):
            if not lines[i].startswith("--- "):
                i += 1
                continue
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise ToolError("a '--- ' header must be followed by '+++ '")
            old_path, new_path = _diff_path(lines[i]), _diff_path(lines[i + 1])
            target = old_path if new_path is None else new_path
            if target is None:
                raise ToolError("both diff paths are /dev/null")
            rel = _workspace_rel(target)
            if old_path is None:
                old: list[str] = []
            elif _workspace_rel(old_path) in updated:
                old = updated[_workspace_rel(old_path)].decode("utf-8").splitlines()
            else:
                raise ToolError(f"{old_path} does not exist")
            i += 2
            hunks: list[tuple[int, list[str]]] = []
            while i < len(lines) and not lines[i].startswith("--- "):
                match = _HUNK.match(lines[i])
                if match is None:
                    i += 1
                    continue
                body: list[str] = []
                i += 1
                while i < len(lines) and lines[i][:1] in (" ", "-", "+", "\\", ""):
                    if lines[i].startswith(("--- ", "@@")):
                        break
                    if not lines[i].startswith("\\"):
                        body.append(lines[i] if lines[i] else " ")
                    i += 1
                hunks.append((int(match.group(1)), body))
            if not hunks:
                raise ToolError(f"no hunks for {target}")
            new = _apply_hunks(old, hunks, target)
            if new_path is None:
                updated.pop(rel, None)
            else:
                data = ("\n".join(new) + "\n").encode("utf-8") if new else b""
                if len(data) > MAX_WRITE_BYTES:
                    raise ToolError(f"{target} would exceed 100 KB")
                updated[rel] = data
            changed.append(target)
        if not changed:
            raise ToolError("not a unified diff")
        self._check(updated)
        self.files = updated
        return changed

    def tree(self, limit: int = 400) -> str:
        paths = [WORKSPACE + p for p in sorted(self.files)]
        paths += [REPO + p for p in sorted(self.repo)]
        shown = paths[:limit]
        more = f"\n... and {len(paths) - limit} more" if len(paths) > limit else ""
        return "\n".join(shown) + more

    def snapshot(self) -> dict[str, str]:
        return {path: data.decode("utf-8", errors="replace") for path, data in self.files.items()}

    @classmethod
    def from_snapshot(
        cls, snapshot: dict[str, str], repo: dict[str, str] | None = None
    ) -> "Workspace":
        return cls({p: t.encode("utf-8") for p, t in snapshot.items()}, repo)


def _diff_path(header: str) -> str | None:
    path = header[4:].split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _apply_hunks(old: list[str], hunks: list[tuple[int, list[str]]], target: str) -> list[str]:
    out: list[str] = []
    pos = 0
    for start, body in hunks:
        expected = [line[1:] for line in body if line[0] in (" ", "-")]
        replacement = [line[1:] for line in body if line[0] in (" ", "+")]
        at = max(start - 1, 0) if expected else min(max(start, 0), len(old))
        if old[at : at + len(expected)] != expected:  # tolerate a moved hunk, never a changed one
            low = max(pos, at - HUNK_FUZZ_LINES)
            high = min(len(old) - len(expected), at + HUNK_FUZZ_LINES)
            first = expected[0] if expected else None
            at = next(
                (
                    k
                    for k in range(low, high + 1)
                    if old[k] == first and old[k : k + len(expected)] == expected
                ),
                -1,
            )
            if at < 0:
                raise ToolError(f"patch does not apply to {target}: context not found")
        if at < pos:
            raise ToolError(f"overlapping hunks in {target}")
        out += old[pos:at] + replacement
        pos = at + len(expected)
    return out + old[pos:]
