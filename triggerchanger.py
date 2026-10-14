# -*- coding: utf-8 -*-
# TriggerChanger — мгновенная автозамена в исходящих сообщениях (Hikka / Heroku).
#
# Как работает: watcher ловит ТВОИ отправленные сообщения, прогоняет текст
# по словарю замен и тут же редактирует сообщение, если что-то совпало.
# Правила хранятся в БД (json Heroku); картинки — base64 в отдельном ключе
# "photo:<pid>", чтобы правка одного правила не переписывала весь json.
#
# Команды:
#   .repmenu                       меню правил кнопками (основной вход)
#   .repadd <триггер> [-r|-w|-c|-n|-e]   добавить замену (визард: замена -> картинка?)
#   .replist [страница]            список правил
#   .repdel <id|all>               удалить правило / всё
#   .reptoggle [on|off]            вкл/выкл автозамену
#   .repclean [on|off]             вкл/выкл чистку ссылок от трекеров
#   .represet да                   полный сброс: правила, картинки, настройки
#   .replogs [id|off]              куда кидать форму отката (по умолчанию — чат логов Hikka)
#   .reptest <текст>               проверить текст без отправки
#
# Режимы совпадения (флаги при добавлении):
#   (без флага)   подстрока, регистр не важен: "да" ловит "да", "Да", "ДА"
#   -w / --word   только целое слово: "да" НЕ ловит "даже"
#   -r / --regex  regex-триггер: например "[?&]si=[^&\s]+" вырезает
#                 трекинг si= из ссылок ютуба (в качестве замены укажи "-")
#                 Для ссылок лучше не городить регулярку, а включить .repclean —
#                 он разбирает query и не ломает ссылку, где si= стоит первым.
#   -c / --case   учитывать регистр
#   -n / --neg    не срабатывать после «не»/«нет»/«неа»/«ни» (подстрока/слово):
#                 «удивлен -n» не тронет «не удивлен», «нет удивлен», «неа удивлен»
#   -e / --end    только в конце сообщения (подстрока/слово): после совпадения
#                 больше нет слов — «нет -e» заменит «нет», «ну нет», «нет!»,
#                 но не тронет «нет удивлен», «нет, спасибо»
#
# Визард .repadd:
#   1) триггер из аргументов (или ответом, если не указан)
#   2) ответь текстом-заменой ("-" = пустая замена, просто вырезать),
#      или картинкой — тогда замена будет картинкой без текста
#   3) бот спросит про картинку: ответь "да" и пришли картинку, или "нет"
#      -> если картинка есть, она отправляется ответом на сработавшее сообщение
# "отмена" на любом шаге — отмена. Отвечать нужно в том же чате, где вызван
# .repadd: в других чатах визард твои сообщения не перехватывает.
#
# Меню .repmenu (то же самое, но кнопками и переживает рестарт dyno):
#   список правил -> карточка правила: режим, регистр, «не после не», «только
#   в конце», текст замены, картинка, удаление. Текст вводится инпут-кнопкой
#   (телега сама подставит @бот в поле ввода), а не перехватом ответов в чате.
#   Картинка — исключение: её кнопкой не передать, поэтому после «🖼 загрузить»
#   пришли фото сообщением в тот же чат, где открыто меню (фото из других чатов
#   модуль не подхватывает).

import base64
import io
import logging
import re
import time

from .. import loader, utils

logger = logging.getLogger(__name__)

MODNAME = "TriggerChanger"
# Heroku на каждый db.set переписывает весь json и держит 15 ревизий в памяти,
# поэтому картинка в base64 дороже, чем выглядит: 2 МБ файла -> ~2.7 МБ текста
MAX_PHOTO = 2 * 1024 * 1024
PER_PAGE = 30
MENU_PER_PAGE = 8
BTN_TEXT = 60  # телега режет подпись кнопки на 64 символах — оставляем запас
# answerCallbackQuery принимает только 200 символов — длинный текст исключения
# превратил бы подсказку в новое падение
ALERT_MAX = 195
# инлайн-форма не умеет разбивать текст, как utils.answer: длинные триггеры и
# замены показываем обрезанными, иначе форма просто не отрисуется
CARD_TEXT = 400
UNDO_PREVIEW = 500
MODE_NAMES = {"sub": "подстрока", "word": "целое слово", "regex": "regex"}
MODE_CYCLE = ("sub", "word", "regex")
CANCEL_WORDS = ("отмена", "cancel", "стоп", "stop", "хватит")
YES_WORDS = ("да", "yes", "+", "угу", "ага")
NO_WORDS = ("нет", "no", "-", "неа")
MAX_EDITED = 500  # сколько последних правок помним, чтобы не править дважды
EDITED_TTL = 600
REEDIT_GUARD = 300  # одно и то же сообщение повторно не правим 5 минут
WIZARD_TTL = 900  # брошенный .repadd не должен вечно глотать сообщения
PHOTO_WAIT_TTL = 300  # столько ждём картинку из меню, чтобы не прилипла чужая
# группа 1 = «не»/«нет»/«ни»/«неа» перед совпадением: с флагом -n такие
# вхождения не трогаем (см. _apply)
NEG_PREFIX = r"((?:[нН][еЕ][тТ]|[нН][еЕ][аА]|[нН][иИ]|[нН][еЕ])\s+)?"
# хвост для -e: дальше до конца сообщения — только не-словá (знаки, эмодзи)
END_SUFFIX = r"(?=[\W]*$)"
# символы, по которым подозреваем забытый -r: в обычных триггерах почти не бывают
REGEX_CHARS = "\\[]{}|*+"

URL_RX = re.compile(r"""https?://[^\s<>"']+""")
# трекер-параметры, которые режем в любых ссылках
TRACKER_PARAMS = {
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "yclid",
    "igshid", "igsh", "mc_cid", "mc_eid", "_openstat", "sxsrf",
}
# а эти — только у ютуба (в других доменах могут быть полезными)
YT_PARAMS = {"si", "feature"}
YT_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
URL_TAIL = ".,;:!?»'\""
FORM_TTL = 900
UNDO_TTL = 3600
MAX_UNDO = 20
LOG_MODULES = ("hikka_logs", "tester")


