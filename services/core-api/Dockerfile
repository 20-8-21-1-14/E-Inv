FROM python:3.11-slim

WORKDIR /app

# Install common library first (cache layer)
COPY common/ /app/common/
RUN pip install --no-cache-dir -e /app/common/

# Install service dependencies
COPY requirements/base.txt requirements/core-api.txt ./
RUN pip install --no-cache-dir -r core-api.txt

# Copy service code
COPY services/core-api/ /app/service/
WORKDIR /app/service

ENV PYTHONPATH=/app/service:/app/common

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
