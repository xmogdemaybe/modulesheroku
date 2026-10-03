# -*- coding: utf-8 -*-
# requires: aiohttp
# GeminiAI — ответы Gemini прямо в сообщениях (Hikka / Heroku).
#
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
#
# Ключ также подхватывается из env GEMINI_API_KEY (Heroku config vars), если
# в БД пусто. Логи — только в терминал (heroku logs), в TG ничего не дублируется.

import asyncio
import base64
import logging
import os
from typing import Any, Optional

import aiohttp

from .. import loader, utils

logger = logging.getLogger(__name__)

MODNAME = "GeminiAI"
API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.8-flash"
MAX_INLINE = 20 * 1024 * 1024  # лимит inline-данных Gemini API
MAX_TEXT_DOC = 100_000  # символы для текстовых файлов
MAX_REPLY_CTX = 8_000  # символы текста сообщения-ответа
ANSWER_LIMIT = 3_700  # ответ Telegram на сообщение — 4096, оставляем запас
REQUEST_TIMEOUT = 300
POLL_TIMEOUT = 240

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


class GeminiError(Exception):
    """Ошибка запроса к Gemini API — текст уходит юзеру в отредактированное сообщение."""


@loader.tds
class GeminiAI(loader.Module):
    """Ask Gemini from any message: text, photos, voice notes, videos, documents"""

    strings = {"name": MODNAME}

    async def client_ready(self, client, db):
        self.client = client
        self.db = db
        self._session: Optional[aiohttp.ClientSession] = None

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
            logger.exception("[GeminiAI] db.set failed for %s", key)

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
        return (self._get("model", "") or "").strip() or DEFAULT_MODEL

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
                        logger.error("[GeminiAI] download failed: %r", exc)
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
            logger.error("[GeminiAI] download failed: %r", exc)
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

    # ---------- api ----------

    @staticmethod
    def _extract_text(data: dict) -> str:
        chunks = []
        for step in data.get("steps", []) or []:
            if step.get("type") != "model_output":
                continue
            for block in step.get("content", []) or []:
                if block.get("type") == "text" and block.get("text"):
                    chunks.append(block["text"])
        return "\n".join(chunks).strip()

    async def _post(self, url: str, key: str, body: Optional[dict] = None) -> dict:
        session = await self._http()
        headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
        last_data: dict = {}
        for attempt in (1, 2):
            async with session.post(url, json=body, headers=headers) as resp:
                try:
                    last_data = await resp.json()
                except Exception:
                    last_data = {}
                if resp.status == 429 and attempt == 1:
                    try:
                        retry = int(resp.headers.get("Retry-After", "5"))
                    except ValueError:
                        retry = 5
                    logger.warning("[GeminiAI] 429, retry in %ss", retry)
                    await asyncio.sleep(min(max(retry, 1), 30))
                    continue
                if resp.status != 200:
                    err = (last_data.get("error") or {}).get("message") or f"HTTP {resp.status}"
                    raise GeminiError(err)
                return last_data
        err = (last_data.get("error") or {}).get("message") or "HTTP 429"
        raise GeminiError(err)

    async def _ask(self, parts: list, chat_id: int, key: str) -> str:
        body: dict = {"model": self._model(), "input": parts}
        si = self._system_instruction()
        if si:
            body["system_instruction"] = si
        ctx_on = bool(self._get("ctx", False))
        body["store"] = ctx_on  # без контекста запросы не хранятся на сервере
        if ctx_on:
            prev = self._get(f"ctx_{chat_id}", "")
            if prev:
                body["previous_interaction_id"] = prev

        data = await self._post(f"{API_BASE}/interactions", key, body)

        status = data.get("status", "")
        iid = data.get("id", "")
        waited = 0
        headers = {"x-goog-api-key": key}
        while status in ("in_progress", "queued") and iid and waited < POLL_TIMEOUT:
            await asyncio.sleep(3)
            waited += 3
            session = await self._http()
            async with session.get(f"{API_BASE}/interactions/{iid}", headers=headers) as resp:
                data = await resp.json()
                status = data.get("status", status)

        if status == "failed":
            errors = data.get("errors") or []
            msg = (errors[0].get("message") if errors and isinstance(errors[0], dict) else "") \
                or "запрос не удался"
            raise GeminiError(msg)

        text = self._extract_text(data)
        if not text:
            raise GeminiError("Gemini вернул пустой ответ (возможно, сработал фильтр)")
        if ctx_on and iid:
            self._set(f"ctx_{chat_id}", iid)
        return text

    # ---------- helpers ----------

    @staticmethod
    def _fit(text: str, limit: int = ANSWER_LIMIT) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n…(обрезано, всего {len(text)} симв.)"

    @staticmethod
    def _mask(key: str) -> str:
        if len(key) <= 12:
            return key[:3] + "…"
        return key[:8] + "…" + key[-4:]

    # ---------- commands ----------

    @loader.command(
        ru_doc="Спросить Gemini: .gemini <запрос> (можно ответом на фото/войс/видео/док)",
        en_doc="Ask Gemini: .gemini <query> (works as reply to photo/voice/video/doc)",
    )
    async def gemini(self, message):
        """Ask Gemini: .gemini <query>, reply to media, or media caption"""
        args = utils.get_args_raw(message).strip()
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
        if media_msg is None and getattr(message, "is_reply", False):
            reply = await message.get_reply_message()
            if reply is not None:
                if self._media_kind(reply):
                    media_msg = reply
                elif (reply.raw_text or "").strip():
                    reply_text = (reply.raw_text or "").strip()[:MAX_REPLY_CTX]

        parts = []
        kind = self._media_kind(media_msg) if media_msg is not None else None
        if media_msg is not None:
            media_parts, err = await self._media_parts(media_msg)
            if err:
                await utils.answer(message, f"❌ {utils.escape_html(err)}")
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
                await utils.answer(
                    message,
                    f"🤖 <b>GeminiAI</b>\n"
                    f"<b>Модель:</b> <code>{utils.escape_html(model)}</code>\n"
                    f"<b>Промпт:</b> <code>{utils.escape_html(str(active))}</code>\n"
                    f"<b>Ключ:</b> <code>{self._mask(key)}</code>\n"
                    f"<b>Контекст чата:</b> {'вкл' if self._get('ctx', False) else 'выкл'}\n\n"
                    f"<b>Использование:</b> <code>.gemini &lt;запрос&gt;</code> — текстом, "
                    f"ответом на фото/войс/видео/док или в подписи к фото.\n"
                    f"Ответ появится прямо в этом сообщении, под запросом.\n"
                    f"<code>.ghelp</code> — все команды.",
                )
                return

        if reply_text:
            parts.append({"type": "text", "text": f"Сообщение, на которое я отвечаю:\n{reply_text}"})
        parts.append({"type": "text", "text": query})

        q_esc = utils.escape_html(query)
        await message.edit(f"<b>{q_esc}</b>\n{SEP}\n⏳ <i>Gemini думает…</i>")
        try:
            answer = await self._ask(parts, message.chat_id, key)
        except GeminiError as exc:
            logger.error("[GeminiAI] api error: %s", exc)
            await message.edit(f"<b>{q_esc}</b>\n{SEP}\n❌ {utils.escape_html(str(exc))}")
            return
        except Exception as exc:
            logger.exception("[GeminiAI] unexpected error")
            await message.edit(
                f"<b>{q_esc}</b>\n{SEP}\n❌ {utils.escape_html(type(exc).__name__)} (см. heroku logs)"
            )
            return
        await message.edit(f"<b>{q_esc}</b>\n{SEP}\n{utils.escape_html(self._fit(answer))}")

    @loader.command(
        ru_doc="API-ключ Gemini: .gkey <ключ> (без аргумента — показать текущий)",
        en_doc="Gemini API key: .gkey <key> (no args — show current)",
    )
    async def gkey(self, message):
        """Set or show Gemini API key"""
        args = utils.get_args_raw(message).strip()
        if args:
            self._set("api_key", args)
            await utils.answer(message, f"🔑 <b>Ключ сохранён:</b> <code>{self._mask(args)}</code>")
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
            self._set("model", args)
            await utils.answer(message, f"✅ Модель: <code>{utils.escape_html(args)}</code>")
        else:
            await utils.answer(
                message,
                f"ℹ️ Модель: <code>{utils.escape_html(self._model())}</code>\n"
                "Сменить: <code>.gmodel gemini-3.7-flash</code>",
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
        self._set(f"ctx_{message.chat_id}", "")
        await utils.answer(message, "🧹 Контекст чата сброшен.")

    @loader.command(
        ru_doc="Справка по командам GeminiAI",
        en_doc="GeminiAI commands help",
    )
    async def ghelp(self, message):
        """Show help"""
        await utils.answer(
            message,
            "🤖 <b>GeminiAI</b>\n\n"
            "<code>.gemini &lt;запрос&gt;</code> — спросить (текст, ответ на фото/войс/видео/док, подпись к фото)\n"
            "<code>.gkey &lt;ключ&gt;</code> — API-ключ (aistudio.google.com → API keys)\n"
            "<code>.gprompt &lt;текст&gt;</code> — глобальный системный промпт\n"
            "<code>.gsave &lt;имя&gt; &lt;текст&gt;</code> — именованный промпт\n"
            "<code>.gprompts</code> — список промптов\n"
            "<code>.guse &lt;имя&gt;|default</code> — активный промпт\n"
            "<code>.gdel &lt;имя&gt;</code> — удалить промпт\n"
            "<code>.gmodel [имя]</code> — модель (сейчас <code>"
            + utils.escape_html(self._model()) + "</code>)\n"
            "<code>.gchat on|off</code> — память диалога на чат\n"
            "<code>.gclear</code> — сбросить контекст чата\n\n"
            "Ответ редактирует твоё сообщение: запрос сверху, ответ ниже.",
        )
