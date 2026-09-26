FROM python:3.11-slim-bookworm

# Install system dependencies for WeasyPrint (PDF generation), Tesseract (OCR),
# poppler-utils (pdf2image / pdftoppm — required for the Tesseract fallback
# in advanced_document_parser._ocr_pdf_page), and LibreOffice Calc (headless
# xlsx -> PDF for the cost-breakdown preview; worker-only, but web and worker
# share this image).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libpangoft2-1.0-0 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    libcairo2 \
    libharfbuzz0b \
    tesseract-ocr \
    poppler-utils \
    libreoffice-calc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (cache-bust: decision-maker-master-2026-04-21)
ARG CACHE_BUST=decision-maker-master-2026-04-21
COPY . .

# Create upload directory
RUN mkdir -p /app/uploads/generated_docs

EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
