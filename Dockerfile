FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ENCBACKUP_CONFIG=/etc/encbackup/docker.toml

# restic and age come from the distribution (signed packages); encbackup only calls them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends restic age ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . \
    && useradd --system --no-create-home --uid 10001 encbackup \
    && install -d -o encbackup /state /data
COPY docker/docker.toml /etc/encbackup/docker.toml

# Everything that must survive (repository, vault, identity) lives in /state; /data is what to back up.
# Root is not needed and the filesystem can be read-only (give the container a tmpfs for /tmp).
USER encbackup
VOLUME ["/state"]
ENTRYPOINT ["encbackup"]
CMD ["--help"]
