"""ASR: стриминговый sherpa-onnx (T-one CTC, русский) на CPU.

Свойства (раздел 7 ТЗ):
  * recognizer создаётся ОДИН раз за сеанс, на фразу — новый поток;
  * каждый поток прогревается лидирующей тишиной (~0.75 с);
  * endpoint-правила: 2.4 с тишины / 1.2 с после непустого текста / 20 с макс;
  * при остановке хвост добивается ~1 с тишины + input_finished();
  * распознавание идёт в отдельном рабочем потоке, не в event loop;
  * события partial/final отдаются наружу одинаково (позже можно вынести в wasm).
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger("voice.asr")

SAMPLE_RATE = 16000
# ~20 с аудио (16 кГц × 2 байта × 100 мс = 3.2 КБ на кадр по 100 мс).
ASR_QUEUE_MAX = 200

# recognizer общий на процесс: сборка ~2 с, повторять её на каждую сессию дорого
_RECOG = None
_RECOG_LOCK = threading.Lock()


class SessionASR:
    """Один recognizer на сеанс + рабочий поток-воркер."""

    def __init__(self, model_dir: str | None = None, num_threads: int | None = None):
        self.model_dir = Path(model_dir or config.asr_model_dir())
        self.num_threads = num_threads or config.asr_num_threads()
        self._recognizer = None
        self._stream = None
        # Ограниченная очередь: если CPU не успевает за потоком (на CMP карта
        # занята LLM, ASR тоже на CPU), старые кадры лучше выбросить, чем
        # копить минуты аудио в памяти. Потери считаем для диагностики.
        self._queue: queue.Queue = queue.Queue(maxsize=ASR_QUEUE_MAX)
        self._dropped = 0
        self._thread: threading.Thread | None = None
        self._running = False
        self._on_event = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_text = ""
        self._utterance_ms = 0.0
        self._t_speech_start: float | None = None
        self._silence_since: float | None = None
        # --- новое: VAD, smart turn, wake word ---
        self._lock = threading.Lock()
        self._finish_req = threading.Event()
        self._endpoint_mode = config.asr_endpoint_mode()
        self._speech_active = False
        self._last_speech = 0.0
        self._wake_mode = False
        self._listening = True      # False в wake-режиме, пока не услышали слово
        self._wake = None
        self._wake_ready_tried = False

    # -- жизненный цикл ---------------------------------------------------
    def _build(self):
        # Recognizer дорогой (~2 с) и не зависит от сеанса: кэшируем на процесс,
        # чтобы каждая новая WebSocket-сессия не платила заново.
        global _RECOG
        with _RECOG_LOCK:
            if _RECOG is not None:
                return _RECOG
            import sherpa_onnx as so

            model = self.model_dir / "model.onnx"
            tokens = self.model_dir / "tokens.txt"
            if not model.is_file() or not tokens.is_file():
                raise FileNotFoundError(f"ASR-модель не найдена в {self.model_dir}")
            t0 = time.monotonic()
            _RECOG = so.OnlineRecognizer.from_t_one_ctc(
                tokens=str(tokens),
                model=str(model),
                num_threads=self.num_threads,
                sample_rate=SAMPLE_RATE,
                feature_dim=80,
                enable_endpoint_detection=(self._endpoint_mode == "sherpa"),
                rule1_min_trailing_silence=config.asr_silence_final(),
                rule2_min_trailing_silence=config.asr_silence_after_text(),
                rule3_min_utterance_length=config.asr_max_utterance(),
                decoding_method="greedy_search",
                provider="cpu",
            )
            log.info("ASR recognizer собран за %.0f мс (%s)",
                     (time.monotonic() - t0) * 1000, self.model_dir)
            return _RECOG

    def start(self, on_event) -> None:
        """on_event(dict) вызывается из рабочего потока; публикуем в loop."""
        self._on_event = on_event
        self._loop = asyncio.get_running_loop()
        self._running = True
        # Сборку recognizer'а и создание стрима делает воркер: иначе первый
        # клиент блокирует event loop на ~2 с (и это съедает начало речи).
        self._thread = threading.Thread(target=self._worker, name="asr-worker", daemon=True)
        self._thread.start()
        log.info("ASR-воркер запускается (endpoint=%s, vad_rms=%.3f)",
                 self._endpoint_mode, config.vad_rms())

    def _new_stream(self) -> None:
        if self._recognizer is None:
            return
        with self._lock:
            self._new_stream_locked()

    def reset_stream(self) -> None:
        """Сброс потока между фразами. Сам recognizer НЕ пересоздаётся."""
        self._new_stream()

    # -- приём аудио ------------------------------------------------------
    def feed(self, pcm16: bytes) -> None:
        if not pcm16:
            return
        try:
            self._queue.put_nowait(pcm16)
        except queue.Full:
            # Воркер не успевает: выбрасываем самый старый кадр, иначе очередь
            # растёт без предела и распознавание отстаёт всё сильнее.
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                pass
            self._dropped += 1
            try:
                self._queue.put_nowait(pcm16)
            except queue.Full:
                pass

    @property
    def dropped_chunks(self) -> int:
        return self._dropped

    def _emit(self, ev: dict) -> None:
        if self._on_event is None or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._on_event, ev)

    # -- wake word --------------------------------------------------------
    def set_wake_mode(self, enabled: bool) -> None:
        """Включить/выключить режим ожидания ключевого слова."""
        self._wake_mode = bool(enabled)
        self._listening = not self._wake_mode
        if self._wake_mode and self._wake is None and not self._wake_ready_tried:
            self._wake_ready_tried = True
            try:
                from .wakeword import build_detector
                self._wake = build_detector()
                if self._wake is None:
                    log.warning("wake word недоступен (нет openwakeword/модели) — режим выключен")
                    self._wake_mode = False
                    self._listening = True
            except Exception as e:  # noqa: BLE001
                log.warning("wake word: %s", e)
                self._wake_mode = False
                self._listening = True

    def _check_wake(self, samples: np.ndarray) -> bool:
        if self._wake is None:
            return False
        try:
            return bool(self._wake.feed(samples))
        except Exception as e:  # noqa: BLE001
            log.warning("wake feed: %s", e)
            return False

    # -- завершение фразы -------------------------------------------------
    def request_finish(self) -> None:
        """Потокобезопасно попросить воркер закрыть текущую фразу."""
        self._finish_req.set()

    def finish_utterance(self) -> None:
        """Добить хвост тишиной и форсированно закрыть вход.

        Без этого модель теряет последние ~1 с речи — свойство модели.
        """
        self.request_finish()
        # дать воркеру шанс обработать; если он стоит — закрываем сами
        for _ in range(30):
            if not self._finish_req.is_set():
                return
            time.sleep(0.02)
        with self._lock:
            self._finish_locked()

    def _finish_locked(self) -> None:
        if self._stream is None:
            self._finish_req.clear()
            return
        tail = int(config.asr_tail_silence() * SAMPLE_RATE)
        try:
            self._stream.accept_waveform(SAMPLE_RATE, np.zeros(tail, dtype=np.float32))
            self._stream.input_finished()
            assert self._recognizer is not None
            while self._recognizer.is_ready(self._stream):
                self._recognizer.decode_stream(self._stream)
            text = self._recognizer.get_result(self._stream).strip() or self._last_text
            if text:
                log.info("ASR finish: «%s» (%.0f мс)", text[:60], self._utterance_ms)
                self._emit({"type": "final", "text": text,
                            "utterance_ms": self._utterance_ms + config.asr_tail_silence() * 1000})
            self._new_stream_locked()
            if self._wake_mode:
                self._listening = False
        except Exception as e:  # noqa: BLE001
            log.warning("ASR finish: %s", e)
        finally:
            self._finish_req.clear()

    def _new_stream_locked(self) -> None:
        """Как _new_stream, но вызывается уже под self._lock."""
        assert self._recognizer is not None
        self._stream = self._recognizer.create_stream()
        lead = int(config.asr_lead_silence() * SAMPLE_RATE)
        self._stream.accept_waveform(SAMPLE_RATE, np.zeros(lead, dtype=np.float32))
        self._last_text = ""
        self._utterance_ms = 0.0
        self._t_speech_start = None
        self._silence_since = None

    def _worker(self) -> None:
        try:
            self._recognizer = self._build()
        except Exception as e:  # noqa: BLE001
            log.error("ASR init: %s", e)
            return
        with self._lock:
            self._new_stream_locked()
        log.info("ASR-воркер готов (endpoint=%s)", self._endpoint_mode)
        while self._running:
            try:
                chunk = self._queue.get(timeout=0.1)
            except queue.Empty:
                # даже без аудио надо исполнять отложенный finish
                if self._finish_req.is_set():
                    with self._lock:
                        self._finish_locked()
                continue
            try:
                samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                now = time.monotonic()

                # --- wake word: пока не услышали — распознаватель не трогаем
                if self._wake_mode and not self._listening:
                    if self._check_wake(samples):
                        with self._lock:
                            self._listening = True
                            self._new_stream_locked()
                        self._emit({"type": "wake"})
                    continue

                rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0

                with self._lock:
                    if self._finish_req.is_set():
                        self._finish_locked()
                        continue

                    # --- VAD: переходы речь/тишина для smart turn
                    if rms >= config.vad_rms():
                        self._last_speech = now
                        if not self._speech_active:
                            self._speech_active = True
                            if self._t_speech_start is None:
                                self._t_speech_start = now
                            log.debug("VAD: речь (rms=%.3f)", rms)
                            self._emit({"type": "speech_start"})
                    else:
                        if (self._speech_active
                                and (now - self._last_speech) * 1000 >= config.vad_hangover_ms()):
                            self._speech_active = False
                            self._silence_since = now
                            log.debug("VAD: тишина (%.0f мс после речи)",
                                     (now - self._last_speech) * 1000)
                            self._emit({"type": "speech_end"})

                    self._stream.accept_waveform(SAMPLE_RATE, samples)
                    self._utterance_ms += len(samples) / SAMPLE_RATE * 1000.0

                    while self._recognizer.is_ready(self._stream):
                        self._recognizer.decode_stream(self._stream)

                    text = self._recognizer.get_result(self._stream).strip()
                    if text and text != self._last_text:
                        self._last_text = text
                        self._emit({"type": "partial", "text": text,
                                    "utterance_ms": self._utterance_ms})

                    if self._endpoint_mode == "sherpa":
                        if self._recognizer.is_endpoint(self._stream):
                            if self._last_text:
                                self._emit({"type": "final", "text": self._last_text,
                                            "utterance_ms": self._utterance_ms})
                            self._recognizer.reset(self._stream)
                            self._last_text = ""
                            self._utterance_ms = 0.0
                    elif self._utterance_ms >= config.asr_max_utterance() * 1000:
                        # жёсткий потолок утилизации — умный детектор обычно
                        # закрывает фразу раньше, это страховка
                        self._finish_locked()
            except Exception as e:  # noqa: BLE001
                log.warning("ASR worker: %s", e)

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._queue.put(b"")
            self._thread.join(timeout=3)
            self._thread = None
        log.info("ASR-воркер остановлен")


class WavASR:
    """Утилита для тестов: распознать контрольный wav целиком."""

    def __init__(self, model_dir: str | None = None):
        self.model_dir = Path(model_dir or config.asr_model_dir())

    def transcribe(self, wav_path: str) -> str:
        import wave

        import sherpa_onnx as so

        rec = so.OnlineRecognizer.from_t_one_ctc(
            tokens=str(self.model_dir / "tokens.txt"),
            model=str(self.model_dir / "model.onnx"),
            num_threads=config.asr_num_threads(),
            sample_rate=SAMPLE_RATE,
            enable_endpoint_detection=False,
            decoding_method="greedy_search",
            provider="cpu",
        )
        with wave.open(wav_path, "rb") as w:
            assert w.getframerate() == SAMPLE_RATE, f"нужно {SAMPLE_RATE} Гц"
            raw = w.readframes(w.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        s = rec.create_stream()
        s.accept_waveform(SAMPLE_RATE, np.zeros(int(0.75 * SAMPLE_RATE), dtype=np.float32))
        s.accept_waveform(SAMPLE_RATE, samples)
        s.accept_waveform(SAMPLE_RATE, np.zeros(int(1.0 * SAMPLE_RATE), dtype=np.float32))
        s.input_finished()
        while rec.is_ready(s):
            rec.decode_stream(s)
        return rec.get_result(s).strip()
