#!/usr/bin/env python3
"""Проверка умного конца реплики и перебивания через живой сервис.

Запуск (на машине с сервисом): .venv/bin/python tests/test_turn_and_barge.py
Требует: cmp-voice на 127.0.0.1:8110, cmp-silero-tts на 8023.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request

import websockets

TTS = "http://127.0.0.1:8023/v1/audio/speech"
WS = "ws://127.0.0.1:8110/ws"


def synth_16k(text: str) -> bytes:
    body = json.dumps({"input": text, "voice": "xenia",
                       "response_format": "pcm"}).encode()
    req = urllib.request.Request(TTS, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        pcm48 = r.read()
    # 48k -> 16k: каждый третий сэмпл (s16le моно)
    raw16 = bytearray()
    for i in range(0, len(pcm48) - 5, 6):
        raw16 += pcm48[i:i + 2]
    return bytes(raw16)


async def measure_final(text: str, label: str) -> tuple[float | None, str]:
    pcm = synth_16k(text)
    async with websockets.connect(WS, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "start"}))
        await asyncio.sleep(0.5)

        state = {"t_end": 0.0, "dt": None, "text": ""}
        done = asyncio.Event()

        async def reader() -> None:
            while not done.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                except Exception:  # noqa: BLE001
                    return
                if isinstance(raw, (bytes, bytearray)):
                    continue
                m = json.loads(raw)
                if m.get("type") == "final":
                    if state["dt"] is None:
                        state["dt"] = time.monotonic() - state["t_end"]
                        state["text"] = m.get("text", "")
                    done.set()

        reader_task = asyncio.create_task(reader())

        chunk = 3200  # 100 мс 16 кГц s16le
        for i in range(0, len(pcm), chunk):
            await ws.send(pcm[i:i + chunk])
            state["t_end"] = time.monotonic()
            await asyncio.sleep(0.1)

        # тишина, пока не придёт final (максимум 5 с)
        silence = b"\x00" * chunk
        deadline = time.monotonic() + 5.0
        while not done.is_set() and time.monotonic() < deadline:
            await ws.send(silence)
            await asyncio.sleep(0.1)

        done.set()
        reader_task.cancel()
        if state["dt"] is None:
            print(f"{label}: final не пришёл за 5 с")
            return None, ""
        print(f"{label}: final через {state['dt']*1000:.0f} мс "
              f"после конца аудио: «{state['text'][:60]}»")
        return state["dt"], state["text"]


async def measure_barge() -> bool:
    async with websockets.connect(WS, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "text", "text": "Расскажи длинную историю про космос"}))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                break
            if isinstance(raw, (bytes, bytearray)):
                await ws.send(json.dumps({"type": "barge_in"}))
                continue
            m = json.loads(raw)
            if m.get("type") == "interrupted":
                print("перебивание: сервер подтвердил interrupted")
                return True
        print("перебивание: НЕ подтверждено")
        return False


async def main() -> int:
    ok = True
    d1, _ = await measure_final("Сколько стоит биткоин сегодня.", "законченная фраза")
    d2, _ = await measure_final("я хотел купить хлеб и", "оборванная фраза")
    if d1 is None or d2 is None:
        ok = False
    else:
        if d1 > 1.5:
            print(f"FAIL: законченная фраза закрылась слишком поздно ({d1:.2f} c)")
            ok = False
        if d2 < 1.0:
            print(f"FAIL: оборванная фраза закрылась слишком рано ({d2:.2f} c)")
            ok = False
        if d2 < d1:
            print("FAIL: адаптивность не сработала (обрыв закрылся раньше точки)")
            ok = False
    ok = await measure_barge() and ok
    print("ИТОГ:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
