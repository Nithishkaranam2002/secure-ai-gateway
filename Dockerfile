FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/

# The database lives on a mounted volume so token usage and the audit log
# survive a container restart, which is the point of using on disk SQLite
# rather than an in memory counter.
RUN mkdir -p /app/data

EXPOSE 8000 8001
