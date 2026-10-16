"""Tests for the CI/CD workflows and their helper scripts (A-05)."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def load_script(relative: str) -> ModuleType:
    """Import a standalone script (scripts/, .github/scripts/) by path."""
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module
