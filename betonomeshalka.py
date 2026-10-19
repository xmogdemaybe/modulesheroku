# -*- coding: utf-8 -*-
# requires: aiohttp
# Как работает: пишешь «.gemini <запрос>» — отдельным сообщением, ответом на
# фото/войс/видео/док или в подписи к фото. Модуль РЕДАКТИРУЕТ твоё сообщение:
# сверху остаётся запрос, ниже — ответ Gemini. Системный промпт, API-ключ,
# именованные промпты и модель хранятся в БД юзербота.
#
# Что понимает из медиа: фото, стикеры (статичные), голосовые, аудио, видео,
# кружки, гифки, PDF/CSV, картинки и текстовые файлы доком. Анимированные
# стикеры (.tgs) и файлы >20 МБ не поддерживаются (лимит инлайн-данных API).
# Ответ на обычное текстовое сообщение тоже работает — его текст уйдёт как
# контекст.
#
# API: Google Gemini Interactions API (POST /v1beta/interactions,
# заголовок x-goog-api-key). По умолчанию gemini-3.8-flash.
#
# Команды:
#   .gemini <запрос>      спросить (можно ответом на медиа или в подписи)
#   .gkey [ключ]          задать/показать API-ключ (без аргумента — показать)
#   .gprompt [текст]      глобальный системный промпт (без аргумента — показать)
#   .gprompts             список именованных промптов
#   .gsave <имя> <текст>  сохранить именованный промпт
#   .guse <имя>|default   выбрать активный промпт (default — глобальный)
#   .gdel <имя>           удалить именованный промпт
#   .gmodel [имя]         показать/сменить модель
#   .gchat [on|off]       контекст диалога на чат (previous_interaction_id)
#   .gclear               сбросить контекст текущего чата
#   .gagent [on|off]      агент-режим: функция-call инструментов (реакция/удалить/править)
#   .gagent window N      сколько недавних сообщений видно модели (её whitelist id)
#   .gconfirm / .gcancel  подтвердить/отменить действия, если инлайн-бота нет
#   .gdebug [on|off]      дебаг: дамп запроса/ответа Gemini с таймингами к ответам
#   .gbot <запрос>        то же, но ответ принудительно rich-ом через heroku-бота
#   .gbotset [auto on|off] статус rich-доставки; авто = таблицы/код/заголовки сами
#
# Ключ также подхватывается из env GEMINI_API_KEY (Heroku config vars), если
# в БД пусто. Логи — только в терминал (heroku logs), в TG ничего не дублируется.

import asyncio
import base64
import html
import logging
import os
import re
import time
from typing import Any, Optional

import aiohttp

from .. import loader, utils

logger = logging.getLogger(__name__)

MODNAME = "betonomeshalka"
API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.8-flash"
MAX_INLINE = 20 * 1024 * 1024  # лимит inline-данных Gemini API
MAX_TEXT_DOC = 100_000  # символы для текстовых файлов
MAX_REPLY_CTX = 8_000  # символы текста сообщения-ответа
ANSWER_LIMIT = 3_700  # ответ Telegram на сообщение — 4096, оставляем запас
REQUEST_TIMEOUT = 300
POLL_TIMEOUT = 240

# Уровень «раздумий» Gemini 3 (thinking_level). По умолчанию модель думает на
# high — отсюда и задержки. medium — баланс; ниже = быстрее.
DEFAULT_THINK = "medium"
THINK_LEVELS = ("minimal", "low", "medium", "high")
# Модели, ответившие «Thinking level is not supported» (gemma и пр.) — им уровень
# не шлём вовсе. Кэш живёт до перезагрузки модуля.
THINK_UNSUPPORTED = set()
MAX_NOTES = 200          # потолок числа заметок
MAX_NOTE_LEN = 4_000     # потолок длины одной заметки

# ---------- агент-режим (function-calling) ----------
MAX_AGENT_DELETE = 10    # максимум сообщений, удаляемых за один вызов инструмента
MAX_AGENT_ROUNDS = 5     # потолок раундов requires_action↔function_result
DEFAULT_AGENT_WINDOW = 12  # сколько недавних сообщений показывать модели
AGENT_WINDOW_CAP = 50
EDIT_MARKER = "✏️ "      # пометка в начале сообщения, отредактированного агентом
AGENT_TTL = 600          # сколько секунд ждать подтверждения действий
AGENT_CAUTION = (
    "У тебя есть инструменты для действий в чате (реакция/удаление/редактирование). "
    "Используй их ТОЛЬКО когда пользователь явно просит совершить действие. "
    "На обычный вопрос просто отвечай текстом, без инструментов. "
    "Действуй только над сообщениями из списка «Недавние сообщения» (их id).")
# Стандартные emoji-реакции Telegram (без variation selector U+FE0F).
REACTION_WHITELIST = {
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢", "🎉", "🤩",
    "🤮", "💩", "🙏", "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭",
    "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
    "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨", "🤝", "✍", "🤗",
    "🎄", "🎅", "☃", "💪", "🤪", "🗿", "🥇", "💅",
}

# Объявления инструментов для Interactions API (flat REST-форма: type=function).
AGENT_TOOLS = [
    {
        "type": "function",
        "name": "set_reaction",
        "description": "Поставить одну emoji-реакцию на сообщение в текущем чате "
                       "(например оценить мем). message_id — из списка недавних сообщений.",
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {"type": "integer",
                               "description": "ID сообщения из окна недавних"},
                "emoji": {"type": "string",
                          "description": "Стандартная emoji-реакция Telegram: ❤ 👍 😂 🔥 🎉 и т.п."},
            },
            "required": ["message_id", "emoji"],
        },
    },
    {
        "type": "function",
        "name": "delete_messages",
        "description": "Удалить сообщения в текущем чате (можно несколько, до 10). "
                       "Только когда пользователь явно просит удалить. Все id — из окна недавних.",
        "parameters": {
            "type": "object",
            "properties": {
                "message_ids": {"type": "array", "items": {"type": "integer"},
                                "description": "Список ID сообщений из окна недавних (до 10)"},
            },
            "required": ["message_ids"],
        },
    },
    {
        "type": "function",
        "name": "edit_message",
        "description": "Изменить текст СВОЕГО (исходящего, outgoing=true) сообщения "
                       "в текущем чате. message_id — из окна недавних.",
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {"type": "integer",
                               "description": "ID своего сообщения из окна недавних"},
                "new_text": {"type": "string", "description": "Новый текст сообщения"},
            },
            "required": ["message_id", "new_text"],
        },
    },
]

IMAGE_MIMES = {
    "image/png", "image/jpeg", "image/webp", "image/heic", "image/heif",
    "image/gif", "image/bmp", "image/tiff",
}
AUDIO_MIMES = {
    "audio/wav", "audio/mp3", "audio/aiff", "audio/aac", "audio/ogg",
    "audio/flac", "audio/mpeg", "audio/m4a", "audio/l16", "audio/opus",
    "audio/alaw", "audio/mulaw", "audio/webm",
}
VIDEO_MIMES = {
    "video/mp4", "video/mpeg", "video/mpg", "video/mov", "video/avi",
    "video/x-flv", "video/webm", "video/wmv", "video/3gpp",
}
DOC_MIMES = {"application/pdf", "text/csv"}
TEXT_EXT_MIME = {
    ".txt": "text/plain", ".md": "text/plain", ".py": "text/plain",
    ".js": "text/plain", ".ts": "text/plain", ".json": "text/plain",
    ".html": "text/plain", ".css": "text/plain", ".log": "text/plain",
    ".cfg": "text/plain", ".ini": "text/plain", ".yml": "text/plain",
    ".yaml": "text/plain", ".xml": "text/plain", ".sh": "text/plain",
    ".c": "text/plain", ".cpp": "text/plain", ".h": "text/plain",
    ".java": "text/plain", ".cs": "text/plain", ".go": "text/plain",
    ".rs": "text/plain", ".php": "text/plain", ".rb": "text/plain",
}

SEP = "──────────"

DEFAULT_QUERY = {
    "photo": "Что изображено на этом фото? Опиши кратко.",
    "video": "Опиши это видео кратко.",
    "audio": "Расшифруй это аудиосообщение дословно.",
    "document": "Что это за файл и о чём он?",
}

# «3.7» / «3.8-flash» → «gemini-3.7-flash» и т.п. Полные имена не трогаем.
_MODEL_BARE = re.compile(r"^\d+(?:\.\d+)?(?:-[a-z0-9]+)?$", re.I)
# 503 «high demand» / 429 «quota» = ключ валиден (авторизация прошла), модель просто занята.
_TRANSIENT_WORDS = (
    "high demand", "quota", "rate limit", "rate-limit", "too many requests",
    "overloaded", "unavailable", "try again later", "capacity", "temporarily",
    "resource_exhausted", "resource exhausted",
)


def _as_dict(data) -> dict:
    """Google изредка отдаёт JSON-массив вместо объекта — .get на нём валит модуль."""
    return data if isinstance(data, dict) else {}


def normalize_model(name: str) -> str:
    n = (name or "").strip()
    if not n or n.lower().startswith("gemini") or n.startswith("models/"):
        return n
    if _MODEL_BARE.match(n):
        if "-" not in n:
            n += "-flash"
        return "gemini-" + n
    return n


def friendly_error(status: int, msg: str) -> str:
    """Добавляет RU-подсказку к временным ошибкам (перегруз/лимит).

    Текст БЕЗ HTML-тегов: показ идёт через utils.escape_html, который экранирует
    любой <...>, поэтому <code> здесь превратился бы в видимый «&lt;code&gt;».
    """
    blob = f"{status} {msg}".lower()
    if status in (429, 500, 503, 504) or any(w in blob for w in _TRANSIENT_WORDS):
        return (f"{msg}\n\n⚠️ Модель перегружена или исчерпан лимит запросов — это "
                f"временно, ключ тут ни при чём. Попробуй позже или смени модель: "
                f".gmodel <имя>.")
    return msg


