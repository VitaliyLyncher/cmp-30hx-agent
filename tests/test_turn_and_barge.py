#!/usr/bin/env python3
"""Проверка умного конца реплики и перебивания через живой сервис.

Запуск (на машине с сервисом): .venv/bin/python tests/test_turn_and_barge.py
Требует: cmp-voice на 127.0.0.1:8110, cmp-silero-tts на 8023.

Метрики:
* dt_total  — от последнего аудио-чанка до final (включает отставание ASR);
* dt_hold   — от события speech_end (VAD) до final: это и есть «умная пауза».
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
    raw16 = bytearray()
    for i in range(0, len(pcm48) - 5, 6):
        raw16 += pcm48[i:i + 2]
    return bytes(raw16)


async def measure_final(text: str, label: str) -> dict:
    pcm = synth_16k(text)
    res = {"label": label, "total": None, "hold": None, "text": ""}
    async with websockets.connect(WS, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "start"}))
        await asyncio.sleep(0.5)

        st = {"t_end": 0.0, "t_speech_end": None}
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
                t = m.get("type")
                if t == "speech_end" and st["t_speech_end"] is None:
                    st["t_speech_end"] = time.monotonic()
                elif t == "final" and res["total"] is None:
                    now = time.monotonic()
                    res["total"] = now - st["t_end"]
                    res["hold"] = (now - st["t_speech_end"]) if st["t_speech_end"] else None
                    res["text"] = m.get("text", "")
                    done.set()

        reader_task = asyncio.create_task(reader())

        chunk = 3200  # 100 мс 16 кГц s16le
        for i in range(0, len(pcm), chunk):
            await ws.send(pcm[i:i + chunk])
            st["t_end"] = time.monotonic()
            await asyncio.sleep(0.1)

        silence = b"\x00" * chunk
        deadline = time.monotonic() + 8.0
        while not done.is_set() and time.monotonic() < deadline:
            await ws.send(silence)
            await asyncio.sleep(0.1)

        done.set()
        reader_task.cancel()
    if res["total"] is None:
        print(f"{label}: final не пришёл за 8 с")
    else:
        hold = f"{res['hold']*1000:.0f} мс" if res["hold"] is not None else "?"
        print(f"{label}: пауза(VAD→final) {hold}, всего {res['total']*1000:.0f} мс, "
              f"текст «{res['text'][:60]}»")
    return res


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
            if json.loads(raw).get("type") == "interrupted":
                print("перебивание: сервер подтвердил interrupted")
                return True
        print("перебивание: НЕ подтверждено")
        return False


async def main() -> int:
    ok = True
    r1 = await measure_final("Сколько стоит биткоин сегодня.", "законченная фраза")
    r2 = await measure_final("я хотел купить хлеб и", "оборванная фраза")
    if r1["hold"] is None or r2["hold"] is None:
        ok = False
    else:
        if r2["hold"] <= r1["hold"] + 0.3:
            print(f"FAIL: нет адаптивности: законченная {r1['hold']*1000:.0f} мс, "
                  f"оборванная {r2['hold']*1000:.0f} мс")
            ok = False
        if r1["total"] is not None and r1["total"] > 4.0:
            print(f"FAIL: слишком долго до ответа ({r1['total']*1000:.0f} мс)")
            ok = False
    ok = await measure_barge() and ok
    print("ИТОГ:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
