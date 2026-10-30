# p2p-runner-cpu: CPU smoke jobs (the Sandboxes fallback, execution-contracts §8) and the
# Sandboxes image. Same entrypoint and the same public package versions as p2p-runner, with
# PyTorch CPU wheels. Build from the repository root:
#   docker build -f images/runner-cpu/Dockerfile -t p2p-runner-cpu:dev .
FROM python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /usr/local/bin/uv
ENV UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1
COPY images/runner-cpu/requirements.lock /tmp/build/
COPY images/runner/requirements.lock /tmp/build/runner.lock
COPY scripts/runner_constraints.py scripts/split_lock.py /tmp/build/
# One install per index (the lock records each package's index): only torch and torchvision
# come from PyTorch's index, everything else from PyPI; hashes are required either way.
RUN python /tmp/build/split_lock.py /tmp/build/requirements.lock /tmp/build/parts \
 && uv venv /opt/venv --python /usr/local/bin/python3.12 \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-config --require-hashes --no-deps \
      --only-binary :all: --index-url https://pypi.org/simple -r /tmp/build/parts/pypi.txt \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-config --require-hashes --no-deps \
      --only-binary :all: --index-url https://download.pytorch.org/whl/cpu \
      -r /tmp/build/parts/pytorch.txt \
 && python /tmp/build/runner_constraints.py /tmp/build/runner.lock /tmp/build/runner-constraints.txt

FROM python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin
# /opt/p2p is created here as root 0755: when a COPY --chmod=0444 creates it, recent BuildKit
# leaves it without a search bit and p2p cannot open its own scripts.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini zstd libgomp1 \
 && rm -rf /var/lib/apt/lists/* \
 && echo 'p2p:x:10001:' >> /etc/group \
 && echo 'p2p:x:10001:10001:p2p:/home/p2p:/usr/sbin/nologin' >> /etc/passwd \
 && install -d -o 10001 -g 10001 -m 0750 /home/p2p \
 && install -d -o 10001 -g 10001 /work \
 && install -d -m 0755 /opt/p2p \
 && command -v setpriv setsid timeout tail >/dev/null \
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} +  # no setuid path to root
COPY --from=build /opt/venv /opt/venv
# uv leaves a world-writable lock file in the venv: nothing in it may be writable by p2p
RUN rm -f /opt/venv/.lock
COPY --from=build --chmod=0444 /tmp/build/runner-constraints.txt /opt/p2p/runner-constraints.txt
COPY --chmod=0444 images/runner/entrypoint.py images/runner/smoke.py images/runner/harvest.py /opt/p2p/
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/venv/bin/python", "-I", "/opt/p2p/entrypoint.py"]
