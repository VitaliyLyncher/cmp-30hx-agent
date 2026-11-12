#!/usr/bin/env python3
"""Проверка команды «скачай трек …» через живой WebSocket-сервис.

Скачивает короткий CC-трек в реальную папку «Музыка», проверяет файл и запись
в панели, затем удаляет файл и запись из истории (чтобы не мусорить).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import websockets

WS = "ws://127.0.0.1:8110/ws"
MUSIC = Path("/mnt/c/Users/Administrator/Music")
FAILS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global FAILS
    if not cond:
        FAILS += 1
    print(("OK   " if cond else "FAIL ") + name + (f" — {extra}" if extra and not cond else ""))


async def main() -> int:
    before = {p.name for p in MUSIC.glob("*.mp3")} if MUSIC.is_dir() else set()
    async with websockets.connect(WS, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "text",
                                  "text": "скачай трек Kevin MacLeod Sneaky Snitch в музыку"}))
        st = None
        turns = []
        for _ in range(600):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
            except asyncio.TimeoutError:
                break
            if isinstance(raw, (bytes, bytearray)):
                continue
            m = json.loads(raw)
            turns.append(m.get("type"))
            if m.get("type") in ("tool_result", "error", "tts_error"):
                print("event:", m)
            if m.get("type") == "ans_token":
                print("ans:", m.get("text", "")[:100])
            if m.get("type") == "tool_call":
                check("модель/роутер вызвал download_track",
                      m.get("name") == "download_track", str(m))
            if m.get("type") == "ready":
                break
        await ws.send(json.dumps({"type": "music_state"}))
        for _ in range(40):
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            if isinstance(raw, (bytes, bytearray)):
                continue
            m = json.loads(raw)
            if m.get("type") == "music_state":
                st = m
                break
    after = {p.name for p in MUSIC.glob("*.mp3")}
    new = after - before
    check("новый mp3 в папке Музыка", bool(new), str(new))
    if st:
        hist = st.get("history") or []
        check("трек в истории панели",
              any(h.get("source") == "local" and h.get("title") in
                  {Path(n).stem for n in new} for h in hist),
              str(hist[:3]))
        # чистим историю от тестовой записи
        for h in hist:
            if h.get("source") == "local" and h.get("title") in {Path(n).stem for n in new}:
                await ws.send(json.dumps({"type": "music_delete", "id": h.get("id"),
                                          "scope": "history"}))
                await ws.recv()
    for n in new:
        try:
            (MUSIC / n).unlink()
        except OSError:
            pass
    print("ИТОГ:", "OK" if FAILS == 0 else f"{FAILS} FAIL")
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
