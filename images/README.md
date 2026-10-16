# Container images

Four images, all built from the repository root (`make images`, tag `dev`; `.dockerignore` is an
allowlist). Every base image is pinned by digest, Python packages come from hash-checked locks
(wheels only), and every image runs as the non-root user `p2p` (uid/gid 10001).

| Image | Used by | Contents |
|---|---|---|
| `p2p-runner` | Exec jobs (GPU) | `images/runner/entrypoint.py` + the ML stack in `images/runner/requirements.lock` (PyTorch CUDA 12.6 wheels) |
| `p2p-runner-cpu` | CPU smoke jobs, Sandboxes | Same entrypoint, same public versions (`images/runner-cpu/requirements.lock`, PyTorch CPU wheels) |
| `p2p-provisioner` | Provision jobs (allowlisted egress) | `images/provisioner/provision.py`, uv, git, zstd, `huggingface_hub`, `pyarrow` |
| `p2p-api` | The Serverless endpoint | The FastAPI app, the built `web/dist`, the `litestream` binary (checksum verified) |

## Layout inside the images

- `/opt/venv`: the image's Python environment (Python 3.12, `python:3.12-slim-trixie`).
- `/opt/p2p/runner-constraints.txt` (runner, runner-cpu, provisioner): generated from the GPU
  runner lock at build time by `scripts/runner_constraints.py`, public versions only, root-owned,
  mode 0444. The provisioner locks a run's extra packages against it and leaves out what the
  runner already provides, so an overlay built once works on both runners.
- `/work`: local scratch disk, owned by `p2p` (archives are extracted here, never on the mount).
- Runners and the provisioner start as `tini -- /opt/venv/bin/python -I <script>`: tini is
  PID 1, reaps orphaned children and forwards SIGTERM (the watchdog relies on it). `-I` makes
  the entrypoint ignore every `PYTHON*` variable (the image's own included) and the working
  directory; the experiment processes it starts get their environment from the entrypoint.
- Nebius jobs run the image ENTRYPOINT (JobSpec has no command field: keep it that way).
  Sandboxes (S-3) runs shell commands in `p2p-runner-cpu`: S-3 must confirm it replaces the
  ENTRYPOINT rather than appending to it.

## GPU

The runner uses PyTorch's CUDA 12.6 wheels (with the `nvidia-*-cu12` runtime libraries) on a slim
Debian base instead of an `nvidia/cuda` base: the libraries would otherwise ship twice. The
NVIDIA driver comes from the host (`NVIDIA_VISIBLE_DEVICES`, `NVIDIA_DRIVER_CAPABILITIES` are
set as in the nvidia/cuda images). The default PyPI wheels are CUDA 13 and need a much newer
driver; CUDA 12.6 runs on any driver of the 12.x era through minor-version compatibility. The
`nvidia-smi` output of spike S-4 and `torch.cuda.is_available()` in S-12 settle it on Nebius.

There is no C compiler in the runner: `torch.compile` and custom Triton kernels (which build a
launcher with `cc` on first use) are not supported. Packages with compiled PyTorch extensions
(torchaudio, xformers, flash-attn, torch-scatter) don't fit one overlay for both runners
(CUDA build vs CPU torch) and are out of scope for now.

## Changing packages

1. Edit `images/runner/pyproject.toml` and `images/runner-cpu/pyproject.toml` (keep both lists
   the same) or `images/provisioner/requirements.in`. The runners' files are lock inputs, not
   projects: they pin torch and torchvision to PyTorch's CUDA 12.6 / CPU index as explicit
   indexes, so nothing else comes from there (the locks carry `# from` annotations and a test
   checks them).
2. `bash scripts/lock_images.sh` (re-locks all three, the CPU runner constrained to the GPU
   runner's versions), then commit the locks.
3. `make images` and `make test-docker`.

The provisioner locks a run's packages the same way: it writes a small `pyproject.toml` that
lists the run's packages plus torch and torchvision from PyTorch's CUDA 12.6 index, so a package
that depends on torch (torchmetrics, lightning, peft) resolves against the runner's build; the
runner's packages are then left out of the overlay.

`tests/images/test_images.py` checks the locks and Dockerfiles without Docker;
`tests/images/test_docker.py` checks the built images (uid, imports, constraints, tini, a real
zstd round trip, deterministic packing, and a provisioned overlay imported by the CPU runner).

## Building

`make images` builds for `linux/amd64` (Nebius, the locks and litestream are amd64 only), also on
an arm64 machine. It needs BuildKit with Dockerfile frontend 1.6 or later (`COPY --chmod`,
`ADD --checksum`): any current Docker Desktop or Docker Engine.

## Size budget

`scripts/check_image_size.sh` (run by `make images`) measures `docker save | gzip -1`:
`p2p-runner` <= 8 GB, `p2p-runner-cpu` <= 2.5 GB. Pull time counts for every job (S-12).

## Pushing

Pushing to the Nebius registry is a human step (deploy, H-04). Jobs and the endpoint always
reference the pushed digest, never a tag.
