# worker/Dockerfile
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1

# System deps: X virtual framebuffer, VNC, noVNC/websockify, and the libraries
# Chromium needs to run headed.
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc novnc websockify \
    ca-certificates curl gnupg \
    fonts-liberation libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpangocairo-1.0-0 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# uv (fast Python package manager).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Resolve deps first for layer caching.
COPY pyproject.toml ./
COPY uv.lock* ./
RUN uv sync --no-install-project || uv pip install --system \
    "browser-use>=0.7.0" "redis>=5.0.0" "httpx>=0.27.0" "pydantic>=2.7.0"

# Install the Chromium browser-use will drive.
RUN uv run playwright install --with-deps chromium || \
    uv run python -m browser_use.cli install || true

COPY src ./src
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

ENV WEBSOCKIFY_PORT=6080 \
    NOVNC_DIR=/usr/share/novnc \
    VNC_TOKEN_DIR=/tmp/vnc-tokens

EXPOSE 6080

CMD ["./entrypoint.sh"]
