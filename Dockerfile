# syntax=docker/dockerfile:1
#
# Multi-stage build:
#   1. sqlite-builder  - build a current SQLite shared library from verified
#      upstream source (the Debian base ships an old libsqlite3).
#   2. deps-builder     - install Python runtime dependencies into a venv.
#   3. final            - non-root runtime image: app code + venv + fresh
#      libsqlite3, nothing else.

FROM python:3.12.3-slim AS sqlite-builder

# SQLite release to build. Bump SQLITE_VERSION/SQLITE_YEAR/SQLITE_SHA3_256
# together after checking https://www.sqlite.org/download.html for the
# current stable release; do not drop the checksum check.
ARG SQLITE_VERSION=3530400
ARG SQLITE_YEAR=2026
ARG SQLITE_SHA3_256=454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/sqlite
RUN wget -q "https://www.sqlite.org/${SQLITE_YEAR}/sqlite-autoconf-${SQLITE_VERSION}.tar.gz" \
    && python3 -c "\
import hashlib, sys; \
expected = '${SQLITE_SHA3_256}'; \
data = open('sqlite-autoconf-${SQLITE_VERSION}.tar.gz', 'rb').read(); \
actual = hashlib.sha3_256(data).hexdigest(); \
sys.exit('checksum mismatch: got ' + actual + ', expected ' + expected) if actual != expected else print('sqlite tarball checksum OK:', actual)" \
    && tar xzf sqlite-autoconf-${SQLITE_VERSION}.tar.gz \
    && cd sqlite-autoconf-${SQLITE_VERSION} \
    && CFLAGS="-O2 -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_RTREE" \
       ./configure --prefix=/opt/sqlite --disable-static \
    && make -j"$(nproc)" \
    && make install

FROM python:3.12.3-slim AS deps-builder

ENV PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /tmp/requirements.txt

FROM python:3.12.3-slim AS final

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH="/app" \
    LD_LIBRARY_PATH="/opt/sqlite/lib"

# Fixed, documented non-root identity; must match the volume ownership
# initialised below and the `user:` line in docker-compose.yml.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home --shell /usr/sbin/nologin app

COPY --from=sqlite-builder /opt/sqlite/lib/ /opt/sqlite/lib/
COPY --from=deps-builder /opt/venv /opt/venv

WORKDIR /app
COPY src/app ./app
COPY requirements.txt ./requirements.txt

# /data is where the named volume mounts at runtime. Pre-creating it here
# with app:app ownership lets Docker's volume copy-up give a brand-new
# volume the right owner on first start, with no root init container.
RUN mkdir -p /data && chown app:app /data

# Build-time checks: the app must import cleanly, and the SQLite version
# actually reachable through Python's sqlite3 module (not the OS package,
# not the CLI) must be the one just built.
RUN python -c "import app" \
    && python -c "\
import sqlite3; \
v = sqlite3.sqlite_version; \
assert v == '3.53.4', 'expected linked sqlite 3.53.4, got ' + v; \
print('sqlite3.sqlite_version', v)"

USER app
ENTRYPOINT ["python", "-m", "app"]
