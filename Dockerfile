# span-viewer: the web app, the ingesters and the sample-data generator in
# one image. docker-compose.yml runs it as `app`, `ingester` and `sample`.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY app/requirements.txt app/requirements.txt
COPY ingester/requirements.txt ingester/requirements.txt
RUN pip install -r app/requirements.txt -r ingester/requirements.txt

COPY app app
COPY ingester ingester
COPY scripts scripts
COPY db db
COPY circuits.example.yaml config.example.toml ./

RUN useradd --system --uid 10001 --home-dir /app span \
    && mkdir -p /app/data && chown span /app/data
USER span

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
