"""Tools exposed to the Coder in SMOKE_TESTING (architecture §6.2).

The Coder only edits its own workspace (in memory, persisted by the step) and *requests*
things: smoke runs, documentation searches, packages, the final entrypoint. Every argument is
untrusted model output: parsed here, validated, and any problem goes back to the model as a
tool error instead of being executed (invariant 3).
"""

import bisect
import difflib
import json
import re
import sys
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.contracts import MAX_CLAIMS_PER_JOB, METRIC_KEY_PATTERN
from app.execution.smoke import (
    SmokeContractError,
    validate_path,
    validate_workspace,
)
from app.models.client import ToolDef
from app.models.untrusted import wrap_untrusted
from app.verdict.scale import MetricUnit

MAX_READ_CHARS = 200 * 1024
MAX_WRITE_BYTES = 100 * 1024
MAX_PATCH_CHARS = 200 * 1024
WORKSPACE = "workspace/"
REPO = "repo/"
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_BARE_HUNK = re.compile(r"^@@(?: .*)?$")  # no line numbers: placed by its context alone
_FRAME = re.compile(r'File "([^"\n]{1,300})", line (\d{1,7})')  # a traceback frame
HUNK_FUZZ_LINES = 200  # a moved hunk is searched only this far from its stated line
BARE_HUNK_WORK = 1_000_000  # lines compared per patch to place hunks without line numbers
NEAR_LINES = 3  # lines shown on each side of a failing or unmatched line
CHANGE_LINES = 40  # diff lines shown after an edit
CHANGE_CHARS = 4_000  # the whole report of an edit
DIFF_LINE_CHARS = 300  # each diff line of it
DIFF_MAX_LINES = 2_000  # a larger changed region is reported by its size
TRACEBACK_LINES = 200  # the stderr lines searched for the failing frame
RUNNER_FLAGS = ("seed", "out", "smoke")  # set by the runner; argparse also accepts prefixes


# Python's own modules and the runner images' base packages: a workspace file named like one
# is imported in its place. A workspace/inspect.py broke numpy (production, 2026-10-30).
SHADOWED_MODULES = frozenset(sys.stdlib_module_names) | {
    "numpy", "scipy", "sklearn", "pandas", "pyarrow", "torch", "torchvision", "transformers",
    "datasets", "timm", "accelerate", "evaluate",
}  # fmt: skip


def _module_name(path: str) -> str | None:
    """The module a top-level workspace .py file or package is imported as: the workspace is
    the script's folder, first on sys.path. Nested files shadow nothing."""
    parts = path.split("/")
    if len(parts) == 1 and parts[0].endswith(".py"):
        return parts[0].removesuffix(".py")
    if len(parts) == 2 and parts[1] == "__init__.py":
        return parts[0]
    return None


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


class Measure(_Args):
    claim_id: str = Field(min_length=1, max_length=64)
    key: str = Field(pattern=METRIC_KEY_PATTERN)  # where metrics.json has the claim's value
    baseline_key: str | None = Field(default=None, pattern=METRIC_KEY_PATTERN)  # RELATIVE
    unit: MetricUnit  # checked against the smoke values; VERDICT aligns with it


class Unmeasured(_Args):
    claim_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=200)  # model text: shown as such


class SetupUsed(_Args):
    """What the script uses for one condition of the plan's setup, and the file that says so."""

    id: str = Field(min_length=1, max_length=8)
    value: str = Field(min_length=1, max_length=80)
    where: str = Field(min_length=1, max_length=200)  # workspace/<path> or repo/<path>
    as_paper: bool  # a CHOICE: whether this is the paper's choice (a NUMBER is compared)


