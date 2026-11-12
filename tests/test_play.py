"""Живая проверка музыкального плеера (mpv) на этом ПК.

Запуск: .venv/bin/python tests/test_play.py
Требует mpv, yt-dlp, ffmpeg и сеть до YouTube + папку музыки (MUSIC_DIR).
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


def _alive() -> bool:
    p = tools._player.get("proc")
    return p is not None and p.poll() is None


def main() -> int:
    # 1) YouTube
    print("play:", tools.play_music("Rammstein Sonne"))
    time.sleep(5)
    check("mpv жив (звук идёт)", _alive(), "плеер завершился")
    print("status:", tools.music_status())
    print("pause:", tools.pause_music())
    time.sleep(0.5)
    check("пауза выставлена", tools._ipc_get("pause") is True)
    print("resume:", tools.resume_music())
    time.sleep(0.5)
    check("пауза снята", tools._ipc_get("pause") is False)
    print("vol:", tools.set_volume("40"))
    time.sleep(0.3)
    vol = tools._ipc_get("volume")
    check("громкость 40", isinstance(vol, (int, float)) and abs(vol - 40) < 1, str(vol))
    print("vol:", tools.set_volume("+10"))
    time.sleep(0.3)
    check("громкость +10", abs((tools._ipc_get("volume") or 0) - 50) < 1)
    print("next:", tools.next_track())
    time.sleep(0.5)
    check("после next плеер жив", _alive())
    print("stop:", tools.stop_music())
    time.sleep(0.5)
    check("после stop плеер мёртв", not _alive())

    # 2) локальная папка
    print("local:", tools.play_local("мелодия"))
    time.sleep(3)
    check("локальный трек играет", _alive(), "mpv не запустился на локальном файле")
    if _alive():
        title = tools._current_title()
        print("local title:", title)
        check("это файл из папки «Музыка»", "мелоди" in title.casefold(), title)
        print("next:", tools.next_track())
        time.sleep(0.5)
        print("stop:", tools.stop_music())

    print()
    print("ИТОГ:", "OK" if FAILS == 0 else f"{FAILS} FAIL")
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
