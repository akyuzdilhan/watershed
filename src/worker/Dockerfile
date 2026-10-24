# Worker image, also used by the replay job. Build context: the repository root.
#   docker build -f src/worker/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml LICENSE ./
COPY src ./src
RUN pip install ".[azure,agents]"
# The demo replay, read by the replay job (python -m generator.publish data/replay/demo-0007.jsonl.gz)
COPY data/replay ./data/replay

RUN useradd --uid 10001 --no-create-home watershed
USER 10001

# KEDA starts the replica when the worker subscription has messages and stops it about 5 minutes after it is empty.
# `listen` returns after an idle period; the loop starts it again so the replica keeps consuming until KEDA scales to zero.
# WORKER_ARGS adds options without a rebuild (for example --auto-approve in judging mode).
CMD ["sh", "-c", "while true; do python -m worker --live --cosmos --writer foundry $WORKER_ARGS; sleep 5; done"]
