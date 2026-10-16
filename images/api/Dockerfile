# p2p-api: the control plane (FastAPI + the built web UI + litestream), deployed as the
# Serverless endpoint. Its start command arrives with the runtime packaging task (A-14).
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
ENV UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
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
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    P2P_ENV=prod \
    DB_URL=sqlite+aiosqlite:////var/lib/p2p/p2p.db \
    PYTHONPATH=/srv/p2p \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin
RUN groupadd --gid 10001 p2p \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin p2p \
 && install -d -o 10001 -g 10001 /var/lib/p2p
COPY --from=build /opt/venv /opt/venv
COPY --from=build /usr/local/bin/litestream /usr/local/bin/litestream
WORKDIR /srv/p2p
COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY config ./config
COPY schemas ./schemas
COPY migrations ./migrations
COPY --from=web /web/dist ./web/dist
USER 10001:10001
EXPOSE 8080