# ---------- markdown/latex → telegram html ----------
# Gemini отвечает Markdown'ом и LaTeX ($\text{CaO}$). Telegram у юзербота рендерит
# HTML (<b>,<i>,<code>,<pre>,<a>,<s>) — превращаем разметку и простые формулы в него,
# чтобы в сообщении не торчали звёздочки и \text{}.

_SUB = str.maketrans("0123456789+-()=", "₀₁₂₃₄₅₆₇₈₉₊₋₍₎₌")
_SUP = str.maketrans("0123456789+-()=ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁽⁾⁼ⁿⁱ")
_LATEX = {
    "\\rightarrow": "→", "\\to": "→", "\\leftarrow": "←", "\\leftrightarrow": "↔",
    "\\Rightarrow": "⇒", "\\cdot": "·", "\\times": "×", "\\div": "÷", "\\pm": "±",
    "\\approx": "≈", "\\neq": "≠", "\\leq": "≤", "\\geq": "≥", "\\infty": "∞",
    "\\degree": "°", "\\alpha": "α", "\\beta": "β", "\\gamma": "γ", "\\delta": "δ",
    "\\Delta": "Δ", "\\mu": "μ", "\\lambda": "λ", "\\pi": "π", "\\sigma": "σ",
    "\\omega": "ω", "\\theta": "θ", "\\rho": "ρ", "\\phi": "φ", "\\sum": "Σ",
    "\\quad": " ", "\\qquad": "  ", "\\,": " ", "\\;": " ", "\\!": "", "\\ ": " ",
}


def latex_to_text(s: str) -> str:
    s = re.sub(r"\\(?:text|mathrm|mathbf|mathit|mathsf|mbox|operatorname)\s*\{([^{}]*)\}",
               r"\1", s)
    for k, v in _LATEX.items():
        s = s.replace(k, v)
    s = re.sub(r"_\{([^{}]*)\}", lambda m: m.group(1).translate(_SUB), s)
    s = re.sub(r"\^\{([^{}]*)\}", lambda m: m.group(1).translate(_SUP), s)
    s = re.sub(r"_(\S)", lambda m: m.group(1).translate(_SUB), s)
    s = re.sub(r"\^(\S)", lambda m: m.group(1).translate(_SUP), s)
    s = s.replace("\\left", "").replace("\\right", "")
    s = re.sub(r"\\[a-zA-Z]+", "", s)
    return s.replace("{", "").replace("}", "").replace("\\", "").strip()


_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")


def _split_row(line: str) -> list:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _clean_cell(c: str) -> str:
    c = re.sub(r"\*\*(.+?)\*\*", r"\1", c)
    c = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", c)
    c = re.sub(r"~~(.+?)~~", r"\1", c)
    return c.replace("`", "").strip()


def _row_aligns(sep_cells: list) -> list:
    aligns = []
    for s in sep_cells:
        s = s.strip()
        left, right = s.startswith(":"), s.endswith(":")
        aligns.append("^" if left and right else ">" if right else "<")
    return aligns


def _render_table(header: list, aligns: list, rows: list) -> str:
    """Строки таблицы → выровненный по колонкам моноширинный текст."""
    ncol = len(header)
    aligns = (aligns + ["<"] * ncol)[:ncol]
    header = [_clean_cell(c) for c in header]
    rows = [[_clean_cell(c) for c in (r + [""] * ncol)[:ncol]] for r in rows]
    widths = [len(h) for h in header]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells):
        parts = []
        for i, cell in enumerate(cells):
            a, w = aligns[i], widths[i]
            parts.append(cell.rjust(w) if a == ">" else cell.center(w) if a == "^" else cell.ljust(w))
        return " | ".join(parts)

    out = [fmt(header), "-+-".join("-" * w for w in widths)]
    out += [fmt(r) for r in rows]
    return "\n".join(out)


def _extract_tables(text: str, blocks: list) -> str:
    """Markdown-таблицы → placeholder блока (в конце станет <pre>). Telegram
    не рендерит | --- |, поэтому ровняем таблицу сами моноширинным текстом."""
    lines = text.split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        if "|" in lines[i] and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            header = _split_row(lines[i])
            if len(header) >= 2:
                aligns = _row_aligns(_split_row(lines[i + 1]))
                rows, j = [], i + 2
                while j < n and lines[j].strip() and "|" in lines[j]:
                    rows.append(_split_row(lines[j]))
                    j += 1
                blocks.append(_render_table(header, aligns, rows))
                out.append(f"\x00B{len(blocks)-1}\x00")
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def md_to_tg_html(text: str) -> str:
    """Markdown+LaTeX → Telegram-HTML. Нераспознанное экранируется."""
    if not text:
        return text
    blocks, inlines, links = [], [], []
    text = re.sub(r"```[^\n]*\n?(.*?)```",
                  lambda m: (blocks.append(m.group(1)), f"\x00B{len(blocks)-1}\x00")[1],
                  text, flags=re.S)
    text = _extract_tables(text, blocks)
    text = re.sub(r"`([^`\n]+)`",
                  lambda m: (inlines.append(m.group(1)), f"\x00I{len(inlines)-1}\x00")[1],
                  text)
    # ссылки прячем в плейсхолдеры ДО emphasis-регулярок: иначе __/~~/*/_ внутри
    # URL (…/__init__.html, site/~user) ломают HTML, а «"» в URL рвёт href.
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
                  lambda m: (links.append((m.group(1), m.group(2))),
                             f"\x00L{len(links)-1}\x00")[1],
                  text)
    text = re.sub(r"\$\$(.+?)\$\$", lambda m: latex_to_text(m.group(1)), text, flags=re.S)
    text = re.sub(r"\$([^$\n]+?)\$", lambda m: latex_to_text(m.group(1)), text)
    text = html.escape(text, quote=False)
    text = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t#]*$", r"<b>\1</b>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_(?!_)(.+?)(?<![\w_])_(?![\w_])", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.S)
    text = re.sub(r"(?m)^([ \t]*)[*+\-][ \t]+", r"\1• ", text)
    text = re.sub(r"\x00I(\d+)\x00",
                  lambda m: "<code>" + html.escape(inlines[int(m.group(1))], quote=False) + "</code>",
                  text)
    text = re.sub(r"\x00L(\d+)\x00",
                  lambda m: '<a href="' + html.escape(links[int(m.group(1))][1], quote=True) + '">'
                            + html.escape(links[int(m.group(1))][0], quote=False) + "</a>",
                  text)
    text = re.sub(r"\x00B(\d+)\x00",
                  lambda m: "<pre><code>" + html.escape(blocks[int(m.group(1))], quote=False) + "</code></pre>",
                  text)
    return text


class GeminiError(Exception):
    """Ошибка запроса к Gemini API — текст уходит юзеру в отредактированное сообщение."""


