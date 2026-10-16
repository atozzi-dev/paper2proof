"""Reference experiment of the friedman-boosting canary (eval set A).

Gradient-boosted regression against linear regression on Friedman #1.

Reference implementation of the protocol in paper.tex: its full runs with seeds 0, 1 and 2
produced the numbers the paper reports. Script contract: docs/specs/execution-contracts.md §6
(`--seed --out [--smoke] [--config]`, writes <out>/metrics.json). Datasets ship with
scikit-learn: no network.
"""

import argparse
import json
from pathlib import Path

from sklearn.datasets import make_friedman1
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_squared_error
from sklearn.model_selection import train_test_split

SMOKE_ROWS = 300  # --smoke trains on the first rows of the training split only


def run(seed: int, smoke: bool = False) -> dict[str, float]:
    x, y = make_friedman1(n_samples=2000, n_features=10, noise=1.0, random_state=0)
    x_train, x_test, y_train, y_test = train_test_split(x, y, test_size=0.3, random_state=seed)
    if smoke:
        x_train, y_train = x_train[:SMOKE_ROWS], y_train[:SMOKE_ROWS]
    boosted = GradientBoostingRegressor(random_state=seed).fit(x_train, y_train)
    linear = LinearRegression().fit(x_train, y_train)
    return {
        "rmse": float(mean_squared_error(y_test, boosted.predict(x_test)) ** 0.5),
        "baseline/rmse": float(mean_squared_error(y_test, linear.predict(x_test)) ** 0.5),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true", help="tiny CPU run (well under 120 s)")
    parser.add_argument("--config", type=Path, help="accepted for the contract; unused")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    metrics = run(args.seed, smoke=args.smoke)
    (args.out / "metrics.json").write_text(json.dumps(metrics, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
