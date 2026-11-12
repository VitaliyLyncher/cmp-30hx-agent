"""Определение конца реплики («smart turn detection»).

В отличие от фиксированной паузы (sherpa endpointing 1.2–2.4 с), здесь пауза
зависит от того, выглядит ли фраза законченной:

* «Где ближайшая заправка?»          — закончено, ждём меньше;
* «Я хотел бы узнать, сколько стоит» — оборвано на союзе/предлоге, ждём дольше;
* «Просто короткая фраза»            — ждём средне;
* совсем короткий огрызок («да», «ну») — не спешим закрывать.

Модуль чистый (без зависимостей): его легко тестировать и подменять.
Решение принимает Session: раз в 100 мс спрашивает should_finalize(now).
"""
from __future__ import annotations

import re
import time

# слова, после которых фраза почти наверняка не закончена
_DANGLING = {
    "и", "а", "но", "или", "что", "чтобы", "если", "когда", "потому", "поэтому",
    "в", "во", "на", "с", "со", "по", "для", "про", "из", "к", "ко", "у", "о",
    "об", "за", "под", "над", "без", "до", "от", "при", "через", "между", "чем",
    "как", "так", "то", "же", "ли", "бы", "не", "ни", "ибо", "хотя", "пока",
}
# слова-вопросы: если фраза начинается с них, конец ждём чуть охотнее
_QUESTION_START = {
    "как", "какой", "какая", "какое", "какие", "что", "кто", "где", "когда",
    "почему", "зачем", "сколько", "куда", "откуда", "можно", "есть",
}
_TERMINAL = ".?!…"
# вопрос-слово в КОНЦЕ фразы означает, что вопрос ещё не задан
_DANGLING_END = {
    "сколько", "какой", "какая", "какое", "какие", "когда", "где", "кто",
    "что", "почему", "зачем", "куда", "откуда", "чем",
}


class TurnDetector:
    """Адаптивная пауза конца реплики.

    Параметры (мс) приходят из конфига; значения по умолчанию подобраны так,
    чтобы обычная законченная фраза закрывалась за ~0.5–0.7 с, а оборванная —
    за ~1.2–1.4 с.
    """

    def __init__(self, *, min_ms: float = 450, default_ms: float = 700,
                 question_ms: float = 600, dangling_ms: float = 1400,
                 short_ms: float = 900, max_ms: float = 2500,
                 min_words: int = 2):
        self.min_ms = min_ms
        self.default_ms = default_ms
        self.question_ms = question_ms
        self.dangling_ms = dangling_ms
        self.short_ms = short_ms
        self.max_ms = max_ms
        self.min_words = min_words

        self.text = ""
        self.last_change = 0.0
        self.speech_started_at = 0.0
        self.silence_since = 0.0
        self.fired = False

    # -- события ASR ------------------------------------------------------
    def on_speech_start(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if not self.speech_started_at:
            self.speech_started_at = now
        self.silence_since = 0.0
        self.fired = False

    def on_partial(self, text: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if text and text != self.text:
            self.text = text
            self.last_change = now
            if not self.speech_started_at:
                self.speech_started_at = now
            self.silence_since = 0.0
            self.fired = False

    def on_silence(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self.speech_started_at and not self.silence_since:
            self.silence_since = now

    def reset(self) -> None:
        self.text = ""
        self.last_change = 0.0
        self.speech_started_at = 0.0
        self.silence_since = 0.0
        self.fired = False

    # -- решение ----------------------------------------------------------
    def hold_ms(self, text: str | None = None) -> float:
        t = (self.text if text is None else text).strip()
        if not t:
            return self.max_ms
        low = t.lower()
        words = re.findall(r"[\w\-]+", low, flags=re.UNICODE)
        if not words:
            return self.default_ms
        last = words[-1]
        # законченная фраза
        if t[-1] in _TERMINAL:
            return self.min_ms
        # вопрос без знака вопроса («сколько стоит биткоин»)
        if words[0] in _QUESTION_START and len(words) >= 2:
            return self.question_ms
        # оборвано на союзе/предлоге/частице или на вопросе без объекта
        if last in _DANGLING or last in _DANGLING_END:
            return self.dangling_ms
        # одно слово — возможно, ещё не договорил
        if len(words) < self.min_words:
            return self.short_ms
        return self.default_ms

    def should_finalize(self, now: float | None = None) -> bool:
        """Пора закрывать реплику? Вызывать раз в 100–200 мс."""
        if self.fired or not self.speech_started_at:
            return False
        now = time.monotonic() if now is None else now
        # жёсткий потолок: дольше max_ms после последнего изменения не ждём
        if self.last_change and (now - self.last_change) * 1000 >= self.max_ms:
            self.fired = True
            return True
        # нет текста вообще (шум/дыхание) — закрываем только по максимуму
        if not self.text:
            if (now - self.speech_started_at) * 1000 >= self.max_ms:
                self.fired = True
                return True
            return False
        if not self.silence_since:
            return False
        silence_ms = (now - self.silence_since) * 1000
        since_change_ms = (now - self.last_change) * 1000
        hold = max(self.hold_ms(), silence_ms)  # для наглядности/отладки
        if silence_ms >= hold and since_change_ms >= hold:
            self.fired = True
            return True
        return False
