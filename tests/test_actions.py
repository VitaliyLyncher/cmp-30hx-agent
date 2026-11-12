"""Юнит-тесты действий (музыка/видео/сайты) без сети и модели.

Запуск: python3 tests/test_actions.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# тесты не зависят от локального .env: инструменты должны быть включены
os.environ.setdefault("VOICE_TOOLS_ENABLED", "1")

from voice import tools  # noqa: E402

FAILS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global FAILS
    if not cond:
        FAILS += 1
    print(("OK   " if cond else "FAIL ") + name + (f" — {extra}" if extra and not cond else ""))


def main() -> int:
    # музыка
    r = tools.music_search("Rammstein Sonne")
    action, human = tools.split_action(r)
    check("music_search вернул действие", action is not None, r[:120])
    check("service по умолчанию youtube_music",
          bool(action) and action.get("service") == "youtube_music", str(action))
    check("url ведёт на поиск YouTube Music",
          bool(action) and action["url"].startswith("https://music.youtube.com/search?q="),
          str(action))
    check("в запросе нет пробелов",
          bool(action) and " " not in action["url"], str(action))
    check("человеческий текст без маркера", not human.startswith("__ACTION__"))

    # другой сервис
    action2, _ = tools.split_action(tools.music_search("jazz", "spotify"))
    check("spotify url", bool(action2) and action2["url"].startswith("https://open.spotify.com/"),
          str(action2))

    # видео
    action3, _ = tools.split_action(tools.video_search("обзор RTX 5090"))
    check("video url youtube", bool(action3) and
          action3["url"].startswith("https://www.youtube.com/results?search_query="),
          str(action3))

    # открытие сайта
    action4, _ = tools.split_action(tools.open_site("https://music.youtube.com"))
    check("open_site разрешает домен", bool(action4), str(action4))
    bad = tools.open_site("https://evil.example.com")
    action5, text5 = tools.split_action(bad)
    check("open_site блокирует чужой домен", action5 is None and "разрешённых" in text5, text5)

    # голый домен -> https
    action6, _ = tools.split_action(tools.open_site("youtube.com"))
    check("open_site добавляет схему", bool(action6) and
          action6["url"].startswith("https://youtube.com"), str(action6))

    # пустые запросы
    check("пустая музыка", "Не понял" in tools.music_search(""))
    check("пустое видео", "Не понял" in tools.video_search(""))

    # allowlist по хосту, а не по подстроке
    check("поддомен разрешён", tools.action_url_allowed("https://www.youtube.com/x"))
    check("похожий домен запрещён", not tools.action_url_allowed("https://youtube.com.evil.io"))

    # split_action на обычном тексте
    a, t = tools.split_action("просто текст")
    check("обычный текст не трогаем", a is None and t == "просто текст")

    # детерминированный роутер простых команд
    r = tools.route_action("включи музыку Rammstein Sonne")
    check("роутер: музыка с исполнителем", r == ("play_music", {"query": "Rammstein Sonne"}), str(r))
    r = tools.route_action("включи музыку")
    check("роутер: музыка без запроса", r is not None and r[0] == "play_music", str(r))
    r = tools.route_action("выключи музыку")
    check("роутер: стоп", r == ("stop_music", {}), str(r))
    r = tools.route_action("найди видео про сборку ПК")
    check("роутер: видео", r == ("video_search", {"query": "сборку ПК"}), str(r))
    r = tools.route_action("открой сайт youtube.com")
    check("роутер: сайт", r == ("open_site", {"target": "youtube.com"}), str(r))
    check("роутер: обычный вопрос не трогаем",
          tools.route_action("сколько стоит биткоин") is None)
    check("роутер: пустая строка", tools.route_action("") is None)

    print()
    print("ИТОГ:", "OK" if FAILS == 0 else f"{FAILS} FAIL")
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
