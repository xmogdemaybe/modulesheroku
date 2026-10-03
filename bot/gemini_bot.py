# -*- coding: utf-8 -*-
# Gemini Bot — Telegram-бот (Bot API, БЕЗ MTProto) с Gemini внутри.
#
# Каждый юзер привязывает СВОЙ Gemini API-ключ: при первом сообщении бот
# просит ключ, проверяет его реальным тест-запросом к Gemini и сохраняет в
# базу. Без ключа бот не отвечает — только просит его прислать.
#
# Первый запуск (никаких export не нужно):
#   pip install pyTelegramBotAPI requests
#   python gemini_bot.py
# Бот сам спросит токен в терминале (получить у @BotFather → /newbot) и
# сохранит его в config.json рядом со скриптом. Остановка — Ctrl+C (чистый
# выход без трейсбеков).
#
# config.json (создаётся автоматически, права 600):
#   {
#     "bot_token": "123456:ABC...",   # обязателен (спросится при запуске)
#     "proxy": "http://127.0.0.1:1080",  # если TG/Gemini блокируются (РФ)
#     "db_path": "/путь/gemini_bot.db"   # необязательно
#   }
#
# ВАЖНО про гео: с российских IP api.telegram.org и generativelanguage.googleapis.com
# могут не отвечать — тогда впиши "proxy" в config.json (VPN/прокси) или крути
# бота на зарубежном VPS.
#
# Команды:
#   /start, /help        справка
#   /key <ключ>          сменить свой Gemini API-ключ (проверяется тест-запросом)
#   /prompt <текст>      системный промпт (без аргумента — показать)
#   /save <имя> <текст>  именованный промпт
#   /prompts             список именованных промптов
#   /use <имя>|default   активный промпт
#   /del <имя>           удалить промпт
#   /model [имя]         модель (по умолчанию gemini-3.8-flash)
#   /chat [on|off]       память диалога (Gemini помнит прошлые реплики)
#   /clear               сбросить память в этом чате
#
# База: SQLite (gemini_bot.db рядом со скриптом). Ключи, промпты, модель и
# память — свои на каждого юзера (user_id). История переписки при /chat on
# живёт на серверах Google (previous_interaction_id), локально только id.
# Логи — в терминал, в чат уходят только ответы и ошибки.

import base64
import html
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time

import requests
import telebot

