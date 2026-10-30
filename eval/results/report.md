## ReproBench-mini

### Set A: canary papers

10 papers, 16 claims; the expected verdicts are for full scale over each canary's seeds.

| Metric | Result | Target |
|---|---|---|
| Verdict accuracy | 100.0% (16/16) | >= 80%: met |
| Coverage (claims with a decisive verdict) | 100.0% (16/16) | |
| Accuracy on the judged claims | 100.0% (16/16) | |
| False accusations | 0 | 0: met |
| Claims without a verdict | 0 | |
| Extraction F1 | 1.00 | |
| Median cost per claim | $0.02 | |
| Median run time | 1.6 min | |
| Total cost | $0.28 | |

| Canary | Claim | Planted | Expected | Verdict |
|---|---|---|---|---|
| breast-cancer-svm | c1 | no | Reproduced | Reproduced |
| breast-cancer-svm | c2 | yes | Not reproduced under our conditions | Not reproduced under our conditions |
| digits-knn-pca | c1 | no | Reproduced | Reproduced |
| digits-knn-pca | c2 | yes | Not reproduced under our conditions | Not reproduced under our conditions |
| digits-logreg | c1 | no | Reproduced | Reproduced |
| digits-mlp | c1 | no | Reproduced | Reproduced |
| digits-mlp | c2 | no | Reproduced | Reproduced |
| digits-naive-bayes | c1 | yes | Not reproduced under our conditions | Not reproduced under our conditions |
| digits-tree | c1 | yes | Not reproduced under our conditions | Not reproduced under our conditions |
| digits-tree | c2 | yes | Not reproduced under our conditions | Not reproduced under our conditions |
| friedman-boosting | c1 | yes | Not reproduced under our conditions | Not reproduced under our conditions |
| friedman-boosting | c2 | no | Reproduced | Reproduced |
| moons-forest | c1 | no | Reproduced | Reproduced |
| moons-forest | c2 | yes | Not reproduced under our conditions | Not reproduced under our conditions |
| synthetic-tree | c1 | yes | Not reproduced under our conditions | Not reproduced under our conditions |
| wine-boosting | c1 | no | Reproduced | Reproduced |

### Set B: real papers

5 papers, 6 verdicts.

| Metric | Result | Target |
|---|---|---|
| Inconclusive rate | 100.0% (6/6) | <= 40%: not met |
| Failed runs | 2 | |
| Median cost per claim | $0.06 | |
| Median run time | 4.7 min | |
| Total cost | $0.74 | |

| Verdict | Claims |
|---|---|
| Inconclusive | 6 |

Conditions: the scale, seeds and hardware of each run's plan, which can differ from the paper's; each run's Proof Bundle lists them.

| Inconclusive reason | Claims |
|---|---|
| HIGH_VARIANCE | 2 |
| IMPLAUSIBLE_GAP | 2 |
| RUNTIME_ERROR | 2 |

### Set C: claim extraction

_Not run yet._

### Routing ablation

_Not run yet._
