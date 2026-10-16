# p2p-provisioner: the provision job image (CPU, allowlisted egress, execution-contracts §3-5).
# Locks and installs a run's extra packages against the runner's constraints, clones the
# official repository and downloads datasets; never runs code from either. Build from the
# repository root:
#   docker build -f images/provisioner/Dockerfile -t p2p-provisioner:dev .
FROM python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /usr/local/bin/uv
ENV UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never
COPY images/provisioner/requirements.lock /tmp/build/
COPY images/runner/requirements.lock /tmp/build/runner.lock
COPY scripts/runner_constraints.py /tmp/build/
RUN uv venv /opt/venv --python /usr/local/bin/python3.12 \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-config --require-hashes --no-deps \
      --only-binary :all: --index-url https://pypi.org/simple -r /tmp/build/requirements.lock \
 && python /tmp/build/runner_constraints.py /tmp/build/runner.lock /tmp/build/runner-constraints.txt

FROM python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini zstd git \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 p2p \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin p2p \
 && install -d -o 10001 -g 10001 /work \
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} +  # no setuid path to root
# uv is the provisioner's package tool at run time (lock + install --target, wheels only)
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /usr/local/bin/uv
COPY --from=build /opt/venv /opt/venv
COPY --from=build --chmod=0444 /tmp/build/runner-constraints.txt /opt/p2p/runner-constraints.txt
COPY --chmod=0444 images/provisioner/provision.py /opt/p2p/
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/venv/bin/python", "-I", "/opt/p2p/provision.py"]
