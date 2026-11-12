"""Голосовой диалоговый сервис.

Конвейер (раздел 3 ТЗ):
  браузер -> PCM16 16 кГц по WebSocket -> стриминговый ASR (CPU, свой поток)
  -> каждый НОВЫЙ partial инкрементально до-префилливается в KV (последний выигрывает)
  -> по final: генерация из горячего кэша, токены стримятся клиенту
  -> клаузы уходят в очередь TTS (отдельный воркер), PCM сразу клиенту.

Текстовый ввод идёт ТЕМ ЖЕ путём: события partial/final -> prefill -> генерация.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import config

config.load_env()  # .env грузится до чтения значений ниже

from . import tools  # noqa: E402
from . import tts as tts_mod  # noqa: E402
from .asr import SessionASR  # noqa: E402
from .llm_client import LastWinsPrefill, LLMError, NativeLLM  # noqa: E402
from .turn import TurnDetector  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S")
log = logging.getLogger("voice.app")

HERE = Path(__file__).resolve().parent
INDEX = HERE / "static" / "index.html"

app = FastAPI(title="CMP Voice Agent")

llm = NativeLLM(config.llm_host(), config.llm_port(),
                config.llm_timeout_read(), config.llm_timeout_connect())
tts_registry = tts_mod.build_registry()

_state = {
    "started_at": time.time(),
    "tts_ready": False,
    "asr_error": None,
    # демон мог перезапуститься: набор инструментов надо переустановить,
    # иначе база остаётся без описаний функций (база 43 вместо ~700 токенов).
    "tools_installed": False,
}


# ---------------------------------------------------------------------------
# Раунды инструментов + история
# ---------------------------------------------------------------------------
def _tool_result_message(name: str, result: str, query: str = "") -> dict:
    """Результат инструмента в истории.

    Формат повторяет шаблон Qwen: блок <tool_response>. Плюс явная пометка,
    что это ГОТОВЫЕ данные для ответа — иначе 9B на одном результате поиска
    отвечает «не могу найти информацию» или выдумывает факты.
    """
    body = (result or "").strip()
    if not body:
        body = "данных не найдено"
    return {
        "role": "tool",
        "content": (f"Результат {name} по запросу «{query}» — это готовые данные "
                    f"для ответа. Ответь на исходный вопрос пользователя "
                    f"одной-двумя фразами, используя только эти факты:"
                    f"{chr(10)}{body}"),
    }


def _is_eog(done: dict) -> bool:
    """Немедленный EOG: ни одного токена и нет вызовов инструментов."""
    return not done.get("tokens") and not done.get("tool_calls")


class Turn:
    """Один ход диалога: генерация + инструменты + TTS."""

    def __init__(self, ws_session: "Session", text: str, source: str):
        self.session = ws_session
        self.text = text
        self.source = source
        self.history = list(ws_session.history)
        self.answer = ""
        self.tool_events: list[dict] = []

    async def run(self) -> None:
        rounds = 0
        max_rounds = config.max_tool_rounds() if config.tools_enabled() else 1
        user_text = self.text
        # base — история ДО текущего хода; turn_hist — история текущего хода,
        # включая реплику пользователя и сырые ответы с вызовами инструментов.
        base_hist = list(self.history)
        hist = base_hist
        turn_hist: list[dict] = []
        is_continue = False
        last_text = ""
        emitted_any = False

        while rounds < max_rounds:
            rounds += 1
            await self.session.send({"type": "gen_start", "round": rounds})

            t_start = time.monotonic()
            buf = ""
            first_token_sent = False

            async def on_token(piece: str) -> None:
                nonlocal buf, first_token_sent, emitted_any
                emitted_any = True
                buf += piece
                if not first_token_sent:
                    first_token_sent = True
                    await self.session.send({
                        "type": "gen_first_token",
                        "ms": round((time.monotonic() - t_start) * 1000),
                    })
                await self.session.send({"type": "ans_token", "text": piece})
                self.session.push_answer_text(piece)

                clauses, rest = tts_mod.split_clauses(buf)
                buf = rest
                for c in clauses:
                    self.session.enqueue_tts(c)

            try:
                done = await llm.generate(user_text, hist, config.max_gen_tokens(),
                                          on_token, is_continue)
                # Немедленный EOG (0 токенов) — промпт «выглядит законченным».
                # Демон уже откатил KV на длину промпта, поэтому повторяем на том
                # же состоянии: reset() обнулил бы накопленный кэш.
                if _is_eog(done):
                    log.warning("немедленный EOG, повтор хода (continue=%s)", is_continue)
                    done = await llm.generate(user_text, hist, config.max_gen_tokens(),
                                              on_token, is_continue)
                    if _is_eog(done):
                        # Второй пустой ответ — это уже не «случайность»: сообщаем
                        # клиенту явную ошибку, а не пустой turn_end.
                        log.error("два пустых ответа подряд — генерация не удалась")
                        await self.session.send({
                            "type": "error",
                            "error": "модель не дала ответа (немедленный EOG)",
                        })
                        await self.session.send({"type": "turn_end",
                                                 "reason": "empty_answer"})
                        return
            except LLMError as e:
                log.error("генерация: %s", e)
                await self.session.send({"type": "error", "error": str(e)})
                await self.session.send({"type": "turn_end", "reason": "llm_error"})
                return

            # остаток текста в TTS
            clauses, _ = tts_mod.split_clauses(buf, final=True)
            for c in clauses:
                self.session.enqueue_tts(c)

            last_text = done.get("text", "")
            self.answer += last_text
            # Панель KV обязана показывать строку и для текстового хода
            # (критерий 14): берём статистику prefill из блока генерации.
            if self.session.source == "text" or done.get("new_tokens") is not None:
                await self.session.send({
                    "type": "kv",
                    "source": self.session.source,
                    "cached_tokens": done.get("cached_tokens"),
                    "new_tokens": done.get("new_tokens"),
                    "prompt_tokens": done.get("prompt_tokens"),
                    "ms_f": done.get("prefill_ms"),
                    "kv_tokens": done.get("kv_tokens"),
                    "base_tokens": None,
                    "fallbacks": None,
                    "ok": True,
                })
            await self.session.send({
                "type": "gen_stats",
                "tokens": done.get("tokens"),
                "tok_s": round(done.get("tok_s", 0.0), 1),
                "first_token_ms": done.get("first_token_ms"),
                "prefill_ms": done.get("prefill_ms"),
                "cached_tokens": done.get("cached_tokens"),
                "new_tokens": done.get("new_tokens"),
                "prompt_tokens": done.get("prompt_tokens"),
                "kv_tokens": done.get("kv_tokens"),
            })

            calls = done.get("tool_calls") or []
            # В историю пишется СЫРОЙ текст генерации (с разметкой вызова), как
            # лежит в KV: иначе модель не видит, что уже вызвала функцию, а
            # следующий промпт расходится с кэшем на всё тело вызова.
            raw_or_text = done.get("raw") or done.get("text", "")
            if not calls:
                turn_hist.append({"role": "assistant", "content": raw_or_text})
                break

            turn_hist.append({"role": "assistant", "content": raw_or_text})
            for call in calls:
                name = call.get("name", "")
                args = call.get("arguments") or {}
                await self.session.send({"type": "tool_call", "name": name, "args": args})
                result = await asyncio.get_running_loop().run_in_executor(
                    None, tools.execute, name, args)
                await self.session.send({"type": "tool_result", "name": name,
                                         "result": result[:600]})
                self.tool_events.append({"name": name, "args": args, "result": result})
                turn_hist.append(_tool_result_message(
                    name, result, str((call.get("arguments") or {}).get("query", ""))))
            # Продолжение идёт по истории хода (без новой реплики пользователя).
            hist = base_hist + [{"role": "user", "content": self.text}] + turn_hist
            is_continue = True

        if not turn_hist:
            turn_hist.append({"role": "assistant", "content": last_text})
        self.session.history = (
            base_hist
            + [{"role": "user", "content": self.text}]
            + turn_hist
        )
        await self.session.send({"type": "turn_end"})
        return


# ---------------------------------------------------------------------------
# Сеанс
# ---------------------------------------------------------------------------
class Session:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.history: list[dict] = []
        self.asr: SessionASR | None = None
        self.prefill = LastWinsPrefill(llm)
        self.turn: Turn | None = None
        self.turn_task: asyncio.Task | None = None
        self.busy = False
        self.answer_text = ""
        self.tts_q: asyncio.Queue = asyncio.Queue()
        self.tts_task: asyncio.Task | None = None
        self.tts_done = asyncio.Event()
        self.tts_pending = 0
        self.source = "asr"
        self.partial_log: list[dict] = []
        self.word_mode = False
        self.word_buf = ""
        self.tts_backend_name = tts_registry.active
        # Выбранный голос для текущего движка (None = голос по умолчанию).
        self.tts_voice: str | None = None
        # Перебивание: воркер TTS прекращает отдавать текущую фразу.
        self._tts_abort = False
        # Умное определение конца реплики.
        self.turn_detector = TurnDetector(
            min_ms=config.turn_min_ms(),
            default_ms=config.turn_default_ms(),
            question_ms=config.turn_question_ms(),
            dangling_ms=config.turn_dangling_ms(),
            short_ms=config.turn_short_ms(),
            max_ms=config.turn_max_ms(),
        )
        self._turn_task: asyncio.Task | None = None
        self._wake_mode = False
        self._closing = False

    # -- отправка ---------------------------------------------------------
    async def send(self, payload: dict) -> None:
        if self._closing:
            return
        try:
            await self.ws.send_text(json.dumps(payload, ensure_ascii=False))
        except (RuntimeError, WebSocketDisconnect):
            self._closing = True

    def push_answer_text(self, piece: str) -> None:
        self.answer_text += piece

    # -- TTS ---------------------------------------------------------------
    def enqueue_tts(self, text: str) -> None:
        if not text.strip() or tts_registry.active == "none":
            return
        self.tts_pending += 1
        self.tts_done.clear()
        self.tts_q.put_nowait(text)

    async def _tts_worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            text = await self.tts_q.get()
            if text is None:
                self.tts_q.task_done()
                return
            try:
                backend = tts_registry.get(self.tts_backend_name)
                async for chunk in backend.stream(text, self.tts_voice):
                    if self._tts_abort:
                        break
                    if chunk:
                        await self.send({"type": "audio_meta",
                                         "sample_rate": backend.sample_rate})
                        await self.ws.send_bytes(chunk)
            except tts_mod.TTSError as e:
                await self.send({"type": "tts_error", "error": str(e), "status": e.status})
            except Exception as e:  # noqa: BLE001
                log.warning("TTS: %s", e)
                await self.send({"type": "tts_error", "error": e.__class__.__name__})
            finally:
                self.tts_pending = max(0, self.tts_pending - 1)
                if self.tts_pending == 0:
                    self.tts_done.set()
                self.tts_q.task_done()

    def start_tts_worker(self) -> None:
        if self.tts_task is None or self.tts_task.done():
            self.tts_task = asyncio.create_task(self._tts_worker())

    async def stop_tts_worker(self) -> None:
        if self.tts_task is not None:
            await self.tts_q.put(None)
            try:
                await asyncio.wait_for(self.tts_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.tts_task.cancel()
            self.tts_task = None

    # -- ASR события -------------------------------------------------------
    async def on_asr_event(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "wake":
            # услышали ключевое слово: сообщаем клиенту (он сыграет «динь»)
            await self.send({"type": "wake", "word": config.wakeword_model()})
            self.turn_detector.on_speech_start()
        elif kind == "speech_start":
            self.turn_detector.on_speech_start()
            await self.send({"type": "speech_start"})
        elif kind == "speech_end":
            self.turn_detector.on_silence()
        elif kind == "partial":
            self.source = "asr"
            self.turn_detector.on_partial(ev["text"])
            await self.send({"type": "partial", "text": ev["text"],
                             "utterance_ms": ev.get("utterance_ms")})
            if not self.busy:
                _tools_check["busy"] = True   # идёт речь: базу не пересчитывать
                self.prefill.submit(ev["text"], self.history,
                                    lambda r: self._on_prefill_stats(r, "asr"))
        elif kind == "final":
            _tools_check["busy"] = False
            self.turn_detector.reset()
            if self.busy:
                return
            await self.send({"type": "final", "text": ev["text"]})
            self.spawn_turn(ev["text"], "asr")

    # -- smart turn: раз в 100 мс проверяем, не пора ли закрыть фразу ------
    async def _turn_loop(self) -> None:
        while not self._closing:
            await asyncio.sleep(0.1)
            try:
                if self.busy or self.asr is None:
                    continue
                if self.turn_detector.should_finalize():
                    self.turn_detector.fired = True
                    log.info("smart turn: закрываю фразу (hold=%.0f мс, text=%r)",
                             self.turn_detector.hold_ms(), self.turn_detector.text[-60:])
                    self.asr.request_finish()
            except Exception as e:  # noqa: BLE001
                log.warning("turn loop: %s", e)

    def start_turn_loop(self) -> None:
        if self._turn_task is None or self._turn_task.done():
            self._turn_task = asyncio.create_task(self._turn_loop())

    # -- перебивание --------------------------------------------------------
    async def interrupt(self, reason: str = "barge_in") -> None:
        """Пользователь заговорил: гасим озвучку и текущий ход."""
        self._tts_abort = True
        while not self.tts_q.empty():
            try:
                self.tts_q.get_nowait()
                self.tts_q.task_done()
            except asyncio.QueueEmpty:
                break
        self.tts_pending = 0
        self.tts_done.set()
        if self.turn_task is not None and not self.turn_task.done():
            self.turn_task.cancel()
        if self.asr is not None:
            self.asr.reset_stream()
        self.turn_detector.reset()
        await self.send({"type": "interrupted", "reason": reason})
        log.info("перебивание: %s", reason)

    async def _on_prefill_stats(self, r: dict, source: str) -> None:
        if not r.get("ok"):
            return
        row = {"type": "kv", "source": source, **r}
        self.partial_log.append(row)
        await self.send(row)

    # -- ход ---------------------------------------------------------------
    def spawn_turn(self, text: str, source: str) -> None:
        """Запускает ход в фоне, чтобы не блокировать чтение WebSocket."""
        if self.turn_task is not None and not self.turn_task.done():
            return
        self.turn_task = asyncio.create_task(self.start_turn(text, source))

    async def start_turn(self, text: str, source: str) -> None:
        text = (text or "").strip()
        if not text or self.busy:
            return
        self.busy = True
        _tools_check["busy"] = True
        self.source = source
        self.answer_text = ""
        self._tts_abort = False
        await self.send({"type": "turn_start", "text": text, "source": source})
        self.start_tts_worker()
        await self.prefill.cancel()

        turn = Turn(self, text, source)
        self.turn = turn
        try:
            # Жёсткий бюджет хода: зависший MCP, медленный демон или «залипший»
            # WebSocket не должны держать сеанс в busy бесконечно.
            await asyncio.wait_for(turn.run(), timeout=config.turn_timeout())
        except asyncio.TimeoutError:
            log.error("ход не уложился в %s с", config.turn_timeout())
            await self.send({"type": "error",
                             "error": f"ответ не получен за {config.turn_timeout():.0f} с"})
        except Exception as e:  # noqa: BLE001
            log.exception("turn: %s", e)
            await self.send({"type": "error", "error": e.__class__.__name__})
        finally:
            # дождаться, пока доиграет вся очередь аудио
            if self.tts_pending > 0:
                try:
                    await asyncio.wait_for(self.tts_done.wait(), timeout=120)
                except asyncio.TimeoutError:
                    log.warning("TTS не доиграл за 120 с")
            await self.send({"type": "ready"})
            if self.asr is not None:
                self.asr.reset_stream()
            self.busy = False
            self.turn = None
            _tools_check["busy"] = False

    # -- приём аудио -------------------------------------------------------
    async def on_audio(self, raw: bytes) -> None:
        if self.asr is None:
            return
        self.asr.feed(raw)

    def ensure_asr(self) -> None:
        if self.asr is not None:
            return
        self.asr = SessionASR()
        self.asr.start(lambda ev: asyncio.create_task(self.on_asr_event(ev)))
        if self._wake_mode:
            self.asr.set_wake_mode(True)
        self.start_turn_loop()

    def stop_asr(self) -> None:
        if self.asr is None:
            return
        self.asr.stop()
        self.asr = None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    if INDEX.is_file():
        return HTMLResponse(INDEX.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html не найден</h1>", status_code=500)


@app.get("/health")
async def health():
    # Health НЕ ходит в демон и НЕ меняет его состояние: во время генерации
    # демон занят, а set_tools вообще стирает KV. Отдаём последний снимок,
    # который обновляет фоновый _stat_refresher.
    llm_stat = _llm_cache["stat"]
    llm_error = _llm_cache["error"]

    tts_desc = None
    tts_ok = False
    try:
        b = tts_registry.get(tts_registry.active)
        tts_desc = b.describe()
        tts_ok = True
    except tts_mod.TTSError as e:
        tts_desc = {"error": str(e)}

    degraded = (llm_stat is None) or (not tts_ok)
    return {
        "status": "degraded" if degraded else "ok",
        "llm": llm_stat,
        "llm_error": llm_error,
        "llm_stat_age_s": round(time.time() - _llm_cache["ts"], 1) if _llm_cache["ts"] else None,
        "tts": tts_registry.active,
        "tts_ok": tts_ok,
        "tts_info": tts_desc,
        "asr": str(config.asr_model_dir()),
        "asr_mode": config.asr_mode(),
        "asr_endpoint": config.asr_endpoint_mode(),
        "wakeword": config.wakeword_enabled(),
        "asr_error": _state["asr_error"],
        "tools_token": tools.token_status(),
        "uptime_s": int(time.time() - _state["started_at"]),
    }


@app.get("/config")
async def cfg():
    return {
        "tts_engines": tts_registry.available(),
        "tts_active": tts_registry.active,
        "asr_modes": ["server"],
        "asr_active": config.asr_mode(),
        "tools_enabled": config.tools_enabled(),
        "max_gen_tokens": config.max_gen_tokens(),
        "max_tool_rounds": config.max_tool_rounds(),
        "turn": {
            "mode": config.asr_endpoint_mode(),
            "min_ms": config.turn_min_ms(),
            "default_ms": config.turn_default_ms(),
            "dangling_ms": config.turn_dangling_ms(),
            "max_ms": config.turn_max_ms(),
        },
        "wakeword": {
            "enabled": config.wakeword_enabled(),
            "model": config.wakeword_model(),
            "threshold": config.wakeword_threshold(),
        },
    }


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.post("/tts")
async def tts_raw(request: Request):
    """Сырой PCM16 по HTTP — для замеров TTFB/RTF."""
    body = await request.json()
    text = str(body.get("text", "")).strip()
    engine = str(body.get("engine") or tts_registry.active)
    if not text:
        return JSONResponse({"error": "пустой text"}, status_code=400)
    try:
        backend = tts_registry.get(engine)
    except tts_mod.TTSError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    chunks = []
    async for c in backend.stream(text):
        chunks.append(c)
    data = b"".join(chunks)
    return Response(content=data, media_type="application/octet-stream",
                    headers={"X-Sample-Rate": str(backend.sample_rate)})


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    s = Session(ws)
    log.info("ws: подключён клиент")

    async def reader():
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            if (data := msg.get("bytes")) is not None:
                await s.on_audio(data)
                continue
            txt = msg.get("text")
            if not txt:
                continue
            try:
                cmd = json.loads(txt)
            except json.JSONDecodeError:
                continue
            await handle_command(s, cmd)

    try:
        await reader()
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("ws: %s", e)
    finally:
        s._closing = True
        if s.turn_task is not None and not s.turn_task.done():
            s.turn_task.cancel()
        if s._turn_task is not None and not s._turn_task.done():
            s._turn_task.cancel()
        s.stop_asr()
        await s.stop_tts_worker()
        await s.prefill.cancel()
        log.info("ws: отключён")


async def handle_command(s: Session, cmd: dict) -> None:
    t = cmd.get("type")

    if t == "start":
        # включить микрофонный тракт
        try:
            s.ensure_asr()
            await s.send({"type": "started"})
        except Exception as e:  # noqa: BLE001
            _state["asr_error"] = f"{e.__class__.__name__}: {e}"
            log.error("ASR не поднялся: %s", e)
            await s.send({"type": "error", "error": f"ASR: {e}"})

    elif t == "stop":
        s.stop_asr()
        await s.send({"type": "stopped"})

    elif t == "finish_utterance":
        if s.asr is not None:
            s.asr.finish_utterance()

    elif t == "text":
        text = str(cmd.get("text", "") or "")
        word_mode = bool(cmd.get("word_mode"))
        await handle_text(s, text, word_mode, final=cmd.get("final", None))

    elif t == "tts_engine":
        name = str(cmd.get("engine", ""))
        if s.busy:
            await s.send({"type": "tts_error", "status": 409,
                          "error": "нельзя переключать TTS во время хода"})
            return
        try:
            tts_registry.get(name)
        except tts_mod.TTSError as e:
            await s.send({"type": "tts_error", "status": e.status, "error": str(e)})
            return
        # Движок — свойство сеанса, а не глобальное: иначе переключение одного
        # клиента меняет голос всем остальным подключённым.
        s.tts_backend_name = name
        # Голоса у движков разные: при смене движка возвращаемся к дефолтному.
        s.tts_voice = None
        await s.send({"type": "tts_engine_set", "engine": name,
                      "voices": tts_registry.get(name).voices()})

    elif t == "tts_voice":
        name = str(cmd.get("voice", "") or "")
        if s.busy:
            await s.send({"type": "tts_error", "status": 409,
                          "error": "нельзя менять голос во время хода"})
            return
        try:
            backend = tts_registry.get(s.tts_backend_name)
        except tts_mod.TTSError as e:
            await s.send({"type": "tts_error", "status": e.status, "error": str(e)})
            return
        voices = backend.voices() or []
        if name and name not in voices:
            await s.send({"type": "tts_error", "status": 400,
                          "error": f"неизвестный голос «{name}» для {backend.name}"})
            return
        s.tts_voice = name or None
        await s.send({"type": "tts_voice_set", "engine": backend.name,
                      "voice": s.tts_voice or (backend.voices() or [None])[0]})

    elif t == "barge_in":
        # клиент услышал речь поверх озвучки — гасим ход и аудио
        await s.interrupt("user_speech")

    elif t == "wake_mode":
        want = bool(cmd.get("enabled"))
        if want and not config.wakeword_enabled():
            await s.send({"type": "wake_mode_set", "enabled": False,
                          "reason": "wake word выключен в конфиге (WAKEWORD_ENABLED=0)"})
            return
        s._wake_mode = want
        if s.asr is not None:
            s.asr.set_wake_mode(want)
        active = bool(s.asr is not None and getattr(s.asr, "_wake_mode", False))
        await s.send({"type": "wake_mode_set", "enabled": active,
                      "word": config.wakeword_model()})

    elif t == "reset":
        if s.busy:
            await s.send({"type": "error", "error": "сброс недоступен во время хода"})
            return
        await s.prefill.cancel()
        await llm.reset()
        s.history = []
        s.word_buf = ""
        await s.send({"type": "reset_done"})

    elif t == "stat":
        try:
            st = await llm.stat()
        except LLMError as e:
            st = {"ok": False, "error": str(e)}
        await s.send({"type": "stat", **st})

    elif t == "ping":
        await s.send({"type": "pong"})


async def handle_text(s: Session, text: str, word_mode: bool, final) -> None:
    """Текстовый ввод: тот же путь, что и ASR.

    word_mode: каждый Enter добавляет фрагмент и уходит в prefill,
    генерация НЕ стартует. Empty + final → генерация.
    """
    text = text.strip()

    if word_mode and final is not True:
        # режим серии партиалов
        if text:
            s.word_buf = (s.word_buf + " " + text).strip() if s.word_buf else text
            s.source = "text"
            await s.send({"type": "partial", "text": s.word_buf, "source": "text"})
            await s.prefill.cancel()
            r = await llm.prefill(s.word_buf, s.history)
            await s._on_prefill_stats(r, "text")
            return
        # пустой ввод в режиме слов = запуск генерации
        if s.word_buf and not s.busy:
            phrase, s.word_buf = s.word_buf, ""
            s.spawn_turn(phrase, "text")
        return

    if not text:
        return
    if s.busy:
        await s.send({"type": "error", "error": "сейчас идёт ход, подождите"})
        return

    s.word_buf = ""
    s.source = "text"
    await s.send({"type": "final", "text": text, "source": "text"})
    # Ход уходит в фоновую задачу: если ждать его здесь, обработчик команд
    # перестанет читать WebSocket, и переключатели не будут заблокированы
    # (и вообще не будут обрабатываться до конца ответа).
    s.spawn_turn(text, "text")


_tools_check = {"ts": 0.0, "busy": False}


async def ensure_tools(llm_stat: dict | None = None, force: bool = False) -> bool:
    """Гарантирует, что демон знает набор инструментов.

    После рестарта демона (или переподключения) KV теряет блок описаний
    функций; без переустановки база станет ~43 токена вместо ~700.
    """
    if not config.tools_enabled():
        return False
    # set_tools пересчитывает базу (снапшот ~4 с) и СТИРАЕТ накопленный KV.
    # Поэтому: не чаще раза в 15 с и никогда во время речи или хода.
    now = time.monotonic()
    if not force:
        if _tools_check["busy"]:
            return False
        if now - _tools_check["ts"] < 15.0:
            return False
        _tools_check["ts"] = now

    want = len(tools.specs())
    have = 0
    try:
        st = llm_stat or await llm.stat()
        have = int(st.get("tools") or 0)
    except LLMError:
        _state["tools_installed"] = False
        return False
    # Источник истины — сам демон: он мог перезапуститься и потерять набор.
    if have == want:
        _state["tools_installed"] = True
        return False
    try:
        r = await llm.set_tools(tools.specs())
        _state["tools_installed"] = True
        log.info("инструменты переустановлены: %s, база %s токенов",
                 r.get("tools"), r.get("base_tokens"))
        return True
    except LLMError as e:
        _state["tools_installed"] = False
        log.warning("не удалось установить инструменты: %s", e)
        return False


# Кэш последнего stat демона: health отдаёт его мгновенно, не дёргая демон
# (во время генерации демон занят, а set_tools вообще стирает KV).
_llm_cache = {"ts": 0.0, "stat": None, "error": None}


async def _stat_refresher() -> None:
    """Фоновое обновление stat + переустановка инструментов после рестарта."""
    while True:
        try:
            st = await llm.stat()
            _llm_cache["stat"] = st
            _llm_cache["error"] = None
            _llm_cache["ts"] = time.time()
            _state["tools_installed"] = True
            await ensure_tools(st)
        except LLMError as e:
            _llm_cache["error"] = str(e)
            _llm_cache["ts"] = time.time()
            _state["tools_installed"] = False
        except Exception as e:  # noqa: BLE001
            log.warning("stat refresher: %s", e)
        await asyncio.sleep(2.0)


@app.on_event("startup")
async def on_startup():
    asyncio.create_task(_startup_async())


async def _startup_async():
    log.info("старт: LLM %s:%s, TTS=%s", config.llm_host(), config.llm_port(),
             tts_registry.active)
    # прогрев TTS в фоне, не блокируя старт
    asyncio.create_task(_warmup_tts())
    asyncio.create_task(_stat_refresher())
    # проверка ASR-модели
    if not (Path(config.asr_model_dir()) / "model.onnx").is_file():
        _state["asr_error"] = f"нет модели в {config.asr_model_dir()}"
        log.error("ASR: %s", _state["asr_error"])

    # установка инструментов в демон
    if config.tools_enabled():
        for _ in range(30):
            try:
                r = await llm.set_tools(tools.specs())
                log.info("инструменты установлены: %s (база %s токенов)",
                         r.get("tools"), r.get("base_tokens"))
                break
            except LLMError as e:
                log.warning("демон ещё не готов: %s", e)
                await asyncio.sleep(2)


async def _warmup_tts():
    try:
        await tts_registry.warmup_all()
        _state["tts_ready"] = True
    except Exception as e:  # noqa: BLE001
        log.warning("прогрев TTS: %s", e)


def main() -> None:
    uvicorn.run(app, host=config.voice_host(), port=config.voice_port(),
                log_level="info", ws_max_size=8 * 1024 * 1024)


if __name__ == "__main__":
    main()