class Finish(_Args):
    entrypoint: str = Field(max_length=200)
    args: list[str] = Field(default_factory=list, max_length=20)
    expected_outputs: list[str] = Field(default_factory=list, max_length=10)
    measures: list[Measure] = Field(default_factory=list, max_length=MAX_CLAIMS_PER_JOB)
    unmeasured: list[Unmeasured] = Field(default_factory=list, max_length=MAX_CLAIMS_PER_JOB)
    setup: list[SetupUsed] = Field(default_factory=list, max_length=20)  # the plan's setup


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
    "smoke run. List every claim to test exactly once: in `measures` with the metrics.json "
    "`key` of the value of the method the claim is about (and `baseline_key` of the value of "
    "its `baseline_name`, RELATIVE claims only) and "
    "the `unit` the values use: fraction (0.941 means 94.1%), percent (94.1, or 0.9 for a 0.9% "
    "error rate) or raw (not a share: loss, BLEU, reward); or in `unmeasured` with a short "
    "reason, only if the experiment truly cannot measure it. If no claim can be tested "
    "faithfully here (the method or its data cannot run), list every claim in `unmeasured`: "
    "the run then ends as untestable instead of testing a stand-in. When your task has a "
    "setup table, also list every one of its conditions once in `setup`: its `id`, the "
    "`value` the full run uses, `where` that value is written (a workspace/ or repo/ "
    "file) and `as_paper` (true when it is the value or the choice of the paper).",
}
TOOL_DEFS = [
    ToolDef(name=name, description=_DESCRIPTIONS[name], parameters=model.model_json_schema())
    for name, model in ARGS.items()
]


_NOT_NAME = re.compile(r"[^\w.]")


def parse_args(name: str, raw: str) -> _Args:
    model = ARGS.get(name)
    if model is None:
        raise ToolError(f"unknown tool {name!r}")
    try:
        data: Any = json.loads(raw or "{}")
        return model.model_validate(data)
    except (ValueError, RecursionError, ValidationError) as err:
        if isinstance(err, ValidationError):
            first = err.errors()[0]
            # names the field to fix; an unknown key is the model's own text: no markup back
            where = _NOT_NAME.sub("_", ".".join(str(part) for part in first["loc"]))[:60]
            why = f"{where}: {first['msg']}" if where else first["msg"]
        else:
            why = "invalid JSON"
        raise ToolError(f"invalid arguments for {name}: {why}") from None


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
    """A bare path is a workspace path: Super VL wrote `train.py` 40 times, each refused for the
    missing prefix (production, 2026-10-30). It goes through the same validation."""
    if path.startswith(REPO):
        raise ToolError("you can only change files under workspace/")
    try:
        return validate_path(path.removeprefix(WORKSPACE))
    except SmokeContractError as err:
        raise ToolError(f"{err}: files go under workspace/") from None


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
        for path in sorted(p for p in files if files[p] != self.files.get(p)):  # this edit's
            if (name := _module_name(path)) in SHADOWED_MODULES:
                raise ToolError(
                    f"workspace/{path} would be imported in place of Python's `{name}` module "
                    "(the script's folder comes first on sys.path) and break the libraries that "
                    "use it: name it differently"
                )

    def write(self, path: str, content: str) -> None:
        rel = _workspace_rel(path)
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise ToolError("a file can be at most 100 KB")
        if self.files.get(rel) == data:
            raise ToolError(f"workspace/{rel} already has exactly this content: nothing changed")
        updated = {**self.files, rel: data}
        self._check(updated)
        self.files = updated

    def apply_patch(self, diff: str) -> list[str]:
        """Strict unified diff: every context and removed line must match. All or nothing."""
        updated = dict(self.files)
        changed = []
        budget = [BARE_HUNK_WORK]  # lines compared for hunks without line numbers, whole patch
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
            hunks: list[tuple[int | None, list[str]]] = []
            while i < len(lines) and not lines[i].startswith("--- "):
                match = _HUNK.match(lines[i])
                if match is None and _BARE_HUNK.match(lines[i]) is None:
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
                hunks.append((int(match.group(1)) if match else None, body))
            if not hunks:
                raise ToolError(f"no hunks for {target}")
            new = _apply_hunks(old, hunks, target, budget)
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
        if updated == self.files:
            raise ToolError("the patch changes nothing: its + lines repeat its - lines")
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


