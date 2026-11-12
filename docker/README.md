# Docker-упаковка cmp-voice

Одна команда вместо ручной установки: три процесса (Silero TTS, LLM-демон,
голосовой сервис) в одном контейнере с доступом к GPU.

## Что нужно один раз

На хосте (в WSL/Linux) должны быть собраны и скачаны:

| Что | Откуда |
|---|---|
| форк `llama.cpp` (`src/llama-prism/build/bin/libllama.so`) | `scripts/build_llama.sh` (или ручная сборка, см. README) |
| демон `daemon/bonsai_daemon` | `scripts/build_daemon.sh` |
| модели LLM/ASR/TTS | README, раздел «Модели» |
| Docker + NVIDIA Container Toolkit | `sudo apt install docker.io docker-compose-v2`; для GPU — `nvidia-container-toolkit` |

## Запуск

```bash
cd ~/cmp-voice
cp docker/.env.example docker/.env      # пути на хосте
docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml ps
curl -s localhost:8110/health | python3 -m json.tool
```

Интерфейс: **http://localhost:8110/** (микрофон — только localhost/HTTPS).

## Обновление

```bash
git pull
docker compose -f docker/docker-compose.yml up -d --build
```

Модели и собранный демон не входят в образ — они монтируются с хоста, поэтому
пересборка занимает минуты, а не час.

## Что внутри образа

- `nvidia/cuda:12.4.1-runtime-ubuntu24.04`
- Python 3.12 + зависимости из `requirements.txt`
- отдельное окружение `.venv-silero` с CPU-сборкой torch (иначе конфликт с
  onnxruntime-gpu — см. README)
- `espeak-ng-data` и `ffmpeg` для нормализации/ASR
- `entrypoint.sh` поднимает silero → демон → voice (healthcheck на `/health`)

## Ограничения

- Образ собирается под CUDA 12.4; если драйвер хоста новее — это нормально
  (CUDA-рантайм совместим снизу вверх).
- Kokoro на GPU в контейнере недоступен: onnxruntime-gpu требует CUDA 13,
  а базовый образ 12.4; Kokoro работает на CPU (как и в systemd-варианте).
- В контейнере нет systemd: перезапуск обеспечивает `restart: unless-stopped`.
- Микрофон в браузере: открывать через `http://localhost:8110/` на хосте
  (порт публикуется только на 127.0.0.1).
