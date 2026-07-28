# Build from the repo root: docker build -f api/Dockerfile -t unlimited-ocr-api:local .
FROM python:3.12-slim

WORKDIR /app
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/grounding_to_markdown.py /app/grounding_to_markdown.py
COPY api/app.py /app/app.py

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