def _apply_hunks(
    old: list[str], hunks: list[tuple[int | None, list[str]]], target: str, budget: list[int]
) -> list[str]:
    out: list[str] = []
    pos = 0
    index: dict[str, list[int]] | None = None  # line -> positions, for hunks without numbers
    for start, body in hunks:
        expected = [line[1:] for line in body if line[0] in (" ", "-")]
        replacement = [line[1:] for line in body if line[0] in (" ", "+")]
        if start is None:
            if index is None:
                index = {}
                for k, line in enumerate(old):
                    index.setdefault(line, []).append(k)
            at = _place(old, expected, pos, target, index, budget)
        else:
            at = max(start - 1, 0) if expected else min(max(start, 0), len(old))
        if old[at : at + len(expected)] != expected:  # tolerate a moved hunk, never a changed one
            low = max(pos, at - HUNK_FUZZ_LINES)
            high = min(len(old) - len(expected), at + HUNK_FUZZ_LINES)
            first = expected[0] if expected else None
            found = next(
                (
                    k
                    for k in range(low, high + 1)
                    if old[k] == first and old[k : k + len(expected)] == expected
                ),
                -1,
            )
            if found < 0:
                raise _not_found(old, expected, at, target)
            at = found
        if at < pos:
            raise ToolError(f"overlapping hunks in {target}")
        out += old[pos:at] + replacement
        pos = at + len(expected)
    return out + old[pos:]


def _place(
    old: list[str],
    expected: list[str],
    pos: int,
    target: str,
    index: dict[str, list[int]],
    budget: list[int],
) -> int:
    """Where a hunk without line numbers goes: the one place its context matches. It runs on
    the event loop, so only the places of the hunk's rarest line are tried, and the lines
    compared across the whole patch are capped (`budget`)."""
    if not expected:
        raise ToolError(f"a hunk without line numbers needs context lines ({target})")
    if len(expected) > HUNK_FUZZ_LINES:
        raise ToolError(
            f"a hunk without line numbers has at most {HUNK_FUZZ_LINES} context and - lines: "
            f"add line numbers ({target})"
        )
    rarest = min(range(len(expected)), key=lambda j: len(index.get(expected[j], ())))
    places = index.get(expected[rarest], [])
    found: list[int] = []
    for at in places[bisect.bisect_left(places, pos + rarest) :]:
        k = at - rarest
        if k + len(expected) > len(old):
            break
        budget[0] -= len(expected)
        if budget[0] < 0:
            raise ToolError(
                f"too many hunks without line numbers to place in {target}: add line numbers"
            )
        if old[k : k + len(expected)] == expected:
            found.append(k)
            if len(found) > 1:
                raise ToolError(
                    f"the context of a hunk without line numbers matches more than one place "
                    f"in {target}: add line numbers or more context lines"
                )
    if not found:
        raise _not_found(old, expected, None, target)
    return found[0]


def _numbered(lines: list[str], line: int, *, mark: bool = False) -> str:
    """`line` (1-based) and NEAR_LINES on each side, numbered; `mark` points at `line`."""
    low, high = max(1, line - NEAR_LINES), min(len(lines), line + NEAR_LINES)
    return "\n".join(
        f"{'>' if mark and n == line else ' '}{n:>5}: {lines[n - 1]}" for n in range(low, high + 1)
    )


