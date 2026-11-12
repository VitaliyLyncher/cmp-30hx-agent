"""Живая проверка скачивания трека (SoundCloud/archive.org -> mp3).

Запуск: .venv/bin/python tests/test_download.py
Качает один короткий трек во временную папку, проверяет mp3 и запись в истории.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("VOICE_TOOLS_ENABLED", "1")

tmpdir = Path(tempfile.mkdtemp(prefix="cmp-dl-"))
os.environ["MUSIC_DIR"] = str(tmpdir)
os.environ["MUSIC_STATE_FILE"] = str(tmpdir / "music.json")

from voice import tools  # noqa: E402

FAILS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global FAILS
    if not cond:
        FAILS += 1
    print(("OK   " if cond else "FAIL ") + name + (f" — {extra}" if extra and not cond else ""))


def main() -> int:
    res = tools.download_track("Kevin MacLeod Sneaky Snitch")
    print("download:", res)
    mp3s = sorted(tmpdir.glob("*.mp3"))
    check("mp3 появился в папке", bool(mp3s), str(list(tmpdir.iterdir())))
    if mp3s:
        check("файл не пустой", mp3s[0].stat().st_size > 100_000,
              f"{mp3s[0].stat().st_size} байт")
        snap = tools.music_snapshot()
        titles = [h.get("title") for h in snap["history"]]
        check("трек записан в историю панели", bool(titles), str(titles))
        check("источник — local", snap["history"][0].get("source") == "local",
              str(snap["history"][0]))
    print("ИТОГ:", "OK" if FAILS == 0 else f"{FAILS} FAIL")
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
