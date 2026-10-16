"""Reference experiment of the breast-cancer-svm canary (eval set A).

An RBF SVM on raw features against a standardized RBF SVM (breast cancer).

Reference implementation of the protocol in paper.tex: its full runs with seeds 0, 1 and 2
produced the numbers the paper reports. Script contract: docs/specs/execution-contracts.md §6
(`--seed --out [--smoke] [--config]`, writes <out>/metrics.json). Datasets ship with
scikit-learn: no network.
"""

import argparse
import json
from pathlib import Path

from sklearn.datasets import load_breast_cancer
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

SMOKE_ROWS = 150  # --smoke trains on the first rows of the training split only


def run(seed: int, smoke: bool = False) -> dict[str, float]:
    x, y = load_breast_cancer(return_X_y=True)
    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=0.3, random_state=seed, stratify=y
    )
    if smoke:
        x_train, y_train = x_train[:SMOKE_ROWS], y_train[:SMOKE_ROWS]
    raw = SVC(kernel="rbf", C=1.0, gamma="scale").fit(x_train, y_train)
    scaled = make_pipeline(StandardScaler(), SVC(kernel="rbf", C=1.0, gamma="scale"))
    scaled.fit(x_train, y_train)
    return {
        "roc_auc": float(roc_auc_score(y_test, raw.decision_function(x_test))),
        "baseline/roc_auc": float(roc_auc_score(y_test, scaled.decision_function(x_test))),
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
