"""Регрессии по итогам код-ревью (voice-слой): история tool-раундов и EOG.

Запуск:  .venv/bin/python tests/test_tool_history.py
Тест не требует GPU, демона и сети: LLM и инструменты подменяются заглушками.
Если в окружении нет fastapi/uvicorn/numpy — тест честно пропускается.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _install_stubs() -> None:
    """Минимальные заглушки тяжёлых зависимостей.

    Тест проверяет логику Turn.run и не нуждается ни в HTTP-сервере, ни в
    реальном numpy. Заглушки ставятся ТОЛЬКО если настоящих модулей нет, чтобы
    на целевой машине тест работал с реальными fastapi/uvicorn/numpy.
    """
    try:
        import fastapi  # noqa: F401
    except Exception:  # noqa: BLE001
        fastapi = types.ModuleType("fastapi")

        class _App:
            def __init__(self, *a, **k) -> None:
                pass

            def _deco(self, *a, **k):
                return lambda fn: fn

            get = post = websocket = on_event = _deco

        class WebSocketDisconnect(Exception):
            pass

        fastapi.FastAPI = _App
        fastapi.Request = object
        fastapi.WebSocket = object
        fastapi.WebSocketDisconnect = WebSocketDisconnect
        sys.modules["fastapi"] = fastapi

        responses = types.ModuleType("fastapi.responses")
        responses.HTMLResponse = object
        responses.JSONResponse = object
        responses.Response = object
        sys.modules["fastapi.responses"] = responses

        uvicorn = types.ModuleType("uvicorn")
        uvicorn.run = lambda *a, **k: None
        sys.modules["uvicorn"] = uvicorn

    try:
        import numpy  # noqa: F401
    except Exception:  # noqa: BLE001
        sys.modules["numpy"] = types.ModuleType("numpy")


_install_stubs()

try:
    from voice import app  # noqa: F401
except Exception as e:  # noqa: BLE001
    print(f"SKIP: не удалось импортировать voice.app ({e.__class__.__name__}: {e})")
    raise SystemExit(0)

TOOL_RAW = (
    "Ищу в интернете.\n<tool_call>\n<function=web_search>\n"
    "<parameter=query>\nвладелец FromSoftware\n</parameter>\n</function>\n</tool_call>"
)
ANSWER = "FromSoftware принадлежит Kadokawa Corporation."


class FakeSession:
    def __init__(self) -> None:
        self.history: list[dict] = []
        self.events: list[dict] = []
        self.audio: list[str] = []
        self.answer_text = ""
        self.source = "text"

    async def send(self, payload: dict) -> None:
        self.events.append(payload)

    def enqueue_tts(self, text: str) -> None:
        self.audio.append(text)

    def push_answer_text(self, piece: str) -> None:
        self.answer_text += piece


class FakeLLM:
    """Первый ответ — вызов инструмента, второй — финальный ответ."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def generate(self, text, history, max_tokens, on_token, is_continue=False):
        self.calls.append({"text": text, "history": [dict(m) for m in history],
                           "continue": is_continue})
        if len(self.calls) == 1:
            await on_token("Ищу в интернете.")
            return {
                "type": "done", "text": "Ищу в интернете.", "raw": TOOL_RAW,
                "tool_calls": [{"name": "web_search",
                                "arguments": {"query": "владелец FromSoftware"}}],
                "tokens": 12, "finish": "tool_calls", "cached_tokens": 706,
                "new_tokens": 12, "prompt_tokens": 718, "prefill_ms": 20,
                "first_token_ms": 300, "tok_s": 20.0, "kv_tokens": 730,
            }
        await on_token(ANSWER)
        return {
            "type": "done", "text": ANSWER, "raw": ANSWER, "tool_calls": [],
            "tokens": 9, "finish": "stop", "cached_tokens": 730, "new_tokens": 9,
            "prompt_tokens": 739, "prefill_ms": 18, "first_token_ms": 280,
            "tok_s": 21.0, "kv_tokens": 748,
        }


async def case_tool_history() -> bool:
    ok = True
    app.llm = FakeLLM()
    app.tools.execute = lambda name, args: "Kadokawa Corporation владеет FromSoftware."

    session = FakeSession()
    turn = app.Turn(session, "кто владелец FromSoftware?", "text")
    await turn.run()

    hist = session.history
    print("\nистория после хода:")
    for m in hist:
        print(f"  {m['role']}: {m['content'][:90]!r}")

    if not hist or hist[0].get("role") != "user":
        print("FAIL: первая запись истории — не реплика пользователя")
        ok = False
    tools_msgs = [m for m in hist if m.get("role") == "tool"]
    if not tools_msgs:
        print("FAIL: в истории нет результата инструмента")
        ok = False
    raw_saved = [m for m in hist
                 if m.get("role") == "assistant" and "<tool_call>" in m.get("content", "")]
    if not raw_saved:
        print("FAIL: разметка вызова потеряна — в историю попал отфильтрованный текст")
        ok = False
    if hist[-1].get("role") != "assistant" or ANSWER not in hist[-1].get("content", ""):
        print("FAIL: последняя запись — не финальный ответ")
        ok = False
    # Второй вызов обязан идти как продолжение: клиент помечает continue=True,
    # а история уже содержит user → assistant(вызов) → tool(результат).
    second = app.llm.calls[1] if len(app.llm.calls) > 1 else {}
    if len(app.llm.calls) != 2 or not second.get("continue"):
        print("FAIL: продолжение после инструмента собрано неверно")
        ok = False
    elif not second.get("history") or second["history"][-1].get("role") != "tool":
        print("FAIL: история продолжения не заканчивается результатом инструмента")
        ok = False
    if ok:
        print("OK: tool-раунд сохраняет raw-разметку и порядок user→assistant→tool→assistant")
    return ok


class EogLLM:
    def __init__(self) -> None:
        self.n = 0

    async def generate(self, text, history, max_tokens, on_token, is_continue=False):
        self.n += 1
        return {"type": "done", "text": "", "raw": "", "tool_calls": [],
                "tokens": 0, "finish": "eog", "cached_tokens": 706,
                "new_tokens": 1, "prompt_tokens": 707, "prefill_ms": 15,
                "first_token_ms": -1, "tok_s": 0.0, "kv_tokens": 707}


async def case_eog() -> bool:
    app.llm = EogLLM()
    app.tools.execute = lambda name, args: ""
    session = FakeSession()
    await app.Turn(session, "тест пустого ответа", "text").run()

    errors = [e for e in session.events if e.get("type") == "error"]
    ends = [e for e in session.events if e.get("type") == "turn_end"]
    ok = True
    if app.llm.n != 2:
        print(f"FAIL: ожидалось 2 попытки генерации, было {app.llm.n}")
        ok = False
    if not errors:
        print("FAIL: два пустых ответа подряд не дали ошибку клиенту")
        ok = False
    if not ends or ends[-1].get("reason") != "empty_answer":
        print("FAIL: turn_end не помечен как empty_answer")
        ok = False
    if ok:
        print("OK: двойной немедленный EOG даёт явную ошибку и turn_end(reason=empty_answer)")
    return ok


async def main() -> int:
    results = [await case_tool_history(), await case_eog()]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
