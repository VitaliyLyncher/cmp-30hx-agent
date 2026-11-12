# syntax=docker/dockerfile:1
# Образ cmp-voice: CUDA runtime + Python + сервис. Модели, собранный демон и
# libs llama.cpp монтируются снаружи (см. docker-compose.yml) — образ не
# содержит 5 ГБ весов и пересобирается за минуты, а не за час.
FROM nvidia/cuda:12.9.2-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3-pip \
        ffmpeg libgomp1 libsndfile1 ca-certificates curl \
        espeak-ng-data \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt

# основное окружение (голосовой сервис, ASR, Kokoro)
RUN python3.12 -m venv /app/.venv \
    && /app/.venv/bin/pip install --upgrade pip \
    && /app/.venv/bin/pip install -r /app/requirements.txt

# отдельное окружение для Silero: torch строго CPU (конфликт с onnxruntime-gpu)
RUN python3.12 -m venv /app/.venv-silero \
    && /app/.venv-silero/bin/pip install --upgrade pip \
    && /app/.venv-silero/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && /app/.venv-silero/bin/pip install fastapi uvicorn numpy pydantic

COPY . /app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8110
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8110/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
