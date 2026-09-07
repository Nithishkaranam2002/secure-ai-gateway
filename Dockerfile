FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY config/ ./config/
COPY start.sh ./start.sh
RUN chmod +x ./start.sh

# The console is a single static file served by the LLM gateway, so it ships
# inside the image rather than being deployed separately.

# The database lives on a mounted volume so token usage and the audit log
# survive a container restart, which is the point of using on disk SQLite
# rather than an in memory counter.
RUN mkdir -p /app/data

EXPOSE 8000 8001