@loader.tds
class Betonomeshalka(loader.Module):
    """Ask Gemini from any message: text, photos, voice notes, videos, documents"""

    strings = {"name": MODNAME}

    async def client_ready(self, client, db):
        self.client = client
        self.db = db
        self._session: Optional[aiohttp.ClientSession] = None
        self._agent_pending: dict = {}

    async def on_unload(self):
        session = getattr(self, "_session", None)
        if session:
            await session.close()
            self._session = None

    # ---------- db ----------

    def _get(self, key: str, default: Any = None) -> Any:
        try:
            return self.db.get(MODNAME, key, default)
        except Exception:
            return default

    def _set(self, key: str, value: Any):
        try:
            self.db.set(MODNAME, key, value)
        except Exception:
            logger.exception("[betonomeshalka] db.set failed for %s", key)

    # ---------- http ----------

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            )
        return self._session

    def _api_key(self) -> str:
        key = (self._get("api_key", "") or "").strip()
        if key:
            return key
        return (os.environ.get("GEMINI_API_KEY", "") or "").strip()

    # ---------- prompts ----------

    def _prompts(self) -> dict:
        prompts = self._get("prompts", {})
        return prompts if isinstance(prompts, dict) else {}

    def _system_instruction(self) -> str:
        active = (self._get("active_prompt", "") or "").strip()
        prompts = self._prompts()
        if active and active in prompts:
            return prompts[active]
        return (self._get("prompt", "") or "").strip()

    def _model(self) -> str:
        return normalize_model(self._get("model", "") or "") or DEFAULT_MODEL

    def _think(self) -> str:
        lvl = (self._get("think", "") or "").strip().lower()
        return lvl if lvl in THINK_LEVELS else DEFAULT_THINK

    # ---------- notes ----------

    def _notes(self) -> list:
        notes = self._get("notes", [])
        return notes if isinstance(notes, list) else []

    def _set_notes(self, notes: list):
        self._set("notes", notes)

    def _add_note(self, text: str) -> int:
        notes = self._notes()
        notes.append(text[:MAX_NOTE_LEN])
        if len(notes) > MAX_NOTES:
            notes = notes[-MAX_NOTES:]
        self._set_notes(notes)
        return len(notes)

    def _del_note(self, num: int) -> Optional[str]:
        """Удаляет заметку по 1-based номеру. Возвращает удалённый текст или None."""
        notes = self._notes()
        if not (1 <= num <= len(notes)):
            return None
        removed = notes.pop(num - 1)
        self._set_notes(notes)
        return removed

    def _notes_block(self, indices) -> str:
        """Текстовый блок выбранных заметок. indices — список 1-based номеров."""
        notes = self._notes()
        picked = []
        for i in indices:
            if 1 <= i <= len(notes):
                picked.append(f"{i}. {notes[i - 1]}")
        if not picked:
            return ""
        return "Мои заметки (используй их как контекст):\n" + "\n".join(picked)

    # ---------- media ----------

    @staticmethod
    def _media_kind(msg) -> Optional[str]:
        if getattr(msg, "photo", None):
            return "photo"
        if getattr(msg, "sticker", None):
            return "sticker"
        if getattr(msg, "voice", None):
            return "voice"
        if getattr(msg, "audio", None):
            return "audio"
        if getattr(msg, "video_note", None):
            return "video"
        if getattr(msg, "video", None):
            return "video"
        if getattr(msg, "gif", None):
            return "video"
        if getattr(msg, "document", None):
            return "document"
        return None

    @staticmethod
    def _mime(msg, fallback: str) -> str:
        f = getattr(msg, "file", None)
        m = getattr(f, "mime_type", None) if f is not None else None
        return m or fallback

    @staticmethod
    def _file_name(msg) -> str:
        f = getattr(msg, "file", None)
        return (getattr(f, "name", None) or "file").strip()

    async def _media_parts(self, msg) -> tuple:
        """Возвращает (parts, error). parts — список контент-блоков для input."""
        kind = self._media_kind(msg)
        if kind is None:
            return [], None

        mime = {
            "photo": lambda: "image/jpeg",
            "sticker": lambda: self._mime(msg, "image/webp"),
            "voice": lambda: "audio/ogg",
            "audio": lambda: self._mime(msg, "audio/mpeg"),
            "video": lambda: self._mime(msg, "video/mp4"),
        }.get(kind, lambda: self._mime(msg, ""))()

        # Проверяем размер ДО download_media: иначе гигантский файл (500 МБ – 2 ГБ)
        # целиком выкачается в RAM и убьёт процесс по OOM.
        fsize = getattr(getattr(msg, "file", None), "size", 0) or 0
        if fsize > MAX_INLINE:
            return [], "файл больше 20 МБ — Gemini такое инлайн не принимает"

        if kind == "document":
            if mime in DOC_MIMES:
                pass  # pdf/csv уходят как document-блок
            elif mime in IMAGE_MIMES:
                kind = "photo"
            elif mime in AUDIO_MIMES:
                kind = "audio"
            elif mime in VIDEO_MIMES:
                kind = "video"
            else:
                name = self._file_name(msg)
                ext = ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""
                if mime.startswith("text/") or ext in TEXT_EXT_MIME:
                    try:
                        raw = await msg.download_media(bytes)
                    except Exception as exc:
                        logger.error("[betonomeshalka] download failed: %r", exc)
                        return [], "не смог скачать файл"
                    text = raw[:MAX_TEXT_DOC * 2].decode("utf-8", errors="replace")
                    cut = "\n…(обрезано)" if len(text) > MAX_TEXT_DOC else ""
                    return [{"type": "text",
                             "text": f"Содержимое файла {name}:\n{text[:MAX_TEXT_DOC]}{cut}"}], None
                return [], f"тип файла не поддерживается ({mime or 'неизвестно'})"

        if kind == "photo" and mime not in IMAGE_MIMES:
            mime = "image/jpeg"
        if kind == "audio" and mime not in AUDIO_MIMES:
            mime = "audio/mpeg"
        if kind == "video" and mime not in VIDEO_MIMES:
            return [], "формат видео не поддерживается Gemini"
        if kind == "sticker" and mime not in IMAGE_MIMES:
            return [], "анимированные стикеры не поддерживаются (только статичные)"

        try:
            data = await msg.download_media(bytes)
        except Exception as exc:
            logger.error("[betonomeshalka] download failed: %r", exc)
            return [], "не смог скачать медиа"
        if not data:
            return [], "медиа пустое"
        if len(data) > MAX_INLINE:
            return [], "файл больше 20 МБ — Gemini такое инлайн не принимает"

        block_type = {"photo": "image", "sticker": "image",
                      "voice": "audio", "audio": "audio",
                      "video": "video", "document": "document"}[kind]
        return [{"type": block_type, "data": base64.b64encode(data).decode(),
                 "mime_type": mime}], None

    async def _album_media(self, msg):
        """Все медиа одного альбома (grouped_id) в хронологическом порядке.

        Возвращает список сообщений; на ошибке — [msg] (одиночное медиа).
        """
        gid = getattr(msg, "grouped_id", None)
        if not gid:
            return [msg]
        try:
            msgs = await self.client.get_messages(
                msg.peer_id, limit=20,
                min_id=max(0, msg.id - 10), max_id=msg.id + 10)
            same = [m for m in msgs
                    if getattr(m, "grouped_id", None) == gid and self._media_kind(m)]
            if not same:
                return [msg]
            same.reverse()  # get_messages отдаёт свежие первыми → разворачиваем
            return same
        except Exception as exc:
            logger.error("[betonomeshalka] album gather failed: %r", exc)
            return [msg]

    # ---------- api ----------

    @staticmethod
    def _extract_text(data: dict) -> str:
        chunks = []
        for step in data.get("steps", []) or []:
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            for block in step.get("content", []) or []:
                if block.get("type") == "text" and block.get("text"):
                    chunks.append(block["text"])
        return "\n".join(chunks).strip()

    @staticmethod
    def _extract_calls(data: dict) -> list:
        """function_call-шаги ответа: [{name, arguments, id}] (для агент-режима)."""
        calls = []
        for step in data.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            fc = step if step.get("type") == "function_call" else step.get("function_call")
            if isinstance(fc, dict) and (fc.get("name") or fc.get("arguments")):
                calls.append({
                    "name": fc.get("name", ""),
                    "arguments": fc.get("arguments") or {},
                    "id": fc.get("id", "") or step.get("id", ""),
                })
        return calls

    async def _post(self, url: str, key: str, body: Optional[dict] = None) -> dict:
        session = await self._http()
        headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
        last_data: dict = {}
        last_status = 0
        for attempt in (1, 2):
            async with session.post(url, json=body, headers=headers) as resp:
                last_status = resp.status
                try:
                    last_data = await resp.json()
                except Exception:
                    last_data = {}
                if resp.status in (429, 500, 503, 504) and attempt == 1:
                    try:
                        retry = int(resp.headers.get("Retry-After", "5"))
                    except ValueError:
                        retry = 5
                    logger.warning("[betonomeshalka] %s, retry in %ss", resp.status, retry)
                    await asyncio.sleep(min(max(retry, 1), 30))
                    continue
                if resp.status != 200:
                    err = (_as_dict(_as_dict(last_data).get("error")).get("message")
                           or f"HTTP {resp.status}")
                    raise GeminiError(friendly_error(resp.status, err))
                return last_data
        err = (_as_dict(_as_dict(last_data).get("error")).get("message")
               or f"HTTP {last_status or 429}")
        raise GeminiError(friendly_error(last_status or 429, err))

    def _debug_on(self) -> bool:
        return bool(self._get("debug", False))

    @staticmethod
    def _describe_body(body: dict) -> str:
        """Человеческий дамп запроса для .gdebug: без ключей, текст обрезан."""
        inp = []
        for p in body.get("input") or []:
            t = p.get("type")
            if t == "text":
                s = p.get("text", "")
                inp.append(f"text[{len(s)}]:{s[:80]!r}")
            elif t == "function_result":
                r = str(p.get("result", ""))
                inp.append(f"result[{p.get('name')}]:{r[:60]!r}")
            else:
                inp.append(f"{t}[{len(p.get('data', '')) // 1024}КБ base64]")
        gen = body.get("generation_config") or {}
        tools = body.get("tools") or []
        return (f"model={body.get('model')} thinking={gen.get('thinking_level', 'НЕТ')}\n"
                f"store={body.get('store')} prev={body.get('previous_interaction_id') or 'НЕТ'} "
                f"sys_instr={len(body.get('system_instruction') or '')} симв"
                + (f", tools={len(tools)}" if tools else "") + "\n"
                f"input: {'; '.join(inp) or 'пусто'}")

    @staticmethod
    def _render_debug(dbg: dict) -> str:
        lines = ["🐞 debug (gemini)", "─ запрос ─", dbg["req"], "─ попытки ─"]
        lines += [f"• {a}" for a in dbg["attempts"]]
        return "\n".join(lines)

    async def _post_and_poll(self, body: dict, key: str, dbg=None) -> dict:
        """POST /interactions + опрос до терминального статуса.

        Возвращает data. Терминальные статусы: completed / requires_action
        (агент) / failed (поднимается как GeminiError). Ретраи на «thinking level
        is not supported» и «invalid argument previous_interaction_id» мутируют
        body и повторяют запрос — цикл конечен.
        """
        model = (body.get("model") or "").lower()
        headers = {"x-goog-api-key": key}
        while True:
            try:
                t1 = time.monotonic()
                data = _as_dict(await self._post(f"{API_BASE}/interactions", key, body))
                t_post = time.monotonic() - t1
                status = data.get("status", "")
                iid = data.get("id", "")
                waited = 0
                while status in ("in_progress", "queued") and iid and waited < POLL_TIMEOUT:
                    await asyncio.sleep(3)
                    waited += 3
                    session = await self._http()
                    try:
                        async with session.get(f"{API_BASE}/interactions/{iid}",
                                               headers=headers) as resp:
                            if resp.status != 200:
                                raise GeminiError(friendly_error(
                                    resp.status, f"HTTP {resp.status} при опросе статуса"))
                            data = _as_dict(await resp.json())
                    except GeminiError:
                        raise
                    except Exception as exc:
                        # 502/503 от балансировщика приходит HTML-ом → resp.json()
                        # валится ContentTypeError. Прерываем опрос, не крутим 240с.
                        logger.warning("[betonomeshalka] poll error: %r", exc)
                        raise GeminiError(f"ошибка опроса статуса: {exc}")
                    new_status = data.get("status")
                    if new_status:
                        status = new_status
                    elif data.get("error"):
                        err = (_as_dict(data.get("error")).get("message")
                               or "ошибка API при опросе")
                        raise GeminiError(friendly_error(0, err))

                if status == "failed":
                    errors = data.get("errors") or []
                    msg = (errors[0].get("message")
                           if errors and isinstance(errors[0], dict) else "") \
                        or "запрос не удался"
                    raise GeminiError(friendly_error(0, msg))

                if dbg is not None:
                    usage = data.get("usage") or data.get("usage_metadata") or {}
                    dbg["attempts"].append(
                        f"HTTP 200 за {t_post:.1f}с (id={iid or '-'}, "
                        f"статус сразу: {status or '-'})\n"
                        f"опрос до «{data.get('status', '?')}»: {waited}с\n"
                        f"ответ: {len(self._extract_text(data))} симв"
                        + (f", токены: {usage}" if usage else ""))
                return data
            except GeminiError as exc:
                msg = str(exc).lower()
                # каждый ретрай удаляет из body то, на что ругнулись, — цикл конечен
                if "thinking level is not supported" in msg and "generation_config" in body:
                    THINK_UNSUPPORTED.add(model)
                    del body["generation_config"]
                    logger.warning("[betonomeshalka] %s: thinking_level не поддержан — "
                                   "повторяю без него", model)
                    continue
                if "invalid argument" in msg and "previous_interaction_id" in body:
                    body.pop("previous_interaction_id")
                    logger.warning("[betonomeshalka] %s: previous_interaction_id отброшен",
                                   model)
                    continue
                if dbg is not None:
                    dbg["attempts"].append(f"ошибка: {str(exc)[:200]}")
                raise

    async def _ask(self, parts: list, ctx_key: str, key: str) -> str:
        model = self._model()
        body: dict = {"model": model, "input": parts}
        # thinking_level понимают только Gemini 3; gemma и компания отвечают ошибкой
        if model.lower() not in THINK_UNSUPPORTED:
            body["generation_config"] = {"thinking_level": self._think()}
        si = self._system_instruction()
        if si:
            body["system_instruction"] = si
        ctx_on = bool(self._get("ctx", False))
        body["store"] = ctx_on  # без контекста запросы не хранятся на сервере
        if ctx_on:
            # цепочка хранится как «модель|id»: чужой interaction_id Google бракует
            saved = self._get(f"ctx_{ctx_key}", "") or ""
            m, _, prev = saved.partition("|")
            if prev and m == model:
                body["previous_interaction_id"] = prev

        dbg = ({"req": self._describe_body(body), "attempts": []}
               if self._debug_on() else None)
        t0 = time.monotonic()
        try:
            data = await self._post_and_poll(body, key, dbg)
        except GeminiError as exc:
            if dbg is not None:
                dbg["attempts"].append(f"ИТОГ: ошибка через {time.monotonic() - t0:.1f}с")
                raise GeminiError(f"{exc}\n\n{self._render_debug(dbg)}")
            raise

        text = self._extract_text(data)
        if not text:
            raise GeminiError("Gemini вернул пустой ответ (возможно, сработал фильтр)")
        iid = data.get("id", "")
        if ctx_on and iid:
            self._set(f"ctx_{ctx_key}", f"{model}|{iid}")
        if dbg is not None:
            dbg["attempts"].append(f"ИТОГ: {time.monotonic() - t0:.1f}с")
            block = self._render_debug(dbg)
            # сначала урезаем ответ, чтобы дамп гарантированно влез в сообщение
            text = self._fit(text, max(500, ANSWER_LIMIT - len(block) - 100)) + "\n\n" + block
        return text

    # ---------- helpers ----------

    @staticmethod
    def _fit(text: str, limit: int = ANSWER_LIMIT) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n…(обрезано, всего {len(text)} симв.)"

    @staticmethod
    def _fit_converted(answer: str, prefix: str, limit: int = 4096) -> str:
        """md_to_tg_html(answer) так, чтобы prefix+результат влез в limit.

        Режем ИСХОДНЫЙ markdown и конвертим заново — так не рвётся HTML и не
        остаётся незакрытых ``` (обрезка готового HTML ломала бы теги/сущности).
        """
        room = limit - len(prefix)
        if room <= 0:
            return ""
        text = answer
        for _ in range(6):
            conv = md_to_tg_html(text)
            if len(conv) <= room:
                return conv
            cut = int(len(text) * (room / max(1, len(conv)))) - 40
            text = text[:max(1, cut)]
        return md_to_tg_html(text)[:room]

    @staticmethod
    def _mask(key: str) -> str:
        if len(key) <= 12:
            return key[:3] + "…"
        return key[:8] + "…" + key[-4:]

    # ---------- доставка rich-ответа через heroku-бота (инлайн-бот юзербота) ----------

    def _has_table(self, text: str) -> bool:
        """Есть ли в ответе markdown-таблица (её бот отрендерит нативно)."""
        lines = text.splitlines()
        for i in range(len(lines) - 1):
            if lines[i].lstrip().startswith("|") and _TABLE_SEP.match(lines[i + 1]):
                return True
        return False

    def _rich_worthy(self, text: str) -> bool:
        """Стоит ли ответ доставлять rich-ом: таблицы/код-блоки/заголовки."""
        if self._has_table(text) or "```" in text:
            return True
        return any(re.match(r"^#{1,6}\s", ln) for ln in text.splitlines())

    def _heroku_bot(self) -> tuple:
        """(токен, @username) инлайн-бота юзербота — того, что «via @…» у форм.

        Токен Hikka держит в БД («heroku.inline»/«bot_token»), дублируем доступ
        через self.inline._token на случай другой структуры.
        """
        inline = getattr(self, "inline", None)
        token = (getattr(inline, "_token", "") or "").strip()
        uname = (getattr(inline, "bot_username", "") or "").strip()
        if not token:
            token = (self.db.get("heroku.inline", "bot_token", "") or "").strip()
        return token, uname

    async def _rich_deliver(self, message, md: str) -> tuple:
        """Шлёт ответ rich-сообщением ОТ heroku-бота (sendRichMessage, Bot API 10.1+).

        Нативные таблицы/маркдаун возможны только в сообщении бота: у MTProto
        rich-блоков нет. Бот должен быть участником чата, иначе Телега ответит
        ошибкой — тогда вызывающий код показывает обычный <pre>-ответ.
        """
        token, uname = self._heroku_bot()
        if not token:
            return False, "нет токена инлайн-бота"
        payload = {
            "chat_id": message.chat_id,
            "rich_message": {"markdown": md},
            "reply_parameters": {"message_id": message.id},
        }
        thread = getattr(message, "message_thread_id", None)
        if thread:
            payload["message_thread_id"] = thread
        try:
            session = await self._http()
            async with session.post(
                    f"https://api.telegram.org/bot{token}/sendRichMessage",
                    json=payload) as resp:
                data = await resp.json()
        except Exception as exc:
            return False, str(exc)
        if isinstance(data, dict) and data.get("ok"):
            return True, uname or "бот"
        desc = (data or {}).get("description") if isinstance(data, dict) else ""
        return False, desc or "sendRichMessage не удался"

    # ---------- агент-инструменты ----------

    async def _recent_window(self, message, extra=()):
        """Окно недавних сообщений: (текст для модели, {id: сообщение}).

        Модель видит только эти id — значит не может удалить или поправить то,
        чего нет в списке. `extra` — сообщения, важные для запроса (то, на что
        ответили), даже если они старше окна.
        """
        size = self._get("agent_window", DEFAULT_AGENT_WINDOW)
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = DEFAULT_AGENT_WINDOW
        size = min(AGENT_WINDOW_CAP, max(1, size))
        try:
            fetched = await self.client.get_messages(message.peer_id, limit=size)
        except Exception:
            logger.exception("[betonomeshalka] window fetch failed")
            fetched = []
        by_id = {}
        for m in list(extra) + list(fetched or []):
            # команду не показываем: снесёт «.gemini удали это» — и отвечать будет
            # некуда (message.edit по удалённому = MessageIdInvalidError)
            if m is not None and m.id != message.id:
                by_id[m.id] = m
        lines = []
        for mid in sorted(by_id):
            m = by_id[mid]
            body = " ".join((m.raw_text or "").split())[:120]
            if not body:
                kind = self._media_kind(m)
                body = f"[{kind or 'без текста'}]"
            lines.append(
                f"#{mid} | {'я' if getattr(m, 'out', False) else 'друг'} | {body}"
            )
        return "\n".join(lines), by_id

    @staticmethod
    def _as_id(value):
        """id сообщения из arguments: модель иногда шлёт число строкой."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    async def _tool_react(self, peer, msg_id, emoji, allowed) -> str:
        mid = self._as_id(msg_id)
        if mid is None or mid not in allowed:
            return (f"отклонено: id {msg_id} недоступен, можно только "
                    f"{sorted(allowed)}")
        em = (emoji or "").replace("️", "").strip()
        if em not in REACTION_WHITELIST:
            return f"отклонено: реакция «{emoji}» не поддерживается Telegram"
        try:
            from telethon.tl.functions.messages import SendReactionRequest
            from telethon.tl.types import ReactionEmoji
            await self.client(SendReactionRequest(
                peer=peer, msg_id=mid, reaction=[ReactionEmoji(emoticon=em)]))
        except Exception as exc:
            return f"ошибка реакции: {type(exc).__name__}: {exc}"
        return f"реакция {em} поставлена на #{mid}"

    async def _still_there(self, peer, ids) -> list:
        """Какие id остались после удаления.

        Telegram может ответить ok и снести сообщение только со своей стороны
        (чужие сообщения в личке, нет прав на revoke в группе) — без этой проверки
        мы врать будем, что удалили.
        """
        try:
            got = await self.client.get_messages(peer, ids=ids)
        except Exception:
            logger.exception("[betonomeshalka] delete verify failed")
            return []
        msgs = got if isinstance(got, (list, tuple)) else [got]
        return [mid for mid, m in zip(sorted(ids), msgs) if m is not None]

    async def _tool_delete(self, peer, ids, allowed, forbidden=()) -> str:
        ids = [i for i in (self._as_id(x) for x in (ids or [])) if i is not None]
        if not ids:
            return "отклонено: список id пуст"
        if len(ids) > MAX_AGENT_DELETE:
            return f"отклонено: максимум {MAX_AGENT_DELETE} сообщений за раз"
        touched = [i for i in ids if i in forbidden]
        if touched:
            # снесёт сообщение с командой — и ответ вписывать будет некуда
            return f"отклонено: id {touched} — это сообщение с запросом, его не удаляем"
        bad = [i for i in ids if i not in allowed]
        if bad:
            return (f"отклонено: id {bad} недоступны, можно только {sorted(allowed)}")
        try:
            await self.client.delete_messages(peer, ids, revoke=True)
        except Exception as exc:
            return f"ошибка удаления: {type(exc).__name__}: {exc}"
        left = await self._still_there(peer, ids)
        logger.info("[betonomeshalka] delete peer=%s ids=%s left=%s",
                    type(peer).__name__, ids, left)
        if left:
            return ("Telegram принял запрос, но "
                    + ", ".join(f"#{i}" for i in left)
                    + " остались в чате — они не мои или нет прав на revoke")
        return "удалено " + ", ".join(f"#{i}" for i in sorted(ids))

    async def _tool_edit(self, peer, msg_id, new_text, by_id) -> str:
        mid = self._as_id(msg_id)
        if mid is None or mid not in by_id:
            return (f"отклонено: id {msg_id} недоступен, можно только "
                    f"{sorted(by_id)}")
        if not getattr(by_id[mid], "out", False):
            return f"отклонено: #{mid} не моё сообщение, редактировать нельзя"
        try:
            await self.client.edit_message(
                peer, mid, EDIT_MARKER + (new_text or ""))
        except Exception as exc:
            return f"ошибка редактирования: {type(exc).__name__}: {exc}"
        return f"сообщение #{mid} отредактировано"

    async def _run_tool(self, call, peer, by_id, forbidden=()) -> str:
        """Выполнить один function_call модели, вернуть строку-результат."""
        name = call.get("name") or ""
        args = call.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        allowed = set(by_id)
        if name == "set_reaction":
            return await self._tool_react(peer, args.get("message_id"),
                                          args.get("emoji"), allowed)
        if name == "delete_messages":
            return await self._tool_delete(peer, args.get("message_ids"), allowed,
                                           set(forbidden))
        if name == "edit_message":
            return await self._tool_edit(peer, args.get("message_id"),
                                         args.get("new_text"), by_id)
        return f"отклонено: неизвестный инструмент {name}"

    @staticmethod
    def _describe_calls(calls: list) -> str:
        lines = []
        for c in calls:
            a = c.get("arguments") or {}
            if not isinstance(a, dict):
                a = {}
            name = c.get("name") or "?"
            if name == "delete_messages":
                ids = sorted(i for i in (Betonomeshalka._as_id(x)
                                         for x in (a.get("message_ids") or []))
                             if i is not None)
                lines.append(f"• удалить {', '.join(f'#{i}' for i in ids) or '—'}")
            elif name == "set_reaction":
                lines.append(
                    f"• реакция {a.get('emoji', '?')} на #{a.get('message_id', '?')}")
            elif name == "edit_message":
                txt = " ".join(str(a.get("new_text", "")).split())[:80]
                lines.append(f"• отредактировать #{a.get('message_id', '?')}: {txt}")
            else:
                lines.append(f"• {name} {a}")
        return "\n".join(lines)

    # ---------- агент-цикл ----------

    def _pending(self) -> dict:
        p = getattr(self, "_agent_pending", None)
        if p is None:
            p = self._agent_pending = {}
        return p

    def _resolve_approval(self, token: str, value: bool) -> bool:
        p = self._pending().get(token)
        if not p:
            return False
        fut = p["fut"]
        if not fut.done():
            fut.set_result(value)
        return True

    async def _await_approval(self, message, calls: list) -> Optional[bool]:
        """Гейт перед действиями: форма ✅/❌, без инлайн-бота — .gconfirm/.gcancel.

        True — разрешено, False — отказ, None — не дождались.
        """
        desc = self._describe_calls(calls)
        fut = asyncio.get_running_loop().create_future()
        token = f"{int(time.time() * 1000)}-{id(fut) % 1000000}"
        self._pending()[token] = {"fut": fut, "chat": message.chat_id}
        shown = False
        inline = getattr(self, "inline", None)
        if inline is not None:
            try:
                shown = bool(await inline.form(
                    f"🔧 Gemini предлагает выполнить:\n{desc}\n\nРазрешить?",
                    message,
                    reply_markup=[[
                        {"text": "✅ Выполнить", "callback": self._agent_approve,
                         "args": (token,)},
                        {"text": "❌ Отмена", "callback": self._agent_reject,
                         "args": (token,)},
                    ]],
                    force_me=True, ttl=AGENT_TTL))
            except Exception:
                logger.exception("[betonomeshalka] inline.form failed")
        if not shown:
            try:
                await message.respond(
                    "🔧 Gemini предлагает выполнить:\n"
                    + utils.escape_html(desc)
                    + "\n\n<i>Инлайн-бота нет: </i><code>.gconfirm</code>"
                      "<i> — выполнить, </i><code>.gcancel</code><i> — отмена"
                      f" (через {AGENT_TTL // 60} мин само отменится).</i>")
            except Exception:
                logger.exception("[betonomeshalka] approval prompt failed")
        try:
            return await asyncio.wait_for(fut, AGENT_TTL)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending().pop(token, None)

    async def _agent_approve(self, call, token: str):
        if not self._resolve_approval(token, True):
            await call.answer("Это подтверждение уже не активно")
            return
        await call.edit("✅ Подтверждено — выполняю.")
        await call.unload()

    async def _agent_reject(self, call, token: str):
        if not self._resolve_approval(token, False):
            await call.answer("Это подтверждение уже не активно")
            return
        await call.edit("❌ Отменено.")
        await call.unload()

    async def _ask_agent(self, message, parts: list, ctx_key: str, key: str,
                         win_text: str, by_id: dict) -> str:
        """Запрос с инструментами: requires_action → гейт → function_result → …"""
        model = self._model()
        body: dict = {
            "model": model, "store": True, "tools": AGENT_TOOLS,
            "input": [{"type": "text",
                       "text": "Недавние сообщения (только их id доступны "
                               f"инструментам):\n{win_text or '— пусто —'}"}] + parts,
        }
        if model.lower() not in THINK_UNSUPPORTED:
            body["generation_config"] = {"thinking_level": self._think()}
        si = self._system_instruction()
        sys_txt = f"{si}\n{AGENT_CAUTION}" if si else AGENT_CAUTION
        body["system_instruction"] = sys_txt
        ctx_on = bool(self._get("ctx", False))
        if ctx_on:
            saved = self._get(f"ctx_{ctx_key}", "") or ""
            m, _, prev = saved.partition("|")
            if prev and m == model:
                body["previous_interaction_id"] = prev

        dbg = None
        if self._debug_on():
            ids = sorted(by_id)
            dbg = {"req": (
                f"peer={type(message.peer_id).__name__} chat={message.chat_id} "
                f"msg={message.id} out={bool(getattr(message, 'out', False))} "
                f"thread={getattr(message, 'message_thread_id', None)}\n"
                f"окно({len(ids)}): {ids}\n" + self._describe_body(body)),
                "attempts": []}
        t0 = time.monotonic()
        actions: list = []
        try:
            for _ in range(MAX_AGENT_ROUNDS):
                data = await self._post_and_poll(body, key, dbg)
                calls = (self._extract_calls(data)
                         if data.get("status") == "requires_action" else [])
                if not calls:
                    break
                approved = await self._await_approval(message, calls)
                if approved is not True:
                    return ("⏳ Не дождался подтверждения — ничего не выполнено."
                            if approved is None else "❌ Отменено — ничего не выполнено.")
                results = []
                for c in calls:
                    res = await self._run_tool(c, message.peer_id, by_id, (message.id,))
                    actions.append(res)
                    results.append({"type": "function_result",
                                    "name": c.get("name", ""),
                                    "call_id": c.get("id", ""), "result": res})
                body = {"model": model, "previous_interaction_id": data.get("id", ""),
                        "input": results, "tools": AGENT_TOOLS, "store": True,
                        # nasledующие раунды тоже должны знать про ограничение окна
                        "system_instruction": sys_txt}
                if model.lower() not in THINK_UNSUPPORTED:
                    body["generation_config"] = {"thinking_level": self._think()}
                if dbg is not None:
                    dbg["attempts"].append("→ " + self._describe_body(body))
            else:
                actions.append("⚠️ достиг лимита раундов, дальше не действовал")
        except GeminiError as exc:
            if dbg is not None:
                dbg["attempts"].append(
                    f"ИТОГ: ошибка через {time.monotonic() - t0:.1f}с")
                raise GeminiError(f"{exc}\n\n{self._render_debug(dbg)}") from exc
            raise

        text = self._extract_text(data)
        iid = data.get("id", "")
        # незакрытый requires_action в ctx цепочку не кладём: следующий запрос
        # с таким previous_interaction_id Google бракует
        if ctx_on and iid and data.get("status") != "requires_action":
            self._set(f"ctx_{ctx_key}", f"{model}|{iid}")
        if actions:
            text = (text + "\n\n" if text else "") + "\n".join(
                f"🔧 {a}" for a in actions)
        if not text:
            raise GeminiError("Gemini вернул пустой ответ (возможно, сработал фильтр)")
        if dbg is not None:
            dbg["attempts"].append(f"ИТОГ: {time.monotonic() - t0:.1f}с")
            block = self._render_debug(dbg)
            text = self._fit(text, max(500, ANSWER_LIMIT - len(block) - 100)) + "\n\n" + block
        return text

    # ---------- commands ----------

    async def _run(self, message, args: str, notes_text: Optional[str] = None,
                   force_rich: bool = False):
        """Общее ядро запроса для .gemini и .gnote."""
        key = self._api_key()
        if not key:
            await utils.answer(
                message,
                "❌ <b>API-ключ не задан.</b>\n<code>.gkey &lt;ключ&gt;</code> — получить: "
                "aistudio.google.com → API keys (нужен VPN с поддерживаемого региона).",
            )
            return

        # медиа: либо в самом сообщении (подпись к фото), либо в том, на что отвечаем
        media_msg = message if self._media_kind(message) else None
        reply_text = ""
        reply = None
        if media_msg is None and getattr(message, "is_reply", False):
            reply = await message.get_reply_message()
            if reply is not None:
                if self._media_kind(reply):
                    media_msg = reply
                elif (reply.raw_text or "").strip():
                    reply_text = (reply.raw_text or "").strip()[:MAX_REPLY_CTX]

        # альбом: собираем все медиа одной группы, а не только отмеченное
        album = await self._album_media(media_msg) if media_msg is not None else []

        # Если команда была подписью к медиа, её текст — caption (лимит 1024),
        # и правка затрёт запрос. Поэтому статус/ответ пишем отдельным сообщением.
        has_media = self._media_kind(message) is not None
        status_box = {}

        async def show(text):
            """Статус/ответ. Правим своё сообщение; если правка невозможна (команда
            отправлена от имени канала, сообщение удалено, нет прав на edit) —
            заводим ОДНО отдельное сообщение и правим дальше его, а не плодим дубли.
            Никогда не бросает."""
            box = status_box.get("m")
            if box is not None:
                try:
                    await box.edit(text)
                except Exception:
                    logger.exception("[betonomeshalka] show: edit box failed")
                    status_box.pop("m")
                    await _send_box(text)
                return
            if not has_media and status_box.get("edit_ok", True) \
                    and getattr(message, "out", False):
                try:
                    await message.edit(text)
                    return
                except Exception as exc:
                    # удалённое/не моё/нет прав — пробуем один раз и больше не лезем
                    logger.warning("[betonomeshalka] show: edit команды недоступна: %s", exc)
                    status_box["edit_ok"] = False
            await _send_box(text)

        async def _send_box(text):
            # в форумах без топия ответ уедет в General
            thread = getattr(message, "message_thread_id", None)
            kw = {"parse_mode": "HTML"}
            if thread:
                kw["message_thread_id"] = thread
            try:
                status_box["m"] = await self.client.send_message(
                    message.peer_id, text, reply_to=message.id, **kw)
            except Exception:
                try:
                    status_box["m"] = await self.client.send_message(
                        message.peer_id, text, **kw)
                except Exception:
                    logger.exception("[betonomeshalka] show: send failed")

        parts = []
        kind = self._media_kind(album[0]) if album else None
        if album:
            if len(album) > 1:
                await show(f"📷 <i>Альбом получен: {len(album)} медиа — загружаю…</i>")
            else:
                await show("📥 <i>Медиа получено — загружаю…</i>")
            total = 0
            for m in album:
                media_parts, err = await self._media_parts(m)
                if err:
                    # именно show(), не utils.answer: иначе при .gemini в подписи
                    # отдельное статус-сообщение остаётся сиротой висеть в чате.
                    await show(f"❌ {utils.escape_html(err)}")
                    return
                total += sum(len(p.get("data", "")) for p in media_parts)
                if total > MAX_INLINE:  # base64-сумма всего альбома vs лимит запроса
                    await show("❌ Альбом суммарно больше 20 МБ — Gemini такое инлайн не принимает")
                    return
                parts.extend(media_parts)

        query = args
        if not query:
            if kind in ("photo", "sticker"):
                query = DEFAULT_QUERY["photo"]
            elif kind == "video":
                query = DEFAULT_QUERY["video"]
            elif kind in ("voice", "audio"):
                query = DEFAULT_QUERY["audio"]
            elif kind == "document":
                query = DEFAULT_QUERY["document"]
            elif reply_text:
                query = "Ответь на это сообщение."
            else:
                active = self._get("active_prompt", "") or "глобальный"
                model = self._model()
                if self._get("agent", False):
                    agent_disp = ("вкл, окно "
                                  + str(self._get("agent_window", DEFAULT_AGENT_WINDOW)))
                else:
                    agent_disp = "выкл"
                await utils.answer(
                    message,
                    f"🤖 <b>betonomeshalka</b>\n"
                    f"<b>Модель:</b> <code>{utils.escape_html(model)}</code>\n"
                    f"<b>Промпт:</b> <code>{utils.escape_html(str(active))}</code>\n"
                    f"<b>Раздумья:</b> <code>{self._think()}</code>\n"
                    f"<b>Ключ:</b> <code>{self._mask(key)}</code>\n"
                    f"<b>Контекст чата:</b> {'вкл' if self._get('ctx', False) else 'выкл'}\n"
                    f"<b>Агент-режим:</b> {agent_disp}\n\n"
                    f"<b>Использование:</b> <code>.gemini &lt;запрос&gt;</code> — текстом, "
                    f"ответом на фото/войс/видео/док или в подписи к фото.\n"
                    f"Ответ появится прямо в этом сообщении, под запросом.\n"
                    f"<code>.ghelp</code> — все команды.",
                )
                return

        if reply_text:
            parts.append({"type": "text", "text": f"Сообщение, на которое я отвечаю:\n{reply_text}"})
        if notes_text:
            parts.append({"type": "text", "text": notes_text})
        parts.append({"type": "text", "text": query})

        model = self._model()
        m_esc = utils.escape_html(model)
        q_disp = query if len(query) <= 300 else query[:300] + "…"
        head = f"<b>{utils.escape_html(q_disp)}</b>\n{SEP}\n"
        # контекст на ТРЕД (топик форума), а не только на чат: иначе в супергруппе
        # с темами все топики делят один chat_id и смешивают диалоги.
        thread = getattr(message, "message_thread_id", None)
        ctx_key = f"{message.chat_id}_{thread}" if thread else str(message.chat_id)
        agent_on = bool(self._get("agent", False))
        win_text, by_id = ("", {})
        if agent_on:
            # цель запроса (то, на что ответили) должна быть видна модели даже если
            # она старше окна, иначе «удали это» на сообщении пониже не сработает
            targets = [m for m in (media_msg, reply) if m is not None]
            win_text, by_id = await self._recent_window(message, targets)
        await show(head + f"⏳ <i>{m_esc} думает…</i>")
        try:
            answer = (await self._ask_agent(message, parts, ctx_key, key,
                                            win_text, by_id)
                      if agent_on else await self._ask(parts, ctx_key, key))
        except GeminiError as exc:
            logger.error("[betonomeshalka] api error: %s", exc)
            await show(head + f"❌ {utils.escape_html(str(exc))}")
            return
        except Exception as exc:
            logger.exception("[betonomeshalka] unexpected error")
            await show(head + f"❌ {utils.escape_html(type(exc).__name__)} (см. heroku logs)")
            return
        # rich-доставка: ответ с таблицами/кодом/заголовками уходит сообщением ОТ
        # heroku-бота (sendRichMessage) — только так в TG бывают нативные таблицы.
        if force_rich or (self._get("relay_auto", True) and self._rich_worthy(answer)):
            ok, info = await self._rich_deliver(message, f"🤖 **{model}**\n\n{answer}")
            if ok:
                await show(head + f"📨 <i>ответ с оформлением ушёл через "
                                  f"@{utils.escape_html(str(info))}</i>")
                return
            logger.warning("[betonomeshalka] rich-доставка не удалась: %s", info)
        prefix = head + f"🤖 <b>{m_esc}</b> — ответ:\n"
        try:
            await show(prefix + self._fit_converted(answer, prefix))
        except Exception:
            # MessageTooLongError и пр. — аварийно показываем чистый урезанный текст,
            # чтобы сообщение не осталось висеть на «⏳ думает…».
            logger.exception("[betonomeshalka] show answer failed")
            await show(prefix + utils.escape_html(answer[:3500]))

    @loader.command(
        ru_doc="Спросить Gemini: .gemini <запрос> (можно ответом на фото/войс/видео/док)",
        en_doc="Ask Gemini: .gemini <query> (works as reply to photo/voice/video/doc)",
    )
    async def gemini(self, message):
        """Ask Gemini: .gemini <query>, reply to media, or media caption"""
        await self._run(message, utils.get_args_raw(message).strip())

    @loader.command(
        ru_doc="API-ключ Gemini: .gkey <ключ> (без аргумента — показать текущий)",
        en_doc="Gemini API key: .gkey <key> (no args — show current)",
    )
    async def gkey(self, message):
        """Set or show Gemini API key"""
        args = utils.get_args_raw(message).strip()
        if args:
            self._set("api_key", args)
            # Удаляем сообщение с открытым ключом: push-уведомление показывает текст
            # ДО правки, а история правок/логгеры сохранят ключ целиком.
            chat = message.chat_id
            try:
                await message.delete()
            except Exception:
                logger.warning("[betonomeshalka] не смог удалить сообщение с ключом")
            await self.client.send_message(
                chat,
                f"🔑 <b>Ключ сохранён:</b> <code>{self._mask(args)}</code>",
                parse_mode="HTML",
            )
            return
        else:
            db_key = (self._get("api_key", "") or "").strip()
            env_key = (os.environ.get("GEMINI_API_KEY", "") or "").strip()
            if db_key:
                src = "БД"
                shown = self._mask(db_key)
            elif env_key:
                src = "env GEMINI_API_KEY"
                shown = self._mask(env_key)
            else:
                await utils.answer(
                    message, "❌ Ключ не задан: <code>.gkey &lt;ключ&gt;</code>"
                )
                return
            await utils.answer(message, f"🔑 <b>Ключ ({src}):</b> <code>{shown}</code>")

    @loader.command(
        ru_doc="Глобальный промпт: .gprompt <текст> (без аргумента — показать)",
        en_doc="Global system prompt: .gprompt <text> (no args — show)",
    )
    async def gprompt(self, message):
        """Set or show the global system prompt"""
        args = utils.get_args_raw(message).strip()
        if args:
            self._set("prompt", args)
            self._set("active_prompt", "")
            await utils.answer(message, "✅ <b>Глобальный промпт сохранён</b> и активирован.")
        else:
            cur = (self._get("prompt", "") or "").strip()
            if not cur:
                await utils.answer(message, "ℹ️ Глобальный промпт пуст: <code>.gprompt &lt;текст&gt;</code>")
            else:
                await utils.answer(message, f"ℹ️ <b>Глобальный промпт:</b>\n{utils.escape_html(cur)}")

    @loader.command(
        ru_doc="Список именованных промптов",
        en_doc="List named prompts",
    )
    async def gprompts(self, message):
        """List named prompts"""
        prompts = self._prompts()
        active = (self._get("active_prompt", "") or "").strip()
        if not prompts:
            await utils.answer(
                message,
                "ℹ️ Именованных промптов нет.\n<code>.gsave &lt;имя&gt; &lt;текст&gt;</code> — добавить.",
            )
            return
        lines = [
            f"{'➤ ' if name == active else '• '}<code>{utils.escape_html(name)}</code>"
            for name in sorted(prompts)
        ]
        await utils.answer(
            message,
            "<b>Промпты:</b>\n" + "\n".join(lines)
            + "\n\n<code>.guse &lt;имя&gt;</code> — активировать, "
              "<code>.guse default</code> — глобальный.",
        )

    @loader.command(
        ru_doc="Сохранить промпт: .gsave <имя> <текст>",
        en_doc="Save named prompt: .gsave <name> <text>",
    )
    async def gsave(self, message):
        """Save a named prompt"""
        args = utils.get_args_raw(message).strip()
        parts = args.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await utils.answer(message, "ℹ️ <code>.gsave &lt;имя&gt; &lt;текст&gt;</code>")
            return
        name, text = parts[0].lower(), parts[1].strip()
        prompts = self._prompts()
        prompts[name] = text
        self._set("prompts", prompts)
        await utils.answer(message, f"✅ Промпт <code>{utils.escape_html(name)}</code> сохранён.")

    @loader.command(
        ru_doc="Активировать промпт: .guse <имя>|default",
        en_doc="Activate prompt: .guse <name>|default",
    )
    async def guse(self, message):
        """Activate a named prompt or fall back to global"""
        args = utils.get_args_raw(message).strip().lower()
        if not args:
            active = (self._get("active_prompt", "") or "").strip()
            await utils.answer(
                message,
                f"ℹ️ Активен: <code>{utils.escape_html(active or 'default (глобальный)')}</code>\n"
                "<code>.guse &lt;имя&gt;</code> / <code>.guse default</code>",
            )
            return
        if args in ("default", "global"):
            self._set("active_prompt", "")
            await utils.answer(message, "✅ Активен глобальный промпт.")
            return
        prompts = self._prompts()
        if args not in prompts:
            await utils.answer(message, f"❌ Промпта <code>{utils.escape_html(args)}</code> нет.")
            return
        self._set("active_prompt", args)
        await utils.answer(message, f"✅ Активен промпт <code>{utils.escape_html(args)}</code>.")

    @loader.command(
        ru_doc="Удалить промпт: .gdel <имя>",
        en_doc="Delete named prompt: .gdel <name>",
    )
    async def gdel(self, message):
        """Delete a named prompt"""
        args = utils.get_args_raw(message).strip().lower()
        prompts = self._prompts()
        if not args or args not in prompts:
            await utils.answer(message, "❌ Такого промпта нет: <code>.gprompts</code>")
            return
        del prompts[args]
        self._set("prompts", prompts)
        if (self._get("active_prompt", "") or "") == args:
            self._set("active_prompt", "")
        await utils.answer(message, f"🗑 Промпт <code>{utils.escape_html(args)}</code> удалён.")

    @loader.command(
        ru_doc="Модель: .gmodel [имя] (по умолчанию gemini-3.8-flash)",
        en_doc="Model: .gmodel [name] (default gemini-3.8-flash)",
    )
    async def gmodel(self, message):
        """Show or set the model"""
        args = utils.get_args_raw(message).strip()
        if args:
            norm = normalize_model(args)
            self._set("model", norm)
            await utils.answer(message, f"✅ Модель: <code>{utils.escape_html(norm)}</code>")
        else:
            await utils.answer(
                message,
                f"ℹ️ Модель: <code>{utils.escape_html(self._model())}</code>\n"
                "Сменить: <code>.gmodel gemini-3.7-flash</code> (или просто <code>.gmodel 3.7</code>)",
            )

    @loader.command(
        ru_doc="Контекст диалога: .gchat on|off (память реплик на чат)",
        en_doc="Chat context: .gchat on|off (per-chat conversation memory)",
    )
    async def gchat(self, message):
        """Toggle per-chat conversation context"""
        args = utils.get_args_raw(message).strip().lower()
        if args in ("on", "1", "enable"):
            self._set("ctx", True)
            await utils.answer(
                message,
                "🟢 <b>Контекст включён.</b> Gemini будет помнить реплики в этом чате "
                "(запросы хранятся на сервере Google). Сброс: <code>.gclear</code>.",
            )
        elif args in ("off", "0", "disable"):
            self._set("ctx", False)
            await utils.answer(message, "🔴 <b>Контекст выключен</b> (запросы не хранятся).")
        else:
            state = "вкл" if self._get("ctx", False) else "выкл"
            await utils.answer(message, f"ℹ️ Контекст чата: <b>{state}</b>\n<code>.gchat on|off</code>")

    @loader.command(
        ru_doc="Сбросить контекст текущего чата",
        en_doc="Reset context of the current chat",
    )
    async def gclear(self, message):
        """Reset per-chat context"""
        thread = getattr(message, "message_thread_id", None)
        ctx_key = f"{message.chat_id}_{thread}" if thread else str(message.chat_id)
        self._set(f"ctx_{ctx_key}", "")
        await utils.answer(message, "🧹 Контекст сброшен.")

    @loader.command(
        ru_doc="Агент-режим: .gagent on|off | .gagent window N (1-50)",
        en_doc="Agent mode: .gagent on|off | .gagent window N (1-50)",
    )
    async def gagent(self, message):
        """Toggle tool-calling agent mode and its recent-messages window"""
        args = utils.get_args_raw(message).strip().lower()
        if args.startswith("window"):
            num = args[len("window"):].strip()
            if num.isdigit() and 1 <= int(num) <= AGENT_WINDOW_CAP:
                self._set("agent_window", int(num))
                await utils.answer(
                    message,
                    f"🪟 Модель видит <b>{int(num)}</b> недавних сообщений — то, чего нет "
                    "в окне, она удалить или изменить не сможет.",
                )
            else:
                await utils.answer(
                    message,
                    f"ℹ️ Сейчас окно: <b>{self._get('agent_window', DEFAULT_AGENT_WINDOW)}</b>.\n"
                    f"Задать: <code>.gagent window N</code> (1–{AGENT_WINDOW_CAP}).",
                )
            return
        if args in ("on", "1", "enable"):
            self._set("agent", True)
            await utils.answer(
                message,
                "🟢 <b>Агент-режим включён.</b> <code>.gemini &lt;инструкция&gt;</code> теперь "
                "может ставить реакции, удалять и править сообщения, но каждое действие "
                "надо подтвердить кнопкой <b>✅</b> (без инлайн-бота — <code>.gconfirm</code>).\n"
                "Окно недавних: <code>.gagent window N</code>.",
            )
        elif args in ("off", "0", "disable"):
            self._set("agent", False)
            await utils.answer(
                message, "🔴 <b>Агент-режим выключен</b> — только текстовые ответы.")
        else:
            await utils.answer(
                message,
                f"🤖 Агент-режим: <b>{'вкл' if self._get('agent', False) else 'выкл'}</b>\n"
                f"Окно недавних: <b>{self._get('agent_window', DEFAULT_AGENT_WINDOW)}</b> сообщ.\n"
                f"<code>.gagent on|off</code>, <code>.gagent window 1..{AGENT_WINDOW_CAP}</code>",
            )

    async def _approve_by_command(self, message, value: bool):
        """.gconfirm/.gcancel — ответ гейту, когда inline-форма недоступна."""
        pend = [t for t, p in self._pending().items()
                if p.get("chat") == message.chat_id]
        if not pend:
            await utils.answer(message, "ℹ️ Ничего не ждёт подтверждения.")
            return
        if self._resolve_approval(pend[-1], value):
            await utils.answer(
                message,
                "✅ Подтверждено — выполняю." if value else "❌ Отменено.")
        else:
            await utils.answer(message, "ℹ️ Это подтверждение уже не активно.")

    @loader.command(
        ru_doc="Подтвердить действие агента (когда нет инлайн-бота)",
        en_doc="Approve pending agent action (when no inline bot)",
    )
    async def gconfirm(self, message):
        """Approve the agent's pending actions"""
        await self._approve_by_command(message, True)

    @loader.command(
        ru_doc="Отменить действие агента (когда нет инлайн-бота)",
        en_doc="Cancel pending agent action (when no inline bot)",
    )
    async def gcancel(self, message):
        """Cancel the agent's pending actions"""
        await self._approve_by_command(message, False)

    @loader.command(
        ru_doc="Уровень раздумий: .gthink [minimal|low|medium|high] (ниже = быстрее)",
        en_doc="Thinking level: .gthink [minimal|low|medium|high] (lower = faster)",
    )
    async def gthink(self, message):
        """Show or set Gemini 3 thinking_level"""
        arg = utils.get_args_raw(message).strip().lower()
        if not arg:
            await utils.answer(
                message,
                f"🧠 Уровень раздумий: <b>{self._think()}</b>\n"
                "Сменить: <code>.gthink minimal|low|medium|high</code>\n"
                "Чем ниже — тем быстрее ответ (high — максимум рассуждений, по умолчанию у Gemini 3).",
            )
            return
        if arg not in THINK_LEVELS:
            await utils.answer(
                message,
                "❌ Доступные уровни: <code>minimal</code>, <code>low</code>, "
                "<code>medium</code>, <code>high</code>.",
            )
            return
        self._set("think", arg)
        await utils.answer(message, f"🧠 Уровень раздумий: <b>{arg}</b>.")

    @loader.command(
        ru_doc="Дебаг: .gdebug on|off — дамп запроса/ответа Gemini с таймингами",
        en_doc="Debug: .gdebug on|off — dump Gemini request/response with timings",
    )
    async def gdebug(self, message):
        """Toggle Gemini request/response debug dump"""
        arg = utils.get_args_raw(message).strip().lower()
        if arg in ("on", "1", "вкл", "enable"):
            self._set("debug", True)
            await utils.answer(
                message,
                "🐞 <b>Дебаг включён</b> — к ответам Gemini прикладываю дамп запроса "
                "и тайминги. Выкл: <code>.gdebug off</code>",
            )
        elif arg in ("off", "0", "выкл", "disable"):
            self._set("debug", False)
            await utils.answer(message, "🐞 <b>Дебаг выключен.</b>")
        else:
            state = "вкл" if self._debug_on() else "выкл"
            await utils.answer(
                message,
                f"🐞 Дебаг: <b>{state}</b>\n<code>.gdebug on|off</code> — показывать, "
                "что уходит в Gemini API и что приходит обратно (с таймингами).",
            )

    @loader.command(
        ru_doc="Спросить Gemini и доставить ответ rich-ом через heroku-бота: .gbot <запрос>",
        en_doc="Ask Gemini and deliver the answer as a rich message via the heroku bot",
    )
    async def gbot(self, message):
        """Same as .gemini but forces rich delivery (native tables/markdown) via the bot"""
        await self._run(message, utils.get_args_raw(message).strip(), force_rich=True)

    @loader.command(
        ru_doc="Rich-доставка: .gbotset — статус; .gbotset auto on|off — авто-реле таблиц",
        en_doc="Rich delivery: .gbotset — status; .gbotset auto on|off — auto-relay tables",
    )
    async def gbotset(self, message):
        """Show/configure rich delivery through the heroku inline bot"""
        raw = utils.get_args_raw(message).strip().lower()
        if raw.split()[:1] == ["auto"]:
            parts = raw.split()
            if len(parts) > 1 and parts[1] in ("on", "off", "вкл", "выкл"):
                on = parts[1] in ("on", "вкл")
                self._set("relay_auto", on)
                await utils.answer(
                    message, f"📨 Авто-доставка rich-ом: <b>{'вкл' if on else 'выкл'}</b>")
            else:
                await utils.answer(message, "ℹ️ <code>.gbotset auto on|off</code>")
            return
        token, uname = self._heroku_bot()
        await utils.answer(
            message,
            f"📨 Rich-доставка через heroku-бота: "
            f"<code>@{utils.escape_html(uname or '?')}</code> "
            f"({'токен есть' if token else '❌ токен не найден'})\n"
            f"Авто (таблицы/код/заголовки в ответе): "
            f"<b>{'вкл' if self._get('relay_auto', True) else 'выкл'}</b> — "
            "<code>.gbotset auto on|off</code>\n"
            "<code>.gbot &lt;запрос&gt;</code> — принудительно rich-ом\n\n"
            "Ответ приходит сообщением ОТ бота реплием на твой запрос. "
            "Бот должен быть участником чата, иначе покажу обычный ответ.",
        )

    @loader.command(
        ru_doc="Заметки: .notes <текст> — добавить, .notes — список по порядку",
        en_doc="Notes: .notes <text> — add, .notes — list in order",
    )
    async def notes(self, message):
        """Add a note or list all notes"""
        text = utils.get_args_raw(message).strip()
        if text:
            n = self._add_note(text)
            await utils.answer(message, f"📝 Заметка сохранена (<b>#{n}</b>).")
            return
        notes = self._notes()
        if not notes:
            await utils.answer(
                message,
                "ℹ️ Заметок пока нет.\nДобавить: <code>.notes &lt;текст&gt;</code>\n"
                "Спросить с заметками: <code>.gnote all &lt;запрос&gt;</code>.",
            )
            return
        lines = [f"<b>{i}.</b> {utils.escape_html(t)}" for i, t in enumerate(notes, 1)]
        # Режем по границе заметок, а не готовый HTML: иначе разрез попадёт внутрь
        # тега <b> или сущности &lt; и Telegram упадёт на парсинге.
        acc, total = [], 0
        for ln in lines:
            if total + len(ln) + 1 > 3_000:
                acc.append("…")
                break
            acc.append(ln)
            total += len(ln) + 1
        body = "\n".join(acc)
        await utils.answer(
            message,
            f"📝 <b>Заметки ({len(notes)}):</b>\n{body}\n\n"
            "<code>.ndel N</code> — удалить по номеру\n"
            "<code>.gnote 1,3 &lt;запрос&gt;</code> или <code>.gnote all &lt;запрос&gt;</code> — "
            "спросить Gemini с этими заметками как контекстом.",
        )

    @loader.command(
        ru_doc="Удалить заметку: .ndel <номер>",
        en_doc="Delete a note: .ndel <number>",
    )
    async def ndel(self, message):
        """Delete a note by its 1-based number"""
        arg = utils.get_args_raw(message).strip()
        if not arg.isdigit():
            await utils.answer(
                message,
                "ℹ️ <code>.ndel &lt;номер&gt;</code> — номера смотри в <code>.notes</code>.",
            )
            return
        removed = self._del_note(int(arg))
        if removed is None:
            await utils.answer(message, f"❌ Заметки <b>#{utils.escape_html(arg)}</b> нет.")
            return
        await utils.answer(message, f"🗑 Заметка <b>#{utils.escape_html(arg)}</b> удалена.")

    @loader.command(
        ru_doc="Спросить Gemini с заметками: .gnote <1,2,3|all> <запрос>",
        en_doc="Ask Gemini with notes as context: .gnote <1,2,3|all> <query>",
    )
    async def gnote(self, message):
        """Ask Gemini injecting selected notes as context"""
        raw = utils.get_args_raw(message).strip()
        if not raw:
            await utils.answer(
                message,
                "ℹ️ <code>.gnote &lt;1,2,3|all&gt; &lt;запрос&gt;</code>\n"
                "В промпт идёт глобальный промпт + выбранные заметки + запрос.\n"
                "Пример: <code>.gnote 1,3 что я хотел купить?</code>",
            )
            return
        notes = self._notes()
        if not notes:
            await utils.answer(message, "ℹ️ Заметок пока нет: <code>.notes &lt;текст&gt;</code>.")
            return
        sel, _, query = raw.partition(" ")
        sel = sel.strip().lower()
        query = query.strip()
        if not query:
            # без запроса _run подставил бы «Ответь на это сообщение» и приклеил
            # личные заметки к чужому посту — требуем явный текст.
            await utils.answer(
                message,
                "❌ Нужен текст запроса: <code>.gnote 1,3 &lt;запрос&gt;</code> "
                "или <code>.gnote all &lt;запрос&gt;</code>.",
            )
            return
        if sel == "all":
            idx = list(range(1, len(notes) + 1))
        else:
            idx = [int(t) for t in sel.split(",") if t.strip().isdigit()]
            if not idx:
                await utils.answer(
                    message,
                    "❌ Не понял номера заметок.\nПример: <code>.gnote 1,3 запрос</code> "
                    "или <code>.gnote all запрос</code>.",
                )
                return
        block = self._notes_block(idx)
        if not block:
            await utils.answer(message, "❌ Заметки с такими номерами не найдены.")
            return
        await self._run(message, query, notes_text=block)

    @loader.command(
        ru_doc="Справка по командам betonomeshalka",
        en_doc="betonomeshalka commands help",
    )
    async def ghelp(self, message):
        """Show help"""
        await utils.answer(
            message,
            "🤖 <b>betonomeshalka</b>\n\n"
            "<code>.gemini &lt;запрос&gt;</code> — спросить (текст, ответ на фото/войс/видео/док, подпись к фото)\n"
            "<code>.gkey &lt;ключ&gt;</code> — API-ключ (aistudio.google.com → API keys)\n"
            "<code>.gprompt &lt;текст&gt;</code> — глобальный системный промпт\n"
            "<code>.gsave &lt;имя&gt; &lt;текст&gt;</code> — именованный промпт\n"
            "<code>.gprompts</code> — список промптов\n"
            "<code>.guse &lt;имя&gt;|default</code> — активный промпт\n"
            "<code>.gdel &lt;имя&gt;</code> — удалить промпт\n"
            "<code>.gmodel [имя]</code> — модель (сейчас <code>"
            + utils.escape_html(self._model()) + "</code>)\n"
            "<code>.gthink [minimal|low|medium|high]</code> — уровень раздумий (сейчас <code>"
            + self._think() + "</code>, ниже = быстрее)\n"
            "<code>.gchat on|off</code> — память диалога на чат\n"
            "<code>.gclear</code> — сбросить контекст чата\n"
            "<code>.gagent on|off</code> — агент-режим: модель сама ставит реакции, удаляет и "
            "правит сообщения, но каждое действие надо подтвердить кнопкой ✅\n"
            "<code>.gagent window N</code> — сколько недавних сообщений ей видно (сейчас <code>"
            + str(self._get("agent_window", DEFAULT_AGENT_WINDOW)) + "</code>); "
            "вне окна id для неё недоступны\n"
            "<code>.gconfirm</code> / <code>.gcancel</code> — ответ гейту, если инлайн-бота нет\n"
            "<code>.gdebug on|off</code> — дебаг: дамп запроса/ответа Gemini с таймингами\n"
            "<code>.gbot &lt;запрос&gt;</code> — как .gemini, но ответ rich-ом через heroku-бота\n"
            "<code>.gbotset</code> — статус rich-доставки; "
            "<code>.gbotset auto on|off</code> — авто-доставка таблиц/кода rich-ом\n"
            "<code>.notes &lt;текст&gt;</code> — заметка; <code>.notes</code> — список; "
            "<code>.ndel N</code> — удалить\n"
            "<code>.gnote &lt;1,3|all&gt; &lt;запрос&gt;</code> — спросить с заметками как контекстом\n\n"
            "Ответ редактирует твоё сообщение: запрос сверху, ответ ниже.",
        )
