# -*- coding: utf-8 -*-
# Fumo AutoPoster — Hikka / Heroku Userbot
#
# Commands:
#   .fumo on/off
#   .fumotarget <@username|chat_id>
#   .fumointerval <minutes|hours>
#   .fumocaption <text>
#   .fumotest
#   .fumostatus
#
# Caption placeholders:
#   {source}  - API source
#   {date}    - UTC date
#   {time}    - UTC time
#
# No external database/files are used.
# Settings are stored in self.db.

import asyncio
import random
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Optional

import aiohttp

from telethon.errors import (
    FloodWaitError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
)

from .. import loader, utils


@loader.tds
class Fumo(loader.Module):
    """Random Touhou Fumo image auto-poster."""

    strings = {
        "name": "Fumo",
    }

    # -----------------------------
    # Configuration
    # -----------------------------

    DEFAULT_INTERVAL = 60 * 60
    MIN_INTERVAL = 60
    MAX_INTERVAL = 7 * 24 * 60 * 60

    HTTP_TIMEOUT = 25
    MAX_IMAGE_SIZE = 25 * 1024 * 1024

    USER_AGENT = (
        "Mozilla/5.0 (compatible; Hikka-Fumo/1.0)"
    )

    # Safebooru's DAPI is documented and supports JSON.
    # Danbooru exposes posts.json publicly.
    #
    # We try several tag combinations because "fumo" and
    # "fumo touhou" do not necessarily return identical sets.
    PROVIDERS = (
        {
            "name": "Safebooru",
            "url": "https://safebooru.org/index.php",
            "base_params": {
                "page": "dapi",
                "s": "post",
                "q": "index",
                "json": "1",
                "limit": "100",
            },
            "tags": (
                "fumo rating:safe",
                "fumo touhou rating:safe",
                "touhou fumo rating:safe",
            ),
        },
        {
            "name": "Danbooru",
            "url": "https://danbooru.donmai.us/posts.json",
            "base_params": {
                "limit": "100",
                "only": (
                    "id,file_url,large_file_url,"
                    "preview_file_url,rating,source"
                ),
            },
            "tags": (
                "fumo rating:safe",
                "fumo touhou rating:safe",
                "touhou fumo rating:safe",
            ),
        },
    )

    # -----------------------------
    # Lifecycle
    # -----------------------------

    async def client_ready(self, client, db):
        self.client = client
        self.db = db

        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._stopping = False

        if self._get("enabled", None) is None:
            self._set("enabled", False)

        if self._get("interval", None) is None:
            self._set("interval", self.DEFAULT_INTERVAL)

        if self._get("target", None) is None:
            self._set("target", None)

        if self._get("caption", None) is None:
            self._set("caption", "")

        if self._get("last_source", None) is None:
            self._set("last_source", "")

        if self._get("last_url", None) is None:
            self._set("last_url", "")

        if self._get("last_time", None) is None:
            self._set("last_time", "")

        self._stopping = False
        self._start_scheduler()

    async def on_unload(self):
        self._stopping = True

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._task = None

        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    def _start_scheduler(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._scheduler_loop()
            )

    # -----------------------------
    # Database
    # -----------------------------

    def _get(self, key: str, default: Any = None):
        return self.db.get(
            self.strings["name"],
            key,
            default,
        )

    def _set(self, key: str, value: Any):
        self.db.set(
            self.strings["name"],
            key,
            value,
        )

    # -----------------------------
    # HTTP
    # -----------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=self.HTTP_TIMEOUT
            )

            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": self.USER_AGENT,
                    "Accept": "application/json",
                },
            )

        return self._session

    async def _get_json(
        self,
        url: str,
        params: dict,
    ):
        session = await self._get_session()

        async with session.get(
            url,
            params=params,
        ) as response:
            response.raise_for_status()
            return await response.json(
                content_type=None
            )

    # -----------------------------
    # API parsing
    # -----------------------------

    @staticmethod
    def _posts_from_payload(payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return [
                item for item in payload
                if isinstance(item, dict)
            ]

        if isinstance(payload, dict):
            posts = payload.get("posts")
            if isinstance(posts, list):
                return [
                    item for item in posts
                    if isinstance(item, dict)
                ]

            if (
                "file_url" in payload
                or "large_file_url" in payload
            ):
                return [payload]

        return []

    @staticmethod
    def _image_url(post: dict) -> Optional[str]:
        # Prefer original/large files.
        for key in (
            "file_url",
            "large_file_url",
            "original",
        ):
            value = post.get(key)
            if (
                isinstance(value, str)
                and value.startswith(("http://", "https://"))
            ):
                return value

        return None

    @staticmethod
    def _image_extension(url: str) -> Optional[str]:
        clean = url.lower().split("?", 1)[0]

        if clean.endswith((".jpg", ".jpeg")):
            return ".jpg"

        if clean.endswith(".png"):
            return ".png"

        if clean.endswith(".webp"):
            return ".webp"

        if clean.endswith(".gif"):
            return ".gif"

        return None

    async def _query_provider(
        self,
        provider: dict,
        tags: str,
    ) -> Optional[dict]:
        params = dict(provider["base_params"])
        params["tags"] = tags

        try:
            payload = await self._get_json(
                provider["url"],
                params,
            )
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ValueError,
        ):
            return None

        posts = self._posts_from_payload(payload)

        if not posts:
            return None

        random.shuffle(posts)

        for post in posts:
            # Safebooru/Gelbooru can expose rating in responses.
            # Do not use anything explicitly non-safe if the API
            # returns a rating field.
            rating = str(post.get("rating", "")).lower()
            if rating in ("explicit", "questionable"):
                continue

            url = self._image_url(post)
            if not url:
                continue

            extension = self._image_extension(url)

            # Telegram/Telethon should receive a real image filename.
            # We deliberately skip unknown extensions instead of
            # sending them as generic documents.
            if extension is None:
                continue

            return {
                "url": url,
                "source": provider["name"],
                "extension": extension,
                "post": post,
            }

        return None

    async def _fetch_fumo(self) -> dict:
        providers = list(self.PROVIDERS)
        random.shuffle(providers)

        errors = []

        for provider in providers:
            tags = list(provider["tags"])
            random.shuffle(tags)

            for tag_query in tags:
                try:
                    result = await self._query_provider(
                        provider,
                        tag_query,
                    )

                    if result:
                        return result

                    errors.append(
                        f'{provider["name"]}: empty'
                    )

                except Exception as exc:
                    errors.append(
                        f'{provider["name"]}: '
                        f'{type(exc).__name__}'
                    )

        raise RuntimeError(
            "All image APIs failed: "
            + ", ".join(errors)
        )

    # -----------------------------
    # Image download
    # -----------------------------

    async def _download_image(
        self,
        image_url: str,
        extension: str,
    ) -> tuple[bytes, str]:
        """
        Download the image and return (bytes, mime_type).

        The returned filename extension is used later when creating
        the BytesIO object. This is important: Telethon can otherwise
        treat an in-memory file as a generic document.
        """

        session = await self._get_session()

        async with session.get(
            image_url,
            headers={
                "User-Agent": self.USER_AGENT,
                "Accept": "image/avif,image/webp,image/apng,"
                "image/svg+xml,image/*,*/*;q=0.8",
            },
        ) as response:
            response.raise_for_status()

            content_type = response.headers.get(
                "Content-Type",
                "",
            ).split(";", 1)[0].strip().lower()

            content_length = response.headers.get(
                "Content-Length"
            )

            if content_length:
                try:
                    if int(content_length) > self.MAX_IMAGE_SIZE:
                        raise RuntimeError(
                            "Image is larger than 25 MB"
                        )
                except ValueError:
                    pass

            data = bytearray()

            async for chunk in response.content.iter_chunked(
                64 * 1024
            ):
                data.extend(chunk)

                if len(data) > self.MAX_IMAGE_SIZE:
                    raise RuntimeError(
                        "Image is larger than 25 MB"
                    )

            if not data:
                raise RuntimeError(
                    "Image response was empty"
                )

            mime_by_extension = {
                ".jpg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
                ".gif": "image/gif",
            }

            # Prefer the extension from the API URL because some
            # CDN endpoints return a generic Content-Type.
            mime_type = mime_by_extension.get(
                extension,
                content_type,
            )

            if mime_type not in (
                "image/jpeg",
                "image/png",
                "image/webp",
                "image/gif",
            ):
                mime_type = "image/jpeg"

            return bytes(data), mime_type

    # -----------------------------
    # Caption
    # -----------------------------

    def _make_caption(self, source: str) -> str:
        template = self._get("caption", "")

        if not template:
            return ""

        now = datetime.now(timezone.utc)

        replacements = {
            "{source}": source,
            "{date}": now.strftime("%Y-%m-%d"),
            "{time}": now.strftime("%H:%M:%S UTC"),
        }

        result = template

        for key, value in replacements.items():
            result = result.replace(key, value)

        return result

    # -----------------------------
    # Target
    # -----------------------------

    async def _resolve_target(self, message=None):
        target = self._get("target", None)

        if target not in (None, ""):
            return await self.client.get_entity(target)

        if message is not None:
            try:
                return await message.get_chat()
            except Exception:
                pass

        # No current message exists during background operation,
        # so Saved Messages is the safe fallback.
        return await self.client.get_entity("me")

    # -----------------------------
    # Send
    # -----------------------------

    async def _send_fumo(
        self,
        target=None,
        reply_message=None,
    ) -> dict:
        if target is None:
            target = await self._resolve_target(
                reply_message
            )

        fumo = await self._fetch_fumo()

        image, mime_type = await self._download_image(
            fumo["url"],
            fumo["extension"],
        )

        filename = (
            "fumo_"
            + datetime.now().strftime(
                "%Y%m%d_%H%M%S"
            )
            + fumo["extension"]
        )

        # Critical part:
        # give the in-memory file a real image filename.
        file = BytesIO(image)
        file.name = filename

        await self.client.send_file(
            target,
            file,
            caption=self._make_caption(
                fumo["source"]
            ),
            force_document=False,
            mime_type=mime_type,
        )

        self._set(
            "last_source",
            fumo["source"],
        )
        self._set(
            "last_url",
            fumo["url"],
        )
        self._set(
            "last_time",
            datetime.now(timezone.utc).isoformat(),
        )

        return fumo

    # -----------------------------
    # Scheduler
    # -----------------------------

    async def _scheduler_loop(self):
        try:
            await asyncio.sleep(5)

            while not self._stopping:
                if not self._get("enabled", False):
                    await asyncio.sleep(30)
                    continue

                interval = int(
                    self._get(
                        "interval",
                        self.DEFAULT_INTERVAL,
                    )
                )

                await asyncio.sleep(
                    max(self.MIN_INTERVAL, interval)
                )

                if self._stopping:
                    break

                if not self._get("enabled", False):
                    continue

                try:
                    target = await self._resolve_target()
                    await self._send_fumo(target=target)

                except FloodWaitError as exc:
                    # Do not kill the scheduler on a Telegram flood wait.
                    await asyncio.sleep(
                        max(1, int(exc.seconds))
                    )

                except (
                    ChatWriteForbiddenError,
                    ChatAdminRequiredError,
                    PermissionError,
                ):
                    # Target cannot currently receive messages.
                    # Keep the module alive; the next cycle can retry.
                    continue

                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    RuntimeError,
                ):
                    # API/CDN failure. Retry on the next interval.
                    continue

                except asyncio.CancelledError:
                    raise

                except Exception:
                    # A bad target/entity or an unexpected Telethon
                    # error must not permanently kill the scheduler.
                    continue

        except asyncio.CancelledError:
            raise
        except Exception:
            return

    # -----------------------------
    # Commands
    # -----------------------------

    @loader.command(
        ru_doc="Включить/выключить автопостинг: .fumo on/off",
        en_doc="Enable/disable autoposting: .fumo on/off",
    )
    async def fumo(self, message):
        args = utils.get_args_raw(message).strip().lower()

        if args in ("on", "enable", "1", "true"):
            self._set("enabled", True)
            self._start_scheduler()

            await utils.answer(
                message,
                "✅ <b>Fumo autoposting enabled.</b>",
            )
            return

        if args in ("off", "disable", "0", "false"):
            self._set("enabled", False)

            await utils.answer(
                message,
                "❌ <b>Fumo autoposting disabled.</b>",
            )
            return

        enabled = self._get("enabled", False)

        await utils.answer(
            message,
            (
                "<b>Usage:</b>\n"
                "<code>.fumo on</code>\n"
                "<code>.fumo off</code>\n\n"
                "<b>Current:</b> "
                + ("enabled" if enabled else "disabled")
            ),
        )

    @loader.command(
        ru_doc="Установить чат/канал: .fumotarget @username или ID",
        en_doc="Set target chat/channel.",
    )
    async def fumotarget(self, message):
        args = utils.get_args_raw(message).strip()

        if not args:
            self._set("target", None)

            await utils.answer(
                message,
                (
                    "🎯 <b>Target reset.</b>\n"
                    "Automatic posts will use Saved Messages "
                    "when no current chat is available."
                ),
            )
            return

        try:
            entity = await self.client.get_entity(args)
        except Exception as exc:
            await utils.answer(
                message,
                (
                    "❌ <b>Couldn't resolve target.</b>\n"
                    f"<code>{utils.escape_html(str(exc))}</code>"
                ),
            )
            return

        stored_target: Any = args

        if args.lstrip("-").isdigit():
            stored_target = int(args)

        self._set("target", stored_target)

        display = (
            getattr(entity, "title", None)
            or getattr(entity, "username", None)
            or str(args)
        )

        await utils.answer(
            message,
            f"🎯 <b>Target:</b> <code>{utils.escape_html(str(display))}</code>",
        )

    @loader.command(
        ru_doc="Интервал: .fumointerval 30m / 2h / 90",
        en_doc="Set interval: 30m / 2h / 90.",
    )
    async def fumointerval(self, message):
        args = utils.get_args_raw(message).strip().lower()

        if not args:
            await utils.answer(
                message,
                "⚠️ <b>Example:</b> <code>.fumointerval 30m</code>",
            )
            return

        try:
            if args.endswith("h"):
                seconds = int(
                    float(args[:-1]) * 3600
                )
            elif args.endswith("m"):
                seconds = int(
                    float(args[:-1]) * 60
                )
            elif args.endswith("s"):
                seconds = int(
                    float(args[:-1])
                )
            else:
                # Plain number = minutes.
                seconds = int(
                    float(args) * 60
                )
        except (ValueError, TypeError):
            await utils.answer(
                message,
                "⚠️ <b>Invalid interval.</b>",
            )
            return

        if not (
            self.MIN_INTERVAL
            <= seconds
            <= self.MAX_INTERVAL
        ):
            await utils.answer(
                message,
                (
                    "⚠️ <b>Interval must be between "
                    "1 minute and 7 days.</b>"
                ),
            )
            return

        self._set("interval", seconds)

        await utils.answer(
            message,
            (
                "⏱ <b>Interval:</b> "
                f"<code>{self._format_interval(seconds)}</code>"
            ),
        )

    @loader.command(
        ru_doc="Установить подпись. Пустая команда очищает её.",
        en_doc="Set caption. Empty command clears it.",
    )
    async def fumocaption(self, message):
        caption = utils.get_args_raw(message)

        self._set("caption", caption)

        if caption:
            await utils.answer(
                message,
                "📝 <b>Caption updated.</b>",
            )
        else:
            await utils.answer(
                message,
                "📝 <b>Caption cleared.</b>",
            )

    @loader.command(
        ru_doc="Скачать и отправить один Fumo в текущий чат.",
        en_doc="Fetch and send one Fumo to the current chat.",
    )
    async def fumotest(self, message):
        status = await utils.answer(
            message,
            "🔎 <b>Fetching Fumo...</b>",
        )

        try:
            fumo = await self._send_fumo(
                target=await message.get_chat()
            )

        except FloodWaitError as exc:
            await utils.answer(
                status,
                (
                    "🐌 <b>Telegram flood wait:</b> "
                    f"<code>{exc.seconds}s</code>"
                ),
            )
            return

        except (
            ChatWriteForbiddenError,
            ChatAdminRequiredError,
            PermissionError,
        ):
            await utils.answer(
                status,
                "🚫 <b>No permission to send media here.</b>",
            )
            return

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            RuntimeError,
        ) as exc:
            await utils.answer(
                status,
                (
                    "❌ <b>Fumo fetch failed.</b>\n"
                    f"<code>{utils.escape_html(str(exc))}</code>"
                ),
            )
            return

        except Exception as exc:
            await utils.answer(
                status,
                (
                    "❌ <b>Send failed.</b>\n"
                    f"<code>{utils.escape_html(str(exc))}</code>"
                ),
            )
            return

        await utils.answer(
            status,
            (
                "✅ <b>Fumo sent.</b>\n"
                f"<i>Source:</i> {fumo['source']}"
            ),
        )

    @loader.command(
        ru_doc="Показать состояние автопостера.",
        en_doc="Show autoposter status.",
    )
    async def fumostatus(self, message):
        enabled = self._get("enabled", False)
        target = self._get("target", None)
        interval = int(
            self._get(
                "interval",
                self.DEFAULT_INTERVAL,
            )
        )
        caption = self._get("caption", "")
        last_source = self._get(
            "last_source",
            "",
        )
        last_time = self._get(
            "last_time",
            "",
        )

        if target in (None, ""):
            target_display = "Saved Messages / fallback"
        else:
            target_display = str(target)

        caption_display = caption or "<empty>"

        await utils.answer(
            message,
            (
                "🌸 <b>Fumo AutoPoster</b>\n\n"
                "<b>Status:</b> "
                + ("🟢 enabled" if enabled else "🔴 disabled")
                + "\n"
                f"<b>Target:</b> <code>"
                f"{utils.escape_html(target_display)}</code>\n"
                f"<b>Interval:</b> <code>"
                f"{self._format_interval(interval)}</code>\n"
                f"<b>Caption:</b> <code>"
                f"{utils.escape_html(caption_display)}</code>\n"
                f"<b>Last source:</b> <code>"
                f"{utils.escape_html(last_source or '<none>')}</code>\n"
                f"<b>Last time:</b> <code>"
                f"{utils.escape_html(last_time or '<none>')}</code>"
            ),
        )

    # -----------------------------
    # Utilities
    # -----------------------------

    @staticmethod
    def _format_interval(seconds: int) -> str:
        seconds = int(seconds)

        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)

        parts = []

        if days:
            parts.append(f"{days}d")

        if hours:
            parts.append(f"{hours}h")

        if minutes:
            parts.append(f"{minutes}m")

        if seconds and not parts:
            parts.append(f"{seconds}s")

        return " ".join(parts) or "0s"
