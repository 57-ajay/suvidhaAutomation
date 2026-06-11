# api/Dockerfile
FROM oven/bun:1.1-alpine

WORKDIR /app

COPY package.json ./
# bun.lockb is generated on first install; copy if present.
COPY bun.lockb* ./
RUN bun install --production

COPY tsconfig.json ./
COPY src ./src

ENV PORT=3000
EXPOSE 3000

CMD ["bun", "run", "src/index.ts"]
