"""Инструменты агента: MCP-поиск wormsoft + локальные (время, калькулятор, таймер).

Требования раздела 6 ТЗ:
  * токен читается ЛЕНИВО (в момент первого вызова), не на импорте;
  * .env ищется: явный путь -> корень проекта -> рядом с приложением;
  * если токена нет — внятный текст с ПЕРЕЧНЕМ проверенных путей, без падения;
  * токен никогда не попадает в логи/ответы/health — только {present, source};
  * ошибки сети/HTTP -> короткий текст результата, без стектрейсов и тела ответа;
  * результаты сжимаются: без markdown-таблиц и длинных URL внутри фразы.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from . import config

log = logging.getLogger("voice.tools")

_token_cache: dict[str, object] = {"loaded": False, "value": "", "source": None}


# ---------------------------------------------------------------------------
# Токен: ленивое чтение
# ---------------------------------------------------------------------------
def _token_paths() -> list[Path]:
    paths: list[Path] = []
    explicit = os.environ.get("WORM_TOKEN_FILE", "").strip()
    if explicit:
        paths.append(Path(explicit).expanduser())
    paths.append(Path(str(config.worm_token_file())).expanduser())
    paths.append(Path.home() / ".wormsoft-token")
    here = Path(__file__).resolve().parent
    paths.append(here.parent / ".wormsoft-token")
    # уникализировать, сохранив порядок
    seen, out = set(), []
    for p in paths:
        s = str(p)
        if s not in seen:
            seen.add(s)
            out.append(p)
    return out


def _read_token() -> tuple[str, str | None]:
    """Возвращает (токен, источник). Источник — 'WORM_TOKEN' или путь к файлу."""
    if _token_cache["loaded"]:
        return str(_token_cache["value"]), _token_cache["source"]  # type: ignore[return-value]

    tok = os.environ.get("WORM_TOKEN", "").strip()
    source: str | None = "WORM_TOKEN" if tok else None
    if not tok:
        for p in _token_paths():
            try:
                if not p.is_file():
                    continue
                # utf-8-sig: BOM; strip: CRLF-хвост из Windows -> 401
                val = p.read_text(encoding="utf-8-sig").strip().replace("\r", "").replace("\n", "")
                if val:
                    tok = val
                    source = str(p)
                    break
            except OSError:
                continue

    _token_cache.update({"loaded": True, "value": tok, "source": source})
    if tok:
        log.info("токен wormsoft найден, источник=%s, длина=%d", source, len(tok))
    else:
        log.warning("токен wormsoft не найден; проверено: %s",
                    ", ".join(str(p) for p in _token_paths()))
    return tok, source


def token_status() -> dict:
    """Для /health: только факт наличия и источник, без значения."""
    tok, source = _read_token()
    return {"present": bool(tok), "source": source, "provider": resolve_provider()}


def reset_token_cache() -> None:
    _token_cache.update({"loaded": False, "value": "", "source": None})


# ---------------------------------------------------------------------------
# MCP JSON-RPC
# ---------------------------------------------------------------------------
class MCPError(RuntimeError):
    pass


def mcp_call(name: str, arguments: dict) -> str:
    """Прямой tools/call, один запрос — один JSON-ответ (без initialize/SSE)."""
    tok, _ = _read_token()
    if not tok:
        checked = "\n".join(f"  - {p}" for p in _token_paths())
        return ("поиск недоступен: не найден токен доступа. Проверены пути:\n" + checked +
                "\nа также переменная окружения WORM_TOKEN."
                "\nОтветить пользователю честно: данные из интернета получить нельзя.")

    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, ensure_ascii=False).encode()

    req = urllib.request.Request(
        config.worm_mcp_url(), data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + tok,
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=config.worm_mcp_timeout()) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # тело ответа не показываем — там может быть чувствительное
        hint = {401: "токен не принят (401)", 403: "доступ запрещён (403)",
                429: "слишком много запросов (429)"}.get(e.code, f"HTTP {e.code}")
        return f"поиск недоступен: {hint}"
    except urllib.error.URLError as e:
        return f"поиск недоступен: сеть недоступна ({e.reason.__class__.__name__})"
    except TimeoutError:
        return "поиск недоступен: истёк таймаут запроса"
    except (json.JSONDecodeError, OSError) as e:
        return f"поиск недоступен: {e.__class__.__name__}"

    if "error" in payload:
        return f"поиск недоступен: ошибка протокола ({payload['error'].get('code', '?')})"

    result = payload.get("result") or {}
    if result.get("isError"):
        return "поиск недоступен: сервер вернул ошибку инструмента"

    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
    return "\n".join(parts) if parts else "поиск недоступен: пустой ответ"


# ---------------------------------------------------------------------------
# Сжатие результата для промпта
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"[ \t]+")


def compress(text: str, limit: int = 900, max_fragments: int = 4) -> str:
    """Убрать дубли пробелов, markdown-таблицы, длинные URL; обрезать по лимиту."""
    if not text:
        return ""

    # если это JSON со списком результатов — разобрать и оставить выжимки
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = None

    frags: list[str] = []
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        for r in data["results"][:max_fragments]:
            title = str(r.get("title", "")).strip()
            snippet = str(r.get("content", "") or r.get("snippet", "")).strip()
            url = str(r.get("url", "")).strip()
            host = ""
            if url:
                m = re.match(r"https?://([^/]+)", url)
                host = m.group(1) if m else ""
            line = f"— {title}" if title else "—"
            if snippet:
                line += ": " + snippet
            if host:
                line += f" (источник: {host})"
            frags.append(line)
    else:
        frags = [text]

    out: list[str] = []
    for f in frags:
        # markdown-таблицы не годятся для озвучки
        lines = [ln for ln in f.splitlines() if not ln.strip().startswith("|")]
        f = " ".join(lines)
        f = _URL_RE.sub("", f)          # URL внутри фразы не проговариваем
        f = _WS_RE.sub(" ", f)
        f = re.sub(r"\s*\n\s*", " ", f).strip()
        if f:
            out.append(f)

    res = "\n".join(out)
    if len(res) > limit:
        res = res[:limit].rsplit(" ", 1)[0] + "…"
    return res


# ---------------------------------------------------------------------------
# Провайдеры веб-поиска
# ---------------------------------------------------------------------------
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


class SearchError(RuntimeError):
    pass


def _http(url: str, *, data: bytes | None = None, headers: dict | None = None,
          timeout: float | None = None) -> bytes:
    h = {"User-Agent": _UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout or config.search_timeout()) as r:
        return r.read()


def _http_json(url: str, **kw) -> dict:
    raw = _http(url, **kw)
    return json.loads(raw.decode("utf-8", "replace"))


def _fmt_results(items: list[dict]) -> str:
    """Список результатов -> компактный текст (без markdown и длинных URL)."""
    lines = []
    for i, it in enumerate(items, 1):
        title = str(it.get("title") or "").strip()
        snippet = str(it.get("snippet") or it.get("content") or "").strip()
        host = ""
        m = re.match(r"https?://([^/]+)", str(it.get("url") or ""))
        if m:
            host = m.group(1)
        line = f"{i}) {title}" if title else f"{i})"
        if snippet:
            line += " — " + snippet
        if host:
            line += f" (источник: {host})"
        lines.append(line)
    return "\n".join(lines) if lines else "поиск недоступен: пустой ответ"


def _search_ddg(query: str, n: int) -> list[dict]:
    """DuckDuckGo HTML: без ключа и регистрации (неофициальный, но живой)."""
    q = urllib.parse.quote_plus(query)
    html = _http(f"https://html.duckduckgo.com/html/?q={q}").decode("utf-8", "replace")
    out: list[dict] = []
    # блоки результатов: <a class="result__a" href="...">Заголовок</a> ... snippet
    for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
            r'(?:.{0,1200}?class="result__snippet"[^>]*>(.*?)</(?:a|div|td)>)?',
            html, re.S | re.I):
        href, title, snippet = m.group(1), m.group(2), m.group(3)
        href = href.replace("&amp;", "&")
        # DDG отдаёт редирект вида //duckduckgo.com/l/?uddg=<encoded url>
        if "uddg=" in href:
            try:
                href = urllib.parse.unquote(href.split("uddg=", 1)[1].split("&", 1)[0])
            except Exception:  # noqa: BLE001
                pass
        title = re.sub(r"<[^>]+>", " ", title or "")
        snippet = re.sub(r"<[^>]+>", " ", snippet or "")
        out.append({"title": title.strip(), "snippet": snippet.strip(), "url": href})
        if len(out) >= n:
            break
    if not out:
        raise SearchError("DuckDuckGo не вернул результатов")
    return out[:n]


def _search_tavily(query: str, n: int) -> list[dict]:
    key = config.tavily_key()
    if not key:
        raise SearchError("нет TAVILY_API_KEY")
    payload = json.dumps({"query": query, "max_results": n,
                          "search_depth": "basic"}).encode()
    data = _http_json("https://api.tavily.com/search", data=payload,
                      headers={"Content-Type": "application/json",
                               "Authorization": "Bearer " + key})
    items = [{"title": r.get("title"), "snippet": r.get("content"),
              "url": r.get("url")} for r in (data.get("results") or [])][:n]
    if not items:
        raise SearchError("Tavily не вернул результатов")
    return items


def _search_brave(query: str, n: int) -> list[dict]:
    key = config.brave_key()
    if not key:
        raise SearchError("нет BRAVE_API_KEY")
    q = urllib.parse.quote_plus(query)
    data = _http_json(f"https://api.search.brave.com/res/v1/web/search?q={q}&count={n}",
                      headers={"X-Subscription-Token": key,
                               "Accept": "application/json"})
    items = ((data.get("web") or {}).get("results") or [])[:n]
    if not items:
        raise SearchError("Brave не вернул результатов")
    return [{"title": r.get("title"),
             "snippet": " ".join(re.sub(r"<[^>]+>", " ", r.get("description") or "").split()),
             "url": r.get("url")} for r in items]


def _search_searxng(query: str, n: int) -> list[dict]:
    base = config.searxng_url().rstrip("/")
    if not base:
        raise SearchError("нет SEARXNG_URL")
    q = urllib.parse.quote_plus(query)
    data = _http_json(f"{base}/search?q={q}&format=json&language=ru")
    items = (data.get("results") or [])[:n]
    if not items:
        raise SearchError("SearXNG не вернул результатов")
    return [{"title": r.get("title"), "snippet": r.get("content"), "url": r.get("url")}
            for r in items]


def _fetch_direct(url: str) -> str:
    raw = _http(url, timeout=config.search_timeout()).decode("utf-8", "replace")
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"&(amp|lt|gt|quot|#39);", lambda m: {"amp": "&", "lt": "<",
                                                       "gt": ">", "quot": '"',
                                                       "#39": "'"}[m.group(1)], text)
    return re.sub(r"\s+", " ", text).strip()


def resolve_provider() -> str:
    p = config.search_provider()
    if p and p != "auto":
        return p
    if config.tavily_key():
        return "tavily"
    if config.brave_key():
        return "brave"
    if config.searxng_url():
        return "searxng"
    if _read_token()[0]:
        return "wormsoft"
    return "ddg"


# ---------------------------------------------------------------------------
# Реализации инструментов
# ---------------------------------------------------------------------------
def web_search(query: str, max_results: int = 5) -> str:
    n = max(1, min(10, int(max_results)))
    provider = resolve_provider()
    try:
        if provider == "wormsoft":
            raw = mcp_call("web_search", {"query": query, "max_results": n})
            if raw.startswith("поиск недоступен"):
                # токен есть, но сервис не ответил — пробуем DDG как запасной
                return _fmt_results(_search_ddg(query, n))
            return compress(raw)
        if provider == "tavily":
            return _fmt_results(_search_tavily(query, n))
        if provider == "brave":
            return _fmt_results(_search_brave(query, n))
        if provider == "searxng":
            return _fmt_results(_search_searxng(query, n))
        return _fmt_results(_search_ddg(query, n))
    except SearchError as e:
        if provider != "ddg":
            try:
                return _fmt_results(_search_ddg(query, n))
            except Exception:  # noqa: BLE001
                pass
        return f"поиск недоступен: {e}"
    except urllib.error.HTTPError as e:
        hint = {401: "ключ/токен не принят (401)", 403: "доступ запрещён (403)",
                429: "слишком много запросов (429)"}.get(e.code, f"HTTP {e.code}")
        return f"поиск недоступен: {hint}"
    except urllib.error.URLError as e:
        return f"поиск недоступен: сеть недоступна ({e.reason.__class__.__name__})"
    except TimeoutError:
        return "поиск недоступен: истёк таймаут запроса"
    except (json.JSONDecodeError, OSError, ValueError) as e:
        return f"поиск недоступен: {e.__class__.__name__}"


def web_fetch(url: str) -> str:
    if resolve_provider() == "wormsoft":
        raw = mcp_call("web_fetch", {"url": url})
        if not raw.startswith("поиск недоступен"):
            return compress(raw, limit=1500, max_fragments=1)
    try:
        text = _fetch_direct(url)
        return compress(text, limit=1500, max_fragments=1)
    except urllib.error.HTTPError as e:
        return f"загрузка недоступна: HTTP {e.code}"
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        return f"загрузка недоступна: {e.__class__.__name__}"


def current_time() -> str:
    now = datetime.now().astimezone()
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    months = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    return (f"Сейчас {now.strftime('%H:%M')}, {days[now.weekday()]}, "
            f"{now.day} {months[now.month - 1]} {now.year} года, "
            f"часовой пояс {now.strftime('%Z') or 'локальный'}.")


_SAFE_EXPR = re.compile(r"^[0-9+\-*/().,%\s^]+$")


def calculator(expression: str) -> str:
    expr = expression.strip().replace("^", "**").replace(",", ".")
    if not expr or not _SAFE_EXPR.match(expression.replace("^", "").replace(",", "")):
        return "калькулятор: допустимы только числа и операции + - * / ( ) % ^"
    try:
        val = eval(expr, {"__builtins__": {}}, {})  # noqa: S307 — вход отфильтрован
    except Exception as e:  # noqa: BLE001
        return f"калькулятор: не удалось вычислить ({e.__class__.__name__})"
    if isinstance(val, float) and val.is_integer():
        val = int(val)
    return f"{expression.strip()} = {val}"


_timers: dict[str, float] = {}


def set_timer(seconds: int, label: str = "") -> str:
    seconds = max(1, min(int(seconds), 3600))
    key = label.strip() or f"таймер-{len(_timers) + 1}"
    _timers[key] = time.monotonic() + seconds
    return f"Таймер «{key}» установлен на {seconds} секунд."


def check_timer(label: str = "") -> str:
    if not _timers:
        return "Активных таймеров нет."
    if label.strip():
        t = _timers.get(label.strip())
        if t is None:
            return f"Таймер «{label}» не найден."
        left = t - time.monotonic()
        return (f"Таймер «{label}» сработал." if left <= 0
                else f"До конца таймера «{label}» {int(left)} секунд.")
    parts = []
    for k, t in _timers.items():
        left = t - time.monotonic()
        parts.append(f"«{k}»: " + ("сработал" if left <= 0 else f"{int(left)} с"))
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Действия: открыть музыку/видео/сайт в браузере пользователя
# ---------------------------------------------------------------------------
# Результат инструмента с действием: первая строка — маркер с JSON, дальше
# человекочитаемый текст (он же уходит модели и в историю).
ACTION_PREFIX = "__ACTION__"

_MUSIC_URLS = {
    "youtube_music": "https://music.youtube.com/search?q={q}",
    "spotify": "https://open.spotify.com/search/{q}",
    "yandex": "https://music.yandex.ru/search?text={q}",
    "soundcloud": "https://soundcloud.com/search?q={q}",
}


def _action(payload: dict, human: str) -> str:
    return ACTION_PREFIX + json.dumps(payload, ensure_ascii=False) + "\n" + human


def split_action(result: str) -> tuple[dict | None, str]:
    """Отделяет действие от текста результата."""
    if not result or not result.startswith(ACTION_PREFIX):
        return None, result
    try:
        line, _, rest = result.partition("\n")
        payload = json.loads(line[len(ACTION_PREFIX):])
        return (payload if isinstance(payload, dict) else None), rest.strip()
    except (json.JSONDecodeError, ValueError):
        return None, result


def _allowed_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) or d.endswith("." + host)
               for d in config.action_domains())


def music_search(query: str, service: str = "") -> str:
    q = urllib.parse.quote_plus((query or "").strip())
    if not q:
        return "Не понял, какую музыку включить. Повторите название."
    svc = (service or config.music_service()).strip().lower()
    tpl = _MUSIC_URLS.get(svc) or _MUSIC_URLS["youtube_music"]
    url = tpl.format(q=q)
    human = f"Открываю музыку «{query}» в {svc.replace('_', ' ')}."
    return _action({"kind": "open_url", "url": url,
                    "title": f"Музыка: {query}", "service": svc}, human)


def video_search(query: str) -> str:
    q = urllib.parse.quote_plus((query or "").strip())
    if not q:
        return "Не понял, какое видео найти. Повторите запрос."
    url = "https://www.youtube.com/results?search_query=" + q
    return _action({"kind": "open_url", "url": url,
                    "title": f"Видео: {query}", "service": "youtube"},
                   f"Открываю видео «{query}» на YouTube.")


def action_url_allowed(url: str) -> bool:
    """Проверка домена для действий, инициированных моделью."""
    return _allowed_url(url)


# ---------------------------------------------------------------------------
# Детерминированный роутер простых голосовых команд
# ---------------------------------------------------------------------------
# Модель 9B не всегда вызывает инструмент на короткую команду («выключи музыку»),
# поэтому очевидные сценарии обрабатываем без генерации: быстрее и предсказуемо.
_STOP_RE = re.compile(r"\b(выключи|останови|остановить|стоп|прекрати)\b.*"
                      r"\b(музык\w*|трек\w*|песн\w*|играет|играй)\b", re.I)
_PLAY_RE = re.compile(r"\b(включи|поставь|сыграй|запусти)\b\s*"
                      r"(?:мне\s+)?(?:какую-нибудь\s+)?(.*)", re.I)
_VIDEO_RE = re.compile(r"\b(найди|покажи|включи|открой)\b\s*"
                       r"(?:мне\s+)?(?:видео|ролик|клип|фильм)\s*(.*)", re.I)
_SITE_RE = re.compile(r"\b(открой|перейди|зайди)\b\s*(?:сайт\s+)?"
                      r"((?:https?://)?[\w.-]+\.[a-z]{2,}(?:/\S*)?)", re.I)
_MEDIA_WORDS = re.compile(r"\b(видео|ролик|клип|фильм|ютуб|youtube)\b", re.I)
_MUSIC_WORDS = re.compile(r"\b(музык\w*|песн\w*|трек\w*|альбом\w*|плейлист\w*)\b", re.I)
_PAUSE_RE = re.compile(r"\b(паузу?|приостанови|поставь на паузу|стоп-пауза)\b", re.I)
_RESUME_RE = re.compile(r"\b(продолжи|продолжай|сними с паузы|играй дальше|дальше играй)\b", re.I)
_NEXT_RE = re.compile(r"\b(следующий|следующ\w*|дальше|некст|переключи трек)\b", re.I)
_PREV_RE = re.compile(r"\b(предыдущий|предыдущ\w*|верни назад|назад трек)\b", re.I)
_STATUS_RE = re.compile(r"\b(что играет|что за (трек|песня|музыка)|какая (песня|музыка) играет)\b", re.I)
_LOCAL_RE = re.compile(r"\b(мою|моя|моей|локальн\w*|из папки|с диска|папк\w* музыка)\b", re.I)


def _playing() -> bool:
    p = _player.get("proc")
    return p is not None and p.poll() is None


def route_action(text: str) -> tuple[str, dict] | None:
    """Простая команда -> (инструмент, аргументы). None — отдать модели."""
    t = (text or "").strip()
    if not t or not config.tools_enabled():
        return None
    if _STOP_RE.search(t):
        return "stop_music", {}

    if _PAUSE_RE.search(t):
        return "pause_music", {}
    if _RESUME_RE.search(t):
        return "resume_music", {}
    if _NEXT_RE.search(t) and (_playing() or _MUSIC_WORDS.search(t)):
        return "next_track", {}
    if _PREV_RE.search(t) and (_playing() or _MUSIC_WORDS.search(t)):
        return "prev_track", {}
    if _STATUS_RE.search(t):
        return "music_status", {}

    m = re.search(r"громкост\w*\s*(?:на\s*)?(\d{1,3})", t, re.I)
    if m:
        return "set_volume", {"level": m.group(1)}
    if re.search(r"\b(громче|прибавь|увеличь громкость)\b", t, re.I):
        return "set_volume", {"level": "+10"}
    if re.search(r"\b(тише|убавь|уменьши громкость|сбавь)\b", t, re.I):
        return "set_volume", {"level": "-10"}

    if _LOCAL_RE.search(t) and (_MUSIC_WORDS.search(t) or "коллекц" in t.lower()):
        q = _MUSIC_WORDS.sub(" ", t)
        q = _LOCAL_RE.sub(" ", q)
        q = re.sub(r"\b(включи|поставь|сыграй|запусти|мне|музыку|треки|песни)\b", " ", q, flags=re.I)
        return "play_local", {"query": q.strip(" .,!-—")}

    m = _VIDEO_RE.match(t)
    if m:
        q = m.group(2).strip(" .,!-—")
        if q:
            q = re.sub(r"^(про|о|об)\s+", "", q, flags=re.I)
            return "video_search", {"query": q}
        return None

    m = _PLAY_RE.match(t)
    if m:
        rest = m.group(2).strip(" .,!-—")
        if _MEDIA_WORDS.search(t) and not _MUSIC_WORDS.search(t):
            return None
        if _MUSIC_WORDS.search(t):
            # «включи музыку рамштайн» -> убрать слово «музыку» из запроса
            q = _MUSIC_WORDS.sub(" ", rest).strip(" .,!-—")
            return "play_music", {"query": q or "популярные песни"}
        if rest and not _SITE_RE.search(t):
            return "play_music", {"query": rest}

    m = _SITE_RE.search(t)
    if m:
        return "open_site", {"target": m.group(2)}
    return None


# --- воспроизведение на этом ПК (yt-dlp -> mpv, звук через WSLg PulseAudio) ---
# mpv выбран вместо ffplay: он умеет плейлист, паузу и громкость через JSON-IPC,
# то есть поддерживает «пауза», «дальше», «громче» без перезапуска.
_player: dict = {"proc": None, "items": [], "titles": [], "query": "",
                 "volume": 80, "sock": None}
_player_lock = threading.Lock()
_MPV_SOCK = "/tmp/cmp-voice-mpv.sock"
_AUDIO_EXT = (".mp3", ".flac", ".ogg", ".m4a", ".wav", ".opus", ".aac", ".wma")


def _pulse_env() -> dict:
    env = os.environ.copy()
    # systemd-сервис не всегда наследует WSLg-переменные: добавляем сами
    if not env.get("PULSE_SERVER") and os.path.exists("/mnt/wslg/PulseServer"):
        env["PULSE_SERVER"] = "unix:/mnt/wslg/PulseServer"
    if not env.get("XDG_RUNTIME_DIR"):
        env["XDG_RUNTIME_DIR"] = "/run/user/0"
    return env


def _mpv_bin() -> str:
    return shutil.which("mpv") or "/usr/bin/mpv"


def _ipc(cmd: list, timeout: float = 2.0) -> dict | None:
    """Команда mpv через unix-socket. None, если плеер не запущен."""
    sock = _player.get("sock")
    if not sock or _player.get("proc") is None:
        return None
    try:
        import socket

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(sock)
            s.sendall((json.dumps({"command": cmd}) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(8192)
                if not chunk:
                    break
                buf += chunk
            if not buf:
                return None
            return json.loads(buf.decode(errors="replace").splitlines()[0])
    except (OSError, ValueError):
        return None


def _ipc_get(prop: str):
    r = _ipc(["get_property", prop])
    if not r or r.get("error") != "success":
        return None
    return r.get("data")


def _resolve_audio(query: str, count: int = 5) -> list[str]:
    """Поиск на YouTube -> прямые URL аудиопотоков (для плейлиста)."""
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings",
           "--default-search", "ytsearch%d" % max(1, count),
           "-f", "bestaudio[ext=m4a]/bestaudio", "-g", query]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=config.ytdlp_timeout(), env=_pulse_env())
    urls = [ln.strip() for ln in (r.stdout or "").splitlines()
            if ln.strip().startswith("http")]
    if r.returncode != 0 or not urls:
        tail = (r.stderr or "").strip().splitlines()
        raise RuntimeError(tail[-1][:120] if tail else f"код {r.returncode}")
    return urls


def music_dir() -> Path:
    d = Path(config.music_dir()).expanduser()
    if d.is_dir():
        return d
    fallback = Path.home() / "Music"
    return fallback if fallback.is_dir() else d


def _scan_local(query: str) -> list[Path]:
    root = music_dir()
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("*")
             if p.is_file() and p.suffix.lower() in _AUDIO_EXT]
    q = (query or "").strip().casefold()
    if not q:
        return sorted(files)
    words = [w for w in re.findall(r"\w+", q) if len(w) > 1]
    if not words:
        return sorted(files)
    def hay(p: Path) -> str:
        return (p.stem + " " + p.parent.name).casefold()
    exact = [p for p in files if all(w in hay(p) for w in words)]
    if exact:
        return sorted(exact)
    # мягкий поиск: хотя бы одно слово, чтобы «включи мелодию» нашло «Тестовая мелодия»
    return sorted(p for p in files if any(w in hay(p) for w in words))


def _start_playlist(items: list[str], titles: list[str], query: str) -> str | None:
    """Запускает mpv с плейлистом. Возвращает текст ошибки или None."""
    binp = _mpv_bin()
    if not os.path.exists(binp):
        return "Не могу играть на этом ПК: нет mpv. Установите mpv (apt install mpv)."
    if not items:
        return "Не нашёл, что включить."
    stop_music()
    sock = f"{_MPV_SOCK}-{os.getpid()}"
    try:
        os.unlink(sock)
    except OSError:
        pass
    cmd = [binp, "--no-video", "--no-terminal", "--really-quiet",
           "--idle=no", f"--input-ipc-server={sock}",
           f"--volume={_player['volume']}", *items]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=_pulse_env())
    except OSError as e:
        return f"Не удалось запустить плеер: {e.__class__.__name__}"
    for _ in range(40):
        if os.path.exists(sock) or proc.poll() is not None:
            break
        time.sleep(0.05)
    with _player_lock:
        _player.update(proc=proc, items=list(items), titles=list(titles),
                       query=query, sock=sock)
    if proc.poll() is not None:
        return "Плеер сразу завершился — возможно, нет доступа к звуку."
    return None


def stop_music() -> str:
    title = _current_title()
    with _player_lock:
        p = _player.get("proc")
        _player.update(proc=None, items=[], titles=[], query="", sock=None)
    if p is None or p.poll() is not None:
        return "Сейчас ничего не играет."
    try:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
    except OSError:
        pass
    return f"Остановил музыку{f' «{title}»' if title else ''}."


def _current_title() -> str:
    t = _ipc_get("media-title")
    if t:
        return str(t)
    with _player_lock:
        titles = _player.get("titles") or []
        idx = _ipc_get("playlist-pos")
        if isinstance(idx, int) and 0 <= idx < len(titles):
            return titles[idx]
    return ""


def pause_music() -> str:
    if _player.get("proc") is None or _player["proc"].poll() is not None:
        return "Сейчас ничего не играет."
    r = _ipc(["set_property", "pause", True])
    if not r or r.get("error") != "success":
        return "Не удалось поставить на паузу."
    return "Поставил на паузу."


def resume_music() -> str:
    if _player.get("proc") is None or _player["proc"].poll() is not None:
        return "Сейчас ничего не играет."
    r = _ipc(["set_property", "pause", False])
    if not r or r.get("error") != "success":
        return "Не удалось продолжить."
    return "Продолжаю."


def toggle_pause() -> str:
    paused = _ipc_get("pause")
    if paused is None:
        return "Сейчас ничего не играет."
    return resume_music() if paused else pause_music()


def next_track() -> str:
    if _player.get("proc") is None or _player["proc"].poll() is not None:
        return "Сейчас ничего не играет."
    pos = _ipc_get("playlist-pos")
    count = _ipc_get("playlist-count")
    if isinstance(pos, int) and isinstance(count, int) and pos + 1 >= count:
        return "Это был последний трек в очереди."
    r = _ipc(["playlist-next"])
    if not r or r.get("error") != "success":
        return "Не удалось переключить трек."
    time.sleep(0.3)
    t = _current_title()
    return f"Следующий трек{f': «{t}»' if t else ''}."


def prev_track() -> str:
    if _player.get("proc") is None or _player["proc"].poll() is not None:
        return "Сейчас ничего не играет."
    pos = _ipc_get("playlist-pos")
    if isinstance(pos, int) and pos <= 0:
        return "Это первый трек в очереди."
    r = _ipc(["playlist-pos", 1])
    if not r or r.get("error") != "success":
        return "Не удалось вернуться к предыдущему треку."
    time.sleep(0.3)
    t = _current_title()
    return f"Предыдущий трек{f': «{t}»' if t else ''}."


def set_volume(level: int | str) -> str:
    if _player.get("proc") is None or _player["proc"].poll() is not None:
        return "Сейчас ничего не играет."
    cur = _ipc_get("volume")
    cur = int(cur) if isinstance(cur, (int, float)) else int(_player.get("volume", 80))
    if isinstance(level, str):
        low = level.strip().lower()
        if low in ("+", "громче", "up"):
            new = cur + 10
        elif low in ("-", "тише", "down"):
            new = cur - 10
        else:
            m = re.search(r"-?\d+", low)
            new = int(m.group()) if m else cur
    else:
        new = int(level)
    new = max(0, min(100, new))
    r = _ipc(["set_property", "volume", new])
    with _player_lock:
        _player["volume"] = new
    if not r or r.get("error") != "success":
        return "Не удалось изменить громкость."
    return f"Громкость {new}%."


def music_status() -> str:
    if _player.get("proc") is None or _player["proc"].poll() is not None:
        return "Сейчас ничего не играет."
    title = _current_title() or "неизвестный трек"
    paused = _ipc_get("pause")
    vol = _ipc_get("volume")
    vol_s = f", громкость {int(vol)}%" if isinstance(vol, (int, float)) else ""
    state = "на паузе" if paused else "играет"
    pos = _ipc_get("playlist-pos")
    count = _ipc_get("playlist-count")
    track = f" ({pos + 1} из {count})" if isinstance(pos, int) and isinstance(count, int) else ""
    return f"{state} «{title}»{track}{vol_s}."


def music_now_playing() -> str:
    return music_status()


def play_music(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "Не понял, какую музыку включить. Повторите название."
    if config.music_playback() == "browser":
        return music_search(q)
    try:
        urls = _resolve_audio(q, count=config.music_playlist_size())
    except subprocess.TimeoutExpired:
        return f"Поиск «{q}» занял слишком долго. Попробуйте ещё раз."
    except Exception as e:  # noqa: BLE001
        return f"Не нашёл трек «{q}»: {e}"
    titles = [f"{q} ({i + 1})" for i in range(len(urls))]
    err = _start_playlist(urls, titles, q)
    if err:
        return err
    return f"Включаю «{q}» на этом компьютере."


def play_local(query: str = "") -> str:
    q = (query or "").strip()
    folder = music_dir()
    if not folder.is_dir():
        return (f"Папка музыки не найдена: {folder}. "
                "Положите файлы туда или задайте MUSIC_DIR.")
    files = _scan_local(q)
    if not files:
        return (f"В папке «{folder.name}» нет подходящих треков"
                f"{f' по запросу «{q}»' if q else ''}.")
    limit = config.music_playlist_size()
    files = files[:limit]
    titles = [p.stem for p in files]
    err = _start_playlist([str(p) for p in files], titles, q or "локальная музыка")
    if err:
        return err
    what = f"«{titles[0]}»" if len(files) == 1 else f"{len(files)} трек(ов), начиная с «{titles[0]}»"
    return f"Включаю из папки «{folder.name}»: {what}."


def open_site(target: str) -> str:
    t = (target or "").strip()
    if not t:
        return "Не понял, какой сайт открыть."
    if not re.match(r"^https?://", t, re.I):
        if _allowed_url("https://" + t):
            t = "https://" + t
        else:
            # не URL — считаем поисковым запросом
            q = urllib.parse.quote_plus(t)
            t = "https://www.youtube.com/results?search_query=" + q
    if not _allowed_url(t):
        return ("Не могу открыть этот адрес: домен не в списке разрешённых "
                "(ACTION_DOMAINS).")
    return _action({"kind": "open_url", "url": t, "title": "Открыть сайт"},
                   f"Открываю {t}.")

TOOL_SPECS: list[dict] = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Поиск информации в интернете. Используй для свежих данных: "
                       "курсы валют и криптовалют, новости, погода, цены, события.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "поисковый запрос"},
            "max_results": {"type": "integer", "description": "сколько результатов, по умолчанию 5"},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "Загрузить текст веб-страницы по адресу и прочитать его.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "полный адрес страницы"},
        }, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "current_time",
        "description": "Текущие дата, время и день недели. Используй, когда спрашивают "
                       "который час, какое сегодня число или день недели.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "calculator",
        "description": "Вычислить арифметическое выражение.",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "например 2*(3+4)"},
        }, "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "set_timer",
        "description": "Поставить таймер на заданное число секунд.",
        "parameters": {"type": "object", "properties": {
            "seconds": {"type": "integer", "description": "длительность в секундах"},
            "label": {"type": "string", "description": "название таймера"},
        }, "required": ["seconds"]}}},
    {"type": "function", "function": {
        "name": "check_timer",
        "description": "Проверить, сколько осталось до таймера или сработал ли он.",
        "parameters": {"type": "object", "properties": {
            "label": {"type": "string", "description": "название таймера"},
        }}}},
    {"type": "function", "function": {
        "name": "play_music",
        "description": "Включить музыку со звуком на этом компьютере. ОБЯЗАТЕЛЬНО вызывай "
                       "на «включи/поставь/сыграй музыку, песню, трек, исполнителя», "
                       "«хочу послушать …». Это основной способ включить музыку.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "исполнитель, песня или описание"},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "stop_music",
        "description": "Остановить музыку, которая сейчас играет.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "play_local",
        "description": "Включить музыку из локальной папки «Музыка» на этом компьютере. "
                       "Вызывай на «включи мою музыку», «поставь из папки», «локальную».",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "часть названия файла/папки (необязательно)"},
        }}}},
    {"type": "function", "function": {
        "name": "pause_music",
        "description": "Поставить музыку на паузу.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "resume_music",
        "description": "Продолжить музыку после паузы.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "next_track",
        "description": "Следующий трек.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "prev_track",
        "description": "Предыдущий трек.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "set_volume",
        "description": "Громкость музыки: 0–100 или '+10'/'-10'.",
        "parameters": {"type": "object", "properties": {
            "level": {"type": "string", "description": "число или +10/-10"},
        }, "required": ["level"]}}},
    {"type": "function", "function": {
        "name": "music_status",
        "description": "Что сейчас играет: трек, позиция, пауза, громкость.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "music_search",
        "description": "Открыть музыкальный сервис в браузере (без воспроизведения здесь). "
                       "Используй только если просят именно открыть сайт/сервис.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "что искать: исполнитель, песня, жанр"},
            "service": {"type": "string", "description":
                        "youtube_music | spotify | yandex | soundcloud (по умолчанию из конфига)"},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "video_search",
        "description": "Найти и открыть видео на YouTube. Вызывай на «найди/включи видео, "
                       "ролик, клип, фильм, посмотри …».",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "что искать"},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "open_site",
        "description": "Открыть сайт в браузере (только из разрешённых доменов).",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "адрес вида https://… или домен"},
        }, "required": ["target"]}}},
]

_REGISTRY = {
    "web_search": lambda a: web_search(a.get("query", ""), a.get("max_results", 5)),
    "web_fetch": lambda a: web_fetch(a.get("url", "")),
    "current_time": lambda a: current_time(),
    "calculator": lambda a: calculator(str(a.get("expression", ""))),
    "set_timer": lambda a: set_timer(a.get("seconds", 0), a.get("label", "")),
    "check_timer": lambda a: check_timer(a.get("label", "")),
    "music_search": lambda a: music_search(str(a.get("query", "")), str(a.get("service", ""))),
    "video_search": lambda a: video_search(str(a.get("query", ""))),
    "open_site": lambda a: open_site(str(a.get("target", ""))),
    "play_music": lambda a: play_music(str(a.get("query", ""))),
    "stop_music": lambda a: stop_music(),
    "play_local": lambda a: play_local(str(a.get("query", ""))),
    "pause_music": lambda a: pause_music(),
    "resume_music": lambda a: resume_music(),
    "next_track": lambda a: next_track(),
    "prev_track": lambda a: prev_track(),
    "set_volume": lambda a: set_volume(str(a.get("level", "+10"))),
    "music_status": lambda a: music_status(),
}


def specs() -> list[dict]:
    return TOOL_SPECS if config.tools_enabled() else []


def execute(name: str, arguments: dict) -> str:
    fn = _REGISTRY.get(name)
    if fn is None:
        return f"инструмент {name} не поддерживается"
    try:
        return fn(arguments or {})
    except Exception as e:  # noqa: BLE001
        log.warning("tool %s: %s", name, e)
        return f"инструмент {name} завершился ошибкой ({e.__class__.__name__})"
