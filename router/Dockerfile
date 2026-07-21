FROM oven/bun:1-debian

# noVNC static files only — same package/path the worker uses.
RUN apt-get update && apt-get install -y --no-install-recommends novnc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json bun.lock* ./
RUN bun install --production
COPY src ./src

ENV PORT=8080 NOVNC_DIR=/usr/share/novnc
EXPOSE 8080
CMD ["bun", "run", "src/index.ts"]
