# -*- coding: utf-8 -*-
# xyetanapythonepidor — Telegram-бот (Bot API, БЕЗ MTProto): Gemini / LM Studio / koboldcpp,
# ключ у каждого юзера свой.
#
# Каждый юзер привязывает СВОЙ Gemini API-ключ: при первом сообщении бот
# просит ключ, проверяет его реальным тест-запросом к Gemini и сохраняет в
# базу. Без ключа бот не отвечает — только просит его прислать.
#
# Первый запуск (никаких export не нужно):
#   pip install pyTelegramBotAPI requests
#   python xyetanapythonepidor.py
# Бот сам спросит токен в терминале (получить у @BotFather → /newbot) и
# сохранит его в config.json рядом со скриптом. Остановка — Ctrl+C (чистый
# выход без трейсбеков).
#
# config.json (создаётся автоматически, права 600):
#   {
#     "bot_token": "123456:ABC...",   # обязателен (спросится при запуске)
#     "proxy": "http://127.0.0.1:1080",  # если TG/Gemini блокируются (РФ)
#     "db_path": "/путь/gemini_bot.db",  # необязательно
#
#     # --- локальные бэкенды (LM Studio / koboldcpp), необязательно ---
#     "lmstudio_url": "http://localhost:1234/v1",   # OpenAI-совместимый сервер
#     "koboldcpp_url": "http://localhost:5001/v1",
#     "lmstudio_model": "qwen2.5-7b",   # необяз.: id модели + подпись в статусе
#     "local_for_all": true,   # true — локальные бэкенды доступны ВСЕМ юзерам
#     "owner_id": 123456789    # твой user_id: тебе локалка доступна всегда,
#                              # даже когда local_for_all=false (чтобы тестить)
#   }
#   Любое из этих полей также читается из env (LMSTUDIO_URL, LOCAL_FOR_ALL,
#   OWNER_ID и т.д.) — env используется, если поля нет в config.json.
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
#   /think [уровень]     раздумья Gemini: minimal|low|medium|high (по умолч. medium)
#   /backend [имя]       gemini | lmstudio | koboldcpp (локалка без ключа Gemini)
#   /chat [on|off]       память диалога (Gemini помнит прошлые реплики)
#   /clear               сбросить память в этом чате
#
# Только владелец (owner_id) — управление моделями LM Studio:
#   /lmsmodels           список скачанных моделей (в памяти / активная)
#   /lmsload <id>        загрузить модель в память
#   /lmsunload <id>      выгрузить модель
#   /lmsuse <id>         приоритетная модель (чат берёт ту, что загружена)
#
# База: SQLite (gemini_bot.db рядом со скриптом — имя не меняем, там ключи
# юзеров). Ключи, промпты, модель и
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
import threading
import time

import requests
import telebot

logger = logging.getLogger("xyetanapythonepidor")
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

# Уровень «раздумий» Gemini 3 (thinking_level). Без него модель думает на high —
# отсюда и задержки. medium — баланс; ниже = быстрее.
DEFAULT_THINK = "medium"
THINK_LEVELS = ("minimal", "low", "medium", "high")

# Локальные OpenAI-совместимые бэкенды. Адреса и доступ настраиваются в
# config.json / env: <backend>_url, <backend>_model, local_for_all, owner_id.
LOCAL_BACKENDS = {"lmstudio": "LM Studio", "koboldcpp": "koboldcpp"}
LOCAL_DEFAULT_URL = {
    "lmstudio": "http://localhost:1234/v1",
    "koboldcpp": "http://localhost:5001/v1",
}
LOCAL_TIMEOUT = (10, 600)     # локальная генерация бывает очень долгой

CONFIG = {}                   # заполняется в main() из config.json (+ env-фолбэк)

# Момент запуска процесса. Апдейты, отправленные РАНЬШЕ него, пришли пока бот был
# выключен — мы их не обрабатываем (иначе на старте в Gemini улетел бы весь накопившийся
# флуд), а лишь один раз на чат пишем «перезапустился, пришли запрос ещё раз».
START_TIME = time.time()

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

