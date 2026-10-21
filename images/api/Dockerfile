# p2p-api: the control plane (FastAPI + the built web UI + litestream), deployed as the
# Serverless endpoint. It starts with images/api/start.sh (Litestream restore, migrations,
# uvicorn under `litestream replicate`).
# Build from the repository root:
#   docker build -f images/api/Dockerfile -t p2p-api:dev .
FROM node:22-trixie-slim@sha256:154ba2f4d6fec323d28e4f4bb86bba4677f1223391a1979cf521304e03a98dfa AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /usr/local/bin/uv
ENV UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv UV_NO_BUILD=1
WORKDIR /srv/p2p
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python3.12
ADD --checksum=sha256:cfb371176d164437ae869f8351cfde49bd1804ae71c61923f75c9cba9c9c006d \
    https://github.com/benbjohnson/litestream/releases/download/v0.5.17/litestream-0.5.17-linux-x86_64.tar.gz \
    /tmp/litestream.tar.gz
RUN mkdir /tmp/litestream \
 && tar -xzf /tmp/litestream.tar.gz -C /tmp/litestream \
 && install -m 0755 /tmp/litestream/litestream /usr/local/bin/litestream

FROM python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
# the commit this image is built from: /readyz reports it and Proof Bundles record it
ARG BUILD_COMMIT
LABEL org.opencontainers.image.revision=$BUILD_COMMIT
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    P2P_BUILD_COMMIT=$BUILD_COMMIT \
    P2P_ENV=prod \
    DB_URL=sqlite+aiosqlite:////var/lib/p2p/p2p.db \
    P2P_VAR_DIR=/var/lib/p2p \
    PYTHONPATH=/srv/p2p \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin
# the slim base has no useradd: the user is written directly
# /opt/p2p is created here as root 0755: when a COPY --chmod=0444 creates it, recent BuildKit
# leaves it without a search bit and p2p cannot open its own scripts.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* \
 && echo 'p2p:x:10001:' >> /etc/group \
 && echo 'p2p:x:10001:10001:p2p:/home/p2p:/usr/sbin/nologin' >> /etc/passwd \
 && install -d -o 10001 -g 10001 -m 0750 /home/p2p \
 && install -d -o 10001 -g 10001 /var/lib/p2p \
 && install -d -m 0755 /opt/p2p
COPY --from=build /opt/venv /opt/venv
# uv leaves a world-writable lock file in the venv: nothing in it may be writable by p2p
RUN rm -f /opt/venv/.lock
COPY --from=build /usr/local/bin/litestream /usr/local/bin/litestream
WORKDIR /srv/p2p
COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY config ./config
COPY schemas ./schemas
COPY migrations ./migrations
COPY docs ./docs
COPY eval/canaries ./eval/canaries
COPY --from=web /web/dist ./web/dist
COPY --chmod=0555 images/api/start.sh /opt/p2p/start.sh
COPY --chmod=0444 images/api/litestream.yml /opt/p2p/litestream.yml
# after the last COPY: no setuid/setgid bit anywhere (no path from p2p to root)
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} +
USER 10001:10001
EXPOSE 8080
# tini as PID 1 (-g: signals reach the whole group, also during restore and migrations)
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["/opt/p2p/start.sh"]
