"""Run artifacts (ingested paper, page images, plans...). Local directory in test/dev; the
S3-backed implementation for `p2p-bundles` comes with the bundle task (A-26)."""

import json
import re
from pathlib import Path
from typing import Any, Protocol

_KEY = re.compile(r"^[a-z0-9][a-z0-9_.-]*(/[a-z0-9][a-z0-9_.-]*)*$")


def check_key(key: str) -> str:
    """Keys are relative, lowercase, slash-separated; no '..', no absolute paths."""
    if not _KEY.fullmatch(key) or ".." in key.split("/"):
        raise ValueError(f"invalid artifact key: {key!r}")
    return key


class ArtifactStore(Protocol):
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def get_bytes(self, key: str) -> bytes: ...
    def put_json(self, key: str, data: Any) -> None: ...
    def get_json(self, key: str) -> Any: ...


class LocalArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / check_key(key)).resolve()
        if not path.is_relative_to(self.root):  # defence in depth
            raise ValueError(f"invalid artifact key: {key!r}")
        return path

    def put_bytes(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def put_json(self, key: str, data: Any) -> None:
        self.put_bytes(key, json.dumps(data, sort_keys=True, ensure_ascii=False).encode())

    def get_json(self, key: str) -> Any:
        return json.loads(self.get_bytes(key))
