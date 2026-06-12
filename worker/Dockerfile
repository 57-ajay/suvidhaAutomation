FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Xvfb + VNC + noVNC/websockify for the live view, and the libraries headed
# Chromium needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc novnc websockify ca-certificates curl \
    fonts-liberation fonts-noto-color-emoji \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libxshmfence1 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libglib2.0-0 libdbus-1-3 libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml ./
COPY uv.lock* ./
RUN uv sync --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src

# Chromium where browser-use 0.12 looks for it (~/.cache/ms-playwright).
# This is the exact command its LocalBrowserWatchdog would otherwise run at
# first launch — baking it in keeps container startup network-free.
RUN uvx playwright install chromium

COPY src ./src
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV WEBSOCKIFY_PORT=6080 \
    NOVNC_DIR=/usr/share/novnc \
    VNC_TOKEN_DIR=/tmp/vnc-tokens

EXPOSE 6080

CMD ["./entrypoint.sh"]
