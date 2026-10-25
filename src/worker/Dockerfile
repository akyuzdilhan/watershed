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
# Exact versions from src/worker/constraints.txt (in the copied src/): two builds of the same commit install the same Python packages.
RUN pip install -c src/worker/constraints.txt ".[azure,agents]"
# The demo replay, read by the replay job (python -m generator.publish data/replay/demo-0007.jsonl.gz)
COPY data/replay ./data/replay
# The league history behind the insights and the storyline (data/history/9/, about 2.5 MB). Deterministic for a seed and
# built from the code in src/, so it is generated here rather than copied: it always matches the names and the league of the image.
RUN python -m league history --seed 9 --seasons 50 --out data/history

RUN useradd --uid 10001 --no-create-home watershed
USER 10001
# The history was written by root: fail the build if the runtime user cannot load it.
RUN python -c "from league.history import History; History.load('data/history/9')"

# KEDA starts the replica when the worker subscription has messages and stops it after its cool-down (infra/resources.bicep).
# --forever keeps one process, and the match state in its memory, through the quiet stretches of a replay (seven of more than
# 30 s in the demo at speed 10); without it the listener stops, and the process started next sees the rest of the match without
# its beginning. The loop only restarts the process after a crash. Seed 9, matchday 30: the league season of the demo match.
# WORKER_EXTRA_ARGS is appended last, without a rebuild (for example --auto-approve in judging mode).
CMD ["sh", "-c", "while true; do python -m worker --live --cosmos --writer foundry --forever --history data/history/9 --league-seed 9 --matchday 30 $WORKER_EXTRA_ARGS; sleep 5; done"]