@loader.tds
class TriggerChangerMod(loader.Module):
    """Автозамена слов/триггеров в отправляемых сообщениях, с БД и картинками"""

    strings = {
        "name": MODNAME,
        "cfg_enabled": "Автозамена в исходящих сообщениях",
        "cfg_clean_links": "Вырезать трекеры (si=, utm_*, fbclid и т.п.) из ссылок",
        "cfg_undo": "Кидать в чат логов форму «↩️ откатить» после каждой автоправки",
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "enabled",
                True,
                lambda: self.strings("cfg_enabled"),
                validator=loader.validators.Boolean(),
            ),
            loader.ConfigValue(
                "clean_links",
                False,
                lambda: self.strings("cfg_clean_links"),
                validator=loader.validators.Boolean(),
            ),
            loader.ConfigValue(
                "undo",
                True,
                lambda: self.strings("cfg_undo"),
                validator=loader.validators.Boolean(),
            ),
        )

    async def client_ready(self, client, db):
        self.client = client
        self.db = db
        self._pending = {}  # uid -> состояние визарда
        self._edited = {}  # (chat_id, msg_id) -> ts, страховка от повторной обработки
        self._undo = {}  # key -> {"msg", "orig", "ts} для отката автоправок
        self._photo_wait = {}  # uid юзера -> (uid правила, ts, chat_id): ждём картинку
        self._menu_forms = {}  # uid -> открытое меню, чтобы обновить его из watcher
        self._menu_chat = {}  # uid -> чат, где открыто меню: там же ждём картинку
        # настройки жили в ключах БД — переносим в config, чтобы правда была одна
        for key in ("enabled", "clean_links"):
            old = self._get(key)
            if old is not None:
                self.config[key] = bool(old)
                self._set(key, None)
        entries = self._get("entries", [])
        if not isinstance(entries, list):
            entries = []
            self._set("entries", entries)
        # после старых удалений id могли остаться «с дырками» (напр. 3, 4, 5) —
        # пересобираем нумерацию подряд: 1..N; заодно выдаём недостающие uid
        if [e.get("id") for e in entries] != list(range(1, len(entries) + 1)) or any(
            not e.get("uid") for e in entries
        ):
            self._store_entries(entries)
        # в старых версиях base64 картинки лежал прямо в правиле: любая правка
        # списка переписывала весь json с балластом — разносим по ключам
        if self._migrate_photos(entries):
            self._store_entries(entries)
        self._gc_photos(entries)

    # ---------- БД ----------

    def _get(self, key, default=None):
        try:
            value = self.db.get(MODNAME, key)
            return default if value is None else value
        except Exception as exc:
            logger.error("[TriggerChanger] db read %s: %r", key, exc)
            return default

    def _set(self, key, value):
        """Записать ключ. None — удалить ключ совсем.

        db.set(key, None) не удаляет запись, а оставляет мёртвый ключ: за
        месяцы правок таких ключей набирается столько же, сколько картинок.
        """
        try:
            if value is None:
                namespace = self.db.setdefault(MODNAME, {})
                if key in namespace:
                    del namespace[key]
                    self.db.save()
                return
            self.db.set(MODNAME, key, value)
        except Exception as exc:
            logger.error("[TriggerChanger] db write %s: %r", key, exc)

    def _store_entries(self, entries):
        """Сохранить правила, пересобрав нумерацию подряд: 1..N (без дырок).

        Заодно выдаём каждому правилу постоянный uid: id после удалений
        съезжают, а кнопка меню должна вести на то же правило, которое на ней
        рисовали.
        """
        seq = int(self._get("uid_seq", 0) or 0)
        for index, entry in enumerate(entries, 1):
            entry["id"] = index
            if not entry.get("uid"):
                seq += 1
                entry["uid"] = f"u{seq}"
        self._set("uid_seq", seq)
        self._set("entries", entries)

    # ---------- картинки правил (отдельные ключи БД) ----------

    def _save_photo(self, data_b64):
        """Положить base64 в свой ключ БД, вернуть pid.

        pid не зависит от id правила: нумерация правил пересобирается при
        каждом удалении, а фото-ключи должны оставаться на месте.
        """
        seq = int(self._get("photo_seq", 0) or 0) + 1
        self._set("photo_seq", seq)
        pid = f"p{seq}"
        self._set(f"photo:{pid}", data_b64)
        return pid

    def _photo_b64(self, entry):
        pid = entry.get("pid")
        return self._get(f"photo:{pid}") if pid else None

    def _drop_photo(self, entry):
        pid = entry.get("pid")
        if pid:
            self._set(f"photo:{pid}", None)

    def _migrate_photos(self, entries):
        moved = False
        for entry in entries:
            if "photo" not in entry:
                continue
            data = entry.pop("photo")
            if data:
                entry["pid"] = self._save_photo(data)
            moved = True
        return moved

    def _photo_keys(self):
        try:
            namespace = self.db.setdefault(MODNAME, {})
            return [
                k for k in namespace
                if isinstance(k, str) and k.startswith("photo:")
            ]
        except Exception as exc:
            logger.error("[TriggerChanger] photo keys scan: %r", exc)
            return []

    def _gc_photos(self, entries):
        """Убрать photo:*-ключи, на которые не ссылается ни одно правило.

        Каждый такой ключ — до ~2.7 МБ base64 в json, который Heroku переписывает
        целиком на любую правку: осиротевшие картинки стоят дороже, чем выглядят.
        """
        alive = {f"photo:{e.get('pid')}" for e in entries if e.get("pid")}
        for key in self._photo_keys():
            if key not in alive:
                self._set(key, None)
                logger.info("[TriggerChanger] удалён осиротевший ключ %s", key)

    def _drop_all_photos(self):
        for key in self._photo_keys():
            self._set(key, None)

    # ---------- замена ----------

    def _compile(self, entry):
        trigger = entry.get("trigger", "")
        if not trigger:
            return None
        flags = 0 if entry.get("case") else re.IGNORECASE
        mode = entry.get("mode", "sub")
        if mode == "regex":
            pattern = trigger
        else:
            core = re.escape(trigger)
            if entry.get("neg"):
                # группа 1 = отрицание перед совпадением (см. NEG_PREFIX);
                # -n такие вхождения пропускает (см. _apply)
                core = NEG_PREFIX + core
            if mode == "word":
                core = r"(?<!\w)" + core + r"(?!\w)"
            if entry.get("end"):
                # после совпадения — только пробелы/знаки/эмодзи до конца
                core = core + END_SUFFIX
            pattern = core
        try:
            return re.compile(pattern, flags)
        except (re.error, ValueError, RecursionError):
            # например паттерн уже содержит inline-флаги (?i) — тогда
            # переданные в compile() флаги запрещены, пробуем без них
            try:
                return re.compile(pattern)
            except (re.error, ValueError, RecursionError):
                return None

    def _regex_error(self, trigger):
        """Проверить регулярку до записи в БД, вернуть текст ошибки или None.

        Ловим и RecursionError: глубоко вложенные скобки роняют компилятор,
        а не возвращают re.error.
        """
        try:
            re.compile(trigger)
        except (re.error, ValueError, RecursionError) as exc:
            return str(exc) or type(exc).__name__
        return None

    def _clean_links(self, text):
        """Вырезать трекер-параметры (si=, utm_* и т.п.) из URL в тексте.

        Делается разбором query, а не регуляркой: параметр может стоять
        первым, в середине и последним, и в каждом случае ссылка должна
        остаться рабочей.
        """
        if not self.config["clean_links"]:
            return text

        def clean(match):
            url = match.group(0)
            trail = ""
            while url and url[-1] in URL_TAIL:
                trail = url[-1] + trail
                url = url[:-1]
            while url.endswith(")") and url.count("(") < url.count(")"):
                trail = ")" + trail
                url = url[:-1]
            head, sep, rest = url.partition("?")
            if not sep or not rest:
                return match.group(0)
            frag = ""
            if "#" in rest:
                rest, _, f = rest.partition("#")
                frag = "#" + f
            parts = head.split("/")
            host = parts[2].lower() if len(parts) > 2 else ""
            yt = host.endswith(YT_HOSTS)
            kept = []
            for part in rest.split("&"):
                if not part:
                    continue
                key = part.split("=", 1)[0].lower()
                if key.startswith("utm_") or key in TRACKER_PARAMS:
                    continue
                if yt and key in YT_PARAMS:
                    continue
                kept.append(part)
            if kept:
                head += "?" + "&".join(kept)
            return head + frag + trail

        return URL_RX.sub(clean, text)

    def _apply(self, text):
        """Вернуть (новый текст, сработавшие правила).

        Совпадение ищем по исходному тексту — чтобы картинка правила не
        терялась, если более раннее правило уже переписало его триггер.
        """
        entries = self._get("entries", []) or []
        new_text = self._clean_links(text)
        matched = []
        for entry in entries:
            rx = self._compile(entry)
            if rx is None:
                continue
            neg = bool(entry.get("neg")) and entry.get("mode", "sub") != "regex"
            if neg:
                # срабатываем, только если есть вхождение БЕЗ отрицания перед ним
                applies = any(m.group(1) is None for m in rx.finditer(text))
            else:
                applies = rx.search(text) is not None
            if not applies:
                continue
            matched.append(entry)
            # repl=None — картинка без текста, сообщение не трогаем;
            # repl="" — явно вырезать триггер из текста
            repl = entry.get("repl")
            if repl is not None:
                if neg:
                    new_text = rx.sub(
                        lambda m: m.group(0) if m.group(1) else repl, new_text
                    )
                else:
                    new_text = rx.sub(lambda _: repl, new_text)
        return new_text, matched

    def _find_dup(self, trigger, mode, case):
        for entry in self._get("entries", []) or []:
            if (
                entry.get("trigger") == trigger
                and entry.get("mode", "sub") == mode
                and bool(entry.get("case")) == bool(case)
            ):
                return entry
        return None

    # ---------- откат автоправок ----------

    def _logchat(self):
        """Куда кидать форму отката: свой чат важнее, иначе берём hikka-логи."""
        own = self._get("logchat")
        if isinstance(own, int) and own:
            return own
        for name in LOG_MODULES:
            try:
                mod = self.lookup(name)
            except Exception:
                continue
            chat = getattr(mod, "logchat", None)
            if isinstance(chat, int) and chat:
                return chat
        return None

    def _chat_name(self, message):
        chat = getattr(message, "chat", None)
        title = getattr(chat, "title", None)
        if title:
            return title
        first = getattr(chat, "first_name", None)
        return f"л/с {first}" if first else "?"

    def _prune_undo(self):
        cutoff = time.time() - UNDO_TTL
        for key in [k for k, v in self._undo.items() if v["ts"] < cutoff]:
            self._undo.pop(key, None)
        if len(self._undo) > MAX_UNDO:
            oldest = sorted(self._undo, key=lambda k: self._undo[k]["ts"])
            for key in oldest[: len(self._undo) - MAX_UNDO]:
                self._undo.pop(key, None)

    def _remember_undo(self, message, orig):
        key = f"{message.chat_id}:{message.id}"
        self._undo[key] = {"msg": message, "orig": orig, "ts": time.time()}
        self._prune_undo()
        return key

    async def _notify_undo(self, message, key):
        """В TG летит только форма отката — остальное остаётся в терминале."""
        chat = self._logchat()
        if chat is None:
            logger.info("[TriggerChanger] msg %s поправлено, но чат логов не задан", message.id)
            return
        entry = self._undo.get(key) or {}
        orig = entry.get("orig", "")
        if len(orig) > UNDO_PREVIEW:
            orig = orig[:UNDO_PREVIEW] + "…"
        header = (
            "🔁 <b>TriggerChanger</b> поправил твоё сообщение\n"
            f"<code>{utils.escape_html(orig)}</code>\n"
            f"💬 <i>{utils.escape_html(self._chat_name(message))}</i>"
        )
        try:
            await self.inline.form(
                header + "\n\nОткатить правку?",
                message=chat,
                reply_markup=[[
                    {"text": "↩️ откатить", "callback": self._inline_undo, "args": (key,)},
                    {"text": "ок", "callback": self._inline_keep, "args": (key,)},
                ]],
                ttl=FORM_TTL,
                force_me=True,
                on_unload=lambda *_: self._undo.pop(key, None),
            )
        except Exception as exc:
            logger.warning("[TriggerChanger] undo form failed: %r", exc)

    async def _inline_undo(self, call, key):
        entry = self._undo.pop(key, None)
        if entry is None:
            await call.answer("поздно — правка уже забыта", alert=True)
            await call.delete()
            return
        try:
            # текст сообщения не парсим как HTML: в нём может быть «<3» или «&»
            await entry["msg"].edit(entry["orig"], parse_mode=lambda t: (t, ()))
        except Exception as exc:
            logger.warning("[TriggerChanger] revert failed (%s): %r", key, exc)
            await call.answer(f"не откатилось: {exc}"[:ALERT_MAX], alert=True)
            await call.delete()
            return
        await call.answer("откатил")
        await call.delete()

    async def _inline_keep(self, call, key):
        self._undo.pop(key, None)
        await call.answer("ок")
        await call.delete()

    # ---------- watcher ----------

    @loader.watcher()
    async def watcher(self, message):
        try:
            if not message.out:
                return
            uid = message.sender_id
            if uid is None:
                return
            text = message.text or ""
            if not isinstance(text, str):
                return

            # команды не глотаем даже во время визарда: иначе .replist,
            # отправленный вместо ответа боту, уедет текстом замены
            for prefix in self.get_prefixes():
                if prefix and text.startswith(prefix):
                    return

            # активный визард перехватывает ответы пользователя — но только в
            # том чате, где его запустили: иначе забытый .repadd превратит
            # сообщение собеседнику в подсказку бота
            state = self._pending.get(uid)
            if state and state.get("chat_id") not in (None, message.chat_id):
                state = None
            if state:
                if time.time() - state.get("ts", 0) > WIZARD_TTL:
                    self._pending.pop(uid, None)
                    logger.info("[TriggerChanger] визард юзера %s истёк", uid)
                else:
                    await self._wizard_step(message, text)
                    return

            # меню ждёт картинку для правила: глотаем только фото и «отмена»,
            # остальные сообщения живут своей жизнью. Просроченное ожидание
            # снимаем — иначе случайное фото через час прилипнет к правилу.
            # Ждём только в том чате, где открыто меню: личное фото, отправленное
            # собеседнику, не должно становиться картинкой правила.
            wait = self._photo_wait.get(uid)
            if wait:
                if time.time() - wait[1] > PHOTO_WAIT_TTL:
                    self._photo_wait.pop(uid, None)
                elif message.chat_id == wait[2]:
                    if message.photo:
                        await self._attach_photo(message, wait[0])
                        return
                    if text.strip().lower() in CANCEL_WORDS:
                        self._photo_wait.pop(uid, None)
                        await utils.answer(message, "❌ Загрузка картинки отменена.")
                        return

            if not self.config["enabled"] or not text:
                return
            if message.fwd_from or message.via_bot_id:
                return
            # id сообщений в супергруппах свои для каждого чата — ключом берём
            # пару (чат, сообщение), иначе правка в одном чате заблокирует
            # сообщение с тем же номером в другом
            key = (message.chat_id, message.id)
            ts = self._edited.get(key)
            if ts is not None and time.time() - ts < REEDIT_GUARD:
                return

            new_text, matched = self._apply(text)
            photo_entries = [e for e in matched if e.get("pid")]

            if new_text != text and 0 < len(new_text) <= 4096:
                try:
                    # parse_mode-колбэк отключает разбор HTML: в замене может
                    # быть «<3» или «&», а клиент по умолчанию парсит текст
                    await message.edit(new_text, parse_mode=lambda t: (t, ()))
                except Exception as exc:
                    logger.warning("[TriggerChanger] edit failed (msg %s): %r", message.id, exc)
                    return
                self._mark_edited(key)
                # без чата логов форма отката всё равно не уйдёт — нечего
                # держать в памяти объект сообщения
                if self.config["undo"] and self._logchat() is not None:
                    await self._notify_undo(
                        message, self._remember_undo(message, text)
                    )

            if photo_entries:
                try:
                    b64 = self._photo_b64(photo_entries[0])
                    if b64:
                        bio = io.BytesIO(base64.b64decode(b64))
                        # без имени Telethon шлёт поток безымянным документом
                        bio.name = "photo.jpg"
                        await message.reply(file=bio)
                        self._mark_edited(key)
                except Exception as exc:
                    logger.warning("[TriggerChanger] photo send failed: %r", exc)
        except Exception as exc:
            logger.exception("[TriggerChanger] watcher error: %r", exc)

    def _mark_edited(self, key):
        self._edited[key] = time.time()
        if len(self._edited) <= MAX_EDITED:
            return
        cutoff = time.time() - EDITED_TTL
        self._edited = {k: v for k, v in self._edited.items() if v > cutoff}
        # в активном чате записи не успевают состариться — режем по количеству,
        # иначе словарь растёт без предела
        if len(self._edited) > MAX_EDITED:
            fresh = sorted(self._edited, key=self._edited.get)[-MAX_EDITED:]
            self._edited = {k: self._edited[k] for k in fresh}

    # ---------- визард ----------

    async def _set_photo(self, state, message):
        try:
            data = await message.download_media(bytes)
        except Exception as exc:
            return False, f"не смог скачать картинку: <code>{utils.escape_html(str(exc))}</code>"
        if not data:
            return False, "картинка пустая."
        if len(data) > MAX_PHOTO:
            return False, f"картинка больше {MAX_PHOTO // 1024 // 1024} МБ."
        state["photo"] = base64.b64encode(data).decode("ascii")
        return True, ""

    async def _finish_wizard(self, message, state):
        uid = message.sender_id
        entries = self._get("entries", []) or []
        entry = {
            "trigger": state["trigger"],
            "repl": state.get("repl"),
            "mode": state.get("mode", "sub"),
            "case": bool(state.get("case", False)),
            "neg": bool(state.get("neg", False)),
            "end": bool(state.get("end", False)),
        }
        photo = state.get("photo")
        if photo:
            entry["pid"] = self._save_photo(photo)
        entries.append(entry)
        self._store_entries(entries)
        self._pending.pop(uid, None)

        flags = []
        if entry["mode"] == "regex":
            flags.append("regex")
        elif entry["mode"] == "word":
            flags.append("слово")
        if entry["case"]:
            flags.append("регистр")
        if entry["neg"]:
            flags.append("не после «не»/«нет»/«неа»/«ни»")
        if entry["end"]:
            flags.append("только в конце")
        flag_str = f" <i>({', '.join(flags)})</i>" if flags else ""
        photo_str = "\n🖼 Картинка: прикреплена" if entry.get("pid") else ""
        # буквальный «[?&]si=...» никогда не совпадёт со ссылкой — напоминаем про -r
        warn = ""
        if self._suspect(entry):
            warn = (
                "\n⚠️ Триггер похож на регулярку, но флаг <code>-r</code> не указан — "
                "совпадение ищется как обычный текст. Если нужна регулярка: "
                f"<code>.repdel {entry['id']}</code> и добавь заново с <code>-r</code>."
            )
        if entry["repl"] is None:
            repl_str = "<i>(без текста)</i>"
        elif entry["repl"] == "":
            repl_str = "<i>(пусто — вырезать)</i>"
        else:
            repl_str = f"<code>{utils.escape_html(self._preview(entry['repl'], 100))}</code>"
        await utils.answer(
            message,
            f"✅ Добавлено <code>#{entry['id']}</code>{flag_str}\n"
            f"<code>{utils.escape_html(self._preview(entry['trigger'], 100))}</code>"
            f" ➜ {repl_str}{photo_str}{warn}",
        )

    async def _wizard_step(self, message, text):
        uid = message.sender_id
        state = self._pending.get(uid)
        if state is None:
            return
        state["ts"] = time.time()  # живой визард не должен умереть на середине
        step = state.get("step")
        stripped = text.strip()

        if stripped.lower() in CANCEL_WORDS:
            self._pending.pop(uid, None)
            await utils.answer(message, "❌ Добавление отменено.")
            return

        if step == "trigger":
            trigger = stripped
            if not trigger:
                await utils.answer(
                    message, "⚠️ Пустой триггер. Напиши его ответом или «отмена»."
                )
                return
            dup = self._find_dup(trigger, state["mode"], state["case"])
            if dup is not None:
                self._pending.pop(uid, None)
                await utils.answer(
                    message,
                    f"⚠️ Такой триггер уже есть (<code>#{dup['id']}</code>). "
                    f"Сначала удали: <code>.repdel {dup['id']}</code>",
                )
                return
            if state["mode"] == "regex":
                err = self._regex_error(trigger)
                if err:
                    self._pending.pop(uid, None)
                    await utils.answer(
                        message,
                        f"❌ Невалидный regex: <code>{utils.escape_html(err)}</code>",
                    )
                    return
            state["trigger"] = trigger
            state["step"] = "repl"
            await utils.answer(
                message,
                "📝 Теперь ответь на это сообщение текстом-заменой.\n"
                "• <code>-</code> — пустая замена (вырезать триггер из текста)\n"
                "• картинка — замена картинкой без текста",
            )
            return

        if step == "repl":
            if message.photo:
                ok, err = await self._set_photo(state, message)
                if not ok:
                    await utils.answer(message, f"❌ {err}")
                    return
                await self._finish_wizard(message, state)
                return
            state["repl"] = "" if stripped == "-" else text
            state["step"] = "photo"
            await utils.answer(
                message,
                "🖼 Будет картинка? Ответь <code>да</code> — и пришли её ответом, "
                "или <code>нет</code>.",
            )
            return

        if step == "photo":
            if message.photo:
                ok, err = await self._set_photo(state, message)
                if not ok:
                    await utils.answer(message, f"❌ {err}")
                    return
                await self._finish_wizard(message, state)
                return
            if stripped.lower() in YES_WORDS:
                state["step"] = "image"
                await utils.answer(message, "🖼 Пришли картинку ответом на это сообщение.")
                return
            if stripped.lower() in NO_WORDS:
                await self._finish_wizard(message, state)
                return
            await utils.answer(
                message, "⚠️ Ответь «да» или «нет» (или пришли картинку ответом)."
            )
            return

        if step == "image":
            if message.photo:
                ok, err = await self._set_photo(state, message)
                if not ok:
                    await utils.answer(message, f"❌ {err}")
                    return
                await self._finish_wizard(message, state)
                return
            await utils.answer(
                message, "⚠️ Нужна картинка ответом на это сообщение (или «отмена»)."
            )
            return

    # ---------- инлайн-меню ----------

    def _menu_entry(self, uid):
        """Вернуть (список правил, правило) или (None, None), если его уже нет."""
        entries = self._get("entries", []) or []
        for entry in entries:
            if entry.get("uid") == uid:
                return entries, entry
        return None, None

    def _short(self, text):
        text = str(text or "").replace("\n", " ").strip()
        return text if len(text) <= BTN_TEXT else text[: BTN_TEXT - 1] + "…"

    def _preview(self, text, limit=CARD_TEXT):
        text = str(text or "")
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _repl_html(self, entry):
        repl = entry.get("repl")
        if repl is None:
            return "<i>(без текста — только картинка)</i>"
        if repl == "":
            return "<i>(пусто — вырезать)</i>"
        return f"<code>{utils.escape_html(self._preview(repl))}</code>"

    def _suspect(self, entry):
        """Триггер похож на регулярку, но режим не regex — забытый -r."""
        return entry.get("mode") != "regex" and any(
            ch in (entry.get("trigger") or "") for ch in REGEX_CHARS
        )

    def _flags_html(self, entry):
        lines = [
            f"⚙️ режим: {MODE_NAMES.get(entry.get('mode', 'sub'), 'подстрока')}",
            f"🔠 регистр: {'да' if entry.get('case') else 'нет'}",
        ]
        if entry.get("mode") != "regex":
            lines.append(f"🚫 не после «не»: {'да' if entry.get('neg') else 'нет'}")
            lines.append(f"📍 только в конце: {'да' if entry.get('end') else 'нет'}")
        return "\n".join(lines)

    def _menu_status(self):
        return (
            f"🔁 автозамена {'🟢' if self.config['enabled'] else '🔴'}   "
            f"🔗 чистка ссылок {'🟢' if self.config['clean_links'] else '🔴'}   "
            f"↩️ откат {'🟢' if self.config['undo'] else '🔴'}"
        )

    def _menu_list(self, page=1):
        entries = self._get("entries", []) or []
        pages = max(1, (len(entries) + MENU_PER_PAGE - 1) // MENU_PER_PAGE)
        page = min(max(1, int(page)), pages)
        chunk = entries[(page - 1) * MENU_PER_PAGE : page * MENU_PER_PAGE]
        text = "📋 <b>Правила автозамены</b>"
        if pages > 1:
            text += f" (стр. {page}/{pages})"
        text += "\n" + self._menu_status()
        if entries:
            text += "\n\n" + "\n".join(
                f"<code>#{e['id']}</code> "
                f"{utils.escape_html(self._preview(e.get('trigger', ''), 80))}"
                for e in chunk
            )
        else:
            text += "\n\nПравил нет — добавь первое кнопкой «➕ правило»."

        markup = []
        for entry in chunk:
            repl = entry.get("repl")
            if entry.get("pid"):
                tail = "🖼"
            elif repl is None:
                tail = "∅"
            else:
                tail = repl or "—"
            markup.append([
                {
                    "text": self._short(f"#{entry['id']} {entry.get('trigger', '')} ➜ {tail}"),
                    "callback": self._menu_open,
                    "args": (entry["uid"],),
                }
            ])
        if pages > 1:
            nav = []
            if page > 1:
                nav.append({"text": "⬅️", "callback": self._menu_page, "args": (page - 1,)})
            if page < pages:
                nav.append({"text": "➡️", "callback": self._menu_page, "args": (page + 1,)})
            markup.append(nav)
        markup.append([
            {
                "text": "➕ правило",
                "input": "триггер: что искать в твоих сообщениях",
                "handler": self._menu_new,
            },
            {
                "text": f"🔁 {'выкл' if self.config['enabled'] else 'вкл'}",
                "callback": self._menu_toggle,
                "args": ("enabled", page),
            },
        ])
        markup.append([
            {
                "text": f"🔗 {'выкл' if self.config['clean_links'] else 'вкл'}",
                "callback": self._menu_toggle,
                "args": ("clean_links", page),
            },
            {
                "text": f"↩️ {'выкл' if self.config['undo'] else 'вкл'}",
                "callback": self._menu_toggle,
                "args": ("undo", page),
            },
            {"text": "❌", "callback": self._menu_close},
        ])
        return text, markup, page

    def _menu_card(self, entry, confirm=False):
        text = (
            f"<b>Правило <code>#{entry['id']}</code></b>\n"
            f"🔎 <code>{utils.escape_html(self._preview(entry.get('trigger', '')))}</code>\n"
            f"➜ {self._repl_html(entry)}\n"
            f"🖼 картинка: {'есть' if entry.get('pid') else 'нет'}\n\n"
            f"{self._flags_html(entry)}"
        )
        if self._suspect(entry):
            text += (
                "\n\n⚠️ <i>Триггер похож на регулярку, но режим не regex — "
                "совпадение ищется как обычный текст.</i>"
            )
        uid = entry["uid"]
        mode = entry.get("mode", "sub")
        flag_row = [
            {
                "text": f"🔠 регистр: {'да' if entry.get('case') else 'нет'}",
                "callback": self._menu_flag,
                "args": (uid, "case"),
            }
        ]
        if mode != "regex":
            flag_row.append({
                "text": f"🚫 после «не»: {'да' if entry.get('neg') else 'нет'}",
                "callback": self._menu_flag,
                "args": (uid, "neg"),
            })
            flag_row.append({
                "text": f"📍 в конце: {'да' if entry.get('end') else 'нет'}",
                "callback": self._menu_flag,
                "args": (uid, "end"),
            })
        markup = [
            [
                {
                    "text": f"⚙️ режим: {MODE_NAMES.get(mode, 'подстрока')}",
                    "callback": self._menu_mode,
                    "args": (uid,),
                }
            ],
            flag_row,
            [
                {
                    "text": "✏️ текст замены",
                    "input": "на что менять («-» = вырезать)",
                    "handler": self._menu_repl,
                    "args": (uid,),
                },
                {"text": "🈳 без текста", "callback": self._menu_notext, "args": (uid,)},
            ],
            [
                {
                    "text": "🖼 убрать картинку" if entry.get("pid") else "🖼 загрузить картинку",
                    "callback": self._menu_photo,
                    "args": (uid,),
                }
            ],
        ]
        if confirm:
            markup.append([
                {"text": "🗑 точно удалить", "callback": self._menu_del, "args": (uid,)},
                {"text": "отмена", "callback": self._menu_open, "args": (uid,)},
            ])
        else:
            markup.append([
                {"text": "🗑 удалить", "callback": self._menu_askdel, "args": (uid,)}
            ])
        markup.append([{"text": "« назад", "callback": self._menu_back, "args": (uid,)}])
        return text, markup

    async def _menu_render(self, call, text, markup):
        try:
            await call.edit(text, markup)
        except Exception as exc:
            logger.warning("[TriggerChanger] menu edit failed: %r", exc)
            await self._menu_answer(call, "меню устарело — открой .repmenu заново", True)

    async def _menu_answer(self, call, text, alert=False):
        try:
            await call.answer(str(text)[:ALERT_MAX], alert=alert)
        except Exception as exc:
            logger.warning("[TriggerChanger] menu answer failed: %r", exc)

    async def _menu_gone(self, call):
        await self._menu_answer(call, "правило уже удалено", True)
        await self._menu_page(call, 1)

    async def _menu_open(self, call, uid):
        _, entry = self._menu_entry(uid)
        if entry is None:
            await self._menu_gone(call)
            return
        text, markup = self._menu_card(entry)
        await self._menu_render(call, text, markup)

    async def _menu_page(self, call, page):
        text, markup, _ = self._menu_list(page)
        await self._menu_render(call, text, markup)

    async def _menu_back(self, call, uid):
        entries = self._get("entries", []) or []
        index = next((i for i, e in enumerate(entries) if e.get("uid") == uid), 0)
        await self._menu_page(call, index // MENU_PER_PAGE + 1)

    async def _menu_toggle(self, call, key, page):
        self.config[key] = not self.config[key]
        await self._menu_answer(call, f"{'вкл' if self.config[key] else 'выкл'}")
        await self._menu_page(call, page)

    async def _menu_close(self, call):
        # закрыл меню — значит и картинку для правила уже не ждём,
        # иначе следующее фото юзера прилипнет к правилу
        self._photo_wait.pop(call.sender_id, None)
        self._menu_forms.pop(call.sender_id, None)
        try:
            await call.delete()
        except Exception as exc:
            logger.warning("[TriggerChanger] menu close failed: %r", exc)

    async def _menu_mode(self, call, uid):
        entries, entry = self._menu_entry(uid)
        if entry is None:
            await self._menu_gone(call)
            return
        mode = entry.get("mode", "sub")
        index = MODE_CYCLE.index(mode) + 1 if mode in MODE_CYCLE else 0
        new = MODE_CYCLE[index % len(MODE_CYCLE)]
        if new == "regex":
            err = self._regex_error(entry.get("trigger") or "")
            if err:
                await self._menu_answer(call, f"невалидный regex: {err}", True)
                return
        entry["mode"] = new
        self._store_entries(entries)
        await self._menu_answer(call, MODE_NAMES[new])
        text, markup = self._menu_card(entry)
        await self._menu_render(call, text, markup)

    async def _menu_flag(self, call, uid, key):
        entries, entry = self._menu_entry(uid)
        if entry is None:
            await self._menu_gone(call)
            return
        if entry.get("mode") == "regex":
            await self._menu_answer(
                call, "в regex-режиме не работает — впиши условие в саму регулярку", True
            )
            return
        entry[key] = not entry.get(key)
        self._store_entries(entries)
        await self._menu_answer(call, "вкл" if entry[key] else "выкл")
        text, markup = self._menu_card(entry)
        await self._menu_render(call, text, markup)

    async def _menu_notext(self, call, uid):
        entries, entry = self._menu_entry(uid)
        if entry is None:
            await self._menu_gone(call)
            return
        entry["repl"] = None
        self._store_entries(entries)
        await self._menu_answer(call, "замена без текста")
        text, markup = self._menu_card(entry)
        await self._menu_render(call, text, markup)

    async def _menu_repl(self, call, query, uid):
        """Инпут-кнопка: телега передаёт введённый текст вторым аргументом."""
        entries, entry = self._menu_entry(uid)
        if entry is None:
            await self._menu_gone(call)
            return
        value = (query or "").strip()
        entry["repl"] = "" if value == "-" else value
        self._store_entries(entries)
        await self._menu_answer(call, "сохранил")
        text, markup = self._menu_card(entry)
        await self._menu_render(call, text, markup)

    async def _menu_new(self, call, query):
        trigger = (query or "").strip()
        if not trigger:
            await self._menu_answer(call, "пустой триггер", True)
            return
        dup = self._find_dup(trigger, "sub", False)
        if dup is not None:
            await self._menu_answer(call, f"такой триггер уже есть (#{dup['id']})", True)
            return
        entries = self._get("entries", []) or []
        entry = {
            "trigger": trigger,
            "repl": "",
            "mode": "sub",
            "case": False,
            "neg": False,
            "end": False,
        }
        entries.append(entry)
        self._store_entries(entries)
        await self._menu_answer(call, f"добавил #{entry['id']}")
        text, markup = self._menu_card(entry)
        await self._menu_render(call, text, markup)

    async def _menu_askdel(self, call, uid):
        _, entry = self._menu_entry(uid)
        if entry is None:
            await self._menu_gone(call)
            return
        text, markup = self._menu_card(entry, confirm=True)
        await self._menu_render(call, text, markup)

    async def _menu_del(self, call, uid):
        entries, entry = self._menu_entry(uid)
        if entry is None:
            await self._menu_gone(call)
            return
        self._drop_photo(entry)
        entries.remove(entry)
        self._store_entries(entries)
        # в _photo_wait лежит кортеж (uid правила, ts, chat_id) — сравниваем
        # с первым элементом, иначе ожидание картинки переживёт правило
        self._photo_wait = {
            k: v for k, v in self._photo_wait.items() if v[0] != uid
        }
        await self._menu_answer(call, "удалил")
        await self._menu_page(call, 1)

    async def _menu_photo(self, call, uid):
        entries, entry = self._menu_entry(uid)
        if entry is None:
            await self._menu_gone(call)
            return
        if entry.get("pid"):
            self._drop_photo(entry)
            entry.pop("pid", None)
            self._store_entries(entries)
            await self._menu_answer(call, "картинку убрал")
            text, markup = self._menu_card(entry)
            await self._menu_render(call, text, markup)
            return
        # кнопку с файлом телега не умеет: ждём фото от пользователя в watcher,
        # но только в том чате, где открыто меню
        chat = self._menu_chat.get(call.sender_id)
        if chat is None:
            await self._menu_answer(
                call, "меню устарело — открой .repmenu заново", True
            )
            return
        self._photo_wait[call.sender_id] = (uid, time.time(), chat)
        await self._menu_answer(
            call, "пришли картинку сообщением в этот же чат — подхвачу", True
        )

    async def _attach_photo(self, message, uid):
        """Подхватить фото, которое пользователь прислал для правила из меню."""
        self._photo_wait.pop(message.sender_id, None)
        entries, entry = self._menu_entry(uid)
        if entry is None:
            logger.info("[TriggerChanger] правило %s пропало — картинку не сохраняю", uid)
            return
        state = {}
        ok, err = await self._set_photo(state, message)
        if not ok:
            await utils.answer(message, f"❌ {err}")
            return
        # старое фото правила больше никто не найдёт — убираем, чтобы не висело в БД
        self._drop_photo(entry)
        entry["pid"] = self._save_photo(state["photo"])
        self._store_entries(entries)
        logger.info("[TriggerChanger] правило #%s: картинка обновлена", entry["id"])
        form = self._menu_forms.get(message.sender_id)
        if form is None:
            return
        text, markup = self._menu_card(entry)
        try:
            await form.edit(text, markup)
        except Exception as exc:
            logger.warning("[TriggerChanger] menu refresh failed: %r", exc)

    # ---------- команды ----------

    @loader.command(
        ru_doc="Статус и справка по TriggerChanger",
        en_doc="TriggerChanger status and help",
    )
    async def rep(self, message):
        """Show status and help"""
        entries = self._get("entries", []) or []
        enabled = self.config["enabled"]
        photos = sum(1 for e in entries if e.get("pid"))
        state = "🟢 вкл" if enabled else "🔴 выкл"
        clean = "🟢 вкл" if self.config["clean_links"] else "🔴 выкл"
        undo = "🟢 вкл" if self.config["undo"] else "🔴 выкл"
        await utils.answer(
            message,
            f"🔁 <b>TriggerChanger</b> — {state}, правил: <code>{len(entries)}</code> "
            f"(с картинками: <code>{photos}</code>)\n"
            f"🔗 Чистка ссылок от трекеров — {clean}\n"
            f"↩️ Откат автоправок в чат логов — {undo}\n\n"
            "<code>.repmenu</code> — меню правил кнопками\n"
            "<code>.repadd &lt;триггер&gt; [-r|-w|-c|-n|-e]</code> — добавить замену (дальше по подсказкам)\n"
            "<code>.replist</code> — список правил\n"
            "<code>.repdel &lt;id|all&gt;</code> — удалить правило / всё\n"
            "<code>.reptoggle [on|off]</code> — вкл/выкл\n"
            "<code>.repclean [on|off]</code> — вкл/выкл чистку ссылок (si=, utm_* и т.п.)\n"
            "<code>.represet да</code> — сбросить всё: правила, картинки, настройки\n"
            "<code>.reptest &lt;текст&gt;</code> — проверить без отправки\n\n"
            "Флаги: <code>-r</code> regex, <code>-w</code> целое слово, "
            "<code>-c</code> регистр, <code>-n</code> не после «не»/«нет»/«неа»/«ни», "
            "<code>-e</code> только в конце сообщения.\n"
            "Пример: <code>.repadd да</code>, в ответ пишешь <code>da✅</code> — и каждое «да» "
            "в твоих сообщениях станет <code>da✅</code> сразу после отправки.",
        )

    @loader.command(
        ru_doc="Меню правил кнопками: .repmenu",
        en_doc="Inline rules menu: .repmenu",
    )
    async def repmenu(self, message):
        """Inline menu: browse and edit rules with buttons"""
        uid = message.sender_id
        text, markup, _ = self._menu_list(1)
        try:
            form = await self.inline.form(
                text,
                message=message,
                reply_markup=markup,
                ttl=FORM_TTL,
                force_me=True,
                on_unload=lambda *_: self._menu_forms.pop(uid, None),
            )
        except Exception as exc:
            logger.warning("[TriggerChanger] menu form failed: %r", exc)
            form = False
        if not form:
            await utils.answer(
                message,
                "❌ Меню не открылось (не настроен инлайн-бот?). "
                "Смотри <code>.replist</code>, добавляй через <code>.repadd</code>.",
            )
            return
        # форма понадобится watcher'у, чтобы обновить карточку после загрузки фото;
        # чат запоминаем, чтобы картинку правила ждать именно здесь
        self._menu_forms[uid] = form
        self._menu_chat[uid] = message.chat_id

    @loader.command(
        ru_doc="Вкл/выкл чистку ссылок от трекеров: .repclean [on|off]; без аргументов — переключить",
        en_doc="Toggle link tracker cleaning: .repclean [on|off]; no args flips",
    )
    async def repclean(self, message):
        """Toggle link tracker cleaning"""
        raw = utils.get_args_raw(message).strip().lower()
        if raw in ("on", "1", "вкл", "enable"):
            new = True
        elif raw in ("off", "0", "выкл", "disable"):
            new = False
        elif not raw:
            new = not self.config["clean_links"]
        else:
            await utils.answer(
                message, "❌ Аргумент: <code>on</code> / <code>off</code> / пусто."
            )
            return
        self.config["clean_links"] = new
        if new:
            await utils.answer(
                message,
                "🔗 Чистка ссылок включена: <code>si=</code>, <code>utm_*</code>, "
                "<code>fbclid</code> и прочие трекеры будут вырезаться.",
            )
        else:
            await utils.answer(message, "🔗 Чистка ссылок выключена.")

    @loader.command(
        ru_doc="Добавить замену: .repadd <триггер> [-r|-w|-c|-n|-e]; дальше по подсказкам бота",
        en_doc="Add a replacement rule: .repadd <trigger> [-r|-w|-c|-n|-e], then follow the prompts",
    )
    async def repadd(self, message):
        """Wizard: add a replacement rule"""
        uid = message.sender_id
        args = utils.get_args_raw(message)
        mode, case, neg, end = "sub", False, False, False
        rest = []
        for token in args.split():
            if token in ("-r", "--regex"):
                mode = "regex"
            elif token in ("-w", "--word"):
                mode = "word"
            elif token in ("-c", "--case"):
                case = True
            elif token in ("-n", "--neg"):
                neg = True
            elif token in ("-e", "--end"):
                end = True
            else:
                rest.append(token)
        trigger = " ".join(rest).strip()

        note = ""
        if mode == "regex" and (neg or end):
            dropped = [name for name, on in (("-n", neg), ("-e", end)) if on]
            neg = end = False
            note = (
                f"⚠️ {' и '.join(dropped)} работает только для подстроки и слова; "
                "в regex-режиме впиши сам: <code>(?&lt;!не\\s)</code> для -n, "
                "<code>$</code> для -e.\n\n"
            )

        state = {"step": "trigger", "mode": mode, "case": case, "neg": neg,
                 "end": end, "trigger": "", "repl": None, "photo": None,
                 "chat_id": message.chat_id, "ts": time.time()}

        if trigger:
            if mode == "regex":
                err = self._regex_error(trigger)
                if err:
                    await utils.answer(
                        message,
                        f"❌ Невалидный regex: <code>{utils.escape_html(err)}</code>",
                    )
                    return
            dup = self._find_dup(trigger, mode, case)
            if dup is not None:
                await utils.answer(
                    message,
                    f"⚠️ Такой триггер уже есть (<code>#{dup['id']}</code>). "
                    f"Сначала удали: <code>.repdel {dup['id']}</code>",
                )
                return
            state["trigger"] = trigger
            state["step"] = "repl"

        self._pending[uid] = state

        if state["step"] == "trigger":
            await utils.answer(
                message,
                note
                + "✏️ Напиши <b>триггер</b> ответом на это сообщение "
                "(что искать в твоих сообщениях). «Отмена» — выйти.\n"
                "💬 Отвечай в этом же чате — в других сообщения визард не ловит.",
            )
        else:
            await utils.answer(
                message,
                note
                + "📝 Теперь ответь на это сообщение текстом-заменой.\n"
                "• <code>-</code> — пустая замена (вырезать триггер из текста)\n"
                "• картинка — замена картинкой без текста",
            )

    @loader.command(
        ru_doc="Список правил: .replist [страница]",
        en_doc="List rules: .replist [page]",
    )
    async def replist(self, message):
        """List replacement rules"""
        entries = self._get("entries", []) or []
        # чистка ссылок — не правило, а настройка: показываем её тут же,
        # иначе не видно, включена ли она
        status = (
            f"🔁 Автозамена — {'🟢 вкл' if self.config['enabled'] else '🔴 выкл'}\n"
            f"🔗 Чистка ссылок от трекеров — "
            f"{'🟢 вкл' if self.config['clean_links'] else '🔴 выкл'}"
        )
        if not entries:
            await utils.answer(
                message,
                f"{status}\n📋 Правил нет. Добавь: <code>.repadd да</code>",
            )
            return
        raw = utils.get_args_raw(message).strip()
        if raw:
            if not raw.isdigit():
                await utils.answer(message, "❌ Номер страницы — число.")
                return
            page = max(1, int(raw))
        else:
            page = 1
        pages = max(1, (len(entries) + PER_PAGE - 1) // PER_PAGE)
        page = min(page, pages)
        chunk = entries[(page - 1) * PER_PAGE : page * PER_PAGE]
        lines = []
        for entry in chunk:
            flags = {"regex": "R", "word": "W"}.get(entry.get("mode", "sub"), "")
            if entry.get("case"):
                flags += "C"
            if entry.get("neg"):
                flags += "N"
            if entry.get("end"):
                flags += "E"
            flag_str = f" <code>[{flags}]</code>" if flags else ""
            photo = " 🖼" if entry.get("pid") else ""
            # список должен читаться, а не скроллиться: длинные триггеры режем
            trig = utils.escape_html(self._preview(entry.get("trigger", ""), 100))
            suspect = self._suspect(entry)
            warn = " ⚠️ <i>похоже на regex без -r</i>" if suspect else ""
            raw_repl = entry.get("repl")
            if raw_repl is None:
                repl = "<i>(без текста)</i>"
            elif raw_repl == "":
                repl = "<i>(пусто)</i>"
            else:
                repl = utils.escape_html(self._preview(raw_repl, 100))
            lines.append(
                f"<code>#{entry['id']}</code>{flag_str}{photo} {trig} ➜ {repl}{warn}"
            )
        header = "📋 <b>Правила автозамены</b>"
        if pages > 1:
            header += f" (стр. {page}/{pages})"
        await utils.answer(message, header + "\n" + status + "\n\n" + "\n".join(lines))

    @loader.command(
        ru_doc="Удалить правило: .repdel <id>, или .repdel all",
        en_doc="Delete a rule: .repdel <id>, or .repdel all",
    )
    async def repdel(self, message):
        """Delete a rule by id (see .replist) or all rules"""
        raw = utils.get_args_raw(message).strip().lower()
        entries = self._get("entries", []) or []
        if raw in ("all", "все"):
            count = len(entries)
            for entry in entries:
                self._drop_photo(entry)
            self._store_entries([])
            await utils.answer(
                message, f"🗑 Удалено всех правил: <code>{count}</code>"
            )
            return
        if not raw.isdigit():
            await utils.answer(
                message, "❌ Укажи id из <code>.replist</code> (или <code>all</code>)."
            )
            return
        rid = int(raw)
        remaining = [e for e in entries if e.get("id") != rid]
        if len(remaining) == len(entries):
            await utils.answer(message, f"❌ Правило <code>#{rid}</code> не найдено.")
            return
        for entry in entries:
            if entry.get("id") == rid:
                self._drop_photo(entry)
        self._store_entries(remaining)
        await utils.answer(
            message, f"🗑 Удалено <code>#{rid}</code>. Осталось: <code>{len(remaining)}</code>"
        )

    @loader.command(
        ru_doc="Вкл/выкл автозамену: .reptoggle [on|off]; без аргументов — переключить",
        en_doc="Toggle replacements: .reptoggle [on|off]; no args flips",
    )
    async def reptoggle(self, message):
        """Toggle the replacer on/off"""
        raw = utils.get_args_raw(message).strip().lower()
        if raw in ("on", "1", "вкл", "enable"):
            new = True
        elif raw in ("off", "0", "выкл", "disable"):
            new = False
        elif not raw:
            new = not self.config["enabled"]
        else:
            await utils.answer(
                message, "❌ Аргумент: <code>on</code> / <code>off</code> / пусто."
            )
            return
        self.config["enabled"] = new
        if new:
            await utils.answer(message, "🟢 Автозамена включена.")
        else:
            await utils.answer(message, "🔴 Автозамена выключена.")

    @loader.command(
        ru_doc="Полный сброс модуля: .represet да — удалить все правила, картинки и настройки",
        en_doc="Full module reset: .represet yes — wipe rules, photos and settings",
    )
    async def represet(self, message):
        """Wipe this module's DB: rules, photos, settings"""
        raw = utils.get_args_raw(message).strip().lower()
        entries = self._get("entries", []) or []
        if raw not in ("да", "yes", "+", "confirm"):
            await utils.answer(
                message,
                f"⚠️ Удалю всё, что хранит этот модуль: правил <code>{len(entries)}</code>, "
                "их картинки и настройки (автозамена, чистка ссылок). "
                "Другие модули не пострадают.\n"
                "Подтверди: <code>.represet да</code>",
            )
            return
        count = len(entries)
        self._store_entries([])
        # чистим все photo:*-ключи, а не только упомянутые в entries: если список
        # когда-то сбрасывался, осиротевшие картинки остались бы в json навсегда
        self._drop_all_photos()
        self._set("photo_seq", 0)
        self._set("uid_seq", 0)
        self._set("logchat", None)
        self.config["enabled"] = True
        self.config["clean_links"] = False
        self.config["undo"] = True
        self._pending = {}
        self._edited = {}
        self._undo = {}
        self._photo_wait = {}
        self._menu_forms = {}
        self._menu_chat = {}
        await utils.answer(
            message,
            f"♻️ Сброс: удалено правил <code>{count}</code>. Настройки по умолчанию "
            "(автозамена вкл, чистка ссылок выкл).",
        )

    @loader.command(
        ru_doc="Куда кидать форму отката: .replogs [id|off]; без аргумента — текущий чат",
        en_doc="Where to send undo forms: .replogs [id|off]; no args = current chat",
    )
    async def replogs(self, message):
        """Set the chat that receives undo forms"""
        raw = utils.get_args_raw(message).strip().lower()
        if raw in ("off", "выкл", "auto"):
            self._set("logchat", None)
            await utils.answer(
                message, "📁 Свой чат сброшен — беру чат логов Hikka (hikka-logs)."
            )
            return
        target = message.chat_id
        if raw:
            if not re.fullmatch(r"-?\d+", raw):
                await utils.answer(
                    message, "❌ Нужен числовый id чата или <code>off</code>."
                )
                return
            target = int(raw)
        self._set("logchat", target)
        await utils.answer(
            message, f"📁 Формы отката будут приходить в <code>{target}</code>."
        )

    @loader.command(
        ru_doc="Проверить текст без отправки: .reptest да привет",
        en_doc="Dry-run replacements on text: .reptest some text",
    )
    async def reptest(self, message):
        """Show how a text would be transformed, without sending anything"""
        text = utils.get_args_raw(message)
        if not text:
            await utils.answer(
                message, "❌ Укажи текст: <code>.reptest да привет</code>"
            )
            return
        new_text, matched = self._apply(text)
        if not matched:
            if new_text == text:
                await utils.answer(message, "ℹ️ Совпадений нет — текст останется как есть.")
                return
            await utils.answer(
                message,
                "🔗 Сработала чистка ссылок (правил нет)\n\n"
                f"<b>Было:</b> <code>{utils.escape_html(text)}</code>\n"
                f"<b>Стало:</b> <code>{utils.escape_html(new_text)}</code>",
            )
            return
        ids = ", ".join(f"#{e['id']}" for e in matched)
        photos = any(e.get("pid") for e in matched)
        result = utils.escape_html(new_text) if new_text else "<i>(пусто)</i>"
        await utils.answer(
            message,
            f"🔎 Сработало: {ids}\n"
            f"🖼 Картинка: {'да' if photos else 'нет'}\n\n"
            f"<b>Было:</b> <code>{utils.escape_html(text)}</code>\n"
            f"<b>Стало:</b> {result}",
        )
