# Build from the repository root: docker build -f src/web/Dockerfile .
# The console imports the worker (Desk, Cosmos sink) and winprob packages from src/.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PUBLIC_MODE=1

WORKDIR /app
COPY src/web/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY src/ /app/src/

# Port 80 matches the placeholder image used at first provisioning.
EXPOSE 80
CMD ["gunicorn", "--bind", "0.0.0.0:80", "--workers", "2", "--worker-class", "gthread", "--threads", "4", "--chdir", "/app/src", "web.app:server"]
