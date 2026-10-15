"""Prompt files: app/models/prompts/<role>_<purpose>.md with a YAML header."""

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


class Prompt(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    role: str
    version: str
    output_schema: str | None
    body: str

    def render(self, **values: str) -> str:
        """Replace `{{name}}` placeholders (no format(): bodies contain JSON braces)."""
        text = self.body
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        return text


@lru_cache(maxsize=64)
def load_prompt(name: str) -> Prompt:
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"prompt {name} has no YAML header")
    header, body = text[4:].split("\n---\n", 1)
    meta = yaml.safe_load(header)
    return Prompt(
        name=name,
        role=str(meta["role"]),
        version=str(meta["version"]),
        output_schema=meta.get("output_schema"),
        body=body.strip() + "\n",
    )
