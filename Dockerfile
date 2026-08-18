# One image, two roles: `polymarket run` writes the capture, `polymarket
# dashboard` serves it. They share everything except the argument they are
# started with, so building them separately would only mean building twice.
#
# Both stages sit on the same python:3.12-slim base on purpose. A virtualenv
# records the absolute path of the interpreter it was built against, so copying
# .venv between stages is only sound when that interpreter is at the same path
# in both -- uv's own image ships a different one, which is why uv arrives here
# as a copied binary rather than as the base.

FROM python:3.12-slim-bookworm AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, from the lockfile alone: this layer is what makes a
# rebuild after a source edit take a second rather than a minute.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

# --no-editable so the package is copied into the venv rather than linked back
# to /app/src, which the final stage does not have.
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-bookworm

# Unbuffered because the capture's only output is its log, and a buffered one
# reaches `docker logs` in silent 8 KB bursts.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# uid 1000 to match the usual first host account, so the bind-mounted capture
# directory is writable without a chown. Override with `user:` in compose if
# yours differs.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data

COPY --from=build --chown=app:app /app/.venv /app/.venv

USER app
WORKDIR /data
VOLUME /data

# TLS trust comes from certifi, which httpx depends on, so no system CA bundle
# is needed -- and neither is curl: the dashboard's health check is a Python
# one-liner in docker-compose.yml.
ENTRYPOINT ["polymarket"]
CMD ["--help"]
