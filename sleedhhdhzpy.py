import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone

from .. import loader, utils


class SleepMod(loader.Module):
    """Автоответ в личных сообщениях с БД, расписанием, задержкой и персональными ответами

    dev: @xmogde
    """

    strings = {
        "name": "Sleep",
    }

    # Значения по умолчанию если БД упадет
    DEFAULT_TEXT = "😴 примерный текст автоответа — поменять через .sleeptext твой текст"
    DEFAULT_DELAY = 3600
    DEFAULT_SCHEDULE = {"enabled": False, "start": None, "end": None}
    DEFAULT_EXCLUDED = {}
    DEFAULT_PERSONAL = {}
    DEFAULT_ACTIVE_GRACE = 300
    DEFAULT_MAX_AGE = 3600

    # Сколько храним отметки «кому уже ответили»
    RESPONSE_RETENTION = 86400
    RESPONSE_PRUNE_AT = 500
    RESPONSE_HARD_CAP = 2000
    ANSWERED_IDS_CAP = 512

    # Всё состояние — одним ключом: db.set в форке синхронно дёргает save()
    # всей базы (heroku/database.py:396), лишний вызов — лишняя сериализация
    STATE_KEY = "state"
    OWNER_PERSIST_EVERY = 60

    # Служебные аккаунты Telegram: код входа, уведомления о входах
    SERVICE_IDS = (777000, 42777)

    # Заметка к исключению — короткая подпись, а не хранилище текстов
    NOTE_MAX_LEN = 100

    async def client_ready(self, client, db):
        self.logger = logging.getLogger("Sleep")
        self.client = client
        self.db = db
        self.last_response_time = {}
        self._owner_last_active = None
        self._owner_persisted_at = 0.0
        # id наших же автоответов: они приходят в watcher как исходящие,
        # и без этого списка каждый автоответ считался бы активностью владельца
        self._auto_reply_ids = deque(maxlen=64)
        # id сообщений, на которые мы уже ответили: диспетчер хикки может
        # повторно доставить тот же апдейт после обрыва соединения
        self._answered_ids = deque(maxlen=self.ANSWERED_IDS_CAP)
        # чаты, где прямо сейчас уходит автоответ. Нужны потому, что исходящая
        # копия ответа прилетает в watcher ещё до возврата из await respond() —
        # по id её в этот момент опознать нельзя
        self._inflight_chats = {}
        # вотчеры запускаются параллельно (dispatcher.py:692, ensure_future),
        # поэтому проверка «не отвечали ли уже» и запись должны быть атомарными
        self._send_lock = asyncio.Lock()
        self._my_id = (await client.get_me()).id
        self._init_db()
        self._restore_state()

    def _init_db(self):
        """Инициализировать БД если её нет (без перезаписи уже заданных значений)"""
        defaults = {
            "sleep_enabled": False,
            "sleep_text": self.DEFAULT_TEXT,
            "sleep_delay": self.DEFAULT_DELAY,
            "excluded_users": dict(self.DEFAULT_EXCLUDED),
            "personal_replies": dict(self.DEFAULT_PERSONAL),
            "schedule": dict(self.DEFAULT_SCHEDULE),
            "active_grace": self.DEFAULT_ACTIVE_GRACE,
            "max_age": self.DEFAULT_MAX_AGE,
        }
        for key, value in defaults.items():
            try:
                if self.db.get("Sleep", key) is None:
                    self.db.set("Sleep", key, value)
            except Exception as e:
                self.logger.error("[Sleep] Ошибка инициализации БД, ключ %s: %s", key, e)

    def _get_db(self, key, default=None):
        """Получить значение из БД с fallback"""
        try:
            value = self.db.get("Sleep", key)
            if value is None:
                return default
            return value
        except Exception as e:
            self.logger.error("[Sleep] Ошибка чтения БД ключ %s: %s", key, e)
            return default

    def _int_db(self, key, default):
        """Получить число из БД, мусор не роняет модуль"""
        try:
            return int(self._get_db(key, default))
        except (TypeError, ValueError):
            self.logger.error("[Sleep] Битое число в БД ключ %s, беру %s", key, default)
            return default

    def _set_db(self, key, value):
        """Установить значение в БД с обработкой ошибок"""
        try:
            self.db.set("Sleep", key, value)
        except Exception as e:
            self.logger.error("[Sleep] Ошибка записи БД ключ %s: %s", key, e)

    def _is_in_schedule(self):
        """
        Проверить, находимся ли мы сейчас внутри интервала расписания.
        Возвращает True ТОЛЬКО если время совпало.
        """
        try:
            schedule = self._get_db("schedule", self.DEFAULT_SCHEDULE)

            if not isinstance(schedule, dict) or not schedule.get("enabled", False):
                return False  # Расписание выключено

            start = schedule.get("start")
            end = schedule.get("end")
            now = datetime.now().time()

            if not start and not end:
                return False

            start_time = datetime.strptime(start, "%H:%M").time() if start else None
            end_time = datetime.strptime(end, "%H:%M").time() if end else None

            # Оба времени заданы (например, 22:00 до 08:00)
            if start_time and end_time:
                if start_time <= end_time:
                    return start_time <= now <= end_time
                return now >= start_time or now <= end_time
            if end_time:
                return now <= end_time
            if start_time:
                return now >= start_time

            return False
        except Exception as e:
            self.logger.error("[Sleep] Ошибка расписания: %s", e)
            return False

    def _schedule_end_if_enabled(self):
        """Время окончания из расписания, но только если расписание включено"""
        schedule = self._get_db("schedule", self.DEFAULT_SCHEDULE)
        if isinstance(schedule, dict) and schedule.get("enabled"):
            return schedule.get("end")
        return None

    def _calc_auto_off(self, end_str):
        """Вычислить timestamp ближайшего времени завершения сна (HH:MM)"""
        if not end_str:
            return None
        try:
            now = datetime.now()
            end_time = datetime.strptime(end_str, "%H:%M").time()
            target_dt = datetime.combine(now.date(), end_time)
            # Если время HH:MM сегодня уже прошло — берем завтрашний день
            if now.time() >= end_time:
                target_dt += timedelta(days=1)
            return target_dt.timestamp()
        except Exception:
            return None

    def _epoch(self):
        """Абсолютные секунды сейчас (в той же шкале, что и message.date)"""
        return datetime.now(timezone.utc).timestamp()

    def _msg_ts(self, message):
        """Абсолютные секунды сообщения. Telethon отдаёт aware UTC datetime"""
        date = getattr(message, "date", None)
        if not isinstance(date, datetime):
            return None
        try:
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return date.timestamp()
        except (OverflowError, OSError, ValueError):
            return None

    def _restore_state(self):
        """Загрузить отметки из БД: без этого рестарт дина забывал, кому уже ответили"""
        stored = self._get_db(self.STATE_KEY, {})
        if not isinstance(stored, dict):
            stored = {}

        restored = {}
        raw_last = stored.get("last_response")
        if isinstance(raw_last, dict):
            for uid, ts in raw_last.items():
                try:
                    restored[str(uid)] = float(ts)
                except (TypeError, ValueError):
                    continue

        cutoff = self._epoch() - self.RESPONSE_RETENTION
        self.last_response_time = {uid: ts for uid, ts in restored.items() if ts > cutoff}
        self._prune_state(self._epoch())

        raw_answered = stored.get("answered_ids")
        if isinstance(raw_answered, (list, tuple)):
            ids = []
            for mid in raw_answered:
                try:
                    ids.append(int(mid))
                except (TypeError, ValueError):
                    continue
            self._answered_ids = deque(ids[-self.ANSWERED_IDS_CAP :], maxlen=self.ANSWERED_IDS_CAP)

        raw_owner = stored.get("owner_active")
        try:
            self._owner_last_active = None if raw_owner is None else float(raw_owner)
        except (TypeError, ValueError):
            self._owner_last_active = None

        self._owner_persisted_at = 0.0

    def _persist_state(self, force=False):
        """Сохранить отметки одним ключом.

        Активность владельца пишем не чаще раза в минуту: исходящих сообщений
        бывает много, а каждый db.set — это полная сериализация базы.
        """
        now = self._epoch()
        if not force and now - self._owner_persisted_at < self.OWNER_PERSIST_EVERY:
            return
        self._owner_persisted_at = now
        self._set_db(
            self.STATE_KEY,
            {
                "last_response": dict(self.last_response_time),
                "answered_ids": list(self._answered_ids),
                "owner_active": self._owner_last_active,
            },
        )

    def _prune_state(self, now_ts):
        """Очистка старых записей, чтобы память не росла бесконечно"""
        if len(self.last_response_time) > self.RESPONSE_PRUNE_AT:
            cutoff = now_ts - self.RESPONSE_RETENTION
            self.last_response_time = {
                uid: ts for uid, ts in self.last_response_time.items() if ts > cutoff
            }

        if len(self.last_response_time) > self.RESPONSE_HARD_CAP:
            self.last_response_time = dict(
                sorted(self.last_response_time.items(), key=lambda kv: kv[1])[-self.RESPONSE_HARD_CAP :]
            )

    def _is_bot(self, message):
        """Бот в личке: автоответ ему запускает пинг-понг до FloodWait"""
        try:
            sender = message.sender
        except Exception:
            # сущности нет в кэше — из-за этого молчать не будем
            return False
        return bool(getattr(sender, "bot", False))

    def _handle_outgoing(self, message):
        """Исходящее: либо наш автоответ, либо владелец действительно проснулся"""
        if message.id in self._auto_reply_ids:
            return

        chat_id = getattr(message, "chat_id", None)
        if chat_id is not None and self._inflight_chats.get(chat_id):
            return

        # Отложка и посты в канал — это не «владелец сел за телефон»:
        # автопостер или запланированное сообщение глушило бы автоответчик
        if getattr(message, "from_scheduled", False) or getattr(message, "action", None):
            return
        if getattr(message, "is_channel", False):
            return

        # Метку берём из message.date: после реконнекта старое исходящее
        # с телефона не должно выглядеть как «он пишет прямо сейчас»
        ts = self._msg_ts(message)
        if ts is None:
            ts = self._epoch()
        if self._owner_last_active is None or ts > self._owner_last_active:
            self._owner_last_active = ts
        self._persist_state()

    def _reserve_inflight(self, chat_id):
        if chat_id is None:
            return
        self._inflight_chats[chat_id] = self._inflight_chats.get(chat_id, 0) + 1

    def _release_inflight(self, chat_id):
        if chat_id is None:
            return
        left = self._inflight_chats.get(chat_id, 0) - 1
        if left > 0:
            self._inflight_chats[chat_id] = left
        else:
            self._inflight_chats.pop(chat_id, None)

    @loader.loop(interval=15, autostart=True)
    async def _auto_off_loop(self):
        """Автовыключение ручного режима по времени окончания расписания.

        Вынесено из watcher'а в отдельный луп: иначе выключение и лог
        срабатывали бы только при входящем сообщении.
        """
        manual_enabled = self._get_db("sleep_enabled", False)
        if not manual_enabled:
            return

        auto_off = self._get_db("sleep_auto_off_time", None)
        if auto_off is None:
            return

        try:
            expired = datetime.now().timestamp() >= float(auto_off)
        except (TypeError, ValueError):
            self._set_db("sleep_auto_off_time", None)
            self.logger.error("[Sleep] Битое значение sleep_auto_off_time, сброшено: %r", auto_off)
            return

        if expired:
            self._set_db("sleep_enabled", False)
            self._set_db("sleep_auto_off_time", None)
            self.logger.info("[Sleep] Время сна окончено, ручной режим автовыключен")

    @loader.watcher()
    async def watcher(self, message):
        """Следить за входящими ПМ и отправлять автоответ"""
        try:
            # 1. Исходящие: наш автоответ или активность владельца
            if getattr(message, "out", False):
                self._handle_outgoing(message)
                return

            if not hasattr(message, "text") or not hasattr(message, "sender_id"):
                return
            if message.is_group or message.is_channel:
                return

            sender_id = message.sender_id
            if sender_id is None or sender_id == self._my_id:
                return

            # 2. Служебные аккаунты, инлайн-боты и обычные боты: автоответ боту
            #    запускает пинг-понг «неизвестная команда» вплоть до FloodWait
            if sender_id in self.SERVICE_IDS or getattr(message, "via_bot_id", None):
                return
            if self._is_bot(message):
                return

            # Служебные сообщения (звонок, «X вступил в чат») не трогаем
            if getattr(message, "action", None):
                return

            # 3. Ручной режим (автовыключением занимается _auto_off_loop)
            manual_enabled = self._get_db("sleep_enabled", False)

            # Если ручной режим НЕ включен И по расписанию не время — МОЛЧИМ
            if not manual_enabled and not self._is_in_schedule():
                return

            # 4. Отвечаем на текст И на медиа: голосовое, кружок, фото и стикер
            #    без подписи имеют пустой text, но человек всё равно писал
            msg_text = getattr(message, "text", "")
            if not isinstance(msg_text, str):
                msg_text = ""
            if not msg_text.strip() and getattr(message, "media", None) is None:
                return

            sender_key = str(sender_id)

            # 5. Проверка исключений
            excluded = self._get_db("excluded_users", self.DEFAULT_EXCLUDED)
            if isinstance(excluded, dict) and sender_key in excluded:
                return

            now_ts = self._epoch()

            # 6. Офлайн-очередь: сообщение протухло, пока юзербота не было в сети
            max_age = self._int_db("max_age", self.DEFAULT_MAX_AGE)
            msg_ts = self._msg_ts(message)
            if max_age > 0 and msg_ts is not None and now_ts - msg_ts > max_age:
                self.logger.info(
                    "[Sleep] Сообщение от %s старше %s сек — не отвечаем", sender_key, max_age
                )
                return

            # 7. Владелец недавно сам писал — значит он онлайн, молчим
            grace = self._int_db("active_grace", self.DEFAULT_ACTIVE_GRACE)
            if (
                grace > 0
                and self._owner_last_active is not None
                and now_ts - self._owner_last_active < grace
            ):
                return

            # 8. Задержка между ответами одному юзеру
            delay = self._int_db("sleep_delay", self.DEFAULT_DELAY)

            # 9. Формирование текста: персональный ответ > общий текст
            response_text = None
            personal = self._get_db("personal_replies", self.DEFAULT_PERSONAL)
            if isinstance(personal, dict):
                candidate = personal.get(sender_key)
                if isinstance(candidate, str) and candidate:
                    response_text = candidate

            if not response_text:
                response_text = self._get_db("sleep_text", self.DEFAULT_TEXT)

            if not response_text or not isinstance(response_text, str):
                return

            # 10. Проверка «уже отвечали» и отметка — под локом, отправка снаружи.
            #     Диспетчер хикки запускает каждый вотчер через ensure_future
            #     (dispatcher.py:692), поэтому очередь после подключения разбирается
            #     параллельно: без лока все копии читают пустой last_response_time
            #     и отвечают залпом. Отметку ставим ДО отправки, иначе неудачная
            #     попытка при обрыве повторяется на каждом следующем сообщении.
            #     Сетевой вызов внутри лока держать нельзя: зависшая отправка
            #     (FloodWait, таймаут) выстроила бы в очередь все входящие.
            chat_id = getattr(message, "chat_id", None)
            async with self._send_lock:
                if message.id in self._answered_ids:
                    return

                if delay > 0:
                    last_time = self.last_response_time.get(sender_key)
                    if last_time and (now_ts - last_time < delay):
                        return

                self._answered_ids.append(message.id)
                self.last_response_time[sender_key] = now_ts
                self._prune_state(now_ts)
                self._persist_state(force=True)
                self._reserve_inflight(chat_id)

            sent = None
            try:
                sent = await message.respond(response_text)
            except Exception as e:
                self.logger.error("[Sleep] Автоответ для %s не ушел: %s", sender_key, e)
            finally:
                if sent is not None:
                    self._auto_reply_ids.append(sent.id)
                self._release_inflight(chat_id)

        except Exception as e:
            self.logger.error("[Sleep] Ошибка в watcher: %s", e)

    async def sleepcmd(self, message):
        """Включить/выключить вручную: .sleep"""
        try:
            enabled = not self._get_db("sleep_enabled", False)
            self._set_db("sleep_enabled", enabled)
            in_schedule = self._is_in_schedule()

            if enabled:
                # Автовыключение считаем только по включенному расписанию
                auto_off = self._calc_auto_off(self._schedule_end_if_enabled())
                self._set_db("sleep_auto_off_time", auto_off)

                schedule_info = ""
                if auto_off:
                    off_time_str = datetime.fromtimestamp(auto_off).strftime("%H:%M")
                    schedule_info = f"\n⏱️ <i>Автовыключение сработает в {off_time_str}</i>"
                if in_schedule:
                    schedule_info += "\nℹ️ <i>По расписанию сейчас время сна — автоответ и так уже работал</i>"

                await utils.answer(message, f"✅ <b>Режим ВКЛЮЧЕН</b>{schedule_info}")
            else:
                self._set_db("sleep_auto_off_time", None)
                note = ""
                if in_schedule:
                    note = "\nℹ️ <i>Но по расписанию сейчас время сна — автоответ продолжит работать</i>"
                await utils.answer(message, f"❌ <b>Режим ВЫКЛЮЧЕН</b>{note}")
        except Exception as e:
            self.logger.error("[Sleep] Ошибка в sleepcmd: %s", e)
            await utils.answer(message, f"❌ Ошибка: {utils.escape_html(str(e))}")

    async def sleepfaqcmd(self, message):
        """Справка по командам: .sleepfaq"""
        text = (
            "😴 <b>Sleep — справка</b>\n\n"
            "<code>.sleep</code> — вкл/выкл вручную\n"
            "<code>.sleeptext</code> — показать текущий текст ответа\n"
            "<code>.sleeptext я сплю, отвечу утром</code> — задать текст (макс 1000 симв.)\n"
            "<code>.sleepdelay</code> — показать задержку между ответами одному юзеру\n"
            "<code>.sleepdelay 60</code> — задать задержку в секундах (0 — без задержки)\n"
            "<code>.sleepactive</code> — показать окно «владелец онлайн»\n"
            "<code>.sleepactive 300</code> — не отвечать, если ты сам писал в последние 300 сек (0 — выкл)\n"
            "<code>.sleepmaxage</code> — показать, сколько секунд сообщение ещё свежее\n"
            "<code>.sleepmaxage 1800</code> — игнорировать лежалое (приехал позже 30 мин после отправки); 0 — выкл\n"
            "<code>.sleepschedule</code> — вкл/выкл расписание\n"
            "<code>.sleepschedule 22:00 08:00</code> — диапазон (с 22:00 до 08:00)\n"
            "<code>.sleepschedule 08:00</code> — одно время = ДО 08:00\n"
            "<code>.sleepschedule on / off</code> — явно вкл/выкл\n"
            "<code>.sleepexclude</code> — список исключённых (им вообще не отвечаем)\n"
            "<code>.sleepexclude 123456 мой бро</code> — добавить/удалить по ID\n"
            "<code>.sleepreply</code> — список персональных ответов\n"
            "<code>.sleepreply 123456 спи давай</code> — личный текст ответа для ID\n"
            "<code>.sleepreply 123456</code> — удалить личный текст\n\n"
            "ℹ️ Ручной режим (<code>.sleep</code>) и расписание — два независимых "
            "переключателя: автоответ идёт, если включено хотя бы одно из них.\n"
            "ℹ️ Но если ты сам недавно писал где-нибудь — автоответ молчит "
            "(окно настраивается через <code>.sleepactive</code>).\n"
            "ℹ️ Если юзербот был без сети и человек писал несколько раз, то после "
            "подключения он получит максимум один ответ: на лежалое старше "
            "<code>.sleepmaxage</code> не отвечаем вообще, а дубли одного и того же "
            "сообщения режем по id. Отметки «кому ответили» лежат в БД, поэтому "
            "перезапуск дина залпа не устраивает.\n"
            "ℹ️ Ботам, инлайн-ботам и служебным аккаунтам Telegram (777000) не "
            "отвечаем — иначе начинается пинг-понг «неизвестная команда».\n"
            "ℹ️ Отвечаем и на голосовые, кружки, фото и стикеры без подписи. "
            "Служебные сообщения (звонок, «вступил в чат») игнорируем.\n"
            "ℹ️ Активностью владельца считаются только живые сообщения: отложка и "
            "посты в каналы (автопостеры) автоответчик не глушат.\n\n"
            "dev: @xmogde, это упоминание только в этом ебучем скрипте, мб будет в других"
        )
        await utils.answer(message, text)

    async def sleeptextcmd(self, message):
        """Установить текст ответа: .sleeptext Твой текст"""
        try:
            text = utils.get_args_raw(message)

            if not text:
                current = self._get_db("sleep_text", self.DEFAULT_TEXT)
                if not current:
                    current = self.DEFAULT_TEXT
                await utils.answer(
                    message, f"📝 Текущий текст:\n{utils.escape_html(current)}"
                )
                return

            if len(text) > 1000:
                await utils.answer(message, "❌ Текст слишком длинный (макс 1000 символов)")
                return

            self._set_db("sleep_text", text)
            await utils.answer(message, "✅ Текст установлен")
        except Exception as e:
            self.logger.error("[Sleep] Ошибка в sleeptextcmd: %s", e)
            await utils.answer(message, f"❌ Ошибка: {utils.escape_html(str(e))}")

    async def sleepdelaycmd(self, message):
        """Установить задержку между ответами: .sleepdelay 60 (в секундах)"""
        try:
            args = utils.get_args(message)

            if not args:
                delay = self._get_db("sleep_delay", self.DEFAULT_DELAY)
                if delay is None:
                    delay = self.DEFAULT_DELAY
                await utils.answer(message, f"⏱️ Текущая задержка: {delay} сек")
                return

            try:
                delay = int(args[0])
            except (ValueError, TypeError):
                await utils.answer(message, "❌ Укажи число (секунды)")
                return

            if delay < 0:
                await utils.answer(message, "❌ Задержка не может быть отрицательной")
                return

            if delay > 3600:
                await utils.answer(message, "❌ Задержка не может быть больше часа")
                return

            self._set_db("sleep_delay", delay)
            await utils.answer(message, f"✅ Задержка установлена: {delay} сек")
        except Exception as e:
            self.logger.error("[Sleep] Ошибка в sleepdelaycmd: %s", e)
            await utils.answer(message, f"❌ Ошибка: {utils.escape_html(str(e))}")

    async def sleepactivecmd(self, message):
        """Окно «владелец онлайн»: .sleepactive | .sleepactive 300 (сек) | .sleepactive 0 (выкл)"""
        try:
            args = utils.get_args(message)

            if not args:
                grace = self._get_db("active_grace", self.DEFAULT_ACTIVE_GRACE)
                await utils.answer(message, f"👀 Окно активности владельца: {grace} сек")
                return

            try:
                grace = int(args[0])
            except (ValueError, TypeError):
                await utils.answer(message, "❌ Укажи число (секунды)")
                return

            if grace < 0:
                await utils.answer(message, "❌ Значение не может быть отрицательным")
                return

            if grace > 86400:
                await utils.answer(message, "❌ Максимум 86400 сек (сутки)")
                return

            self._set_db("active_grace", grace)
            if grace == 0:
                await utils.answer(message, "✅ Проверка активности владельца выключена")
            else:
                await utils.answer(
                    message,
                    f"✅ Если ты сам писал в последние {grace} сек — автоответ молчит",
                )
        except Exception as e:
            self.logger.error("[Sleep] Ошибка в sleepactivecmd: %s", e)
            await utils.answer(message, f"❌ Ошибка: {utils.escape_html(str(e))}")

    async def sleepmaxagecmd(self, message):
        """Сколько секунд сообщение ещё свежее: .sleepmaxage | .sleepmaxage 1800 | 0 (выкл)"""
        try:
            args = utils.get_args(message)

            if not args:
                max_age = self._int_db("max_age", self.DEFAULT_MAX_AGE)
                if max_age == 0:
                    await utils.answer(
                        message, "🕰️ Проверка возраста сообщений выключена — отвечаем и на лежалые"
                    )
                else:
                    await utils.answer(
                        message,
                        f"🕰️ Не отвечаем на сообщения, которые приехали позже {max_age} сек "
                        f"(~{round(max_age / 60)} мин) после отправки",
                    )
                return

            try:
                max_age = int(args[0])
            except (ValueError, TypeError):
                await utils.answer(message, "❌ Укажи число (секунды)")
                return

            if max_age < 0:
                await utils.answer(message, "❌ Значение не может быть отрицательным")
                return

            if max_age > 86400:
                await utils.answer(message, "❌ Максимум 86400 сек (сутки)")
                return

            self._set_db("max_age", max_age)
            if max_age == 0:
                await utils.answer(message, "✅ Проверка возраста сообщений выключена")
            else:
                await utils.answer(
                    message,
                    f"✅ Сообщения старше {max_age} сек игнорируем — при подключении после обрыва "
                    f"лежалое не отвечаем",
                )
        except Exception as e:
            self.logger.error("[Sleep] Ошибка в sleepmaxagecmd: %s", e)
            await utils.answer(message, f"❌ Ошибка: {utils.escape_html(str(e))}")

    async def sleepschedulecmd(self, message):
        """
        Управление расписанием:
        .sleepschedule — переключить (ВКЛ/ВЫКЛ) с сохранением диапазона
        .sleepschedule 22:00 08:00 — установить диапазон (с 22:00 до 08:00)
        .sleepschedule 10:00 — установить время окончания (до 10:00)
        .sleepschedule off — отключить расписание
        """
        try:
            args = utils.get_args(message)
            schedule = self._get_db("schedule", self.DEFAULT_SCHEDULE)
            if not isinstance(schedule, dict):
                schedule = dict(self.DEFAULT_SCHEDULE)

            # Если без аргументов — переключаем статус ВКЛ/ВЫКЛ
            if not args:
                schedule["enabled"] = not schedule.get("enabled", False)
                self._set_db("schedule", schedule)
            elif args[0].lower() in ["off", "выкл"]:
                schedule["enabled"] = False
                self._set_db("schedule", schedule)
            elif args[0].lower() in ["on", "вкл"]:
                schedule["enabled"] = True
                self._set_db("schedule", schedule)
            elif len(args) == 1:
                # Одно время — время окончания (до HH:MM)
                end = args[0]
                try:
                    datetime.strptime(end, "%H:%M")
                except ValueError:
                    await utils.answer(message, "❌ Неверный формат времени (HH:MM)")
                    return
                schedule = {"enabled": True, "start": None, "end": end}
                self._set_db("schedule", schedule)
            elif len(args) >= 2:
                # Два времени (с HH:MM до HH:MM)
                start, end = args[0], args[1]
                try:
                    datetime.strptime(start, "%H:%M")
                    datetime.strptime(end, "%H:%M")
                except ValueError:
                    await utils.answer(message, "❌ Неверный формат времени (HH:MM)")
                    return
                schedule = {"enabled": True, "start": start, "end": end}
                self._set_db("schedule", schedule)

            # Пересчитываем таймер автовыключения для ручного режима, если он сейчас активен
            if self._get_db("sleep_enabled", False):
                self._set_db("sleep_auto_off_time", self._calc_auto_off(self._schedule_end_if_enabled()))

            # Красивый вывод статуса и диапазона
            is_enabled = schedule.get("enabled", False)
            start = schedule.get("start")
            end = schedule.get("end")

            status_str = "✅ <b>Расписание ВКЛЮЧЕНО</b>" if is_enabled else "❌ <b>Расписание ВЫКЛЮЧЕНО</b>"

            if start and end:
                range_str = f"с {start} до {end}"
            elif end:
                range_str = f"до {end}"
            elif start:
                range_str = f"с {start}"
            else:
                range_str = "диапазон не задан"

            # _is_in_schedule() с пустыми границами всегда False, поэтому
            # «включено» без диапазона — это не работающее расписание
            warn = ""
            if is_enabled and not start and not end:
                warn = (
                    "\n⚠️ <i>Без диапазона расписание не сработает никогда. "
                    "Задай время: <code>.sleepschedule 22:00 08:00</code></i>"
                )

            await utils.answer(message, f"{status_str}\n⏱️ Диапазон: <b>{range_str}</b>{warn}")

        except Exception as e:
            self.logger.error("[Sleep] Ошибка в sleepschedulecmd: %s", e)
            await utils.answer(message, f"❌ Ошибка: {utils.escape_html(str(e))}")

    async def sleepexcludecmd(self, message):
        """Управление исключениями: .sleepexclude (список) или .sleepexclude ID [заметка] (добавить/удалить)"""
        try:
            args = utils.get_args_raw(message).split(maxsplit=1)

            excluded = self._get_db("excluded_users", self.DEFAULT_EXCLUDED)
            if not isinstance(excluded, dict):
                excluded = dict(self.DEFAULT_EXCLUDED)

            # Если вызвали без аргументов — выводим список
            if not args or not args[0]:
                if excluded:
                    text = "📋 <b>Исключения:</b>\n"
                    for uid_str, data in excluded.items():
                        if isinstance(data, dict):
                            note = data.get("note", "без заметки")
                        else:
                            note = str(data)
                        text += f"• <code>{utils.escape_html(uid_str)}</code>: {utils.escape_html(note)}\n"
                    await utils.answer(message, text)
                else:
                    await utils.answer(message, "📋 Список исключений пуст")
                return

            user_id = args[0]
            # Исключение сверяется в вотчере по str(message.sender_id), поэтому
            # @username сюда записать нельзя — запись просто не сработала бы
            if not user_id.isdigit():
                await utils.answer(message, "❌ ID должен быть числом (узнать: .id в ответ на сообщение)")
                return

            note = args[1] if len(args) > 1 else "без заметки"
            if len(note) > self.NOTE_MAX_LEN:
                note = note[: self.NOTE_MAX_LEN]

            # Переключатель: если уже есть — удаляем, если нет — добавляем
            if user_id in excluded:
                del excluded[user_id]
                self._set_db("excluded_users", excluded)
                await utils.answer(message, f"❌ Юзер <code>{user_id}</code> удален из исключений")
            else:
                excluded[user_id] = {"note": note}
                self._set_db("excluded_users", excluded)
                await utils.answer(
                    message,
                    f"✅ Юзер <code>{user_id}</code> добавлен в исключения "
                    f"(<i>{utils.escape_html(note)}</i>)",
                )

        except Exception as e:
            self.logger.error("[Sleep] Ошибка в sleepexcludecmd: %s", e)
            await utils.answer(message, f"❌ Ошибка: {utils.escape_html(str(e))}")

    async def sleepreplycmd(self, message):
        """Персональный ответ: .sleepreply (список) | .sleepreply ID текст (задать) | .sleepreply ID (удалить)"""
        try:
            args = utils.get_args_raw(message).split(maxsplit=1)

            replies = self._get_db("personal_replies", self.DEFAULT_PERSONAL)
            if not isinstance(replies, dict):
                replies = dict(self.DEFAULT_PERSONAL)

            # Без аргументов — список
            if not args or not args[0]:
                if replies:
                    text = "💬 <b>Персональные ответы:</b>\n"
                    for uid_str, personal_text in replies.items():
                        text += (
                            f"• <code>{utils.escape_html(uid_str)}</code>: "
                            f"{utils.escape_html(personal_text)}\n"
                        )
                    await utils.answer(message, text)
                else:
                    await utils.answer(message, "💬 Персональных ответов нет")
                return

            user_id = args[0]
            if not user_id.isdigit():
                await utils.answer(message, "❌ ID должен быть числом")
                return

            # Только ID — удаляем
            if len(args) == 1:
                if user_id in replies:
                    del replies[user_id]
                    self._set_db("personal_replies", replies)
                    await utils.answer(message, f"❌ Персональный ответ для <code>{user_id}</code> удален")
                else:
                    await utils.answer(
                        message,
                        "❌ Для этого ID персональный ответ не задан. Задать: <code>.sleepreply ID текст</code>",
                    )
                return

            # ID + текст — устанавливаем
            text = args[1].strip()
            if not text:
                await utils.answer(message, "❌ Текст не может быть пустым")
                return

            if len(text) > 1000:
                await utils.answer(message, "❌ Текст слишком длинный (макс 1000 символов)")
                return

            replies[user_id] = text
            self._set_db("personal_replies", replies)
            await utils.answer(message, f"✅ Персональный ответ для <code>{user_id}</code> установлен")

        except Exception as e:
            self.logger.error("[Sleep] Ошибка в sleepreplycmd: %s", e)
            await utils.answer(message, f"❌ Ошибка: {utils.escape_html(str(e))}")
