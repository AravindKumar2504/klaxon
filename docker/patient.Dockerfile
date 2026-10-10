# One image for the four patient services: fake provider, webhook receiver,
# payments API and ledger consumer. compose.yaml picks the service with `command`.
FROM python:3.12.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Exact dependency versions first, so editing our code does not reinstall them.
# --no-deps plus pip check: only what the lock file names gets installed, and it must be complete.
COPY requirements/services.lock.txt requirements/services.lock.txt
RUN pip install --no-deps -r requirements/services.lock.txt && pip check

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps .

# Run as an unprivileged user. /data holds the fake provider's own SQLite file.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin klaxon \
    && mkdir /data && chown klaxon /data
USER klaxon
EXPOSE 8000
