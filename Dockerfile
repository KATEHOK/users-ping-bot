# syntax=docker/dockerfile:1
#
# Multi-stage build:
#   1. deps-builder - install Python runtime dependencies into a venv.
#   2. final        - non-root runtime image: app code + venv, using the
#      base image's system SQLite (build fails if it is older than 3.35,
#      which the code needs for RETURNING).
#
# Base image: python 3.12.14 slim, pinned by digest. To bump: pull the new
# python:3.12-slim, read its patch version and digest with `docker image
# inspect`, and update BASE_IMAGE below.

ARG BASE_IMAGE=python:3.12.14-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

FROM ${BASE_IMAGE} AS deps-builder

ENV PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt /tmp/requirements.txt
ARG PIP_VERSION=25.0.1
RUN pip install "pip==${PIP_VERSION}" \
    && pip install -r /tmp/requirements.txt

FROM ${BASE_IMAGE} AS final

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH="/app"

# Fixed, documented non-root identity; must match the volume ownership
# initialised below and the `user:` line in docker-compose.yml.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home --shell /usr/sbin/nologin app

COPY --from=deps-builder /opt/venv /opt/venv

WORKDIR /app
COPY src/app ./app
COPY requirements.txt ./requirements.txt

# /data is where the named volume mounts at runtime. Pre-creating it here
# with app:app ownership lets Docker's volume copy-up give a brand-new
# volume the right owner on first start, with no root init container.
RUN mkdir -p /data && chown app:app /data

# Build-time checks: the app must import cleanly, and the system SQLite
# reached through Python's sqlite3 module must support RETURNING (>= 3.35).
RUN python -c "import app" \
    && python -c "\
import sqlite3; \
v = sqlite3.sqlite_version; \
assert sqlite3.sqlite_version_info >= (3, 35, 0), 'sqlite too old: ' + v; \
print('sqlite3.sqlite_version', v)"

USER app
ENTRYPOINT ["python", "-m", "app"]
