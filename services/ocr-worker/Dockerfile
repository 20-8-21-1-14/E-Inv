FROM python:3.11-slim

# System deps for OpenCV and pdf2image
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    poppler-utils \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY common/ /app/common/
RUN pip install --no-cache-dir -e /app/common/

COPY requirements/base.txt requirements/ocr-worker.txt ./
RUN pip install --no-cache-dir -r ocr-worker.txt

COPY services/ocr-worker/ /app/service/
WORKDIR /app/service

ENV PYTHONPATH=/app/service:/app/common
ENV OMP_NUM_THREADS=4

# Models are mounted via volume at /app/model_registry
CMD ["celery", "-A", "celery_app", "worker", "--loglevel=info", "--pool=solo", "--concurrency=1"]
