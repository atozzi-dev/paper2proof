"""Well-known datasets a claim names, matched to the Hugging Face copy the provisioner fetches:
experiments run offline, where torchvision cannot download MNIST (seen in production)."""

import pytest

from app.discovery.known_datasets import KNOWN, named_in
from app.domain.discovery import HF_DATASET_ID


def _ids(dataset: str, text: str = "") -> list[str]:
    return [k.hf_id for k in named_in(dataset, text)]


@pytest.mark.parametrize(
    ("dataset", "expected"),
    [
        ("MNIST", ["ylecun/mnist"]),
        ("mnist (60k train / 10k test)", ["ylecun/mnist"]),
        ("Permuted MNIST", ["ylecun/mnist"]),
        ("Fashion-MNIST", ["zalando-datasets/fashion_mnist"]),
        ("FashionMNIST", ["zalando-datasets/fashion_mnist"]),
        ("Fashion MNIST and MNIST", ["zalando-datasets/fashion_mnist", "ylecun/mnist"]),
        ("CIFAR-10", ["uoft-cs/cifar10"]),
        ("CIFAR10", ["uoft-cs/cifar10"]),
        ("CIFAR-100", ["uoft-cs/cifar100"]),
        ("cifar_100", ["uoft-cs/cifar100"]),
        ("CIFAR-10 and CIFAR-100", ["uoft-cs/cifar100", "uoft-cs/cifar10"]),
        ("IMDb movie reviews", ["stanfordnlp/imdb"]),
        ("SST-2", ["stanfordnlp/sst2"]),
        ("GLUE SST2", ["stanfordnlp/sst2"]),
        # CatBoost and InterpretML (production, 2026-10-30): UCI Adult, no planned copy
        ("Adult", ["AiresPucrs/adult-census-income"]),
        ("adult-income", ["AiresPucrs/adult-census-income"]),
        ("UCI Adult census income", ["AiresPucrs/adult-census-income"]),
        ("Adult Income", ["AiresPucrs/adult-census-income"]),
    ],
)
def test_known_datasets_are_named(dataset: str, expected: list[str]) -> None:
    assert _ids(dataset) == expected


@pytest.mark.parametrize(
    "dataset",
    [
        "KMNIST",
        "EMNIST letters",
        "notMNIST",
        "CIFAR-1000",
        "SST-5",
        "IMDB-BINARY",  # a graph benchmark (TU datasets), not the reviews
        "IMDB-M",
        "IMDB-WIKI",  # face images with ages
        "ImageNet",
        "adult faces",  # lowercase "adult" is a word, not the dataset
        "Adult patients (MIMIC-III)",
        "",
    ],
)
def test_other_datasets_name_none(dataset: str) -> None:
    assert _ids(dataset) == []


@pytest.mark.parametrize(
    "dataset",
    [
        "digits (scikit-learn load_digits, 8x8 MNIST-like images)",
        "sklearn digits, a small MNIST",
        "make_moons",
        "synthetic MNIST-like data generated in code",
    ],
)
def test_data_bundled_with_scikit_learn_or_generated_names_none(dataset: str) -> None:
    assert _ids(dataset) == []


def test_the_text_counts_only_when_the_dataset_field_names_none() -> None:
    assert _ids("standard benchmark", "Ours reaches 99% on MNIST") == ["ylecun/mnist"]
    assert _ids("CIFAR-10", "unlike earlier MNIST results") == ["uoft-cs/cifar10"]
    assert _ids("digits", "on the scikit-learn digits, a tiny MNIST") == []


def test_every_hugging_face_id_is_one_the_provisioner_accepts() -> None:
    assert all(HF_DATASET_ID.fullmatch(k.hf_id) for k in KNOWN)
    assert all(k.hf_id.lower() in k.ids and all(i == i.lower() for i in k.ids) for k in KNOWN)
