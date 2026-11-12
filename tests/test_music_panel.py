#!/usr/bin/env python3
"""Проверка панели «Музыка» через живой сервис.

Сценарий:
1. music_state — пустой или с историей, но структура верная.
2. «включи музыку …» — в истории появляется трек, playing = True.
3. music_control pause → paused = True; stop → playing = False.
4. music_like по id → liked = True.

Запуск: .venv/bin/python tests/test_music_panel.py
"""
from __future__ import annotations

import asyncio
import json

import websockets

WS = "ws://127.0.0.1:8110/ws"
FAILS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global FAILS
    if not cond:
        FAILS += 1
    print(("OK   " if cond else "FAIL ") + name + (f" — {extra}" if extra and not cond else ""))


async def recv_state(ws, timeout: float = 30) -> dict:
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if isinstance(raw, (bytes, bytearray)):
            continue
        m = json.loads(raw)
        if m.get("type") == "music_state":
            return m


async def main() -> int:
    async with websockets.connect(WS, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "music_state"}))
        st = await recv_state(ws)
        check("music_state приходит", isinstance(st.get("history"), list), str(st)[:200])

        await ws.send(json.dumps({"type": "text", "text": "включи музыку Rammstein Sonne"}))
        await recv_state(ws)          # первый снимок может быть до старта
        st = await recv_state(ws, timeout=90)
        check("в истории есть трек", len(st["history"]) >= 1, str(st["history"])[:200])
        check("играет", st.get("playing") is True, str(st)[:200])

        await ws.send(json.dumps({"type": "music_control", "action": "pause"}))
        st = await recv_state(ws)
        check("пауза через панель", st.get("paused") is True, st.get("msg"))

        await ws.send(json.dumps({"type": "music_control", "action": "stop"}))
        st = await recv_state(ws)
        check("стоп через панель", st.get("playing") is False, st.get("msg"))

        item = st["history"][0]
        await ws.send(json.dumps({"type": "music_like", "id": item["id"]}))
        st = await recv_state(ws)
        check("лайк через панель", st["history"][0].get("liked") is True,
              str(st["history"][0]))

    print("ИТОГ:", "OK" if FAILS == 0 else f"{FAILS} FAIL")
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
