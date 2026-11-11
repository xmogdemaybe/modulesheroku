# BadApple — заливает фотки канала на аватарку от новых к старым (Heroku).
#
# Идея: старые авы не удаляются — листание накопленных аватарок проигрывает
# анимацию. Курсор (last_id сообщения) хранится в БД, рестарт продолжает с того
# же места. Флуд-вейт: спим ровно сколько просит Telegram, курсор не двигаем.
# Дошли до начала канала — уведомление в сохраненки и стоп.
# После загрузки модуль на паузе: запуск — .badapple_start.
# Предохранитель: 10 ошибок подряд (flood-паузы не в счет) — уведомление
# в сохраненки один раз на серию, успешный шаг сбрасывает счетчик.
#
# Команды:
#   .badapple                  статус
#   .badapple_start            возобновить
#   .badapple_stop             пауза (курсор сохраняется)
#   .badapple_interval <сек>   интервал между заливками (мин. 5, дефолт 15)
#   .badapple_channel <user>   сменить канал (курсор сбрасывается)
#   .badapple_reset            залить канал заново (будут дубли в галерее)

import io
import logging
import time

from herokutl.errors import FloodWaitError
from herokutl.tl.functions.photos import UploadProfilePhotoRequest
from herokutl.tl.types import Message

from .. import loader, utils

logger = logging.getLogger(__name__)

DEFAULT_CHANNEL = "badappletg"
DEFAULT_INTERVAL = 15
ERROR_STREAK_LIMIT = 10


class BadAppleMod(loader.Module):
    """Заливает фотки канала на аватарку от новых к старым, старые авы не удаляет"""

    strings = {"name": "BadApple"}

    def _reset_cursor(self):
        self.set("last_id", 0)
        self.set("uploaded", 0)
        self.set("done", False)
        self.set("next_at", 0)

    async def _upload_next(self):
        async for msg in self._client.iter_messages(
            self.get("channel", DEFAULT_CHANNEL),
            offset_id=self.get("last_id", 0),
        ):
            if not msg.photo:
                continue

            data = await msg.download_media(bytes)
            if not data:
                logger.warning("Пустое скачивание сообщения %s, пропускаю", msg.id)
                self.set("last_id", msg.id)
                continue

            file = await self._client.upload_file(
                io.BytesIO(data), file_name=f"badapple_{msg.id}.jpg"
            )
            await self._client(UploadProfilePhotoRequest(file=file))
            self.set("last_id", msg.id)
            self.set("uploaded", self.get("uploaded", 0) + 1)
            return

        self.set("done", True)
        await self._client.send_message(
            self.tg_id,
            "<b>BadApple: канал залит полностью.</b> "
            f"Загружено аватарок: {self.get('uploaded', 0)}.",
        )

    @loader.loop(interval=5, autostart=True, wait_before=True)
    async def _worker(self):
        if not self.get("enabled", False) or self.get("done", False):
            return
        if time.time() < self.get("next_at", 0):
            return

        delay = self.get("interval", DEFAULT_INTERVAL)
        try:
            await self._upload_next()
        except FloodWaitError as e:
            delay = e.seconds + 5
            logger.warning("FloodWait, пауза %s с", e.seconds)
        except Exception as e:
            logger.exception("Ошибка шага заливки BadApple")
            streak = self.get("err_streak", 0) + 1
            self.set("err_streak", streak)
            if streak == ERROR_STREAK_LIMIT:
                await self._client.send_message(
                    self.tg_id,
                    "<b>BadApple: заливка встала.</b> "
                    f"{ERROR_STREAK_LIMIT} ошибок подряд, последняя: {e}. "
                    "Ретраи продолжаются, курсор на месте.",
                )
        else:
            self.set("err_streak", 0)
        self.set("next_at", round(time.time() + delay))

    @loader.command()
    async def badapple(self, message: Message):
        """— статус заливки аватарок"""
        state = (
            "завершено"
            if self.get("done", False)
            else "работает"
            if self.get("enabled", False)
            else "пауза"
        )
        await utils.answer(
            message,
            "<b>BadApple</b>\n"
            f"Канал: <code>{self.get('channel', DEFAULT_CHANNEL)}</code>\n"
            f"Состояние: {state}\n"
            f"Залито: {self.get('uploaded', 0)}\n"
            f"Курсор (last_id): {self.get('last_id', 0)}\n"
            f"Ошибок подряд: {self.get('err_streak', 0)}\n"
            f"Интервал: {self.get('interval', DEFAULT_INTERVAL)} с\n"
            f"Следующая заливка через: "
            f"{max(0, round(self.get('next_at', 0) - time.time()))} с",
        )

    @loader.command()
    async def badapple_start(self, message: Message):
        """— возобновить заливку"""
        self.set("enabled", True)
        self.set("next_at", 0)
        await utils.answer(message, "<b>BadApple: запущено.</b>")

    @loader.command()
    async def badapple_stop(self, message: Message):
        """— пауза, курсор сохраняется"""
        self.set("enabled", False)
        await utils.answer(message, "<b>BadApple: пауза.</b>")

    @loader.command()
    async def badapple_interval(self, message: Message):
        """<секунды> — интервал между заливками, минимум 5"""
        args = utils.get_args_raw(message)
        if not args.isdigit() or int(args) < 5:
            await utils.answer(message, "<b>Нужно число секунд &gt;= 5.</b>")
            return
        self.set("interval", int(args))
        await utils.answer(message, f"<b>Интервал: {args} с.</b>")

    @loader.command()
    async def badapple_channel(self, message: Message):
        """<username|ссылка> — сменить канал, курсор сбрасывается"""
        args = utils.get_args_raw(message).strip()
        if not args:
            await utils.answer(message, "<b>Укажи username или ссылку на канал.</b>")
            return
        channel = args.removeprefix("https://").removeprefix("t.me/").lstrip("@")
        self.set("channel", channel)
        self._reset_cursor()
        await utils.answer(message, f"<b>Канал: {channel}. Курсор сброшен.</b>")

    @loader.command()
    async def badapple_reset(self, message: Message):
        """— сбросить курсор и залить канал заново (будут дубли в галерее)"""
        self._reset_cursor()
        await utils.answer(
            message, "<b>Курсор сброшен, заливка пойдет с конца канала.</b>"
        )