"""Discovery result: produced by DISCOVERING (A-17), consumed by the planner (A-18)."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.models import DatasetReq

GITHUB_REPO = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
# The provisioner (images/provisioner/provision.py) enforces the same patterns and caps.
HF_DATASET_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,95})?$"
)
HF_SPLIT = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_DATASETS = 3
MAX_DATASETS_BYTES = 20 * 2**30


class RepoRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str  # canonical https://github.com/<owner>/<repo>
    source: Literal["paper", "hf", "tavily"]  # where the link came from
    official: bool  # linked by the authors (paper text / HF summary)

    @field_validator("url")
    @classmethod
    def _github_only(cls, url: str) -> str:
        if not GITHUB_REPO.fullmatch(url):
            raise ValueError(f"not a canonical GitHub repository URL: {url!r}")
        return url


class Discovery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: RepoRef | None
    datasets: list[DatasetReq] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
