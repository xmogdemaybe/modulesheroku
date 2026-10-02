import logging
from datetime import datetime, timedelta

from .. import loader, utils


class SleepMod(loader.Module):
    """Автоответ в личных сообщениях с БД, расписанием, задержкой и персональными ответами

    мета_девелопер @xmogde
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

    async def client_ready(self, client, db):
        self.logger = logging.getLogger("Sleep")
        self.client = client
        self.db = db
        self.last_response_time = {}
        self._my_id = (await client.get_me()).id
        self._init_db()

    def _init_db(self):
        """Инициализировать БД если её нет (без перезаписи уже заданных значений)"""
        defaults = {
            "sleep_enabled": False,
            "sleep_text": self.DEFAULT_TEXT,
            "sleep_delay": self.DEFAULT_DELAY,
            "excluded_users": dict(self.DEFAULT_EXCLUDED),
            "personal_replies": dict(self.DEFAULT_PERSONAL),
            "schedule": dict(self.DEFAULT_SCHEDULE),
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

        if datetime.now().timestamp() >= float(auto_off):
            self._set_db("sleep_enabled", False)
            self._set_db("sleep_auto_off_time", None)
            self.logger.info("[Sleep] Время сна окончено, ручной режим автовыключен")

    @loader.watcher()
    async def watcher(self, message):
        """Следить за входящими ПМ и отправлять автоответ"""
        try:
            # 1. Базовые проверки сообщения
            if getattr(message, "out", False):
                return
            if not hasattr(message, "text") or not hasattr(message, "sender_id"):
                return
            if message.is_group or message.is_channel:
                return
            if message.sender_id is None or message.sender_id == self._my_id:
                return

            # 2. Ручной режим (автовыключением занимается _auto_off_loop)
            manual_enabled = self._get_db("sleep_enabled", False)

            # Если ручной режим НЕ включен И по расписанию не время — МОЛЧИМ
            if not manual_enabled and not self._is_in_schedule():
                return

            # 3. Проверка текста сообщения
            msg_text = getattr(message, "text", None)
            if not msg_text or not isinstance(msg_text, str):
                return

            sender_id = str(message.sender_id)

            # 4. Проверка исключений
            excluded = self._get_db("excluded_users", self.DEFAULT_EXCLUDED)
            if isinstance(excluded, dict) and sender_id in excluded:
                return

            # 5. Проверка задержки (delay)
            delay = self._get_db("sleep_delay", self.DEFAULT_DELAY)
            try:
                delay = int(delay)
            except (ValueError, TypeError):
                delay = self.DEFAULT_DELAY

            now_ts = datetime.now().timestamp()
            if delay > 0:
                last_time = self.last_response_time.get(sender_id)
                if last_time and (now_ts - last_time < delay):
                    return

            # 6. Формирование текста: персональный ответ > общий текст
            response_text = None
            personal = self._get_db("personal_replies", self.DEFAULT_PERSONAL)
            if isinstance(personal, dict):
                candidate = personal.get(sender_id)
                if isinstance(candidate, str) and candidate:
                    response_text = candidate

            if not response_text:
                response_text = self._get_db("sleep_text", self.DEFAULT_TEXT)

            if not response_text or not isinstance(response_text, str):
                return

            await message.respond(response_text)
            self.last_response_time[sender_id] = now_ts

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
            await utils.answer(message, f"❌ Ошибка: {e}")

    async def sleepfaqcmd(self, message):
        """Справка по командам: .sleepfaq"""
        text = (
            "😴 <b>Sleep — справка</b>\n\n"
            "<code>.sleep</code> — вкл/выкл вручную\n"
            "<code>.sleeptext</code> — показать текущий текст ответа\n"
            "<code>.sleeptext я сплю, отвечу утром</code> — задать текст (макс 1000 симв.)\n"
            "<code>.sleepdelay</code> — показать задержку между ответами одному юзеру\n"
            "<code>.sleepdelay 60</code> — задать задержку в секундах (0 — без задержки)\n"
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
            "переключателя: автоответ идёт, если включено хотя бы одно из них.\n\n"
            "мета_девелопер @xmogde"
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
                await utils.answer(message, f"📝 Текущий текст:\n{current}")
                return

            if len(text) > 1000:
                await utils.answer(message, "❌ Текст слишком длинный (макс 1000 символов)")
                return

            self._set_db("sleep_text", text)
            await utils.answer(message, f"✅ Текст установлен")
        except Exception as e:
            self.logger.error("[Sleep] Ошибка в sleeptextcmd: %s", e)
            await utils.answer(message, f"❌ Ошибка: {e}")

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
            await utils.answer(message, f"❌ Ошибка: {e}")

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

            await utils.answer(message, f"{status_str}\n⏱️ Диапазон: <b>{range_str}</b>")

        except Exception as e:
            self.logger.error("[Sleep] Ошибка в sleepschedulecmd: %s", e)
            await utils.answer(message, f"❌ Ошибка: {e}")

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
                        text += f"• <code>{uid_str}</code>: {note}\n"
                    await utils.answer(message, text)
                else:
                    await utils.answer(message, "📋 Список исключений пуст")
                return

            user_id = args[0]
            note = args[1] if len(args) > 1 else "без заметки"

            # Переключатель: если уже есть — удаляем, если нет — добавляем
            if user_id in excluded:
                del excluded[user_id]
                self._set_db("excluded_users", excluded)
                await utils.answer(message, f"❌ Юзер <code>{user_id}</code> удален из исключений")
            else:
                excluded[user_id] = {"note": note}
                self._set_db("excluded_users", excluded)
                await utils.answer(message, f"✅ Юзер <code>{user_id}</code> добавлен в исключения (<i>{note}</i>)")

        except Exception as e:
            self.logger.error("[Sleep] Ошибка в sleepexcludecmd: %s", e)
            await utils.answer(message, f"❌ Ошибка: {e}")

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
                        text += f"• <code>{uid_str}</code>: {personal_text}\n"
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
            await utils.answer(message, f"❌ Ошибка: {e}")
