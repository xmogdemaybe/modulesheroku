# -*- coding: utf-8 -*-
# TriggerChanger — мгновенная автозамена в исходящих сообщениях (Hikka / Heroku).
#
# Как работает: watcher ловит ТВОИ отправленные сообщения, прогоняет текст
# по словарю замен и тут же редактирует сообщение, если что-то совпало.
# Правила хранятся в БД (json Heroku), картинки — в base64.
#
# Команды:
#   .repadd <триггер> [-r|-w|-c|-n|-e]   добавить замену (визард: замена -> картинка?)
#   .replist [страница]            список правил
#   .repdel <id|all>               удалить правило / всё
#   .reptoggle [on|off]            вкл/выкл автозамену
#   .repclean [on|off]             вкл/выкл чистку ссылок от трекеров
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
# "отмена" на любом шаге — отмена.

import base64
import io
import logging
import re
import time

from .. import loader, utils

logger = logging.getLogger(__name__)

MODNAME = "TriggerChanger"
MAX_PHOTO = 5 * 1024 * 1024
PER_PAGE = 30
CANCEL_WORDS = ("отмена", "cancel", "стоп", "stop", "хватит")
YES_WORDS = ("да", "yes", "+", "угу", "ага")
NO_WORDS = ("нет", "no", "-", "неа")
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


