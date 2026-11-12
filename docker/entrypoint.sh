#!/bin/bash
# entrypoint.sh — поднимает три процесса в одном контейнере:
#   silero (CPU TTS, :8023), bonsai_daemon (LLM, :18521), voice (ASGI, :8110).
# systemd в контейнере нет — супервизия простая: если падает любой процесс,
# контейнер завершается (docker перезапустит, restart: unless-stopped).
set -euo pipefail
ROOT=/app
export PATH="$ROOT/.venv/bin:$PATH"
export PYTHONPATH="$ROOT"

: "${BONSAI_MODEL:=/models/bonsai9/PQ2_0.gguf}"
: "${ASR_MODEL_DIR:=/app/tone_asr/sherpa-onnx-streaming-t-one-russian-2025-09-08}"
: "${SILERO_MODEL:=/app/models/silero/v5_6_ru.pt}"
: "${TTS_SILERO_URL:=http://127.0.0.1:8023}"
export BONSAI_MODEL ASR_MODEL_DIR SILERO_MODEL TTS_SILERO_URL

[ -f "$BONSAI_MODEL" ] || { echo "нет модели LLM: $BONSAI_MODEL"; exit 1; }
[ -d "$ASR_MODEL_DIR" ] || { echo "нет модели ASR: $ASR_MODEL_DIR"; exit 1; }
[ -f "$ROOT/daemon/bonsai_daemon" ] || { echo "нет демона: /app/daemon/bonsai_daemon (смонтируйте собранный)"; exit 1; }

pids=()
shutdown() {
    echo "[entrypoint] остановка…"
    for p in "${pids[@]}"; do kill -TERM "$p" 2>/dev/null || true; done
    wait 2>/dev/null || true
    exit 0
}
trap shutdown TERM INT

# 1) Silero TTS (torch CPU, отдельный процесс — иначе конфликт CUDA-контекстов)
echo "[entrypoint] silero :8023"
ROOT="$ROOT" CUDA_VISIBLE_DEVICES="" \
    "$ROOT/.venv-silero/bin/python" -m uvicorn silero.app:app \
    --host 127.0.0.1 --port 8023 --log-level info &
pids+=($!)

# 2) демон LLM
echo "[entrypoint] bonsai_daemon :18521"
LD_LIBRARY_PATH="$ROOT/src/llama-prism/build/bin:${LD_LIBRARY_PATH:-}" \
    "$ROOT/daemon/bonsai_daemon" &
pids+=($!)

# дать демону подняться, чтобы health успел
sleep 15

# 3) голосовой сервис — им и живём (exec, чтобы сигналы доходили)
echo "[entrypoint] voice :8110"
exec "$ROOT/.venv/bin/python" -m voice.app
