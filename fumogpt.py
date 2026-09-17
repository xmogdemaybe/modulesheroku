# -*- coding: utf-8 -*-
#
# Fumo AutoPoster for Hikka / Heroku Userbot
#
# Commands:
#   .fumo on/off
#   .fumotarget <chat_id/@username>
#   .fumointerval <minutes|hours>
#   .fumocaption <text>
#   .fumotest
#   .fumostatus
#
# Examples:
#   .fumo on
#   .fumotarget @my_channel
#   .fumointerval 30m
#   .fumointerval 2h
#   .fumocaption Random Fumo! {source} {date}
#   .fumocaption
#   .fumotest
#
# Empty .fumocaption clears the caption.
#

import asyncio
import random
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Optional

import aiohttp

from .. import loader, utils


@loader.tds
class Fumo(loader.Module):
    """Automatically posts random Touhou Fumo images."""

    strings = {
        "name": "Fumo",

        "enabled": (
            "<emoji document_id=5210952533914471191>✅</emoji> "
            "<b>Fumo autoposting enabled.</b>"
        ),

        "disabled": (
            "<emoji document_id=5210952533914471191>❌</emoji> "
            "<b>Fumo autoposting disabled.</b>"
        ),

        "interval_set": (
            "<emoji document_id=5210952533914471191>⏱</emoji> "
            "<b>Interval:</b> <code>{}</code>"
        ),

        "target_set": (
            "<emoji document_id=5210952533914471191>🎯</emoji> "
            "<b>Target:</b> <code>{}</code>"
        ),

        "caption_set": (
            "<emoji document_id=5210952533914471191>📝</emoji> "
            "<b>Caption template updated.</b>"
        ),

        "caption_cleared": (
            "<emoji document_id=5210952533914471191>📝</emoji> "
            "<b>Caption cleared.</b>"
        ),

        "invalid_interval": (
            "<emoji document_id=5210952533914471191>⚠️</emoji> "
            "<b>Invalid interval.</b>\n\n"
            "Examples: <code>30m</code>, <code>2h</code>, "
            "<code>90</code>"
        ),

        "fetching": (
            "<emoji document_id=5210952533914471191>🔎</emoji> "
            "<b>Looking for a Fumo...</b>"
        ),

        "fetch_failed": (
            "<emoji document_id=5210952533914471191>❌</emoji> "
            "<b>Couldn't find a suitable Fumo image.</b>\n"
            "<code>{}</code>"
        ),

        "send_failed": (
            "<emoji document_id=5210952533914471191>❌</emoji> "
            "<b>Couldn't send the Fumo.</b>\n"
            "<code>{}</code>"
        ),

        "test_done": (
            "<emoji document_id=5210952533914471191>✅</emoji> "
            "<b>Fumo sent.</b>\n"
            "<i>Source:</i> <code>{}</code>"
        ),

        "status": (
            "<emoji document_id=5210952533914471191>🌸</emoji> "
            "<b>Fumo AutoPoster</b>\n\n"
            "<b>Status:</b> {}\n"
            "<b>Target:</b> <code>{}</code>\n"
            "<b>Interval:</b> <code>{}</code>\n"
            "<b>Caption:</b> <code>{}</code>\n"
            "<b>Last source:</b> <code>{}</code>"
        ),

        "permission_error": (
            "<emoji document_id=5210952533914471191>🚫</emoji> "
            "<b>No permission to send media to the target chat.</b>"
        ),

        "flood_wait": (
            "<emoji document_id=5210952533914471191>🐌</emoji> "
            "<b>Telegram requested a flood wait of {} seconds.</b>"
        ),
    }

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    DEFAULT_INTERVAL = 60 * 60  # 1 hour
    MIN_INTERVAL = 60           # 1 minute
    MAX_INTERVAL = 7 * 24 * 60 * 60

    REQUEST_TIMEOUT = 20
    MAX_IMAGE_SIZE = 25 * 1024 * 1024

    USER_AGENT = (
        "Mozilla/5.0 (compatible; Hikka-Fumo-Module/1.0)"
    )

    # APIs are intentionally independent. If one dies, the next is tried.
    API_ENDPOINTS = (
        (
            "Safebooru",
            "https://safebooru.org/index.php",
            {
                "page": "dapi",
                "s": "post",
                "q": "index",
                "json": "1",
                "limit": "100",
                "tags": "fumo",
            },
        ),
        (
            "Danbooru",
            "https://danbooru.donmai.us/posts.json",
            {
                "limit": "100",
                "tags": "fumo rating:safe",
            },
        ),
        (
            "Gelbooru",
            "https://gelbooru.com/index.php",
            {
                "page": "dapi",
                "s": "post",
                "q": "index",
                "json": "1",
                "limit": "100",
                "tags": "fumo rating:safe",
            },
        ),
    )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def client_ready(self, client, db):
        self.client = client
        self.db = db

        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._stopping = False

        # Initialize defaults in the native Hikka DB.
        if self.db.get(self.strings["name"], "enabled", None) is None:
            self.db.set(self.strings["name"], "enabled", False)

        if self.db.get(self.strings["name"], "interval", None) is None:
            self.db.set(
                self.strings["name"],
                "interval",
                self.DEFAULT_INTERVAL,
            )

        if self.db.get(self.strings["name"], "target", None) is None:
            self.db.set(
                self.strings["name"],
                "target",
                None,
            )

        if self.db.get(self.strings["name"], "caption", None) is None:
            self.db.set(
                self.strings["name"],
                "caption",
                "",
            )

        if self.db.get(self.strings["name"], "last_source", None) is None:
            self.db.set(
                self.strings["name"],
                "last_source",
                "",
            )

        # Start scheduler immediately.
        self._stopping = False
        self._start_scheduler()

    async def on_unload(self):
        """
        Explicitly stop the scheduler and HTTP session.

        This is important because otherwise a module reload could leave
        an orphaned asyncio task running in the background.
        """
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
        """Start exactly one scheduler task."""
        if self._task is not None and not self._task.done():
            return

        self._task = asyncio.create_task(
            self._scheduler_loop()
        )

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if (
            self._session is None
            or self._session.closed
        ):
            timeout = aiohttp.ClientTimeout(
                total=self.REQUEST_TIMEOUT
            )

            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": self.USER_AGENT,
                    "Accept": "application/json",
                },
            )

        return self._session

    async def _request_json(
        self,
        url: str,
        params: dict,
    ) -> Any:
        session = await self._get_session()

        async with session.get(
            url,
            params=params,
        ) as response:
            response.raise_for_status()
            return await response.json(
                content_type=None
            )

    # ------------------------------------------------------------------
    # Image fetching
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_url(post: dict) -> Optional[str]:
        """
        Extract an image URL from different booru response formats.
        """

        # Most common.
        for key in (
            "file_url",
            "large_file_url",
            "original",
            "source",
        ):
            value = post.get(key)

            if (
                isinstance(value, str)
                and value.startswith(("http://", "https://"))
            ):
                # "source" can be the artist's webpage rather than
                # the actual image, so don't blindly use it unless
                # it looks like an image.
                if key == "source":
                    lowered = value.lower()

                    if not lowered.endswith(
                        (
                            ".jpg",
                            ".jpeg",
                            ".png",
                            ".gif",
                            ".webp",
                        )
                    ):
                        continue

                return value

        return None

    @staticmethod
    def _normalise_posts(payload: Any) -> list[dict]:
        """
        Convert different booru API response structures into
        a list of dictionaries.
        """

        if isinstance(payload, list):
            return [
                item
                for item in payload
                if isinstance(item, dict)
            ]

        if isinstance(payload, dict):
            # Some APIs return {"posts": [...]}
            posts = payload.get("posts")

            if isinstance(posts, list):
                return [
                    item
                    for item in posts
                    if isinstance(item, dict)
                ]

            # Or a single post.
            if "file_url" in payload:
                return [payload]

        return []

    async def _fetch_from_api(
        self,
        name: str,
        url: str,
        params: dict,
    ) -> Optional[dict]:
        """
        Query one API and return a random usable post.
        """

        try:
            payload = await self._request_json(
                url,
                params,
            )

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ValueError,
        ):
            return None

        posts = self._normalise_posts(payload)

        if not posts:
            return None

        # Randomise locally instead of relying on every API having
        # a random=true parameter.
        random.shuffle(posts)

        for post in posts:
            image_url = self._extract_url(post)

            if not image_url:
                continue

            # Skip obviously non-image links.
            lowered = image_url.lower()

            if not lowered.split("?")[0].endswith(
                (
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".gif",
                    ".webp",
                )
            ):
                continue

            return {
                "url": image_url,
                "source": name,
                "post": post,
            }

        return None

    async def _fetch_fumo(self) -> dict:
        """
        Try every configured booru in order.

        Raises RuntimeError when all providers fail.
        """

        # Randomise provider order too, so the same provider isn't
        # always hammered first.
        providers = list(self.API_ENDPOINTS)
        random.shuffle(providers)

        errors = []

        for name, url, params in providers:
            try:
                result = await self._fetch_from_api(
                    name,
                    url,
                    params,
                )

                if result:
                    return result

                errors.append(f"{name}: empty")

            except Exception as exc:
                errors.append(
                    f"{name}: {type(exc).__name__}"
                )

        raise RuntimeError(
            "; ".join(errors) or "all APIs failed"
        )

    async def _download_image(
        self,
        image_url: str,
    ) -> bytes:
        """
        Download an image asynchronously.

        A size limit prevents an accidentally huge file from
        being loaded into memory.
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

            content_length = response.headers.get(
                "Content-Length"
            )

            if content_length:
                try:
                    if int(content_length) > self.MAX_IMAGE_SIZE:
                        raise RuntimeError(
                            "image is too large"
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
                        "image is too large"
                    )

            if not data:
                raise RuntimeError(
                    "empty image response"
                )

            return bytes(data)

    # ------------------------------------------------------------------
    # Caption handling
    # ------------------------------------------------------------------

    def _make_caption(
        self,
        source: str,
    ) -> str:
        template = self._get(
            "caption",
            "",
        )

        if not template:
            return ""

        now = datetime.now(
            timezone.utc
        )

        replacements = {
            "{source}": source,
            "{date}": now.strftime(
                "%Y-%m-%d"
            ),
            "{time}": now.strftime(
                "%H:%M:%S UTC"
            ),
        }

        result = template

        for key, value in replacements.items():
            result = result.replace(
                key,
                value,
            )

        return result

    # ------------------------------------------------------------------
    # Target handling
    # ------------------------------------------------------------------

    async def _resolve_target(
        self,
        message=None,
    ):
        """
        Resolve configured target.

        If no target is configured:
          1. use current chat when a message is available;
          2. otherwise use Saved Messages ("me").
        """

        target = self._get(
            "target",
            None,
        )

        if target is None or target == "":
            if message is not None:
                try:
                    return await message.get_chat()
                except Exception:
                    pass

            return await self.client.get_entity("me")

        return await self.client.get_entity(
            target
        )

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def _send_fumo(
        self,
        target=None,
        reply_message=None,
    ) -> dict:
        """
        Fetch, download and send one Fumo.
        """

        if target is None:
            target = await self._resolve_target(
                reply_message
            )

        fumo = await self._fetch_fumo()

        image = await self._download_image(
            fumo["url"]
        )

        caption = self._make_caption(
            fumo["source"]
        )

        filename = (
            "fumo_"
            + datetime.now().strftime(
                "%Y%m%d_%H%M%S"
            )
            + ".jpg"
        )

        await self.client.send_file(
            target,
            BytesIO(image),
            caption=caption,
            file_name=filename,
            force_document=False,
        )

        self._set(
            "last_source",
            fumo["source"],
        )

        self._set(
            "last_post_url",
            fumo["url"],
        )

        self._set(
            "last_post_time",
            datetime.now(
                timezone.utc
            ).isoformat(),
        )

        return fumo

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    async def _scheduler_loop(self):
        """
        Main background loop.

        The loop sleeps for the configured interval and then
        posts one Fumo.
        """

        # Small initial delay prevents an immediate post directly
        # after module loading.
        try:
            await asyncio.sleep(5)

            while not self._stopping:
                enabled = bool(
                    self._get(
                        "enabled",
                        False,
                    )
                )

                interval = int(
                    self._get(
                        "interval",
                        self.DEFAULT_INTERVAL,
                    )
                )

                if not enabled:
                    # Don't spin at 100% CPU while disabled.
                    await asyncio.sleep(30)
                    continue

                # Wait before each automatic post.
                await asyncio.sleep(
                    max(
                        self.MIN_INTERVAL,
                        interval,
                    )
                )

                if self._stopping:
                    break

                # Re-check state after sleeping.
                if not self._get(
                    "enabled",
                    False,
                ):
                    continue

                try:
                    target = await self._resolve_target()

                    await self._send_fumo(
                        target=target
                    )

                except asyncio.CancelledError:
                    raise

                except Exception:
                    # Automatic operation must not kill the scheduler.
                    #
                    # Errors are intentionally swallowed here because
                    # there may be nowhere sensible to send an error
                    # message if the target itself is unavailable.
                    continue

        except asyncio.CancelledError:
            raise

        except Exception:
            # Last-resort protection: the module itself must remain
            # unloadable even if the scheduler encounters something
            # unexpected.
            return

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @loader.command(
        ru_doc="Включить/выключить автоматическую отправку Fumo.",
        en_doc="Enable/disable automatic Fumo posting.",
    )
    async def fumo(self, message):
        """Enable/disable automatic Fumo posting."""

        args = utils.get_args_raw(message).strip().lower()

        if args in (
            "on",
            "enable",
            "1",
            "true",
        ):
            self._set(
                "enabled",
                True,
            )

            self._start_scheduler()

            await utils.answer(
                message,
                self.strings["enabled"],
            )
            return

        if args in (
            "off",
            "disable",
            "0",
            "false",
        ):
            self._set(
                "enabled",
                False,
            )

            await utils.answer(
                message,
                self.strings["disabled"],
            )
            return

        status = (
            "enabled"
            if self._get(
                "enabled",
                False,
            )
            else "disabled"
        )

        await utils.answer(
            message,
            (
                "<b>Usage:</b>\n"
                "<code>.fumo on</code>\n"
                "<code>.fumo off</code>\n\n"
                f"<b>Current:</b> {status}"
            ),
        )

    @loader.command(
        ru_doc="Установить чат/канал назначения.",
        en_doc="Set the destination chat/channel.",
    )
    async def fumotarget(self, message):
        """Set target chat/channel."""

        args = utils.get_args_raw(message).strip()

        if not args:
            self._set(
                "target",
                None,
            )

            await utils.answer(
                message,
                (
                    "<emoji document_id=5210952533914471191>"
                    "🎯</emoji> "
                    "<b>Target reset.</b>\n\n"
                    "Automatic posts will use the current "
                    "chat when possible, otherwise Saved Messages."
                ),
            )
            return

        # Try to resolve now, so a typo doesn't get persisted.
        try:
            entity = await self.client.get_entity(
                args
            )

        except Exception as exc:
            await utils.answer(
                message,
                (
                    "<emoji document_id=5210952533914471191>"
                    "❌</emoji> "
                    "<b>Couldn't resolve target.</b>\n"
                    f"<code>{utils.escape_html(str(exc))}</code>"
                ),
            )
            return

        # Store the user's original ID/username.
        #
        # Numeric IDs are stored as integers where possible.
        stored_target: Any = args

        try:
            if args.lstrip("-").isdigit():
                stored_target = int(args)
        except Exception:
            pass

        self._set(
            "target",
            stored_target,
        )

        display = getattr(
            entity,
            "title",
            None,
        ) or getattr(
            entity,
            "username",
            None,
        ) or str(args)

        await utils.answer(
            message,
            self.strings["target_set"].format(
                display
            ),
        )

    @loader.command(
        ru_doc="Установить интервал: 30m, 2h или просто минуты.",
        en_doc="Set interval: 30m, 2h or plain minutes.",
    )
    async def fumointerval(self, message):
        """Set automatic posting interval."""

        args = utils.get_args_raw(message).strip().lower()

        if not args:
            await utils.answer(
                message,
                self.strings["invalid_interval"],
            )
            return

        try:
            if args.endswith("h"):
                hours = float(
                    args[:-1]
                )

                seconds = int(
                    hours * 60 * 60
                )

            elif args.endswith("m"):
                minutes = float(
                    args[:-1]
                )

                seconds = int(
                    minutes * 60
                )

            elif args.endswith("s"):
                seconds = int(
                    float(args[:-1])
                )

            else:
                # Bare number = minutes.
                seconds = int(
                    float(args) * 60
                )

        except (
            ValueError,
            TypeError,
        ):
            await utils.answer(
                message,
                self.strings["invalid_interval"],
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
                    "<emoji document_id=5210952533914471191>"
                    "⚠️</emoji> "
                    "<b>Interval must be between "
                    "1 minute and 7 days.</b>"
                ),
            )
            return

        self._set(
            "interval",
            seconds,
        )

        await utils.answer(
            message,
            self.strings["interval_set"].format(
                self._format_interval(seconds)
            ),
        )

    @loader.command(
        ru_doc="Установить подпись. Без аргументов — очистить.",
        en_doc="Set caption. No arguments clears it.",
    )
    async def fumocaption(self, message):
        """Set custom caption."""

        caption = utils.get_args_raw(
            message
        )

        if caption == "":
            self._set(
                "caption",
                "",
            )

            await utils.answer(
                message,
                self.strings["caption_cleared"],
            )
            return

        self._set(
            "caption",
            caption,
        )

        await utils.answer(
            message,
            self.strings["caption_set"],
        )

    @loader.command(
        ru_doc="Отправить один случайный Fumo прямо сейчас.",
        en_doc="Send one random Fumo immediately.",
    )
    async def fumotest(self, message):
        """Fetch and send one Fumo immediately."""

        status_message = await utils.answer(
            message,
            self.strings["fetching"],
        )

        try:
            # Manual trigger always sends to the current chat.
            fumo = await self._send_fumo(
                target=await message.get_chat(),
                reply_message=message,
            )

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            RuntimeError,
        ) as exc:
            await utils.answer(
                status_message,
                self.strings["fetch_failed"].format(
                    utils.escape_html(
                        str(exc)
                    )
                ),
            )
            return

        except Exception as exc:
            # FloodWaitError and other Telethon exceptions are
            # handled explicitly below where available, while
            # keeping this module compatible with different forks.
            error_name = type(exc).__name__

            if error_name == "FloodWaitError":
                seconds = getattr(
                    exc,
                    "seconds",
                    0,
                )

                await utils.answer(
                    status_message,
                    self.strings["flood_wait"].format(
                        seconds
                    ),
                )
                return

            if (
                "ChatWriteForbidden" in error_name
                or "ChatAdminRequired" in error_name
                or "Forbidden" in error_name
            ):
                await utils.answer(
                    status_message,
                    self.strings["permission_error"],
                )
                return

            await utils.answer(
                status_message,
                self.strings["send_failed"].format(
                    utils.escape_html(
                        f"{error_name}: {exc}"
                    )
                ),
            )
            return

        await utils.answer(
            status_message,
            self.strings["test_done"].format(
                fumo["source"]
            ),
        )

    @loader.command(
        ru_doc="Показать состояние Fumo AutoPoster.",
        en_doc="Show Fumo AutoPoster status.",
    )
    async def fumostatus(self, message):
        """Show module status."""

        enabled = self._get(
            "enabled",
            False,
        )

        target = self._get(
            "target",
            None,
        )

        if target is None:
            target_display = (
                "current chat / Saved Messages"
            )
        else:
            target_display = str(target)

        interval = int(
            self._get(
                "interval",
                self.DEFAULT_INTERVAL,
            )
        )

        caption = self._get(
            "caption",
            "",
        )

        if not caption:
            caption = "<empty>"

        last_source = self._get(
            "last_source",
            "",
        ) or "<none>"

        await utils.answer(
            message,
            self.strings["status"].format(
                "🟢 enabled"
                if enabled
                else "🔴 disabled",
                target_display,
                self._format_interval(interval),
                caption,
                last_source,
            ),
        )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _format_interval(seconds: int) -> str:
        seconds = int(seconds)

        days, seconds = divmod(
            seconds,
            86400,
        )

        hours, seconds = divmod(
            seconds,
            3600,
        )

        minutes, seconds = divmod(
            seconds,
            60,
        )

        parts = []

        if days:
            parts.append(
                f"{days}d"
            )

        if hours:
            parts.append(
                f"{hours}h"
            )

        if minutes:
            parts.append(
                f"{minutes}m"
            )

        if seconds and not parts:
            parts.append(
                f"{seconds}s"
            )

        return " ".join(parts) or "0s"