def _not_found(old: list[str], expected: list[str], at: int | None, target: str) -> ToolError:
    """A patch whose context is not in the file: show the file where it was meant to go (the
    first context line matched loosely, else the stated line), so the next patch can copy it."""
    message = (
        f"patch does not apply to {target}: context not found (its context and - lines must "
        "copy the file exactly)"
    )
    first = next((line.strip() for line in expected if line.strip()), None)
    near = next((k for k, line in enumerate(old) if first and line.strip() == first), at)
    if near is None or not old:
        return ToolError(f"{message}: read the file first")
    near = min(near, len(old) - 1)
    excerpt = wrap_untrusted(_numbered(old, near + 1), source="workspace", max_chars=4000)
    return ToolError(f"{message}. The file around line {near + 1}:\n{excerpt}")


def _shift(line: str, offset: int) -> str:
    """A hunk header of a diff taken from line `offset` on, renumbered as in the whole file."""
    match = _HUNK.match(line)
    if match is None or not offset:
        return line
    old, new = int(match.group(1)) + offset, int(match.group(3)) + offset
    old_n = f",{match.group(2)}" if match.group(2) is not None else ""
    new_n = f",{match.group(4)}" if match.group(4) is not None else ""
    return f"@@ -{old}{old_n} +{new}{new_n} @@"


def describe_change(path: str, old: bytes | None, new: bytes | None) -> str:
    """What an edit did: line counts and a short unified diff. Super VL's patches added the
    fixed line and kept the broken one, then ran the same error 8 times (production,
    2026-10-30); "OK" alone never showed it. Only the changed region is diffed (difflib grows
    faster than quadratically) and a large one is reported by its size alone."""
    if new is None:
        return f"{path}: deleted"
    new_lines = new.decode("utf-8", errors="replace").splitlines()
    if old is None:
        return f"{path}: new file, {len(new_lines)} lines"
    old_lines = old.decode("utf-8", errors="replace").splitlines()
    same, shortest = 0, min(len(old_lines), len(new_lines))
    while same < shortest and old_lines[same] == new_lines[same]:
        same += 1
    tail = 0
    while tail < shortest - same and old_lines[-1 - tail] == new_lines[-1 - tail]:
        tail += 1
    old_end, new_end = len(old_lines) - tail, len(new_lines) - tail
    if max(old_end, new_end) - same > DIFF_MAX_LINES:
        return (
            f"{path}: {old_end - same} lines from line {same + 1} on replaced by "
            f"{new_end - same} lines (too many to show: read the file)"
        )
    start = max(same - 1, 0)  # one line of context on each side
    region = difflib.unified_diff(
        old_lines[start : old_end + 1], new_lines[start : new_end + 1], lineterm="", n=1
    )
    diff = [_shift(line, start) for line in list(region)[2:]]
    added = sum(1 for line in diff if line.startswith("+"))
    removed = sum(1 for line in diff if line.startswith("-"))
    shown = [
        line if len(line) <= DIFF_LINE_CHARS else line[:DIFF_LINE_CHARS] + " [cut]"
        for line in diff[:CHANGE_LINES]
    ]
    more = (
        [f"... and {len(diff) - CHANGE_LINES} more diff lines"] if len(diff) > CHANGE_LINES else []
    )
    head = f"{path}: {added} line{'' if added == 1 else 's'} added, {removed} removed"
    return "\n".join([head, *shown, *more])


def describe_changes(before: dict[str, bytes], after: dict[str, bytes]) -> str:
    """Every workspace file an edit changed, as describe_change shows it, within CHANGE_CHARS.
    Pure (CPU pool)."""
    paths = sorted({p for p in {*before, *after} if before.get(p) != after.get(p)})
    parts: list[str] = []
    for i, path in enumerate(paths):
        if sum(len(part) for part in parts) > CHANGE_CHARS:
            parts.append(f"... and {len(paths) - i} more files changed")
            break
        parts.append(describe_change(WORKSPACE + path, before.get(path), after.get(path)))
    text = "\n".join(parts)
    return text if len(text) <= CHANGE_CHARS else text[:CHANGE_CHARS] + "\n[cut]"


