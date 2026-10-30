FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/ ./api/
COPY frontend/ ./frontend/
# Only the built retrieval index is needed at runtime — not the raw
# per-page crawl_output/ files or the intermediate urls.json.
COPY crawler/chunks.json crawler/url_index.json ./crawler/

# Cloud Run injects $PORT (defaults to 8080); uvicorn must bind to it.
ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT}
