FROM python:3.11-slim

# Keep Python output unbuffered and skip .pyc files for stable, fast logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so Docker can cache the layer independently.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code is baked into the image; no download at startup.
COPY app ./app

# Run as the image's non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Container-level health check (uses Python, so no extra packages needed).
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"

# All startup initialization (dependency install, code copy) is done at build;
# this just serves the app.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
