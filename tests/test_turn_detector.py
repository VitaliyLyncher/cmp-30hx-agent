"""Юнит-тесты smart turn detection (voice/turn.py).

Запуск: python3 tests/test_turn_detector.py  (без зависимостей и GPU)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice.turn import TurnDetector  # noqa: E402

FAILS = 0


def check(name: str, got, want) -> None:
    global FAILS
    ok = got == want
    if not ok:
        FAILS += 1
    print(("OK   " if ok else "FAIL ") + name + f" (got {got!r}, want {want!r})")


def main() -> int:
    d = TurnDetector()
    # законченные фразы — короткая пауза
    check("точка", d.hold_ms("Сколько это стоит."), d.min_ms)
    check("вопрос-знак", d.hold_ms("Где заправка?"), d.min_ms)
    # вопрос без знака — чуть короче обычного
    check("вопрос без знака", d.hold_ms("где ближайшая заправка"), d.question_ms)
    # оборвано на союзе/предлоге — ждём дольше
    check("обрыв на «и»", d.hold_ms("Я хотел купить хлеб и"), d.dangling_ms)
    check("обрыв на «сколько»", d.hold_ms("подскажи сколько"), d.dangling_ms)
    check("обрыв на предлоге", d.hold_ms("поехали в"), d.dangling_ms)
    # одно слово — средняя пауза
    check("одно слово", d.hold_ms("да"), d.short_ms)
    # обычная фраза
    check("обычная", d.hold_ms("включи музыку"), d.default_ms)

    # временная логика: фраза закончена, тишина 0.5 с -> закрываем
    d.reset()
    t0 = 1000.0
    d.on_speech_start(t0)
    d.on_partial("Сколько стоит биткоин.", t0 + 0.2)
    d.on_silence(t0 + 0.3)
    check("рано (0.4 c)", d.should_finalize(t0 + 0.7), False)
    d.on_silence(t0 + 0.3)
    check("пора (0.5 c)",
          d.should_finalize(t0 + 0.3 + d.hold_ms() / 1000 + 0.01), True)
    check("повторно не срабатывает", d.should_finalize(t0 + 10), False)

    # оборванная фраза не закрывается на короткой паузе, но закрывается по max
    d.reset()
    d.on_speech_start(t0)
    d.on_partial("поехали в", t0 + 0.2)
    d.on_silence(t0 + 0.3)
    check("обрыв: 0.7 c мало", d.should_finalize(t0 + 1.0), False)
    check("обрыв: max_ms закрывает", d.should_finalize(t0 + 0.2 + d.max_ms / 1000 + 0.01), True)

    # без речи вообще — ничего не закрываем
    d.reset()
    check("нет речи", d.should_finalize(t0 + 5), False)

    print()
    print("ИТОГ:", "OK" if FAILS == 0 else f"{FAILS} FAIL")
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
