FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

WORKDIR /app

# OCR permanece local e inclui o idioma português.
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY pii_gateway ./pii_gateway

# Imagem de conveniência, sem torch. Para a L2 real, use:
#   uv sync --frozen --extra ner --no-dev
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    ENABLE_NER=false

EXPOSE 8000

CMD ["uvicorn", "pii_gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
