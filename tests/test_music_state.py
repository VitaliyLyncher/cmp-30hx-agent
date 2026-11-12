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
    check("попал в избранное", len(liked.get("favorites") or []) == 1,
          str(liked.get("favorites")))

    # перезагрузка из файла
    tools._hist_loaded = False
    tools._history.clear()
    tools._favorites.clear()
    snap2 = tools.music_snapshot()
    check("лайк сохранился на диск", snap2["history"][0]["liked"] is True,
          str(snap2["history"]))
    check("история сохранилась", len(snap2["history"]) == 2)
    check("избранное сохранилось", len(snap2.get("favorites") or []) == 1,
          str(snap2.get("favorites")))

    # удаление из истории: избранное остаётся
    fav_id = snap2["favorites"][0]["id"]
    hist_id = snap2["history"][0]["id"]
    after_del = tools.music_forget(hist_id, "history")
    check("история удалена", len(after_del["history"]) == 1, str(after_del["history"]))
    check("избранное не тронуто", len(after_del.get("favorites") or []) == 1,
          str(after_del.get("favorites")))

    # удаление из избранного
    after_unfav = tools.music_forget(fav_id, "favorites")
    check("избранное удалено", len(after_unfav.get("favorites") or []) == 0,
          str(after_unfav.get("favorites")))

    # повторный лайк и удаление "всюду"
    item2 = after_unfav["history"][0]
    tools.music_like(item2["id"])
    both = tools.music_forget(item2["title"], "all")
    check("удаление всюду", len(both["history"]) == 0 and len(both.get("favorites") or []) == 0,
          str(both)[:200])

    # снятие лайка по имени
    tools._record("Тестовая мелодия", "local", "/tmp/x.wav", "мелодия")
    tools.music_like("Тестовая мелодия")
    unliked = tools.music_like("Тестовая мелодия")
    check("лайк снят", unliked["history"][0]["liked"] is False,
          str(unliked["history"]))

    # неизвестный трек
    bad = tools.music_like("нет такого")
    check("неизвестный трек не ломает", "Не нашёл" in (bad.get("msg") or ""))

    # пустое избранное
    empty = tools.music_play_favorites()
    check("пустое избранное не падает", "пусто" in (empty.get("msg") or "").lower(),
          empty.get("msg"))

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
