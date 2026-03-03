FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

COPY config/config.example.yml ./config/
COPY .claude/commands/ ./.claude/commands/
COPY pwa/ ./pwa/
COPY scripts/ ./scripts/

RUN mkdir -p /app/data

ENTRYPOINT ["python", "-m", "src.main"]
CMD ["standalone", "--config", "/app/config/config.yml"]
