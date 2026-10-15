"""Coder tools: the workspace is the only thing the Coder can change, and only within limits."""

import json

import pytest

from app.orchestrator.coder_tools import (
    MAX_READ_CHARS,
    TOOL_DEFS,
    ToolError,
    Workspace,
    parse_args,
)


def test_tool_definitions_are_the_documented_set() -> None:
    assert [t.name for t in TOOL_DEFS] == [
        "read_file", "write_file", "apply_patch", "run_smoke", "search_docs",
        "request_packages", "finish",
    ]  # fmt: skip


@pytest.mark.parametrize(
    "path",
    ["../../etc/x", "/etc/passwd", "workspace/../x.py", "repo/x.py", "x.py", "workspace/"],
)
def test_writes_stay_in_the_workspace(path: str) -> None:
    ws = Workspace()
    with pytest.raises(ToolError):
        ws.write(path, "x")
    assert ws.files == {}


def test_write_read_and_limits() -> None:
    ws = Workspace(repo={"README.md": "hello", "big.txt": "y" * (MAX_READ_CHARS + 10)})
    ws.write("workspace/train.py", "print(1)\n")
    assert ws.read("workspace/train.py") == "print(1)\n"
    assert ws.read("repo/README.md") == "hello"
    assert len(ws.read("repo/big.txt")) <= MAX_READ_CHARS + 100
    with pytest.raises(ToolError):
        ws.read("repo/missing.py")
    with pytest.raises(ToolError):
        ws.write("workspace/huge.py", "x" * (100 * 1024 + 1))
    assert "workspace/train.py" in ws.tree()
    assert "repo/README.md" in ws.tree()


def test_apply_patch_edits_and_creates_files() -> None:
    ws = Workspace()
    ws.write("workspace/train.py", "a = 1\nb = 2\nc = 3\n")
    diff = (
        "--- a/workspace/train.py\n+++ b/workspace/train.py\n"
        "@@ -1,3 +1,3 @@\n a = 1\n-b = 2\n+b = 20\n c = 3\n"
        "--- /dev/null\n+++ b/workspace/new.py\n@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n"
    )
    ws.apply_patch(diff)
    assert ws.read("workspace/train.py") == "a = 1\nb = 20\nc = 3\n"
    assert ws.read("workspace/new.py") == "x = 1\ny = 2\n"


@pytest.mark.parametrize(
    "diff",
    [
        "--- a/workspace/train.py\n+++ b/../../etc/passwd\n@@ -1,1 +1,1 @@\n-a = 1\n+x\n",
        "--- a/workspace/train.py\n+++ b/workspace/train.py\n@@ -1,1 +1,1 @@\n-nope\n+x\n",
        "not a diff at all",
        "--- a/workspace/missing.py\n+++ b/workspace/missing.py\n@@ -1,1 +1,1 @@\n-a\n+b\n",
    ],
)
def test_bad_patches_change_nothing(diff: str) -> None:
    ws = Workspace()
    ws.write("workspace/train.py", "a = 1\n")
    with pytest.raises(ToolError):
        ws.apply_patch(diff)
    assert ws.files == {"train.py": b"a = 1\n"}


def test_tool_arguments_are_validated() -> None:
    assert parse_args("write_file", json.dumps({"path": "workspace/a.py", "content": "x"}))
    for name, raw in [
        ("write_file", "{not json"),
        ("write_file", json.dumps({"path": 5, "content": "x"})),
        ("request_packages", json.dumps({"names": [f"p{i}" for i in range(6)], "reason": "x"})),
        ("run_smoke", json.dumps({"script": "train.py", "args": ["--x"] * 30})),
        ("nonexistent", "{}"),
    ]:
        with pytest.raises(ToolError):
            parse_args(name, raw)


def test_snapshot_round_trip() -> None:
    ws = Workspace()
    ws.write("workspace/a.py", "é\n")
    again = Workspace.from_snapshot(ws.snapshot())
    assert again.files == ws.files
