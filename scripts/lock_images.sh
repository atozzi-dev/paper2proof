#!/usr/bin/env bash
# Re-lock the image requirements with hashes (A-06): wheels only, Python 3.12, manylinux_2_28.
# The runners' inputs are images/runner{,-cpu}/pyproject.toml, which pin torch and torchvision
# to PyTorch's CUDA 12.6 / CPU index (explicit indexes: nothing else is taken from them); the
# CPU runner is constrained to the GPU runner's public versions. Run after changing an input;
# commit the locks.
set -euo pipefail
cd "$(dirname "$0")/.."

common=(--python-version 3.12 --python-platform x86_64-manylinux_2_28 --only-binary :all:
  --generate-hashes --no-config --emit-index-annotation --index-url https://pypi.org/simple
  --custom-compile-command "scripts/lock_images.sh")

uv pip compile "${common[@]}" -o images/runner/requirements.lock images/runner/pyproject.toml

# the GPU runner's versions, at a relative path (it shows up in the CPU lock's annotations)
versions=images/runner-cpu/gpu-versions.txt
trap 'rm -f "$versions"' EXIT
uv run --no-project python scripts/runner_constraints.py images/runner/requirements.lock "$versions"
uv pip compile "${common[@]}" -c "$versions" \
  -o images/runner-cpu/requirements.lock images/runner-cpu/pyproject.toml

uv pip compile "${common[@]}" \
  -o images/provisioner/requirements.lock images/provisioner/requirements.in