# Шлётся ОДИН раз на чат за пропущенные оффлайн-сообщения (см. START_TIME).
RESTART_NOTICE = (
    "🔄 <b>Я перезапустился</b> и не видел твои сообщения, пока был выключен — они "
    "не обработаны.\nПришли запрос ещё раз, пожалуйста."
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


# ---------- markdown/latex → telegram html ----------
# Telegram БЕСПЛАТНО рендерит HTML-разметку (<b>,<i>,<code>,<pre>,<a>,<s>) у ботов.
# Gemini же отвечает Markdown'ом и LaTeX ($\text{CaO}$) — превращаем их в HTML и
# юникод, чтобы юзер видел формулы и жирный текст, а не сырьё со звёздочками.

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
    """Простой LaTeX ($...$) → читаемый юникод: \\text{}, индексы, стрелки."""
    s = re.sub(r"\\(?:text|mathrm|mathbf|mathit|mathsf|mbox|operatorname)\s*\{([^{}]*)\}",
               r"\1", s)
    for k, v in _LATEX.items():
        s = s.replace(k, v)
    s = re.sub(r"_\{([^{}]*)\}", lambda m: m.group(1).translate(_SUB), s)
    s = re.sub(r"\^\{([^{}]*)\}", lambda m: m.group(1).translate(_SUP), s)
    s = re.sub(r"_(\S)", lambda m: m.group(1).translate(_SUB), s)
    s = re.sub(r"\^(\S)", lambda m: m.group(1).translate(_SUP), s)
    s = s.replace("\\left", "").replace("\\right", "")
    s = re.sub(r"\\[a-zA-Z]+", "", s)   # неизвестные команды — убираем
    return s.replace("{", "").replace("}", "").replace("\\", "").strip()


def md_to_tg_html(text: str) -> str:
    """Markdown+LaTeX от Gemini → Telegram-HTML. Нераспознанное экранируется."""
    if not text:
        return text
    blocks, inlines = [], []
    text = re.sub(r"```[^\n]*\n?(.*?)```",
                  lambda m: (blocks.append(m.group(1)), f"\x00B{len(blocks)-1}\x00")[1],
                  text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`",
                  lambda m: (inlines.append(m.group(1)), f"\x00I{len(inlines)-1}\x00")[1],
                  text)
    text = re.sub(r"\$\$(.+?)\$\$", lambda m: latex_to_text(m.group(1)), text, flags=re.S)
    text = re.sub(r"\$([^$\n]+?)\$", lambda m: latex_to_text(m.group(1)), text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t#]*$", r"<b>\1</b>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_(?!_)(.+?)(?<![\w_])_(?![\w_])", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.S)
    text = re.sub(r"(?m)^([ \t]*)[*+\-][ \t]+", r"\1• ", text)   # маркеры списка → •
    text = re.sub(r"\x00I(\d+)\x00",
                  lambda m: "<code>" + html.escape(inlines[int(m.group(1))], quote=False) + "</code>",
                  text)
    text = re.sub(r"\x00B(\d+)\x00",
                  lambda m: "<pre><code>" + html.escape(blocks[int(m.group(1))], quote=False) + "</code></pre>",
                  text)
    return text


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
                ctx INTEGER DEFAULT 0,
                think TEXT DEFAULT '',
                backend TEXT DEFAULT 'gemini'
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
        # миграция старой базы: докидываем недостающие колонки
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "think" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN think TEXT DEFAULT ''")
        if "backend" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN backend TEXT DEFAULT 'gemini'")


def get_user(user_id: int) -> dict:
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return dict(row)


def set_user(user_id: int, field: str, value):
    assert field in ("api_key", "prompt", "model", "active_prompt", "ctx", "think", "backend")
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


def think_for(user: dict) -> str:
    lvl = (user.get("think") or "").strip().lower()
    return lvl if lvl in THINK_LEVELS else DEFAULT_THINK


def cfg_get(key: str, default=None):
    """Значение из config.json, иначе из env (KEY в верхнем регистре), иначе default."""
    if key in CONFIG and CONFIG[key] not in (None, ""):
        return CONFIG[key]
    env = os.environ.get(key.upper())
    return env if env not in (None, "") else default


def owner_id():
    v = cfg_get("owner_id")
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def local_for_all() -> bool:
    return str(cfg_get("local_for_all", "")).lower() in ("1", "true", "yes", "on")


def backend_for(user: dict) -> str:
    be = (user.get("backend") or "gemini").strip().lower()
    return be if (be in LOCAL_BACKENDS or be == "gemini") else "gemini"


def backend_allowed(uid: int, be: str) -> bool:
    if be == "gemini":
        return True
    if be not in LOCAL_BACKENDS:
        return False
    own = owner_id()
    return local_for_all() or (own is not None and uid == own)


def local_url(backend: str) -> str:
    return (cfg_get(f"{backend}_url") or LOCAL_DEFAULT_URL.get(backend, "")).rstrip("/")


def local_model_id(backend: str) -> str:
    return cfg_get(f"{backend}_model") or "local-model"


def status_label(user: dict) -> str:
    """Что показываем в статусе «…думает»: имя модели Gemini или локальный бэкенд."""
    be = backend_for(user)
    if be == "gemini":
        return model_for(user)
    if be == "lmstudio":
        # конкретную модель знает только сервер (что сейчас загружено) — в статусе
        # пишем нейтрально, точное имя придёт в строке ответа из data['model']
        return LOCAL_BACKENDS["lmstudio"]
    return cfg_get(f"{be}_model") or LOCAL_BACKENDS[be]


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
    body = {"model": model_for(user), "input": parts,
            "generation_config": {"thinking_level": think_for(user)}}
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


# ---------- локальные бэкенды (LM Studio / koboldcpp, OpenAI-совместимые) ----------

def gemini_parts_to_openai(parts: list) -> tuple:
    """Gemini content-блоки → OpenAI 'content'. Возвращает (content, error).

    Локальные серверы понимают текст и (LM Studio с vision-моделью) картинки как
    data-URI. Аудио/видео/документы не поддерживаются — возвращаем понятную ошибку.
    """
    content = []
    for p in parts:
        t = p.get("type")
        if t == "text":
            content.append({"type": "text", "text": p.get("text", "")})
        elif t == "image":
            mime = p.get("mime_type", "image/jpeg")
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{p.get('data', '')}"}})
        else:
            return None, f"локальный бэкенд не поддерживает тип «{t}» (только текст и фото)"
    return content, None


def ask_local(backend: str, parts: list, user: dict, chat_id: int) -> tuple:
    """Запрос к LM Studio / koboldcpp. Возвращает (text, label)."""
    url = local_url(backend)
    if not url:
        raise GeminiError(f"не задан адрес {backend} (поле {backend}_url в config.json)")
    content, err = gemini_parts_to_openai(parts)
    if err:
        raise GeminiError(err)
    if backend == "lmstudio":
        # целимся в реально загруженную модель, иначе LM Studio авто-загрузит
        # дефолтную из конфига и словит OOM
        model_id, merr = lmstudio_chat_model(cfg_get("lmstudio_model") or "")
        if merr:
            raise GeminiError(merr)
    else:
        model_id = local_model_id(backend)
    messages = []
    si = system_instruction_for(user)
    if si:
        messages.append({"role": "system", "content": si})
    messages.append({"role": "user", "content": content})
    body = {"model": model_id, "messages": messages, "stream": False}
    try:
        resp = requests.post(f"{url}/chat/completions", json=body, timeout=LOCAL_TIMEOUT)
    except requests.RequestException as exc:
        raise GeminiError(
            f"не достучаться до {LOCAL_BACKENDS.get(backend, backend)} ({url}): "
            f"{type(exc).__name__}: {exc}\nСервер запущен и адрес верный?")
    if resp.status_code != 200:
        raise GeminiError(f"{backend} вернул HTTP {resp.status_code}: {(resp.text or '')[:300]}")
    try:
        data = resp.json()
    except ValueError:
        raise GeminiError(f"{backend}: ответ не JSON")
    choices = data.get("choices") or []
    text = ""
    if choices:
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not text:
        raise GeminiError(f"{backend} вернул пустой ответ")
    return text, (data.get("model") or status_label(user))


def dispatch_ask(parts: list, user: dict, chat_id: int) -> tuple:
    """Единая точка: (answer_text, model_label). Выбирает Gemini или локальный бэкенд."""
    be = backend_for(user)
    if be in LOCAL_BACKENDS:
        return ask_local(be, parts, user, chat_id)
    return ask_gemini(parts, user, chat_id), model_for(user)


# ---------- управление моделями LM Studio (только владелец бота) ----------
# Чат идёт по OpenAI-совместимому адресу {host}/v1, а управление — по корню хоста
# в /api/v0/* и /api/v1/*. Поэтому host root = local_url('lmstudio') без «/v1».

LMS_LIST_TIMEOUT = (10, 30)   # listing быстрый; load/unload — LOCAL_TIMEOUT


def is_owner(uid) -> bool:
    own = owner_id()
    return own is not None and uid == own


def lms_mgmt_base() -> str:
    """Корень хоста LM Studio для management-API (chat base без хвоста «/v1»)."""
    base = local_url("lmstudio")
    if base.endswith("/v1"):
        base = base[:-3]
    return base.rstrip("/")


def set_cfg(key: str, value):
    """Меняет значение в памяти (CONFIG) и сохраняет в config.json."""
    CONFIG[key] = value
    try:
        cfg = load_config()
        cfg[key] = value
        save_config(cfg)
    except Exception:
        logger.exception("failed to persist config key %s", key)


def lms_list_models() -> list:
    """GET /api/v0/models → [{id,type,state,...}]. Кидает GeminiError при сбое."""
    base = lms_mgmt_base()
    if not base:
        raise GeminiError("не задан адрес lmstudio (поле lmstudio_url в config.json)")
    try:
        resp = requests.get(f"{base}/api/v0/models", timeout=LMS_LIST_TIMEOUT)
    except requests.RequestException as exc:
        raise GeminiError(
            f"не достучаться до LM Studio ({base}): {type(exc).__name__}: {exc}\n"
            f"Сервер запущен и адрес верный?")
    if resp.status_code != 200:
        raise GeminiError(f"LM Studio вернул HTTP {resp.status_code}: {(resp.text or '')[:200]}")
    try:
        data = resp.json()
    except ValueError:
        raise GeminiError("LM Studio: ответ не JSON")
    return data.get("data") or []


def _lms_instance_id(key: str) -> str:
    """instance_id загруженной модели из /api/v1/models; при неудаче — сам ключ.

    У «дефолтных» инстансов instance_id совпадает с ключом модели, но если модель
    грузили с кастомным конфигом — id отличается, поэтому сначала пробуем уточнить.
    """
    base = lms_mgmt_base()
    try:
        resp = requests.get(f"{base}/api/v1/models", timeout=LMS_LIST_TIMEOUT)
        if resp.status_code == 200:
            for m in (resp.json() or {}).get("models") or []:
                if m.get("key") == key:
                    insts = m.get("loaded_instances") or []
                    if insts and insts[0].get("id"):
                        return insts[0]["id"]
    except Exception:
        logger.debug("instance_id lookup failed for %s", key, exc_info=True)
    return key


def lms_load(key: str) -> tuple:
    """POST /api/v1/models/load {model:key}. Возвращает (ok, message)."""
    base = lms_mgmt_base()
    if not base:
        return False, "не задан адрес lmstudio (поле lmstudio_url в config.json)"
    try:
        resp = requests.post(f"{base}/api/v1/models/load", json={"model": key},
                             timeout=LOCAL_TIMEOUT)
    except requests.RequestException as exc:
        return False, f"не достучаться до LM Studio ({base}): {type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {(resp.text or '')[:200]}"
    return True, "загружена в память"


def lms_unload(key: str) -> tuple:
    """POST /api/v1/models/unload {instance_id}. Возвращает (ok, message)."""
    base = lms_mgmt_base()
    if not base:
        return False, "не задан адрес lmstudio (поле lmstudio_url в config.json)"
    iid = _lms_instance_id(key)
    try:
        resp = requests.post(f"{base}/api/v1/models/unload", json={"instance_id": iid},
                             timeout=LOCAL_TIMEOUT)
    except requests.RequestException as exc:
        return False, f"не достучаться до LM Studio ({base}): {type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {(resp.text or '')[:200]}"
    return True, "выгружена из памяти"


def lmstudio_chat_model(prefer: str = "") -> tuple:
    """Какая модель СЕЙЧАС загружена в LM Studio и пригодна для чата.

    Возвращает (model_key, error). LM Studio АВТОМАТИЧЕСКИ грузит модель, если в
    /v1/chat/completions попросить незагруженную, — отсюда OOM, когда бот слал дефолт
    из конфига вместо реально загруженной модели. Поэтому всегда целимся в уже
    загруженную (llm/vlm, не embeddings); если ничего не загружено — ошибка, а не
    молчаливая автозагрузка.
    """
    try:
        models = lms_list_models()
    except GeminiError:
        # не смогли спросить состояние — падаем на конфиг (прежнее поведение)
        return (prefer or local_model_id("lmstudio")), None
    loaded = [m.get("id") for m in models
              if m.get("state") == "loaded" and m.get("type") in ("llm", "vlm") and m.get("id")]
    if prefer and prefer in loaded:
        return prefer, None
    if loaded:
        return loaded[0], None
    return None, ("в LM Studio сейчас не загружено ни одной чат-модели — а автозагрузка "
                  "может съесть всю память. Загрузи сам: <code>/lmsload &lt;id&gt;</code> "
                  "(список: <code>/lmsmodels</code>) или кнопкой в LM Studio.")


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


def is_stale(message) -> bool:
    """True, если сообщение прислали ДО запуска бота (оно накопилось оффлайн)."""
    d = getattr(message, "date", None)
    return bool(d) and d < START_TIME


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
    "/think [minimal|low|medium|high] — уровень раздумий (ниже = быстрее)\n"
    "/backend [gemini|lmstudio|koboldcpp] — облако Gemini или локальный сервер\n"
    "/chat [on|off] — память диалога\n"
    "/clear — сбросить память в этом чате"
)

# Только для владельца бота (owner_id) — управление моделями LM Studio.
OWNER_HELP = (
    "\n\n<b>Управление LM Studio (только ты):</b>\n"
    "/lmsmodels — список скачанных моделей (какая в памяти / активная)\n"
    "/lmsload &lt;id&gt; — загрузить модель в память\n"
    "/lmsunload &lt;id&gt; — выгрузить модель\n"
    "/lmsuse &lt;id&gt; — приоритетная модель (чат берёт ту, что ЗАГРУЖЕНА)"
)


def create_bot(token: str) -> telebot.TeleBot:
    bot = telebot.TeleBot(token, threaded=True, parse_mode="HTML")

    def answer_error(message, text):
        try:
            bot.reply_to(message, f"❌ {esc(text)}")
        except Exception:
            logger.exception("failed to send error")

    # Пропущенные оффлайн-апдейты: одно уведомление на чат, без обработки запросов.
    restart_notified = set()
    restart_lock = threading.Lock()

    def notify_restart(message):
        chat_id = message.chat.id
        with restart_lock:
            if chat_id in restart_notified:
                return
            restart_notified.add(chat_id)
        try:
            bot.reply_to(message, RESTART_NOTICE)
        except Exception:
            logger.exception("failed to send restart notice")

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

    def handle_lms(message, cmd, args):
        """Управление моделями LM Studio (только владелец). list/load/unload/use."""
        if cmd == "lmsmodels":
            try:
                models = lms_list_models()
            except GeminiError as exc:
                bot.reply_to(message, f"❌ {esc(exc)}")
                return
            if not models:
                bot.reply_to(message, "ℹ️ В LM Studio нет скачанных моделей.")
                return
            active = cfg_get("lmstudio_model") or ""
            lines = []
            for m in models:
                mid = m.get("id") or "?"
                typ = m.get("type") or ""
                loaded = (m.get("state") or "") == "loaded"
                mark = "➤ " if mid == active else ("✅ " if loaded else "• ")
                tail = " — в памяти" if loaded else ""
                lines.append(f"{mark}<code>{esc(mid)}</code>"
                             + (f" <i>({esc(typ)})</i>" if typ else "") + tail)
            body = fit("\n".join(lines))
            bot.reply_to(
                message,
                "<b>Модели LM Studio:</b> (➤ активная, ✅ в памяти)\n" + body
                + "\n\n<code>/lmsload &lt;id&gt;</code> — загрузить, "
                  "<code>/lmsunload &lt;id&gt;</code> — выгрузить, "
                  "<code>/lmsuse &lt;id&gt;</code> — сделать активной для чата.")
            return

        if cmd == "lmsuse":
            key = args.strip()
            if not key:
                cur = cfg_get("lmstudio_model") or "(не задана — используется local-model)"
                bot.reply_to(message, f"ℹ️ Активная модель LM Studio: <code>{esc(cur)}</code>\n"
                             "Сменить: /lmsuse &lt;id&gt; (список: /lmsmodels)")
                return
            set_cfg("lmstudio_model", key)
            bot.reply_to(message, f"✅ Приоритетная модель LM Studio: <code>{esc(key)}</code>.\n"
                         "Чат всегда использует ту модель, что сейчас ЗАГРУЖЕНА в LM Studio "
                         "(чтобы не авто-грузить лишнее и не словить OOM); эта — приоритет, "
                         "когда загружено несколько.\n"
                         "Загрузить: <code>/lmsload " + esc(key) + "</code>, "
                         "бэкенд: <code>/backend lmstudio</code>.")
            return

        # load / unload
        key = args.strip()
        if not key:
            bot.reply_to(message, f"ℹ️ /{cmd} &lt;id модели&gt; (список: /lmsmodels)")
            return
        verb_load = cmd == "lmsload"
        status = bot.reply_to(message,
                              f"⏳ <i>{'Загружаю' if verb_load else 'Выгружаю'} "
                              f"<code>{esc(key)}</code>…</i>")
        ok, detail = (lms_load(key) if verb_load else lms_unload(key))
        icon = "✅" if ok else "❌"
        bot.edit_message_text(f"{icon} <code>{esc(key)}</code>: {esc(detail)}",
                              message.chat.id, status.message_id)

    def handle_command(message, cmd, args):
        uid = message.from_user.id
        user = get_user(uid)

        if cmd in ("start", "help"):
            help_text = HELP_TEXT + (OWNER_HELP if is_owner(uid) else "")
            if not api_key_for(user):
                bot.reply_to(message, help_text + "\n\n" + NO_KEY_TEXT)
            else:
                bot.reply_to(message, help_text)

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

        elif cmd == "think":
            lvl = args.lower()
            if not lvl:
                bot.reply_to(message, f"🧠 Уровень раздумий: <b>{think_for(user)}</b>\n"
                             "Сменить: /think minimal|low|medium|high\n"
                             "Чем ниже — тем быстрее ответ (high — максимум рассуждений, "
                             "по умолчанию у Gemini 3).")
            elif lvl not in THINK_LEVELS:
                bot.reply_to(message, "❌ Доступно: <code>minimal</code>, <code>low</code>, "
                             "<code>medium</code>, <code>high</code>.")
            else:
                set_user(uid, "think", lvl)
                bot.reply_to(message, f"🧠 Уровень раздумий: <b>{lvl}</b>.")

        elif cmd == "backend":
            cur = backend_for(user)
            be = args.lower()
            if not be:
                avail = ["gemini"] + [b for b in LOCAL_BACKENDS if backend_allowed(uid, b)]
                bot.reply_to(message, f"ℹ️ Бэкенд: <b>{esc(cur)}</b>\n"
                             f"Доступны тебе: {', '.join('<code>'+esc(a)+'</code>' for a in avail)}\n"
                             "Сменить: /backend gemini|lmstudio|koboldcpp")
            elif be not in LOCAL_BACKENDS and be != "gemini":
                bot.reply_to(message, "❌ Не знаю такого бэкенда. Есть: "
                             "<code>gemini</code>, <code>lmstudio</code>, <code>koboldcpp</code>.")
            elif not backend_allowed(uid, be):
                bot.reply_to(message, "❌ Локальные бэкенды сейчас отключены для всех "
                             "(владелец бота не включил <code>local_for_all</code>).")
            else:
                set_user(uid, "backend", be)
                if be == "gemini":
                    bot.reply_to(message, "✅ Бэкенд: <b>Gemini</b> (облако, нужен свой API-ключ).")
                else:
                    bot.reply_to(message, f"✅ Бэкенд: <b>{esc(LOCAL_BACKENDS[be])}</b> "
                                 f"(<code>{esc(local_url(be))}</code>). API-ключ Gemini не нужен.")

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

        elif cmd in ("lmsmodels", "lmsload", "lmsunload", "lmsuse"):
            if not is_owner(uid):
                bot.reply_to(message, "❌ Управление моделями LM Studio — только для владельца бота.")
                return
            handle_lms(message, cmd, args)

        else:
            bot.reply_to(message, "Не знаю такой команды. /help")

    # --- альбомы (media_group): все фото вместе, ОДИН запрос вместо N ---
    albums = {}          # media_group_id -> [messages]
    album_timers = {}    # media_group_id -> threading.Timer
    recent_albums = {}   # media_group_id -> (timestamp, [media msgs]) для ответов на альбом
    album_lock = threading.Lock()
    ALBUM_WAIT = 1.2     # сколько ждём остальные фото альбома
    ALBUM_TTL = 180      # сколько храним альбом для возможных ответов на него

    def _cache_album(mgid, media_msgs):
        now = time.time()
        with album_lock:
            recent_albums[mgid] = (now, media_msgs)
            for k in [k for k, (ts, _) in recent_albums.items() if now - ts > ALBUM_TTL]:
                recent_albums.pop(k, None)
            if len(recent_albums) > 30:
                for k in sorted(recent_albums, key=lambda x: recent_albums[x][0])[:-30]:
                    recent_albums.pop(k, None)

    def _cached_album(mgid):
        if not mgid:
            return None
        with album_lock:
            item = recent_albums.get(mgid)
            if not item:
                return None
            ts, msgs = item
            if time.time() - ts > ALBUM_TTL:
                recent_albums.pop(mgid, None)
                return None
            return list(msgs)

    def default_group_query(media_msgs):
        kinds = [media_kind(m) for m in media_msgs]
        if len(media_msgs) > 1:
            if all(k in ("photo", "sticker") for k in kinds):
                return "Что изображено на этих фото? Опиши кратко."
            return "Опиши эти медиа кратко."
        k = kinds[0] if kinds else ""
        if k in ("photo", "sticker"):
            return DEFAULT_QUERY["photo"]
        if k == "video":
            return DEFAULT_QUERY["video"]
        if k in ("voice", "audio"):
            return DEFAULT_QUERY["audio"]
        if k == "document":
            return DEFAULT_QUERY["document"]
        return "Опиши это."

    def ask_and_reply(chat_id, media_msgs, query, reply_text, user, initial_status):
        """Статус → качаем медиа → ОДИН вопрос (Gemini или локалка) → правим в ответ."""
        label = status_label(user)
        status_msg = bot.send_message(chat_id, initial_status)
        parts = []
        for m in media_msgs:
            media_parts, err = build_media_parts(bot, m)
            if err:
                bot.edit_message_text(f"❌ {esc(err)}", chat_id, status_msg.message_id)
                return
            parts.extend(media_parts)
        if reply_text:
            parts.append({"type": "text", "text": f"Сообщение, на которое я отвечаю:\n{reply_text}"})
        parts.append({"type": "text", "text": query})
        head = f"<b>{esc(query[:500])}</b>\n{SEP}\n"
        bot.edit_message_text(head + f"⏳ <i>{esc(label)} думает…</i>",
                              chat_id, status_msg.message_id)
        try:
            answer, used = dispatch_ask(parts, user, chat_id)
        except GeminiError as exc:
            logger.error("api error: %s", exc)
            bot.edit_message_text(head + f"❌ {esc(exc)}", chat_id, status_msg.message_id)
            return
        except Exception as exc:
            logger.exception("unexpected error")
            bot.edit_message_text(head + f"❌ {esc(type(exc).__name__)} (см. логи)",
                                  chat_id, status_msg.message_id)
            return
        bot.edit_message_text(head + f"🤖 <b>{esc(used)}</b> — ответ:\n{md_to_tg_html(fit(answer))}",
                              chat_id, status_msg.message_id)

    def handle_group(msgs):
        first = msgs[0]
        chat_id = first.chat.id
        uid = first.from_user.id if first.from_user else 0
        user = get_user(uid)
        if backend_for(user) == "gemini" and not api_key_for(user):
            bot.reply_to(first, NO_KEY_TEXT)
            return
        media_msgs = [m for m in msgs if media_kind(m)]
        query = ""
        for m in msgs:
            cap = (getattr(m, "caption", "") or "").strip()
            if cap:
                query = cap
                break
        if not media_msgs and not query:
            return
        if not query:
            query = default_group_query(media_msgs)
        ask_and_reply(chat_id, media_msgs, query, "", user,
                      f"📷 Альбом получен: {len(media_msgs)} медиа — загружаю…")

    def flush_album(mgid):
        with album_lock:
            msgs = albums.pop(mgid, [])
            album_timers.pop(mgid, None)
        if not msgs:
            return
        _cache_album(mgid, [m for m in msgs if media_kind(m)])
        try:
            handle_group(msgs)
        except Exception:
            logger.exception("album error")

    def schedule_album(mgid, message):
        with album_lock:
            albums.setdefault(mgid, []).append(message)
            old = album_timers.get(mgid)
            if old:
                old.cancel()
            timer = threading.Timer(ALBUM_WAIT, flush_album, args=(mgid,))
            album_timers[mgid] = timer
            timer.start()

    def handle_content(message):
        """Одиночное сообщение (не альбом): текст/медиа/ответ на медиа → Gemini."""
        uid = message.from_user.id if message.from_user else 0
        user = get_user(uid)
        chat_id = message.chat.id

        # нет ключа — любое текстовое сообщение считается попыткой его ввести
        # (для локальных бэкендов ключ Gemini не нужен)
        if backend_for(user) == "gemini" and not api_key_for(user):
            text = (message.text or "").strip()
            if text and looks_like_key(text):
                try_save_key(message, text)
            else:
                bot.reply_to(message, NO_KEY_TEXT)
            return

        media_msgs = []
        reply_text = ""
        if media_kind(message):
            media_msgs = [message]
            query = (message.caption or "").strip()
        elif getattr(message, "reply_to_message", None):
            reply = message.reply_to_message
            cached = _cached_album(getattr(reply, "media_group_id", None))
            if cached:
                media_msgs = cached            # ответ на альбом → берём все его фото
            elif media_kind(reply):
                media_msgs = [reply]
            elif (reply.text or reply.caption or "").strip():
                reply_text = (reply.text or reply.caption or "").strip()[:8_000]
            query = (message.text or "").strip()
        else:
            query = (message.text or "").strip()

        if not media_msgs and not query and not reply_text:
            return  # пустое сообщение — игнор
        if media_msgs and not query:
            query = default_group_query(media_msgs)
        if not query and reply_text:
            query = "Ответь на это сообщение."

        n = len(media_msgs)
        if n > 1:
            initial = f"📷 Альбом: {n} медиа — загружаю…"
        elif n == 1:
            initial = "📥 Медиа получено — загружаю…"
        else:
            initial = f"⏳ <i>{esc(status_label(user))} думает…</i>"
        ask_and_reply(chat_id, media_msgs, query, reply_text, user, initial)

    @bot.message_handler(commands=[
        "start", "help", "key", "prompt", "prompts", "save", "use", "del",
        "model", "think", "backend", "chat", "clear",
        "lmsmodels", "lmsload", "lmsunload", "lmsuse",
    ])
    def on_command(message):
        if is_stale(message):
            notify_restart(message)   # накопилось оффлайн — не выполняем, зовём повторить
            return
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
        if is_stale(message):
            notify_restart(message)   # пропущенные оффлайн-запросы — один раз зовём повторить
            return
        try:
            mgid = getattr(message, "media_group_id", None)
            if mgid:
                schedule_album(mgid, message)   # соберём альбом и спросим один раз
                return
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
    global DB_PATH, CONFIG
    CONFIG = cfg
    if cfg.get("db_path"):
        DB_PATH = cfg["db_path"]

    token = ensure_token(cfg)
    apply_proxy(cfg)
    db_init()

    bot = create_bot(token)
    logger.info("бот запущен (db: %s, модель по умолчанию: %s)", DB_PATH, DEFAULT_MODEL)
    logger.info("локальные бэкенды: local_for_all=%s, owner_id=%s; lmstudio=%s, koboldcpp=%s",
                local_for_all(), owner_id(), local_url("lmstudio"), local_url("koboldcpp"))
    print("Бот работает. Остановка — Ctrl+C.")

    # SIGTERM (docker/kill) → тот же чистый выход, что и Ctrl+C
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    try:
        # Пропущенные оффлайн-апдейты забираем (skip_pending=False), но не обрабатываем:
        # is_stale() отсечёт их и один раз на чат попросит повторить запрос.
        bot.infinity_polling(timeout=30, long_polling_timeout=25,
                             skip_pending=False, logger_level=logging.ERROR)
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
