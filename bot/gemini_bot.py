# -*- coding: utf-8 -*-
# Gemini Bot — Telegram-бот (Bot API, БЕЗ MTProto) с Gemini внутри.
#
# Другу, который не может держать юзербота: это обычный бот, созданный через
# @BotFather. Шлёшь ему текст / фото / войс / видео / доку — он отвечает
# сообщением: твой запрос сверху, «──────────», ответ Gemini ниже (бот
# редактирует СВОЁ сообщение, пока «думает»).
#
# Запуск:
#   export BOT_TOKEN="токен от @BotFather"
#   export GEMINI_API_KEY="ключ Gemini"   # общий ключ для всех (или каждый юзер задаёт свой через /key)
#   export HTTPS_PROXY="http://127.0.0.1:1080"  # если Telegram/Gemini блокируются (РФ)
#   python gemini_bot.py
#
# ВАЖНО про гео: с российских IP api.telegram.org и generativelanguage.googleapis.com
# могут не отвечать. Бот должен крутиться там, где оба хоста доступны: VPS за
# рубежом, или включённый VPN/прокси (HTTPS_PROXY подхватывается автоматически).
#
# Команды:
#   /start, /help        справка
#   /key <ключ>          свой Gemini API-ключ (иначе используется общий из env)
#   /prompt <текст>      системный промпт (без аргумента — показать)
#   /save <имя> <текст>  именованный промпт
#   /prompts             список именованных промптов
#   /use <имя>|default   активный промпт
#   /del <имя>           удалить промпт
#   /model [имя]         модель (по умолчанию gemini-3.8-flash)
#   /chat [on|off]       память диалога (Gemini помнит прошлые реплики)
#   /clear               сбросить память в этом чате
#
# База: SQLite (файл gemini_bot.db рядом со скриптом, путь — env DB_PATH).
# Настройки и промпты — свои на каждого юзера; память диалога — на пару
# (чат, юзер). История переписки при /chat on живёт на серверах Google
# (previous_interaction_id), локально хранится только id последнего ответа.
#
# Логи — только в терминал. В чат уходят лишь ответы и сообщения об ошибках.

import base64
import html
import logging
import os
import sqlite3
import time

import requests
import telebot

