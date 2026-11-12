"""Юнит-тесты истории/лайков панели «Музыка» (без mpv и сети).

Запуск: python3 tests/test_music_state.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("VOICE_TOOLS_ENABLED", "1")

from voice import tools  # noqa: E402

FAILS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global FAILS
    if not cond:
        FAILS += 1
    print(("OK   " if cond else "FAIL ") + name + (f" — {extra}" if extra and not cond else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "music.json"
    os.environ["MUSIC_STATE_FILE"] = str(tmp)
    tools._hist_loaded = False
    tools._history.clear()
    tools._favorites.clear()

    tools._record("Rammstein Sonne (1)", "youtube", "https://example/1", "Rammstein Sonne")
    tools._record("Тестовая мелодия", "local", "/tmp/x.wav", "мелодия")

    snap = tools.music_snapshot()
    check("снимок содержит историю", len(snap["history"]) == 2, str(snap["history"]))
    check("свежий трек первым",
          snap["history"][0]["title"] == "Тестовая мелодия", str(snap["history"]))
    check("по умолчанию не лайкнут", snap["history"][0]["liked"] is False)
    check("история не играет", snap["playing"] is False)

    item = snap["history"][0]
    liked = tools.music_like(item["id"])
    check("лайк поставлен", liked["history"][0]["liked"] is True, str(liked["history"][0]))
    check("есть сообщение", "избранное" in (liked.get("msg") or ""), liked.get("msg"))

    # перезагрузка из файла
    tools._hist_loaded = False
    tools._history.clear()
    tools._favorites.clear()
    snap2 = tools.music_snapshot()
    check("лайк сохранился на диск", snap2["history"][0]["liked"] is True,
          str(snap2["history"]))
    check("история сохранилась", len(snap2["history"]) == 2)

    # снятие лайка по имени
    unliked = tools.music_like("Тестовая мелодия")
    check("лайк снят", unliked["history"][0]["liked"] is False)

    # неизвестный трек
    bad = tools.music_like("нет такого")
    check("неизвестный трек не ломает", "Не нашёл" in (bad.get("msg") or ""))

    # управление без плеера не падает
    ctl = tools.music_control("status")
    check("status без плеера", "ничего не играет" in (ctl.get("msg") or "").lower(),
          ctl.get("msg"))
    ctl2 = tools.music_control("unknown-action")
    check("неизвестное действие", "Неизвестное" in (ctl2.get("msg") or ""))

    print()
    print("ИТОГ:", "OK" if FAILS == 0 else f"{FAILS} FAIL")
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
