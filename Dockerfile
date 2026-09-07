FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini pyproject.toml README.md ./
COPY data/models/bge-small-zh-v1.5-onnx-int8 ./data/models/bge-small-zh-v1.5-onnx-int8

EXPOSE 8000
CMD ["uvicorn", "app.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
