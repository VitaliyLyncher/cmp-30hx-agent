#!/usr/bin/env python3
"""Проверка режима wake word (asr) через живой сервис.

Сценарий:
1. Включаем wake_mode, произносим фразу БЕЗ ключевого слова — сервис молчит.
2. Произносим «Джарвис, сколько стоит биткоин сегодня» — ждём событие wake,
   затем ответ без слова «джарвис» в тексте.

Запуск: .venv/bin/python tests/test_wake.py
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request

import websockets

TTS = "http://127.0.0.1:8023/v1/audio/speech"
WS = "ws://127.0.0.1:8110/ws"
WAKE = "джарвис"


def synth_16k(text: str) -> bytes:
    body = json.dumps({"input": text, "voice": "xenia",
                       "response_format": "pcm"}).encode()
    req = urllib.request.Request(TTS, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        pcm48 = r.read()
    raw16 = bytearray()
    for i in range(0, len(pcm48) - 5, 6):
        raw16 += pcm48[i:i + 2]
    return bytes(raw16)


async def speak(ws, pcm: bytes, silence_s: float = 2.5) -> None:
    chunk = 3200
    for i in range(0, len(pcm), chunk):
        await ws.send(pcm[i:i + chunk])
        await asyncio.sleep(0.1)
    for _ in range(int(silence_s * 10)):
        await ws.send(b"\x00" * chunk)
        await asyncio.sleep(0.1)


async def collect(ws, timeout: float) -> list[dict]:
    events: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
        except asyncio.TimeoutError:
            continue
        if isinstance(raw, (bytes, bytearray)):
            continue
        m = json.loads(raw)
        events.append(m)
        if m.get("type") == "ready":
            break
    return events


async def main() -> int:
    ok = True
    async with websockets.connect(WS, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "start"}))
        await asyncio.sleep(0.3)
        await ws.send(json.dumps({"type": "wake_mode", "enabled": True}))
        ack = await collect(ws, 3)
        enabled = [e for e in ack if e.get("type") == "wake_mode_set"]
        print("wake_mode:", enabled[0] if enabled else "нет ответа")
        if not enabled or not enabled[0].get("enabled"):
            print("FAIL: режим wake word не включился")
            return 1

        # 1) без ключевого слова — не должно быть хода
        await speak(ws, synth_16k("сколько стоит биткоин сегодня."))
        ev1 = await collect(ws, 4)
        turns1 = [e for e in ev1 if e.get("type") in ("turn_start", "final")]
        print("без ключевого слова:", [e.get("type") for e in ev1][:12])
        if turns1:
            print("FAIL: без wake-слова сервис начал ход")
            ok = False

        # 2) с ключевым словом
        await speak(ws, synth_16k("Джарвис, сколько стоит биткоин сегодня."))
        ev2 = await collect(ws, 40)
        wakes = [e for e in ev2 if e.get("type") == "wake"]
        finals = [e for e in ev2 if e.get("type") == "final"]
        turns = [e for e in ev2 if e.get("type") == "turn_start"]
        print("wake:", len(wakes), "| final:", [f.get("text", "")[:70] for f in finals],
              "| turn:", len(turns))
        if not wakes:
            print("FAIL: ключевое слово не распознано")
            ok = False
        if finals and WAKE in finals[0].get("text", "").lower():
            print("FAIL: ключевое слово не вырезано из вопроса")
            ok = False
        if not turns:
            print("FAIL: ход не начался после ключевого слова")
            ok = False
        try:
            await ws.send(json.dumps({"type": "wake_mode", "enabled": False}))
        except Exception:  # noqa: BLE001
            pass
    print("ИТОГ:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
