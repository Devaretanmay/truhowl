# Truhowl GitHub App daemon.
# Build:  docker build -f docker/Dockerfile.app -t truhowl-app:1.1.3 .
# Run:    docker run -p 8080:8080 --env-file .env -v truhowl-data:/data truhowl-app:1.1.3
# Requires TRUHOWL_WEBHOOK_SECRET. See docs/DEPLOY.md.

FROM rust:1.82-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --break-system-packages maturin
WORKDIR /src
COPY Cargo.toml Cargo.lock pyproject.toml README.md ./
COPY src ./src
COPY python ./python
RUN maturin build --release --out /wheel

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
        git nodejs npm \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /wheel/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
RUN useradd -m truhowl && mkdir -p /data && chown truhowl:truhowl /data
USER truhowl
ENV PORT=8080 \
    TRUHOWL_REPOS_DIR=/data/repos \
    TRUHOWL_INSTALLATIONS_DIR=/data/installations
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD \
    python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:' + __import__('os').environ.get('PORT','8080') + '/health')"
CMD ["sh", "-c", "truhowl app serve --port ${PORT:-8080}"]