@loader.tds
class TriggerChangerMod(loader.Module):
    """Автозамена слов/триггеров в отправляемых сообщениях, с БД и картинками"""

    strings = {"name": MODNAME}

    async def client_ready(self, client, db):
        self.client = client
        self.db = db
        self._pending = {}  # uid -> состояние визарда
        self._edited = {}  # msg_id -> ts, страховка от повторной обработки
        if self._get("enabled") is None:
            self._set("enabled", True)
        entries = self._get("entries", [])
        if not isinstance(entries, list):
            entries = []
            self._set("entries", entries)
        # после старых удалений id могли остаться «с дырками» (напр. 3, 4, 5) —
        # пересобираем нумерацию подряд: 1..N
        if [e.get("id") for e in entries] != list(range(1, len(entries) + 1)):
            self._store_entries(entries)

    # ---------- БД ----------

    def _get(self, key, default=None):
        try:
            value = self.db.get(MODNAME, key)
            return default if value is None else value
        except Exception as exc:
            logger.error("[TriggerChanger] db read %s: %r", key, exc)
            return default

    def _set(self, key, value):
        try:
            self.db.set(MODNAME, key, value)
        except Exception as exc:
            logger.error("[TriggerChanger] db write %s: %r", key, exc)

    def _store_entries(self, entries):
        """Сохранить правила, пересобрав нумерацию подряд: 1..N (без дырок)."""
        for index, entry in enumerate(entries, 1):
            entry["id"] = index
        self._set("entries", entries)

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
        except (re.error, ValueError):
            # например паттерн уже содержит inline-флаги (?i) — тогда
            # переданные в compile() флаги запрещены, пробуем без них
            try:
                return re.compile(pattern)
            except (re.error, ValueError):
                return None

    def _clean_links(self, text):
        """Вырезать трекер-параметры (si=, utm_* и т.п.) из URL в тексте.

        Делается разбором query, а не регуляркой: параметр может стоять
        первым, в середине и последним, и в каждом случае ссылка должна
        остаться рабочей.
        """
        if not self._get("clean_links", False):
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

    # ---------- watcher ----------

    @loader.watcher()
    async def watcher(self, message):
        try:
            if not getattr(message, "out", False):
                return
            uid = getattr(message, "sender_id", None)
            if uid is None:
                return
            text = getattr(message, "text", None) or ""
            if not isinstance(text, str):
                return

            # активный визард перехватывает ответы пользователя
            if uid in self._pending:
                await self._wizard_step(message, text)
                return

            if not self._get("enabled", True) or not text:
                return
            if getattr(message, "fwd_from", None) or getattr(message, "via_bot_id", None):
                return
            for prefix in self.get_prefixes():
                if prefix and text.startswith(prefix):
                    return
            ts = self._edited.get(message.id)
            if ts is not None and time.time() - ts < 300:
                return

            new_text, matched = self._apply(text)

            if new_text != text and 0 < len(new_text) <= 4096:
                try:
                    await message.edit(new_text)
                except Exception as exc:
                    logger.warning("[TriggerChanger] edit failed (msg %s): %r", message.id, exc)
                    return
                self._edited[message.id] = time.time()
                if len(self._edited) > 500:
                    cutoff = time.time() - 600
                    self._edited = {
                        k: v for k, v in self._edited.items() if v > cutoff
                    }

            photo_entries = [e for e in matched if e.get("photo")]
            if photo_entries:
                try:
                    data = base64.b64decode(photo_entries[0]["photo"])
                    await message.respond(file=io.BytesIO(data))
                except Exception as exc:
                    logger.warning("[TriggerChanger] photo send failed: %r", exc)
        except Exception as exc:
            logger.exception("[TriggerChanger] watcher error: %r", exc)

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
            "photo": state.get("photo"),
        }
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
        photo_str = "\n🖼 Картинка: прикреплена" if entry["photo"] else ""
        # буквальный «[?&]si=...» никогда не совпадёт со ссылкой — напоминаем про -r
        warn = ""
        if entry["mode"] != "regex" and any(
            ch in entry["trigger"] for ch in REGEX_CHARS
        ):
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
            repl_str = f"<code>{utils.escape_html(entry['repl'])}</code>"
        await utils.answer(
            message,
            f"✅ Добавлено <code>#{entry['id']}</code>{flag_str}\n"
            f"<code>{utils.escape_html(entry['trigger'])}</code> ➜ {repl_str}{photo_str}{warn}",
        )

    async def _wizard_step(self, message, text):
        uid = message.sender_id
        state = self._pending.get(uid)
        if state is None:
            return
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

    # ---------- команды ----------

    @loader.command(
        ru_doc="Статус и справка по TriggerChanger",
        en_doc="TriggerChanger status and help",
    )
    async def rep(self, message):
        """Show status and help"""
        entries = self._get("entries", []) or []
        enabled = self._get("enabled", True)
        photos = sum(1 for e in entries if e.get("photo"))
        state = "🟢 вкл" if enabled else "🔴 выкл"
        clean = "🟢 вкл" if self._get("clean_links", False) else "🔴 выкл"
        await utils.answer(
            message,
            f"🔁 <b>TriggerChanger</b> — {state}, правил: <code>{len(entries)}</code> "
            f"(с картинками: <code>{photos}</code>)\n"
            f"🔗 Чистка ссылок от трекеров — {clean}\n\n"
            "<code>.repadd &lt;триггер&gt; [-r|-w|-c|-n|-e]</code> — добавить замену (дальше по подсказкам)\n"
            "<code>.replist</code> — список правил\n"
            "<code>.repdel &lt;id|all&gt;</code> — удалить правило / всё\n"
            "<code>.reptoggle [on|off]</code> — вкл/выкл\n"
            "<code>.repclean [on|off]</code> — вкл/выкл чистку ссылок (si=, utm_* и т.п.)\n"
            "<code>.reptest &lt;текст&gt;</code> — проверить без отправки\n\n"
            "Флаги: <code>-r</code> regex, <code>-w</code> целое слово, "
            "<code>-c</code> регистр, <code>-n</code> не после «не»/«нет»/«неа»/«ни», "
            "<code>-e</code> только в конце сообщения.\n"
            "Пример: <code>.repadd да</code>, в ответ пишешь <code>da✅</code> — и каждое «да» "
            "в твоих сообщениях станет <code>da✅</code> сразу после отправки.",
        )

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
            new = not self._get("clean_links", False)
        else:
            await utils.answer(
                message, "❌ Аргумент: <code>on</code> / <code>off</code> / пусто."
            )
            return
        self._set("clean_links", new)
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
                 "end": end, "trigger": "", "repl": None, "photo": None}

        if trigger:
            if mode == "regex":
                try:
                    re.compile(trigger)
                except re.error as exc:
                    await utils.answer(
                        message,
                        f"❌ Невалидный regex: <code>{utils.escape_html(str(exc))}</code>",
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
                "(что искать в твоих сообщениях). «Отмена» — выйти.",
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
        if not entries:
            await utils.answer(
                message, "📋 Правил нет. Добавь: <code>.repadd да</code>"
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
            photo = " 🖼" if entry.get("photo") else ""
            trig = utils.escape_html(entry.get("trigger", ""))
            suspect = entry.get("mode", "sub") != "regex" and any(
                ch in entry.get("trigger", "") for ch in REGEX_CHARS
            )
            warn = " ⚠️ <i>похоже на regex без -r</i>" if suspect else ""
            raw_repl = entry.get("repl")
            if raw_repl is None:
                repl = "<i>(без текста)</i>"
            elif raw_repl == "":
                repl = "<i>(пусто)</i>"
            else:
                repl = utils.escape_html(raw_repl)
            lines.append(
                f"<code>#{entry['id']}</code>{flag_str}{photo} {trig} ➜ {repl}{warn}"
            )
        header = "📋 <b>Правила автозамены</b>"
        if pages > 1:
            header += f" (стр. {page}/{pages})"
        await utils.answer(message, header + "\n" + "\n".join(lines))

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
            new = not self._get("enabled", True)
        else:
            await utils.answer(
                message, "❌ Аргумент: <code>on</code> / <code>off</code> / пусто."
            )
            return
        self._set("enabled", new)
        if new:
            await utils.answer(message, "🟢 Автозамена включена.")
        else:
            await utils.answer(message, "🔴 Автозамена выключена.")

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
        photos = any(e.get("photo") for e in matched)
        result = utils.escape_html(new_text) if new_text else "<i>(пусто)</i>"
        await utils.answer(
            message,
            f"🔎 Сработало: {ids}\n"
            f"🖼 Картинка: {'да' if photos else 'нет'}\n\n"
            f"<b>Было:</b> <code>{utils.escape_html(text)}</code>\n"
            f"<b>Стало:</b> {result}",
        )
