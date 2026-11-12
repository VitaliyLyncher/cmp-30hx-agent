"""Живая проверка воспроизведения музыки на этом ПК (yt-dlp + ffplay).

Запуск: .venv/bin/python tests/test_play.py
Требует ffmpeg/ffplay и сеть до YouTube.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice import tools  # noqa: E402

FAILS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global FAILS
    if not cond:
        FAILS += 1
    print(("OK   " if cond else "FAIL ") + name + (f" — {extra}" if extra and not cond else ""))


def main() -> int:
    print("play:", tools.play_music("Rammstein Sonne"))
    time.sleep(6)
    with tools._player_lock:
        p = tools._player.get("proc")
    alive = p is not None and p.poll() is None
    check("ffplay жив через 6 с (звук идёт)", alive,
          "процесс завершился — вероятно, не смог открыть аудио")
    print("now:", tools.music_now_playing())
    print("stop:", tools.stop_music())
    time.sleep(1)
    check("после stop_music процесс завершён", p is None or p.poll() is not None)
    check("повторный stop не падает", "не играет" in tools.stop_music().lower())

    print()
    print("ИТОГ:", "OK" if FAILS == 0 else f"{FAILS} FAIL")
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