_NAME = r"[A-Za-z_][A-Za-z0-9_.]{0,59}"  # a Python name: never markup from the script's output
_UNEXPECTED_ARGUMENT = re.compile(
    rf"^TypeError: ({_NAME})\(\) got an unexpected keyword argument '({_NAME})'$"
)
_NO_ATTRIBUTE = re.compile(
    rf"^AttributeError: (?:module '({_NAME})'|'({_NAME})' object) has no attribute '({_NAME})'$"
)
_NO_IMPORT = re.compile(rf"^ImportError: cannot import name '({_NAME})' from '({_NAME})'")
_BUILTIN_TYPES = {
    "NoneType",
    "int",
    "float",
    "str",
    "bytes",
    "bool",
    "list",
    "dict",
    "tuple",
    "set",
}
_NEWER = "it was removed or renamed in a newer release than you may know"


def error_hints(files: dict[str, bytes], stderr: str) -> list[str]:
    """A note when the last error names something the installed library lacks: Super VL read
    "unexpected keyword argument 'multi_class'" as a wrong value and changed it 4 times before
    removing it (local Set A, 2026-11-02). Only Python names reach the note, and only names the
    Coder wrote itself (stderr can come from the paper's repository), never about a function or
    class its own files define. Pure (CPU pool)."""
    lines = [line.strip() for line in stderr.splitlines()[-TRACEBACK_LINES:] if len(line) <= 1000]
    last = next((line for line in reversed(lines) if re.match(r"\w+(Error|Exception)\b", line)), "")
    code = "\n".join(
        data.decode("utf-8", "replace") for p, data in files.items() if p.endswith(".py")
    )

    def written(name: str) -> bool:
        return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", code) is not None

    def defined(owner: str) -> bool:
        name = re.escape(owner.split(".")[-1])
        return re.search(rf"^[ \t]*(?:def|class)[ \t]+{name}\b", code, re.MULTILINE) is not None

    if (match := _UNEXPECTED_ARGUMENT.match(last)) is not None:
        owner = match.group(1).removesuffix(".__init__")
        if not written(match.group(2)) or defined(owner):
            return []
        return [
            f"note: {owner} in the installed version takes no `{match.group(2)}` argument: "
            f"{_NEWER}. Remove the argument, or pass what replaced it; changing its value will "
            "not help"
        ]
    if (match := _NO_ATTRIBUTE.match(last)) and match.group(2) not in _BUILTIN_TYPES:
        owner = match.group(1) or match.group(2)
        if not written(match.group(3)) or defined(owner):
            return []
        return [
            f"note: the installed `{owner}` has no `{match.group(3)}`: {_NEWER}. Use what this "
            "version offers instead (search_docs can tell), never a stand-in"
        ]
    if (match := _NO_IMPORT.match(last)) is not None:
        if not written(match.group(1)):
            return []
        return [
            f"note: the installed `{match.group(2)}` has no `{match.group(1)}` to import: "
            f"{_NEWER}. Import what this version offers instead (search_docs can tell)"
        ]
    return []


def failing_lines(files: dict[str, bytes], stderr: str) -> str | None:
    """The innermost traceback frame in the Coder's own files, with its neighbours. Only the
    last lines of stderr are read, and only lines a traceback could write. Pure (CPU pool)."""
    lines = [line for line in stderr.splitlines()[-TRACEBACK_LINES:] if len(line) <= 1000]
    for where, number in reversed(_FRAME.findall("\n".join(lines))):
        if WORKSPACE not in where:
            continue
        data = files.get(where.rsplit(WORKSPACE, 1)[1])
        if data is None:
            continue
        text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        source = text.split("\n")  # as Python numbers lines: splitlines() also splits on \x0c
        line = int(number)
        if 1 <= line <= len(source):
            path = WORKSPACE + where.rsplit(WORKSPACE, 1)[1]
            return f"{path}, line {line}:\n" + _numbered(source, line, mark=True)
    return None
