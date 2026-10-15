"""Load config/verdict.yaml. Its SHA-256 is stored with every verdict (reproducibility)."""

import hashlib
from pathlib import Path

import yaml

from app.domain.models import VerdictConfig

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "verdict.yaml"


def load_verdict_config(path: Path = DEFAULT_PATH) -> VerdictConfig:
    return VerdictConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def config_sha256(path: Path = DEFAULT_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
