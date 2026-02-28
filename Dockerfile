FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

COPY config/config.example.yml ./config/
COPY .claude/commands/ ./.claude/commands/

RUN mkdir -p /app/data

ENTRYPOINT ["python", "-m", "src.main"]
CMD ["standalone", "--config", "/app/config/config.yml"]
