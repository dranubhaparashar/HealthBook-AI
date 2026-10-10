FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY api ./api
COPY web ./web
COPY migrations ./migrations
COPY alembic.ini .
ENV BABYCARE_DATA_DIR=/data
RUN useradd --uid 10001 --create-home babycare && mkdir /data && chown babycare:babycare /data && chmod 700 /data
USER babycare
VOLUME /data
EXPOSE 8000
# Render injects PORT; local Docker keeps 8000.
CMD ["sh","-c","uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log"]