logger = logging.getLogger("gemini_bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.8-flash"
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_bot.db"))
MAX_FILE = 20 * 1024 * 1024   # лимит скачивания Bot API = лимит инлайн-данных Gemini
MAX_TEXT_DOC = 100_000        # символы для текстовых файлов
ANSWER_LIMIT = 3_800          # лимит сообщения TG 4096, с запасом на оформление
REQUEST_TIMEOUT = (15, 300)   # (connect, read): thinking-модели отвечают долго
POLL_TIMEOUT = 240

SEP = "──────────"

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
TEXT_DOC_EXTS = {
    ".txt", ".md", ".py", ".js", ".ts", ".json", ".html", ".css", ".log",
    ".cfg", ".ini", ".yml", ".yaml", ".xml", ".sh", ".c", ".cpp", ".h",
    ".java", ".cs", ".go", ".rs", ".php", ".rb",
}

DEFAULT_QUERY = {
    "photo": "Что изображено на этом фото? Опиши кратко.",
    "video": "Опиши это видео кратко.",
    "audio": "Расшифруй это аудиосообщение дословно.",
    "document": "Что это за файл и о чём он?",
}


class GeminiError(Exception):
    pass


def esc(text) -> str:
    return html.escape(str(text), quote=False)


# ---------- база ----------
# Соединение на каждую операцию: хендлеры telebot работают в разных потоках,
# а sqlite-коннекцию между потоками таскать нельзя (check_same_thread).

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                api_key TEXT DEFAULT '',
                prompt TEXT DEFAULT '',
                model TEXT DEFAULT '',
                active_prompt TEXT DEFAULT '',
                ctx INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS prompts(
                user_id INTEGER, name TEXT, text TEXT,
                PRIMARY KEY(user_id, name)
            );
            CREATE TABLE IF NOT EXISTS contexts(
                chat_id INTEGER, user_id INTEGER, interaction_id TEXT,
                PRIMARY KEY(chat_id, user_id)
            );
            """
        )


def get_user(user_id: int) -> dict:
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return dict(row)


def set_user(user_id: int, field: str, value):
    assert field in ("api_key", "prompt", "model", "active_prompt", "ctx")
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
        conn.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, user_id))


def list_prompts(user_id: int) -> dict:
    with db() as conn:
        rows = conn.execute("SELECT name, text FROM prompts WHERE user_id=?", (user_id,)).fetchall()
    return {r["name"]: r["text"] for r in rows}


def save_prompt(user_id: int, name: str, text: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO prompts(user_id,name,text) VALUES(?,?,?) "
            "ON CONFLICT(user_id,name) DO UPDATE SET text=excluded.text",
            (user_id, name, text),
        )


def del_prompt(user_id: int, name: str) -> bool:
    with db() as conn:
        cur = conn.execute("DELETE FROM prompts WHERE user_id=? AND name=?", (user_id, name))
        deleted = bool(cur.rowcount)
        if deleted:
            conn.execute(
                "UPDATE users SET active_prompt='' WHERE user_id=? AND active_prompt=?",
                (user_id, name),
            )
    return deleted


def get_ctx(chat_id: int, user_id: int) -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT interaction_id FROM contexts WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    return row["interaction_id"] if row else ""


def set_ctx(chat_id: int, user_id: int, iid: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO contexts(chat_id,user_id,interaction_id) VALUES(?,?,?) "
            "ON CONFLICT(chat_id,user_id) DO UPDATE SET interaction_id=excluded.interaction_id",
            (chat_id, user_id, iid),
        )


def clear_ctx(chat_id: int, user_id: int):
    with db() as conn:
        conn.execute("DELETE FROM contexts WHERE chat_id=? AND user_id=?", (chat_id, user_id))


# ---------- gemini ----------

def api_key_for(user: dict) -> str:
    key = (user.get("api_key") or "").strip()
    if key:
        return key
    return (os.environ.get("GEMINI_API_KEY", "") or "").strip()


def system_instruction_for(user: dict) -> str:
    active = (user.get("active_prompt") or "").strip()
    if active:
        prompts = list_prompts(user["user_id"])
        if active in prompts:
            return prompts[active]
    return (user.get("prompt") or "").strip()


def model_for(user: dict) -> str:
    return (user.get("model") or "").strip() or DEFAULT_MODEL


def extract_text(data: dict) -> str:
    chunks = []
    for step in data.get("steps", []) or []:
        if step.get("type") != "model_output":
            continue
        for block in step.get("content", []) or []:
            if block.get("type") == "text" and block.get("text"):
                chunks.append(block["text"])
    return "\n".join(chunks).strip()


def ask_gemini(parts: list, user: dict, chat_id: int) -> str:
    key = api_key_for(user)
    if not key:
        raise GeminiError("API-ключ не задан: /key <ключ> (или общий через env GEMINI_API_KEY)")

    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    body = {"model": model_for(user), "input": parts}
    si = system_instruction_for(user)
    if si:
        body["system_instruction"] = si
    ctx_on = bool(user.get("ctx"))
    body["store"] = ctx_on  # без контекста запрос не сохраняется у Google
    if ctx_on:
        prev = get_ctx(chat_id, user["user_id"])
        if prev:
            body["previous_interaction_id"] = prev

    data = None
    for attempt in (1, 2):
        resp = requests.post(f"{API_BASE}/interactions", json=body, headers=headers,
                             timeout=REQUEST_TIMEOUT)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 429 and attempt == 1:
            try:
                wait = min(max(int(resp.headers.get("Retry-After", "5")), 1), 30)
            except ValueError:
                wait = 5
            logger.warning("429, retry in %ss", wait)
            time.sleep(wait)
            continue
        if resp.status_code != 200:
            err = (data.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
            raise GeminiError(err)
        break
    else:
        raise GeminiError((data.get("error") or {}).get("message") or "HTTP 429")

    status, iid = data.get("status", ""), data.get("id", "")
    waited = 0
    while status in ("in_progress", "queued") and iid and waited < POLL_TIMEOUT:
        time.sleep(3)
        waited += 3
        r = requests.get(f"{API_BASE}/interactions/{iid}",
                         headers={"x-goog-api-key": key}, timeout=REQUEST_TIMEOUT)
        data = r.json()
        status = data.get("status", status)

    if status == "failed":
        errors = data.get("errors") or []
        msg = (errors[0].get("message") if errors and isinstance(errors[0], dict) else "") \
            or "запрос не удался"
        raise GeminiError(msg)

    text = extract_text(data)
    if not text:
        raise GeminiError("Gemini вернул пустой ответ (возможно, сработал фильтр)")
    if ctx_on and iid:
        set_ctx(chat_id, user["user_id"], iid)
    return text


# ---------- медиа ----------

def media_kind(msg) -> str:
    """Контент-тип сообщения в порядке специфики (photo раньше document и т.д.)."""
    if msg is None:
        return ""
    for attr, kind in (
        ("photo", "photo"), ("sticker", "sticker"), ("voice", "voice"),
        ("audio", "audio"), ("video_note", "video"), ("animation", "video"),
        ("video", "video"), ("document", "document"),
    ):
        if getattr(msg, attr, None):
            return kind
    return ""


def _file_meta(msg, kind: str) -> tuple:
    """(file_id, mime, name, size) по типу медиа."""
    if kind == "photo":
        p = msg.photo[-1]  # наибольший размер
        return p.file_id, "image/jpeg", "photo.jpg", getattr(p, "file_size", 0)
    if kind == "voice":
        v = msg.voice
        return v.file_id, "audio/ogg", "voice.ogg", getattr(v, "file_size", 0)
    if kind == "audio":
        a = msg.audio
        return a.file_id, a.mime_type or "audio/mpeg", a.file_name or "audio", getattr(a, "file_size", 0)
    if kind == "sticker":
        s = msg.sticker
        return s.file_id, s.mime_type or "image/webp", "sticker", getattr(s, "file_size", 0)
    if kind == "video":
        src = msg.video_note or msg.animation or msg.video
        mime = getattr(src, "mime_type", None) or "video/mp4"
        return src.file_id, mime, getattr(src, "file_name", None) or "video", getattr(src, "file_size", 0)
    d = msg.document
    return d.file_id, d.mime_type or "", d.file_name or "file", getattr(d, "file_size", 0)


def build_media_parts(bot, msg) -> tuple:
    """(parts, error). Скачивает медиа и собирает контент-блоки для input."""
    kind = media_kind(msg)
    if not kind:
        return [], None

    file_id, mime, name, size = _file_meta(msg, kind)
    if size and size > MAX_FILE:
        return [], "файл больше 20 МБ — не влезает ни в Bot API, ни в Gemini"

    if kind == "document":
        if mime in DOC_MIMES:
            pass  # pdf/csv уходят document-блоком
        elif mime in IMAGE_MIMES:
            kind = "photo"
        elif mime in AUDIO_MIMES:
            kind = "audio"
        elif mime in VIDEO_MIMES:
            kind = "video"
        else:
            ext = ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""
            if mime.startswith("text/") or ext in TEXT_DOC_EXTS:
                try:
                    raw = bot.download_file(bot.get_file(file_id).file_path).read()
                except Exception as exc:
                    logger.error("download failed: %r", exc)
                    return [], "не смог скачать файл"
                text = raw[: MAX_TEXT_DOC * 2].decode("utf-8", errors="replace")
                cut = "\n…(обрезано)" if len(text) > MAX_TEXT_DOC else ""
                return [{"type": "text",
                         "text": f"Содержимое файла {name}:\n{text[:MAX_TEXT_DOC]}{cut}"}], None
            return [], f"тип файла не поддерживается ({mime or 'неизвестно'})"

    if kind == "sticker" and mime not in IMAGE_MIMES:
        return [], "анимированные/видео-стикеры не поддерживаются (только статичные)"
    if kind == "photo" and mime not in IMAGE_MIMES:
        mime = "image/jpeg"
    if kind == "audio" and mime not in AUDIO_MIMES:
        mime = "audio/mpeg"
    if kind == "video" and mime not in VIDEO_MIMES:
        return [], "формат видео не поддерживается Gemini"

    try:
        data = bot.download_file(bot.get_file(file_id).file_path).read()
    except Exception as exc:
        logger.error("download failed: %r", exc)
        return [], "не смог скачать медиа (файл больше 20 МБ или недоступен)"
    if not data:
        return [], "медиа пустое"
    if len(data) > MAX_FILE:
        return [], "файл больше 20 МБ — Gemini такое инлайн не принимает"

    block_type = {"photo": "image", "sticker": "image", "voice": "audio",
                  "audio": "audio", "video": "video", "document": "document"}[kind]
    return [{"type": block_type, "data": base64.b64encode(data).decode(),
             "mime_type": mime}], None


def fit(text: str, limit: int = ANSWER_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(обрезано, всего {len(text)} симв.)"


# ---------- бот ----------

def parse_command(text: str) -> tuple:
    """/cmd@BotName args -> ('cmd', 'args'); для не-команд ('', text)."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return "", text
    head, _, rest = text.partition(" ")
    cmd = head[1:].split("@")[0].lower()
    return cmd, rest.strip()


HELP_TEXT = (
    "🤖 <b>Gemini Bot</b>\n\n"
    "Просто напиши текст, либо кинь фото/войс/видео/документ (можно с подписью-"
    "вопросом, можно отвечать сообщением на медиа).\n\n"
    "<b>Команды:</b>\n"
    "/key &lt;ключ&gt; — свой Gemini API-ключ\n"
    "/prompt &lt;текст&gt; — системный промпт\n"
    "/save &lt;имя&gt; &lt;текст&gt; — именованный промпт\n"
    "/prompts — список промптов\n"
    "/use &lt;имя&gt;|default — активный промпт\n"
    "/del &lt;имя&gt; — удалить промпт\n"
    "/model [имя] — модель (сейчас по умолчанию <code>" + DEFAULT_MODEL + "</code>)\n"
    "/chat [on|off] — память диалога\n"
    "/clear — сбросить память в этом чате"
)


def create_bot(token: str) -> telebot.TeleBot:
    # Прокси для РФ: requests сам подхватит HTTPS_PROXY из env, а telebot
    # (polling/getFile) ходит через apihelper — ему прокси задаётся отдельно.
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        telebot.apihelper.proxy = {"https": proxy, "http": os.environ.get("HTTP_PROXY", "")}
        logger.info("proxy configured: %s", proxy)

    bot = telebot.TeleBot(token, threaded=True, parse_mode="HTML")

    def answer_error(message, text):
        try:
            bot.reply_to(message, f"❌ {esc(text)}")
        except Exception:
            logger.exception("failed to send error")

    def handle_command(message, cmd, args):
        uid = message.from_user.id
        user = get_user(uid)

        if cmd in ("start", "help"):
            bot.reply_to(message, HELP_TEXT)

        elif cmd == "key":
            if args:
                set_user(uid, "api_key", args)
                bot.reply_to(message, f"🔑 Ключ сохранён: <code>{esc(args[:8])}…</code>")
            else:
                shown = "свой" if (user.get("api_key") or "").strip() else \
                    ("общий (env)" if os.environ.get("GEMINI_API_KEY") else "НЕ задан")
                bot.reply_to(message, f"🔑 Ключ: {shown}. Задать свой: /key &lt;ключ&gt;")

        elif cmd == "prompt":
            if args:
                set_user(uid, "prompt", args)
                set_user(uid, "active_prompt", "")
                bot.reply_to(message, "✅ Системный промпт сохранён и активирован.")
            else:
                cur = (user.get("prompt") or "").strip()
                bot.reply_to(message, f"ℹ️ Промпт:\n{esc(cur)}" if cur
                             else "ℹ️ Промпт пуст: /prompt &lt;текст&gt;")

        elif cmd == "save":
            parts = args.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                bot.reply_to(message, "ℹ️ /save &lt;имя&gt; &lt;текст&gt;")
                return
            name = parts[0].lower()
            save_prompt(uid, name, parts[1].strip())
            bot.reply_to(message, f"✅ Промпт <code>{esc(name)}</code> сохранён.")

        elif cmd == "prompts":
            prompts = list_prompts(uid)
            if not prompts:
                bot.reply_to(message, "ℹ️ Промптов нет. Добавить: /save &lt;имя&gt; &lt;текст&gt;")
                return
            active = (user.get("active_prompt") or "").strip()
            lines = [f"{'➤ ' if n == active else '• '}<code>{esc(n)}</code>" for n in sorted(prompts)]
            bot.reply_to(message, "<b>Промпты:</b>\n" + "\n".join(lines)
                         + "\n\n/use &lt;имя&gt; — активировать, /use default — глобальный.")

        elif cmd == "use":
            name = args.lower()
            if not name:
                active = (user.get("active_prompt") or "").strip()
                bot.reply_to(message, f"ℹ️ Активен: <code>{esc(active or 'default (глобальный)')}</code>")
            elif name in ("default", "global"):
                set_user(uid, "active_prompt", "")
                bot.reply_to(message, "✅ Активен глобальный промпт.")
            elif name in list_prompts(uid):
                set_user(uid, "active_prompt", name)
                bot.reply_to(message, f"✅ Активен промпт <code>{esc(name)}</code>.")
            else:
                bot.reply_to(message, f"❌ Промпта <code>{esc(name)}</code> нет: /prompts")

        elif cmd == "del":
            name = args.lower()
            if name and del_prompt(uid, name):
                bot.reply_to(message, f"🗑 Промпт <code>{esc(name)}</code> удалён.")
            else:
                bot.reply_to(message, "❌ Такого промпта нет: /prompts")

        elif cmd == "model":
            if args:
                set_user(uid, "model", args)
                bot.reply_to(message, f"✅ Модель: <code>{esc(args)}</code>")
            else:
                bot.reply_to(message, f"ℹ️ Модель: <code>{esc(model_for(user))}</code>\n"
                             "Сменить: /model gemini-3.7-flash")

        elif cmd == "chat":
            state = args.lower()
            if state in ("on", "1", "enable"):
                set_user(uid, "ctx", 1)
                bot.reply_to(message, "🟢 Память диалога включена: Gemini будет помнить "
                             "прошлые реплики (история хранится у Google). Сброс: /clear")
            elif state in ("off", "0", "disable"):
                set_user(uid, "ctx", 0)
                bot.reply_to(message, "🔴 Память выключена, каждый вопрос с чистого листа.")
            else:
                on = bool(user.get("ctx"))
                bot.reply_to(message, f"ℹ️ Память диалога: <b>{'вкл' if on else 'выкл'}</b>\n"
                             "/chat on|off")

        elif cmd == "clear":
            clear_ctx(message.chat.id, uid)
            bot.reply_to(message, "🧹 Память в этом чате сброшена.")

        else:
            bot.reply_to(message, "Не знаю такой команды. /help")

    def handle_content(message):
        """Любое не-командное сообщение: текст/медиа → Gemini."""
        uid = message.from_user.id if message.from_user else 0
        user = get_user(uid)
        if not api_key_for(user):
            answer_error(message, "API-ключ не задан. /key &lt;ключ&gt; (aistudio.google.com → "
                         "Get API key, нужен VPN с поддерживаемого региона) "
                         "или попроси владельца задать общий env GEMINI_API_KEY.")
            return

        # медиа: в самом сообщении или в том, на которое отвечают
        media_msg = message if media_kind(message) else None
        reply_text = ""
        if media_msg is None and getattr(message, "reply_to_message", None):
            reply = message.reply_to_message
            if media_kind(reply):
                media_msg = reply
            elif (reply.text or reply.caption or "").strip():
                reply_text = (reply.text or reply.caption or "").strip()[:8_000]

        parts = []
        kind = media_kind(media_msg)
        if media_msg is not None:
            media_parts, err = build_media_parts(bot, media_msg)
            if err:
                answer_error(message, err)
                return
            parts.extend(media_parts)

        query = (message.caption if message.photo or message.video or message.document
                 or message.animation or message.sticker or message.audio
                 else message.text) or ""
        query = query.strip()
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
                return  # пустое сообщение без медиа — игнор

        if reply_text:
            parts.append({"type": "text", "text": f"Сообщение, на которое я отвечаю:\n{reply_text}"})
        parts.append({"type": "text", "text": query})

        # бот не может редактировать чужие сообщения — шлём своё и правим его
        status_msg = bot.send_message(message.chat.id,
                                      f"<b>{esc(query[:500])}</b>\n{SEP}\n⏳ <i>Gemini думает…</i>")
        try:
            answer = ask_gemini(parts, user, message.chat.id)
        except GeminiError as exc:
            logger.error("api error: %s", exc)
            bot.edit_message_text(f"<b>{esc(query[:500])}</b>\n{SEP}\n❌ {esc(exc)}",
                                  message.chat.id, status_msg.message_id)
            return
        except Exception as exc:
            logger.exception("unexpected error")
            bot.edit_message_text(f"<b>{esc(query[:500])}</b>\n{SEP}\n❌ {esc(type(exc).__name__)} "
                                  f"(см. логи)", message.chat.id, status_msg.message_id)
            return
        bot.edit_message_text(f"<b>{esc(query[:500])}</b>\n{SEP}\n{esc(fit(answer))}",
                              message.chat.id, status_msg.message_id)

    @bot.message_handler(commands=[
        "start", "help", "key", "prompt", "prompts", "save", "use", "del",
        "model", "chat", "clear",
    ])
    def on_command(message):
        cmd, args = parse_command(message.text)
        try:
            handle_command(message, cmd, args)
        except Exception:
            logger.exception("command error")
            answer_error(message, "внутренняя ошибка (см. логи)")

    @bot.message_handler(content_types=[
        "text", "photo", "voice", "audio", "video", "video_note",
        "document", "sticker", "animation",
    ])
    def on_content(message):
        # команды в группах могут прилетать как текст «/cmd@BotName» — фильтруем
        cmd, _ = parse_command(message.text or "")
        if cmd:
            return
        try:
            handle_content(message)
        except Exception:
            logger.exception("content error")
            try:
                answer_error(message, "внутренняя ошибка (см. логи)")
            except Exception:
                pass

    return bot


def main():
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit('BOT_TOKEN не задан. Получи токен у @BotFather и запусти:\n'
                         '  export BOT_TOKEN="..." && python gemini_bot.py')
    db_init()
    bot = create_bot(token)
    logger.info("bot started (db: %s, default model: %s)", DB_PATH, DEFAULT_MODEL)
    # retry_on_error: сеть может моргать (особенно через прокси) — не роняем бота
    bot.infinity_polling(timeout=30, long_polling_timeout=25)


if __name__ == "__main__":
    main()
