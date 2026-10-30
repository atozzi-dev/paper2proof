"""Reference experiment of the digits-tree canary (eval set A).

A depth-8 decision tree against logistic regression on digits.

Reference implementation of the protocol in paper.tex: its full runs with seeds 0, 1 and 2
produced the numbers the paper reports. Script contract: docs/specs/execution-contracts.md §6
(`--seed --out [--smoke] [--config]`, writes <out>/metrics.json). Datasets ship with
scikit-learn: no network.
"""

import argparse
import json
from pathlib import Path

from sklearn.datasets import load_digits
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier

SMOKE_ROWS = 300  # --smoke trains on the first rows of the training split only


def run(seed: int, smoke: bool = False) -> dict[str, float]:
    x, y = load_digits(return_X_y=True)
    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=0.3, random_state=seed, stratify=y
    )
    if smoke:
        x_train, y_train = x_train[:SMOKE_ROWS], y_train[:SMOKE_ROWS]
    tree = DecisionTreeClassifier(max_depth=8, random_state=seed).fit(x_train, y_train)
    linear = make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=2000))
    linear.fit(x_train, y_train)
    return {
        "accuracy": float(accuracy_score(y_test, tree.predict(x_test))),
        "baseline/accuracy": float(accuracy_score(y_test, linear.predict(x_test))),
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