logger = logging.getLogger("gemini_bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# telebot любит спамить «Warning: this message appearance will be changed...»
# и служебным INFO — глушим, в консоли оставляем только своё и ошибки.
logging.getLogger("telebot").setLevel(logging.ERROR)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.8-flash"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "gemini_bot.db")
MAX_FILE = 20 * 1024 * 1024   # лимит скачивания Bot API = лимит инлайн-данных Gemini
MAX_TEXT_DOC = 100_000        # символы для текстовых файлов
ANSWER_LIMIT = 3_800          # лимит сообщения TG 4096, с запасом на оформление
REQUEST_TIMEOUT = (15, 300)   # (connect, read): thinking-модели отвечают долго
POLL_TIMEOUT = 240
TEST_POLL_TIMEOUT = 60

SEP = "──────────"

# «3.7» / «3.8-flash» → «gemini-3.7-flash» и т.п. Полные имена не трогаем.
_MODEL_BARE = re.compile(r"^\d+(?:\.\d+)?(?:-[a-z0-9]+)?$", re.I)

# 503 «high demand» и 429 «quota» значат, что авторизация ПРОШЛА и ключ валиден,
# просто модель перегружена/лимит. Это не «плохой ключ».
TRANSIENT_WORDS = (
    "high demand", "quota", "rate limit", "rate-limit", "too many requests",
    "overloaded", "unavailable", "try again later", "capacity", "temporarily",
    "resource_exhausted", "resource exhausted",
)
TRANSIENT_HINT = (
    "модель перегружена или исчерпан лимит запросов (high demand / quota). "
    "Это временно и НЕ значит, что ключ плохой"
)

NO_KEY_TEXT = (
    "🔑 <b>Сначала привяжи свой Gemini API-ключ</b> — просто отправь его следующим "
    "сообщением (выглядит как <code>AIza...</code>).\n\n"
    "Где взять: зайди с VPN на <code>aistudio.google.com</code> → <b>Get API key</b> → "
    "<b>Create API key</b> и скопируй его сюда.\n\n"
    "Ключ проверю тест-запросом и сохраню в базу — он твой личный, у каждого своя связка."
)

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


# ---------- конфиг ----------

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (ValueError, OSError):
        logger.warning("config.json повреждён — начинаю с пустого")
        return {}


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    try:
        os.chmod(CONFIG_PATH, 0o600)  # внутри токен — не для чужих глаз
    except OSError:
        pass


def ensure_token(cfg: dict) -> str:
    token = (cfg.get("bot_token") or "").strip()
    if token:
        return token
    print("Токен бота не найден в config.json.")
    print("Получи его у @BotFather: /newbot → имя → username → токен.")
    token = input("Вставь токен сюда и нажми Enter: ").strip()
    if not token:
        raise SystemExit("Без токена запускать нечего — пока.")
    cfg["bot_token"] = token
    save_config(cfg)
    print(f"Токен сохранён в {CONFIG_PATH} (больше спрашивать не буду).")
    return token


def apply_proxy(cfg: dict):
    proxy = (cfg.get("proxy") or "").strip()
    if not proxy:
        return
    # requests (запросы к Gemini) берёт прокси из env, telebot — из apihelper
    os.environ["HTTPS_PROXY"] = proxy
    os.environ["HTTP_PROXY"] = proxy
    telebot.apihelper.proxy = {"https": proxy, "http": proxy}
    logger.info("proxy: %s", proxy)


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
    # Ключ строго свой у каждого юзера (из базы), общих ключей из env больше нет.
    return (user.get("api_key") or "").strip()


def system_instruction_for(user: dict) -> str:
    active = (user.get("active_prompt") or "").strip()
    if active:
        prompts = list_prompts(user["user_id"])
        if active in prompts:
            return prompts[active]
    return (user.get("prompt") or "").strip()


def normalize_model(name: str) -> str:
    """«3.7» → «gemini-3.7-flash», «3.8-flash» → «gemini-3.8-flash»; полные имена как есть."""
    n = (name or "").strip()
    if not n or n.lower().startswith("gemini") or n.startswith("models/"):
        return n
    if _MODEL_BARE.match(n):
        if "-" not in n:
            n += "-flash"
        return "gemini-" + n
    return n


def is_transient_msg(msg: str) -> bool:
    blob = (msg or "").lower()
    return any(w in blob for w in TRANSIENT_WORDS)


def model_for(user: dict) -> str:
    return normalize_model(user.get("model") or "") or DEFAULT_MODEL


def extract_text(data: dict) -> str:
    chunks = []
    for step in data.get("steps", []) or []:
        if step.get("type") != "model_output":
            continue
        for block in step.get("content", []) or []:
            if block.get("type") == "text" and block.get("text"):
                chunks.append(block["text"])
    return "\n".join(chunks).strip()


def _poll_interaction(key: str, iid: str, status: str, data: dict, timeout: int) -> dict:
    waited = 0
    while status in ("in_progress", "queued") and iid and waited < timeout:
        time.sleep(3 if timeout > 60 else 2)
        waited += 3 if timeout > 60 else 2
        r = requests.get(f"{API_BASE}/interactions/{iid}",
                         headers={"x-goog-api-key": key}, timeout=REQUEST_TIMEOUT)
        data = r.json()
        status = data.get("status", status)
    return data


def classify_error(status_code: int, data: dict) -> tuple:
    """(kind, message): kind = 'invalid_key' | 'transient' | 'other'.

    'transient' (429/500/503/504, high demand, quota) = авторизация прошла, ключ
    валиден, просто модель перегружена/лимит. 'invalid_key' — ключ реально плохой.
    """
    err = (data or {}).get("error") or {}
    message = err.get("message") or f"HTTP {status_code}"
    status = str(err.get("status") or "").upper()
    blob = f"{message} {status}".lower()
    if (status_code in (401, 403)
            or "api key not valid" in blob or "api_key_invalid" in blob
            or "invalid api key" in blob or "key not valid" in blob
            or "permission_denied" in blob):
        return "invalid_key", message
    if status_code in (429, 500, 503, 504) or is_transient_msg(blob):
        return "transient", message
    return "other", message


def _check_status(data: dict) -> str:
    """Возвращает текст ответа или кидает GeminiError по статусу interaction."""
    status = data.get("status", "")
    if status == "failed":
        errors = data.get("errors") or []
        msg = (errors[0].get("message") if errors and isinstance(errors[0], dict) else "") \
            or "запрос не удался"
        if is_transient_msg(msg):
            msg += "\n\n⚠️ Модель перегружена/лимит — попробуй позже или смени: /model <имя>."
        raise GeminiError(msg)
    text = extract_text(data)
    if not text:
        raise GeminiError("Gemini вернул пустой ответ (возможно, сработал фильтр)")
    return text


def test_key(key: str, model: str = "") -> tuple:
    """Мини-запрос для проверки ключа. Возвращает (kind, detail):
    'ok' (detail — ответ модели) | 'transient' | 'invalid_key' | 'other' | 'network'.
    Один запрос — не жжёт квоту ретраями.
    """
    model = normalize_model(model) or DEFAULT_MODEL
    body = {
        "model": model,
        "input": "Ответь одним словом: OK",
        "store": False,
        "generation_config": {"max_output_tokens": 8},
    }
    try:
        resp = requests.post(f"{API_BASE}/interactions", json=body,
                             headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                             timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        return "network", f"{type(exc).__name__}: {exc}"
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200:
        return classify_error(resp.status_code, data)
    data = _poll_interaction(key, data.get("id", ""), data.get("status", ""), data, TEST_POLL_TIMEOUT)
    if data.get("status", "") == "failed":
        errors = data.get("errors") or []
        msg = (errors[0].get("message") if errors and isinstance(errors[0], dict) else "") \
            or "запрос не удался"
        return ("transient" if is_transient_msg(msg) else "other"), msg
    return "ok", (extract_text(data) or "(ключ сработал, но ответ пустой)")


def looks_like_key(text: str) -> bool:
    t = (text or "").strip()
    return 15 <= len(t) <= 200 and not any(c.isspace() for c in t)


def ask_gemini(parts: list, user: dict, chat_id: int) -> str:
    key = api_key_for(user)
    if not key:
        raise GeminiError("API-ключ не задан: пришли его сообщением или /key <ключ>")

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
    resp = None
    for attempt in (1, 2):
        resp = requests.post(f"{API_BASE}/interactions", json=body, headers=headers,
                             timeout=REQUEST_TIMEOUT)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code in (429, 500, 503, 504) and attempt == 1:
            try:
                wait = min(max(int(resp.headers.get("Retry-After", "5")), 1), 30)
            except ValueError:
                wait = 5
            logger.warning("%s, retry in %ss", resp.status_code, wait)
            time.sleep(wait)
            continue
        break

    if resp is None or resp.status_code != 200:
        kind, err = classify_error(resp.status_code if resp is not None else 0, data or {})
        if kind == "transient":
            err += "\n\n⚠️ Модель перегружена/лимит — попробуй позже или смени: /model <имя>."
        raise GeminiError(err)

    data = _poll_interaction(key, data.get("id", ""), data.get("status", ""), data, POLL_TIMEOUT)
    text = _check_status(data)
    if ctx_on and data.get("id"):
        set_ctx(chat_id, user["user_id"], data["id"])
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


def _download(bot, file_id) -> bytes:
    """Скачивает файл и всегда отдаёт bytes.

    pyTelegramBotAPI download_file возвращает bytes (не файл-объект), но в разных
    версиях бывает BytesIO — поэтому .read() только если он реально есть.
    """
    raw = bot.download_file(bot.get_file(file_id).file_path)
    return raw.read() if hasattr(raw, "read") else raw


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
                    raw = _download(bot, file_id)
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
        data = _download(bot, file_id)
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
    "/key &lt;ключ&gt; — сменить свой Gemini API-ключ\n"
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
    bot = telebot.TeleBot(token, threaded=True, parse_mode="HTML")

    def answer_error(message, text):
        try:
            bot.reply_to(message, f"❌ {esc(text)}")
        except Exception:
            logger.exception("failed to send error")

    def try_save_key(message, key: str):
        """Валидирует ключ тест-запросом к Gemini и сохраняет на юзера.

        Ключ СОХРАНЯЕТСЯ даже если модель перегружена (503/429): это значит, что
        авторизация прошла и ключ валиден. Отклоняем только реально невалидный ключ
        (401/403/API key not valid) и когда до Gemini вовсе не достучаться (гео/прокси).
        """
        uid = message.from_user.id
        model = model_for(get_user(uid))
        status = bot.reply_to(message, f"🔑 Проверяю ключ тест-запросом к Gemini (<code>{esc(model)}</code>)…")
        kind, detail = test_key(key, model)

        if kind == "invalid_key":
            logger.warning("invalid key for %s: %s", uid, detail)
            bot.edit_message_text(
                f"❌ <b>Ключ не принят:</b> {esc(detail)}\n\nПришли другой ключ "
                f"сообщением или через /key &lt;ключ&gt;.",
                message.chat.id, status.message_id)
            return

        if kind == "network":
            logger.warning("key check network error for %s: %s", uid, detail)
            bot.edit_message_text(
                f"❌ <b>Не достучаться до Gemini:</b> {esc(detail)}\n"
                f"Это проблема доступа, а не ключа. Включи VPN/прокси "
                f"(поле <code>proxy</code> в config.json) и пришли ключ ещё раз.",
                message.chat.id, status.message_id)
            return

        # ok / transient / other → авторизация прошла, ключ сохраняем
        set_user(uid, "api_key", key)
        if kind == "ok":
            bot.edit_message_text(
                f"✅ <b>Ключ сохранён и проверен!</b>\n"
                f"Тест-ответ Gemini: <i>{esc(str(detail)[:200])}</i>\n\n"
                f"Теперь просто напиши вопрос или кинь фото/войс. /help — команды.",
                message.chat.id, status.message_id)
        elif kind == "transient":
            bot.edit_message_text(
                f"✅ <b>Ключ сохранён</b> (авторизация прошла).\n"
                f"⚠️ Тест-ответ не получен: {esc(TRANSIENT_HINT)}.\n\n"
                f"Попробуй через минуту или смени модель: <code>/model 3.7</code>.\n"
                f"Детали: <i>{esc(str(detail)[:200])}</i>",
                message.chat.id, status.message_id)
        else:  # other
            bot.edit_message_text(
                f"✅ <b>Ключ сохранён</b> (авторизация прошла).\n"
                f"⚠️ Тест-запрос вернул: <i>{esc(str(detail)[:200])}</i>\n"
                f"Если модель задана криво — поправь: <code>/model &lt;имя&gt;</code>.",
                message.chat.id, status.message_id)

    def handle_command(message, cmd, args):
        uid = message.from_user.id
        user = get_user(uid)

        if cmd in ("start", "help"):
            if not api_key_for(user):
                bot.reply_to(message, HELP_TEXT + "\n\n" + NO_KEY_TEXT)
            else:
                bot.reply_to(message, HELP_TEXT)

        elif cmd == "key":
            if args and looks_like_key(args):
                try_save_key(message, args.strip())
            elif args:
                bot.reply_to(message, "❌ Это не похоже на ключ (без пробелов, 15+ символов).")
            else:
                has = bool(api_key_for(user))
                bot.reply_to(
                    message,
                    f"🔑 Свой ключ: {'привязан ✅' if has else 'НЕ привязан ❌'}\n"
                    f"Задать/сменить: /key &lt;ключ&gt; или просто отправь ключ сообщением.")

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
                norm = normalize_model(args)
                set_user(uid, "model", norm)
                bot.reply_to(message, f"✅ Модель: <code>{esc(norm)}</code>")
            else:
                bot.reply_to(message, f"ℹ️ Модель: <code>{esc(model_for(user))}</code>\n"
                             "Сменить: /model gemini-3.7-flash (или просто /model 3.7)")

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

        # нет ключа — любое текстовое сообщение считается попыткой его ввести
        if not api_key_for(user):
            text = (message.text or "").strip()
            if text and looks_like_key(text):
                try_save_key(message, text)
            else:
                bot.reply_to(message, NO_KEY_TEXT)
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
    cfg = load_config()
    global DB_PATH
    if cfg.get("db_path"):
        DB_PATH = cfg["db_path"]

    token = ensure_token(cfg)
    apply_proxy(cfg)
    db_init()

    bot = create_bot(token)
    logger.info("бот запущен (db: %s, модель по умолчанию: %s)", DB_PATH, DEFAULT_MODEL)
    print("Бот работает. Остановка — Ctrl+C.")

    # SIGTERM (docker/kill) → тот же чистый выход, что и Ctrl+C
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    try:
        # skip_pending: после перезапуска не отвечать на старые сообщения
        bot.infinity_polling(timeout=30, long_polling_timeout=25,
                             skip_pending=True, logger_level=logging.ERROR)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        try:
            bot.stop_bot()  # остановить polling + закрыть пул воркеров
        except Exception:
            pass
        print("\n✅ Бот остановлен. Пока!")
        sys.exit(0)


if __name__ == "__main__":
    main()
