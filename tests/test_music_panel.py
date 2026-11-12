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


async def wait_state(ws, pred, deadline_s: float = 90) -> dict:
    """Периодически просим music_state, пока не выполнится условие."""
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline_s
    last = {}
    while loop.time() < end:
        await ws.send(json.dumps({"type": "music_state"}))
        last = await recv_state(ws)
        if pred(last):
            return last
        await asyncio.sleep(1.0)
    return last


async def main() -> int:
    async with websockets.connect(WS, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "music_state"}))
        st = await recv_state(ws)
        check("music_state приходит", isinstance(st.get("history"), list), str(st)[:200])

        await ws.send(json.dumps({"type": "text", "text": "включи музыку Rammstein Sonne"}))
        st = await wait_state(ws, lambda s: s.get("playing") is True, 120)
        check("в истории есть трек", len(st.get("history") or []) >= 1,
              str(st.get("history"))[:200])
        check("играет", st.get("playing") is True, str(st)[:200])

        await ws.send(json.dumps({"type": "music_control", "action": "pause"}))
        st = await recv_state(ws)
        check("пауза через панель", st.get("paused") is True, st.get("msg"))

        await ws.send(json.dumps({"type": "music_control", "action": "stop"}))
        st = await recv_state(ws)
        check("стоп через панель", st.get("playing") is False, st.get("msg"))

        item = (st.get("history") or [{}])[0]
        check("в истории есть id", bool(item.get("id")), str(item))
        if item.get("id"):
            await ws.send(json.dumps({"type": "music_like", "id": item["id"]}))
            st = await recv_state(ws)
            check("лайк через панель", st["history"][0].get("liked") is True,
                  str(st["history"][0]))

    print("ИТОГ:", "OK" if FAILS == 0 else f"{FAILS} FAIL")
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
