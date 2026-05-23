FROM python:3.11-slim

WORKDIR /app

COPY common/ /app/common/
RUN pip install --no-cache-dir -e /app/common/

COPY requirements/base.txt requirements/admin-api.txt ./
RUN pip install --no-cache-dir -r admin-api.txt

COPY services/admin-api/ /app/service/
WORKDIR /app/service

ENV PYTHONPATH=/app/service:/app/common

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
