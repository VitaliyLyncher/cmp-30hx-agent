"""Wake word через openWakeWord (onnxruntime, CPU).

Пакет openWakeWord идёт с английскими моделями («hey jarvis», «alexa», …).
Русское слово требует своей обученной модели — путь задаётся WAKEWORD_MODEL_PATH.

Модуль ленивый: если пакета/модели нет, build_detector() возвращает None,
и сервис молча работает без wake word (режим не включается).
"""
from __future__ import annotations

import logging

import numpy as np

from . import config

log = logging.getLogger("voice.wakeword")

_FRAME = 1280  # 80 мс при 16 кГц — стандартный шаг openWakeWord


class WakeWordDetector:
    def __init__(self, model_names: list[str] | None = None, threshold: float = 0.5,
                 model_paths: list[str] | None = None):
        from openwakeword.model import Model  # тяжёлый импорт только при использовании

        self.threshold = float(threshold)
        self._names = model_names or []
        self._buf = np.zeros(0, dtype=np.int16)
        paths = list(model_paths or [])
        if not paths:
            # openWakeWord 0.4 принимает ТОЛЬКО пути к .onnx/.tflite, а не имена
            try:
                from openwakeword.utils import get_pretrained_model_paths
                paths = list(get_pretrained_model_paths(self._names))
            except Exception as e:  # noqa: BLE001
                log.warning("не удалось получить пути моделей wake word: %s", e)
                paths = []
        if not paths:
            raise RuntimeError("модели wake word не найдены")
        self._model = Model(wakeword_model_paths=paths)
        log.info("wake word готов: %s (порог %.2f)", paths, self.threshold)

    def feed(self, samples: np.ndarray) -> bool:
        """samples — float32 [-1..1]; True, если услышали ключевое слово."""
        pcm = np.clip(samples * 32768.0, -32768, 32767).astype(np.int16)
        if pcm.size:
            self._buf = np.concatenate([self._buf, pcm])
        hit = False
        while self._buf.size >= _FRAME:
            chunk, self._buf = self._buf[:_FRAME], self._buf[_FRAME:]
            scores = self._model.predict(chunk)
            for name, score in (scores or {}).items():
                if float(score) >= self.threshold:
                    log.info("wake word: %s (%.2f)", name, score)
                    hit = True
                    break
            if hit:
                break
        if hit:
            self._buf = np.zeros(0, dtype=np.int16)
            try:
                self._model.reset()
            except Exception:  # noqa: BLE001
                pass
        return hit

    def close(self) -> None:
        self._buf = np.zeros(0, dtype=np.int16)


def build_detector():
    """Detector или None, если wake word недоступен."""
    if not config.wakeword_enabled():
        return None
    try:
        import openwakeword  # noqa: F401
    except Exception as e:  # noqa: BLE001
        log.warning("openwakeword не установлен (%s) — wake word выключен",
                    e.__class__.__name__)
        return None

    path = config.wakeword_model_path().strip()
    if path:
        return WakeWordDetector([], config.wakeword_threshold(), model_paths=[path])

    name = config.wakeword_model().strip() or "hey_jarvis"
    try:
        # при первом запуске дотягиваем модели (они малы, ~1 МБ на слово)
        from openwakeword.utils import download_models
        download_models(model_names=[name])
    except Exception as e:  # noqa: BLE001
        log.warning("не удалось скачать модель wake word %s: %s", name, e)
    return WakeWordDetector([name], config.wakeword_threshold())
