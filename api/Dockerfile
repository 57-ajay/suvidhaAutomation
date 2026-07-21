FROM oven/bun:1-alpine

WORKDIR /app

COPY package.json ./
COPY bun.lock* ./
RUN bun install --production

COPY tsconfig.json ./
COPY src ./src

ENV PORT=3000
EXPOSE 3000

CMD ["bun", "run", "src/index.ts"]
