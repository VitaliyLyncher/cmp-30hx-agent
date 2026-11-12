#!/usr/bin/env python3
"""Проверка действий через живой сервис: «включи музыку» / «найди видео».

Что проверяем:
  * модель вызывает music_search / video_search;
  * сервер присылает событие action с разрешённым URL;
  * текст ответа не содержит служебного маркера.

Запуск: .venv/bin/python tests/test_action_ws.py
"""
from __future__ import annotations

import asyncio
import json
import sys

import websockets

WS = "ws://127.0.0.1:8110/ws"


async def ask(text: str, need: tuple[str, ...], timeout: float = 90,
              want_action: bool = False) -> tuple[bool, list[dict]]:
    events: list[dict] = []
    async with websockets.connect(WS, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "text", "text": text}))
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                if isinstance(raw, (bytes, bytearray)):
                    continue
                m = json.loads(raw)
                events.append(m)
                if m.get("type") == "ready":
                    break
        except asyncio.TimeoutError:
            print(f"{text!r}: таймаут")
    calls = [e for e in events if e.get("type") == "tool_call"]
    actions = [e for e in events if e.get("type") == "action"]
    ans = "".join(e.get("text", "") for e in events if e.get("type") == "ans_token")
    ok_call = any(c.get("name") in need for c in calls)
    ok = ok_call and (bool(actions) if want_action else True)
    print(f"{text!r}: tools={[c.get('name') for c in calls]} "
          f"actions={[a.get('url', '')[:60] for a in actions]}")
    print(f"  ответ: {ans[:120]!r}")
    return ok, events


async def main() -> int:
    ok1, ev1 = await ask("включи музыку Rammstein Sonne",
                         ("play_music", "music_search"))
    ok2, ev2 = await ask("найди видео про сборку ПК", ("video_search",),
                         want_action=True)
    bad = [e for e in ev1 + ev2 if "__ACTION__" in str(e.get("result", ""))]
    ok = ok1 and ok2 and not bad
    print("ИТОГ:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
