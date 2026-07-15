FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY pyproject.toml README.md ./

EXPOSE 8000
CMD ["uvicorn", "app.api.server:app", "--host", "0.0.0.0", "--port", "8000"]

