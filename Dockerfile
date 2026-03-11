FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . .

RUN uv pip install --system --no-cache -e .

RUN mkdir -p /root/.nanobot/workspace/skills /root/.nanobot/workspace/memory /root/.nanobot/workspace/sessions /root/.nanobot/cron

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g @four-meme/four-meme-ai@latest tsx

RUN chmod +x start.sh

EXPOSE 18790

CMD ["bash", "start.sh"]
