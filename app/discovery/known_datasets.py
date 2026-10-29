"""Well-known datasets by name, each with the Hugging Face copy the provisioner fetches. The
Planner sometimes plans no data for a claim on, say, MNIST ("loaded via torchvision"), but the
experiment runs offline, where torchvision cannot download: the provisioned parquet copy is
what works. Claim text is untrusted: it is only matched against these curated patterns, and
only the curated names and ids below ever reach a plan or its notes."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class KnownDataset:
    name: str  # as plan notes show it
    hf_id: str  # the public, ungated parquet copy on Hugging Face
    pattern: re.Pattern[str]
    ids: frozenset[str]  # every Hugging Face id of the same data (lowercase): hf_id, legacy ids


def _known(name: str, hf_id: str, pattern: str, legacy: str) -> KnownDataset:
    return KnownDataset(
        name, hf_id, re.compile(pattern, re.IGNORECASE), frozenset({hf_id.lower(), legacy})
    )


# Most specific first: each match is blanked out before the next pattern runs, so Fashion-MNIST
# never also reads as MNIST (CIFAR-100 against CIFAR-10 is a word boundary already).
KNOWN: tuple[KnownDataset, ...] = (
    _known(
        "Fashion-MNIST",
        "zalando-datasets/fashion_mnist",
        r"\bfashion[-_ ]?mnist\b",
        "fashion_mnist",
    ),
    _known("MNIST", "ylecun/mnist", r"\bmnist\b", "mnist"),
    _known("CIFAR-100", "uoft-cs/cifar100", r"\bcifar[-_ ]?100\b", "cifar100"),
    _known("CIFAR-10", "uoft-cs/cifar10", r"\bcifar[-_ ]?10\b", "cifar10"),
    # IMDB-BINARY / IMDB-MULTI are graph benchmarks and IMDB-WIKI is face images: not reviews
    _known(
        "IMDB",
        "stanfordnlp/imdb",
        r"\bimdb\b(?![-_ ]?(?:binary|multi|wiki|b|m)\b)",
        "imdb",
    ),
    _known("SST-2", "stanfordnlp/sst2", r"\bsst[-_ ]?2\b", "sst2"),
)
# Data that comes with scikit-learn or is generated in code: never one of the above, even when
# described as "MNIST-like" (load_digits is 8x8 digits, not MNIST)
_BUNDLED = re.compile(
    r"\b(?:scikit[-_ ]?learn|sklearn|load_[a-z0-9_]+|make_[a-z0-9_]+|synthetic)\b", re.IGNORECASE
)


def _named(field: str) -> list[KnownDataset]:
    found = []
    for known in KNOWN:
        if known.pattern.search(field):
            found.append(known)
            field = known.pattern.sub(" ", field)
    return found


def named_in(dataset: str, text: str) -> list[KnownDataset]:
    """The known datasets a claim names: its `dataset` field, or its text when the field names
    none. A field that says its data comes with scikit-learn or is synthetic names none."""
    for field in (dataset, text):
        if _BUNDLED.search(field):
            return []
        found = _named(field)
        if found:
            return found
    return []
