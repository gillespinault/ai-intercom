FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY src/ ./src/
COPY config/config.example.yml ./config/

RUN mkdir -p /app/data

ENTRYPOINT ["python", "-m", "src.main"]
CMD ["standalone", "--config", "/app/config/config.yml"]
