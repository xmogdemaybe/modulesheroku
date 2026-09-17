# meta developer: @your_username
# meta banner: https://i.imgur.com/8Qp4w0E.jpeg
# requires: aiohttp

import asyncio
import io
import logging
import random
import re
import time
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, List

import aiohttp
from telethon import errors
from telethon.tl.types import Message

from .. import loader, utils

logger = logging.getLogger(__name__)


class SafeDict(dict):
    """Prevents KeyError when formatting caption templates with missing placeholders."""
    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


@loader.tds
class FumoPosterMod(loader.Module):
    """Automatically fetches Touhou Fumo plushie photos from boorus and posts them on a schedule."""

    strings = {
        "name": "FumoPoster",
        "fetching": "ᗜˬᗜ <i>Fetching random Fumo...</i>",
        "fetch_error": "❌ <b>Error:</b> <i>Failed to retrieve Fumo image from all available booru providers.</i>",
        "send_error": "❌ <b>Error sending media:</b> <code>{error}</code>",
        "toggled_on": "🟢 <b>Fumo Auto-Poster enabled.</b>\n⏱ <b>Interval:</b> <code>{interval}</code>\n🎯 <b>Target:</b> <code>{target}</code>",
        "toggled_off": "🔴 <b>Fumo Auto-Poster disabled.</b>",
        "target_set": "🎯 <b>Target chat updated:</b> <b>{title}</b> (<code>{chat_id}</code>)",
        "invalid_chat": "❌ <b>Could not resolve target chat:</b> <code>{error}</code>",
        "interval_set": "⏱ <b>Interval updated:</b> <code>{readable}</code> ({seconds}s)",
        "interval_invalid": "❌ <b>Invalid format!</b> Use values like <code>30m</code>, <code>2h</code>, <code>1d</code>, or integer minutes.",
        "caption_set": "📝 <b>Caption template updated:</b>\n<blockquote>{caption}</blockquote>",
        "caption_cleared": "📝 <b>Caption cleared. Images will now be sent without caption.</b>",
        "status": (
            "ᗜˬᗜ <b><u>Fumo Auto-Poster Status</u></b>\n\n"
            "• <b>Status:</b> {status_icon} <b>{status_text}</b>\n"
            "• <b>Target Chat:</b> <code>{target}</code>\n"
            "• <b>Interval:</b> <code>{interval}</code>\n"
            "• <b>Next Post:</b> <code>{next_post}</code>\n"
            "• <b>Cached Post IDs:</b> <code>{cached_count}</code>\n\n"
            "<b>Caption Template:</b>\n<blockquote>{caption}</blockquote>"
        ),
    }

    def __init__(self):
        self.config = loader.ModuleConfig()
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self.client = None
        self.db = None

    async def client_ready(self, client, db):
        self.client = client
        self.db = db

        # Browser headers to avoid 403 Forbidden on CDNs & Boorus
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,image/*,*/*;q=0.8",
        }
        self._session = aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30)
        )

        # Cancel any previous task instances and launch background scheduler
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._poster_loop())

    async def on_unload(self):
        """Lifecycle hook: gracefully shuts down background task and HTTP session."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self._session and not self._session.closed:
            await self._session.close()

    # -------------------------------------------------------------------------
    # Booru Scraping & Image Fetching Core
    # -------------------------------------------------------------------------

    async def _fetch_safebooru(self) -> List[Dict[str, Any]]:
        """Fetch post entries from Safebooru."""
        page = random.randint(0, 8)
        url = f"https://safebooru.org/index.php?page=dapi&s=post&q=index&json=1&tags=fumo&limit=40&pid={page}"
        async with self._session.get(url) as resp:
            if resp.status != 200:
                return []
            data = await resp.json(content_type=None)
            posts = []
            for item in data:
                img_file = item.get("image", "")
                dir_path = str(item.get("directory", "")).strip("/")
                if not img_file or not dir_path:
                    continue
                
                # Check directory format variance
                if dir_path.startswith("images"):
                    img_url = f"https://safebooru.org/{dir_path}/{img_file}"
                else:
                    img_url = f"https://safebooru.org/images/{dir_path}/{img_file}"

                posts.append({
                    "id": f"safebooru_{item.get('id')}",
                    "file_url": img_url,
                    "post_url": f"https://safebooru.org/index.php?page=post&s=view&id={item.get('id')}",
                    "tags": item.get("tags", ""),
                })
            return posts

    async def _fetch_danbooru(self) -> List[Dict[str, Any]]:
        """Fetch post entries from Danbooru."""
        url = "https://danbooru.donmai.us/posts.json?tags=fumo+order:random&limit=30"
        async with self._session.get(url) as resp:
            if resp.status != 200:
                return []
            data = await resp.json(content_type=None)
            posts = []
            for item in data:
                file_url = item.get("file_url") or item.get("large_file_url")
                if not file_url:
                    continue
                posts.append({
                    "id": f"danbooru_{item.get('id')}",
                    "file_url": file_url,
                    "post_url": f"https://danbooru.donmai.us/posts/{item.get('id')}",
                    "tags": item.get("tag_string", ""),
                })
            return posts

    async def _fetch_gelbooru(self) -> List[Dict[str, Any]]:
        """Fetch post entries from Gelbooru."""
        url = "https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&tags=fumo+sort:random&limit=30"
        async with self._session.get(url) as resp:
            if resp.status != 200:
                return []
            data = await resp.json(content_type=None)
            items = data.get("post", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            posts = []
            for item in items:
                file_url = item.get("file_url")
                if not file_url:
                    continue
                posts.append({
                    "id": f"gelbooru_{item.get('id')}",
                    "file_url": file_url,
                    "post_url": f"https://gelbooru.com/index.php?page=post&s=view&id={item.get('id')}",
                    "tags": item.get("tags", ""),
                })
            return posts

    async def _fetch_yandere(self) -> List[Dict[str, Any]]:
        """Fetch post entries from Yande.re."""
        url = "https://yande.re/post.json?tags=fumo&limit=35"
        async with self._session.get(url) as resp:
            if resp.status != 200:
                return []
            data = await resp.json(content_type=None)
            posts = []
            for item in data:
                file_url = item.get("file_url") or item.get("jpeg_url") or item.get("sample_url")
                if not file_url:
                    continue
                posts.append({
                    "id": f"yandere_{item.get('id')}",
                    "file_url": file_url,
                    "post_url": f"https://yande.re/post/show/{item.get('id')}",
                    "tags": item.get("tags", ""),
                })
            return posts

    async def _download_image(self, file_url: str) -> Optional[io.BytesIO]:
        """Download raw bytes of an image and wrap them in a named io.BytesIO."""
        try:
            headers = {"Referer": file_url}
            async with self._session.get(file_url, headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                if len(data) < 512:
                    return None

                buf = io.BytesIO(data)
                # Resolve extension
                ext = "jpg"
                if "." in file_url.split("?")[0]:
                    ext = file_url.split("?")[0].split(".")[-1].lower()
                    if ext not in ["jpg", "jpeg", "png", "webp", "gif"]:
                        ext = "jpg"

                buf.name = f"fumo_{int(time.time())}.{ext}"
                buf.seek(0)
                return buf
        except Exception as e:
            logger.debug("Failed to download image %s: %s", file_url, e)
            return None

    async def _get_random_fumo(self) -> Optional[Tuple[io.BytesIO, str, str, str]]:
        """
        Queries booru providers with fallback mechanism.
        Returns: (BytesIO_buffer, post_url, tags, post_id) or None.
        """
        providers = [
            self._fetch_safebooru,
            self._fetch_danbooru,
            self._fetch_gelbooru,
            self._fetch_yandere,
        ]
        random.shuffle(providers)

        seen_ids: List[str] = self.db.get(self.strings["name"], "seen_ids", [])

        for provider in providers:
            try:
                posts = await provider()
                if not posts:
                    continue

                # Filter out previously posted images to avoid duplicates
                candidates = [p for p in posts if p["id"] not in seen_ids]
                if not candidates:
                    candidates = posts  # Fallback to full list if all cached

                random.shuffle(candidates)

                for post in candidates:
                    buf = await self._download_image(post["file_url"])
                    if buf:
                        return buf, post["post_url"], post["tags"], post["id"]
            except Exception as err:
                logger.debug("Provider %s failed: %s", provider.__name__, err)
                continue

        return None

    # -------------------------------------------------------------------------
    # Background Scheduler Loop
    # -------------------------------------------------------------------------

    async def _poster_loop(self):
        """Asynchronous scheduler running in the background."""
        while True:
            try:
                enabled = self.db.get(self.strings["name"], "enabled", False)
                interval = self.db.get(self.strings["name"], "interval", 3600)
                last_post = self.db.get(self.strings["name"], "last_post_time", 0.0)
                now = time.time()

                if enabled and (now - last_post >= interval):
                    await self._execute_autopost()
                    self.db.set(self.strings["name"], "last_post_time", time.time())

                # Responsive tick-rate allows instantaneous updates when toggled or reconfigured
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Unexpected error in Fumo background loop: %s", e)
                await asyncio.sleep(30)

    async def _execute_autopost(self):
        """Dispatches an automated post to the configured target chat."""
        target_chat = self.db.get(self.strings["name"], "target_chat", "me")
        result = await self._get_random_fumo()

        if not result:
            logger.warning("Fumo auto-post skipped: All providers failed to return an image.")
            return

        buf, post_url, tags, post_id = result
        caption = self._build_caption(post_url, tags)

        try:
            await self.client.send_file(
                target_chat,
                file=buf,
                caption=caption,
                parse_mode="html",
            )
            # Update cache of sent IDs
            seen_ids = self.db.get(self.strings["name"], "seen_ids", [])
            seen_ids.append(post_id)
            self.db.set(self.strings["name"], "seen_ids", seen_ids[-100:])
        except errors.FloodWaitError as flood:
            logger.warning("Fumo auto-poster hit FloodWait: sleeping for %d seconds", flood.seconds)
            await asyncio.sleep(flood.seconds)
        except (errors.ChatWriteForbiddenError, errors.ChannelPrivateError) as perm_err:
            logger.error("Auto-post permissions error in target chat: %s. Disabling.", perm_err)
            self.db.set(self.strings["name"], "enabled", False)
        except Exception as exc:
            logger.exception("Failed to dispatch Fumo auto-post: %s", exc)

    def _build_caption(self, source_url: str, tags: str) -> str:
        """Applies configured caption template with formatting parameters."""
        default_caption = 'ᗜˬᗜ <b>Fumo Fumo!</b>\n<a href="{source}">Image Source</a>'
        template = self.db.get(self.strings["name"], "caption", default_caption)
        if not template.strip():
            return ""

        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        formatted = template.format_map(
            SafeDict(
                source=source_url,
                date=now_str,
                tags=tags[:120] + ("..." if len(tags) > 120 else ""),
            )
        )
        return formatted

    # -------------------------------------------------------------------------
    # Utility Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _parse_time(time_str: str) -> Optional[int]:
        """Parses human intervals like '30m', '2h', '1d', or numeric minutes."""
        time_str = time_str.strip().lower()
        if time_str.isdigit():
            return int(time_str) * 60

        match = re.match(r"^(\d+)\s*([smhd])$", time_str)
        if not match:
            return None

        val, unit = match.groups()
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        return int(val) * multipliers[unit]

    @staticmethod
    def _format_time(seconds: int) -> str:
        """Converts seconds into a clean human-readable duration."""
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            rem = seconds % 60
            return f"{seconds // 60}m" + (f" {rem}s" if rem else "")
        hours = seconds // 3600
        rem_min = (seconds % 3600) // 60
        return f"{hours}h" + (f" {rem_min}m" if rem_min else "")

    # -------------------------------------------------------------------------
    # Commands
    # -------------------------------------------------------------------------

    async def fumocmd(self, message: Message):
        """Fetch and send a random Fumo photo to the current chat immediately."""
        status_msg = await utils.answer(message, self.strings["fetching"])

        result = await self._get_random_fumo()
        if not result:
            await utils.answer(status_msg, self.strings["fetch_error"])
            return

        buf, post_url, tags, post_id = result
        caption = self._build_caption(post_url, tags)

        try:
            await self.client.send_file(
                message.chat_id,
                file=buf,
                caption=caption,
                parse_mode="html",
                reply_to=message.reply_to_msg_id,
            )
            await status_msg.delete()

            # Track in deduplication history
            seen = self.db.get(self.strings["name"], "seen_ids", [])
            seen.append(post_id)
            self.db.set(self.strings["name"], "seen_ids", seen[-100:])
        except Exception as e:
            await utils.answer(status_msg, self.strings["send_error"].format(error=str(e)))

    async def fumotogglecmd(self, message: Message):
        """Toggle automated Fumo posting on/off."""
        is_enabled = not self.db.get(self.strings["name"], "enabled", False)
        self.db.set(self.strings["name"], "enabled", is_enabled)

        if is_enabled:
            interval = self.db.get(self.strings["name"], "interval", 3600)
            target = self.db.get(self.strings["name"], "target_chat", "me")
            text = self.strings["toggled_on"].format(
                interval=self._format_time(interval),
                target=target,
            )
        else:
            text = self.strings["toggled_off"]

        await utils.answer(message, text)

    async def fumochatcmd(self, message: Message):
        """Configure auto-post destination chat: .fumochat [chat_id|@username|me] (defaults to current chat)"""
        args = utils.get_args_raw(message).strip()

        try:
            if not args:
                target_raw = message.chat_id
            elif args.lower() in ["me", "saved"]:
                target_raw = "me"
            elif args.startswith("-100") or args.isdigit() or (args.startswith("-") and args[1:].isdigit()):
                target_raw = int(args)
            else:
                target_raw = args

            entity = await self.client.get_entity(target_raw)
            resolved_id = utils.get_chat_id(entity)
            title = getattr(entity, "title", None) or getattr(entity, "first_name", "Saved Messages")

            self.db.set(self.strings["name"], "target_chat", resolved_id)
            await utils.answer(
                message,
                self.strings["target_set"].format(title=title, chat_id=resolved_id),
            )
        except Exception as e:
            await utils.answer(message, self.strings["invalid_chat"].format(error=str(e)))

    async def fumointervalcmd(self, message: Message):
        """Configure posting interval: .fumointerval <value> (e.g. 30m, 2h, 1d, 45)"""
        args = utils.get_args_raw(message).strip()
        if not args:
            await utils.answer(message, self.strings["interval_invalid"])
            return

        seconds = self._parse_time(args)
        if not seconds or seconds < 60:
            await utils.answer(
                message,
                "❌ <b>Interval must be at least 60 seconds (1m) to avoid Telegram rate-limits.</b>",
            )
            return

        self.db.set(self.strings["name"], "interval", seconds)
        await utils.answer(
            message,
            self.strings["interval_set"].format(
                readable=self._format_time(seconds),
                seconds=seconds,
            ),
        )

    async def fumocaptioncmd(self, message: Message):
        """Set caption template (supports {source}, {date}, {tags}). Pass 'clear' or 'none' to remove."""
        args = utils.get_args_raw(message).strip()

        if not args:
            await utils.answer(
                message,
                "ℹ️ <b>Usage:</b> <code>.fumocaption <template></code>\n"
                "• Placeholders: <code>{source}</code>, <code>{date}</code>, <code>{tags}</code>\n"
                "• Send <code>.fumocaption clear</code> to remove captions completely.",
            )
            return

        if args.lower() in ["clear", "none", "-"]:
            self.db.set(self.strings["name"], "caption", "")
            await utils.answer(message, self.strings["caption_cleared"])
        else:
            self.db.set(self.strings["name"], "caption", args)
            await utils.answer(message, self.strings["caption_set"].format(caption=utils.escape_html(args)))

    async def fumostatuscmd(self, message: Message):
        """Display the current configuration and operational status of FumoPoster."""
        enabled = self.db.get(self.strings["name"], "enabled", False)
        interval = self.db.get(self.strings["name"], "interval", 3600)
        target = self.db.get(self.strings["name"], "target_chat", "me")
        last_post = self.db.get(self.strings["name"], "last_post_time", 0.0)
        caption = self.db.get(self.strings["name"], "caption", 'ᗜˬᗜ <b>Fumo Fumo!</b>\n<a href="{source}">Image Source</a>')
        seen_count = len(self.db.get(self.strings["name"], "seen_ids", []))

        # Calculate time until next execution
        if enabled:
            remaining = max(0, int(interval - (time.time() - last_post)))
            next_post = f"in {self._format_time(remaining)}" if remaining > 0 else "Pending loop tick..."
        else:
            next_post = "Disabled"

        text = self.strings["status"].format(
            status_icon="🟢" if enabled else "🔴",
            status_text="Active" if enabled else "Disabled",
            target=target,
            interval=self._format_time(interval),
            next_post=next_post,
            cached_count=seen_count,
            caption=utils.escape_html(caption) if caption.strip() else "<i>(None)</i>",
        )
        await utils.answer(message, text)