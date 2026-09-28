# Sleep.py (полная версия с исправленным fallback и watcher) XYINYAAAA
import logging
from datetime import datetime, timedelta
import asyncio
from .. import loader, utils

class SleepMod(loader.Module):
    """Автоответ в личных сообщениях с БД, расписанием, задержкой и персональными ответами"""
    
    strings = {
        "name": "Sleep",
    }

    # Значения по умолчанию если БД упадет
    DEFAULT_TEXT = "я ушЁл спать✅  не говорим много а то это говно будет спамить✅ спокойных снов da✅ def"
    DEFAULT_DELAY = 3600
    DEFAULT_SCHEDULE = {"enabled": False, "start": None, "end": None}
    DEFAULT_EXCLUDED = {}

    def init(self):
        self.logger = logging.getLogger("Sleep") # <-- ДОБАВЬ ЭТУ СТРОЧКУ

    async def client_ready(self, client, db):
        self.client = client
        self.db = db
        self.last_response_time = {}
        
        try:
            self._init_db()
        except Exception as e:
            self.logger.error(f"[Sleep] Ошибка инициализации БД: {e}")

    def _init_db(self):
        """Инициализировать БД если её нет"""
        try:
            if not self.db.get("Sleep", "sleep_enabled"):
                self.db.set("Sleep", "sleep_enabled", False)
            if not self.db.get("Sleep", "sleep_text"):
                self.db.set("Sleep", "sleep_text", self.DEFAULT_TEXT)
            if not self.db.get("Sleep", "sleep_delay"):
                self.db.set("Sleep", "sleep_delay", self.DEFAULT_DELAY)
            if not self.db.get("Sleep", "excluded_users"):
                self.db.set("Sleep", "excluded_users", self.DEFAULT_EXCLUDED)
            if not self.db.get("Sleep", "schedule"):
                self.db.set("Sleep", "schedule", self.DEFAULT_SCHEDULE)
        except Exception as e:
            self.logger.error(f"[Sleep] Ошибка при инициализации БД: {e}")

    def _get_db(self, key, default=None):
        """Получить значение из БД с fallback"""
        try:
            value = self.db.get("Sleep", key)
            if value is None:
                return default
            return value
        except Exception as e:
            self.logger.error(f"[Sleep] Ошибка чтения БД ключ {key}: {e}")
            return default

    def _set_db(self, key, value):
        """Установить значение в БД с обработкой ошибок"""
        try:
            self.db.set("Sleep", key, value)
        except Exception as e:
            self.logger.error(f"[Sleep] Ошибка записи БД ключ {key}: {e}")

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
                else:
                    return now >= start_time or now <= end_time
            elif end_time:
                return now <= end_time
            elif start_time:
                return now >= start_time

            return False
        except Exception as e:
            try:
                self.logger.error(f"[Sleep] Ошибка расписания: {e}")
            except AttributeError:
                pass
            return False

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

    @loader.watcher()
    async def watcher(self, message):
        """Следить за входящими ПМ и отправлять автоответ"""
        try:
            # 1. Базовые проверки сообщения
            if not hasattr(message, 'text') or not hasattr(message, 'sender_id'):
                return
            if message.is_group or message.is_channel:
                return

            me = await self.client.get_me()
            if message.sender_id == me.id:
                return

            # 2. Проверка автовыключения ручного режима по времени окончания расписания
            manual_enabled = self._get_db("sleep_enabled", False)
            if manual_enabled:
                auto_off = self._get_db("sleep_auto_off_time", None)
                if auto_off and datetime.now().timestamp() >= float(auto_off):
                    manual_enabled = False
                    self._set_db("sleep_enabled", False)
                    self._set_db("sleep_auto_off_time", None)
                    try:
                        self.logger.info("[Sleep] Время сна окончено, ручной режим автовыключен")
                    except Exception:
                        pass

            in_schedule = self._is_in_schedule()

            # Если ручной режим НЕ включен И по расписанию не время — МОЛЧИМ
            if not manual_enabled and not in_schedule:
                return

            # 3. Проверка текста сообщения
            msg_text = getattr(message, 'text', None)
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

            if delay > 0:
                last_time = self.last_response_time.get(sender_id)
                if last_time and (datetime.now().timestamp() - last_time < delay):
                    return

            # 6. Формирование и отправка текста
            response_text = None
            if isinstance(excluded, dict) and isinstance(excluded.get(sender_id), dict):
                response_text = excluded[sender_id].get("personal_text")

            if not response_text:
                response_text = self._get_db("sleep_text", self.DEFAULT_TEXT)

            if not response_text or not isinstance(response_text, str):
                return

            await message.respond(response_text)
            self.last_response_time[sender_id] = datetime.now().timestamp()

        except Exception as e:
            try:
                self.logger.error(f"[Sleep] Ошибка в watcher: {e}")
            except Exception:
                pass

    async def sleepcmd(self, message):
        """Включить/выключить вручную: .sleep"""
        try:
            enabled = self._get_db("sleep_enabled", False)
            enabled = not enabled
            self._set_db("sleep_enabled", enabled)

            if enabled:
                # Рассчитываем автовыключение по времени расписания (если задано)
                schedule = self._get_db("schedule", self.DEFAULT_SCHEDULE)
                end_str = schedule.get("end") if isinstance(schedule, dict) else None
                auto_off = self._calc_auto_off(end_str)
                self._set_db("sleep_auto_off_time", auto_off)

                schedule_info = ""
                if auto_off:
                    off_time_str = datetime.fromtimestamp(auto_off).strftime("%H:%M")
                    schedule_info = f"\n⏱️ <i>Автовыключение сработает в {off_time_str}</i>"

                await utils.answer(message, f"✅ <b>Режим ВКЛЮЧЕН</b>{schedule_info}")
            else:
                self._set_db("sleep_auto_off_time", None)
                await utils.answer(message, "❌ <b>Режим ВЫКЛЮЧЕН</b>")
        except Exception as e:
            try:
                self.logger.error(f"[Sleep] Ошибка в sleepcmd: {e}")
            except Exception:
                pass
            await utils.answer(message, f"❌ Ошибка: {e}")

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
            self.logger.error(f"[Sleep] Ошибка в sleeptextcmd: {e}")
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
            self.logger.error(f"[Sleep] Ошибка в sleepdelaycmd: {e}")
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
                schedule = self.DEFAULT_SCHEDULE.copy()

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
                auto_off = self._calc_auto_off(schedule.get("end"))
                self._set_db("sleep_auto_off_time", auto_off)

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
            try:
                self.logger.error(f"[Sleep] Ошибка в sleepschedulecmd: {e}")
            except Exception:
                pass
            await utils.answer(message, f"❌ Ошибка: {e}")

    async def sleepexcludecmd(self, message):
        """Управление исключениями: .sleepexclude (список) или .sleepexclude ID [заметка] (добавить/удалить)"""
        try:
            args = utils.get_args_raw(message).split(maxsplit=1)
            
            excluded = self._get_db("excluded_users", self.DEFAULT_EXCLUDED)
            if not isinstance(excluded, dict):
                excluded = self.DEFAULT_EXCLUDED.copy()
            
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
            self.logger.error(f"[Sleep] Ошибка в sleepexcludecmd: {e}")
            await utils.answer(message, f"❌ Ошибка: {e}")
            
