"""Model prices from config/pricing.yaml (USD per 1M tokens). Never hardcoded (invariant 5)."""

import math
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "pricing.yaml"


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input: float = Field(ge=0)
    output: float = Field(ge=0)


class Pricing:
    def __init__(self, prices: dict[str, ModelPrice], batch_discount: float = 1.0) -> None:
        self._prices = {k.lower(): v for k, v in prices.items()}
        self.batch_discount = batch_discount

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> "Pricing":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        prices = {k: ModelPrice.model_validate(v) for k, v in raw["models"].items()}
        return cls(prices, float(raw.get("batch_discount", 1.0)))

    def price(self, model_id: str) -> ModelPrice | None:
        return self._prices.get(model_id.lower())

    def cost(self, model_id: str, tokens_in: int, tokens_out: int) -> float | None:
        """Actual cost, or None when the model has no price (shown as "n/a")."""
        price = self.price(model_id)
        if price is None:
            return None
        return tokens_in * price.input / 1e6 + tokens_out * price.output / 1e6

    def estimate(self, model_id: str, tokens_in: int, tokens_out: int) -> float:
        """Pre-call estimate for the budget: unknown models use the most expensive price."""
        price = self.price(model_id) or ModelPrice(
            input=max((p.input for p in self._prices.values()), default=0.0),
            output=max((p.output for p in self._prices.values()), default=0.0),
        )
        return tokens_in * price.input / 1e6 + tokens_out * price.output / 1e6


def estimate_tokens(text: str) -> int:
    """Rough upper-ish estimate (4 characters per token) used only for budget reservations."""
    return math.ceil(len(text) / 4)
