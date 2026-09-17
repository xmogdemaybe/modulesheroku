# meta developer: @xmogde
# requires: aiohttp

import asyncio
import io
import json
import logging
import random
import re
import time
import xml.etree.ElementTree as ET
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
    """Automated Touhou Fumo plushie poster with multi-booru cascade and live network diagnostics."""

    strings = {
        "name": "FumoPoster",
        "fetching": "ᗜˬᗜ <i>Fetching random Fumo from providers...</i>",
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
            "• <b>Cached Post IDs:</b> <code>{cached_count}</code>\n"
            "• <b>Last Known Error:</b> <code>{last_error}</code>\n\n"
            "<b>Caption Template:</b>\n<blockquote>{caption}</blockquote>"
        ),
    }

    def __init__(self):
        self.config = loader.ModuleConfig()
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_diagnostics: List[Dict[str, Any]] = []
        self._last_error_str: str = "None"
        self.client = None
        self.db = None

    async def client_ready(self, client, db):
        self.client = client
        self.db = db

        # Bypass strict SSL certificate checks common on minimal Docker/Heroku dynos
        connector = aiohttp.TCPConnector(ssl=False)

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/xml,text/html,image/*,*/*;q=0.8",
        }
        self._session = aiohttp.ClientSession(
            connector=connector,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=25)
        )

        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._poster_loop())

    async def on_unload(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self._session and not self._session.closed:
            await self._session.close()

    # -------------------------------------------------------------------------
    # Provider Implementations (Safe + Booru + Fallbacks)
    # -------------------------------------------------------------------------

    async def _fetch_reddit_fumo(self) -> Tuple[str, List[Dict[str, Any]], str]:
        """Fetch photos from r/FUMOFUMO (Very high reliability on VPS/Datacenters)."""
        url = "https://www.reddit.com/r/FUMOFUMO/hot.json?limit=50"
        async with self._session.get(url) as resp:
            text = await resp.text()
            if resp.status != 200:
                snippet = text[:150].replace("\n", " ")
                raise RuntimeError(f"HTTP {resp.status}: {snippet}")

            data = json.loads(text)
            children = data.get("data", {}).get("children", [])
            posts = []
            for child in children:
                pdata = child.get("data", {})
                img_url = pdata.get("url", "")
                if any(img_url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]) or "i.redd.it" in img_url:
                    posts.append({
                        "id": f"reddit_{pdata.get('id')}",
                        "file_url": img_url,
                        "post_url": f"https://reddit.com{pdata.get('permalink')}",
                        "tags": pdata.get("title", "Touhou Fumo"),
                    })

            return url, posts, f"Parsed {len(posts)} images from Reddit JSON"

    async def _fetch_safebooru(self) -> Tuple[str, List[Dict[str, Any]], str]:
        """Fetch posts from Safebooru (handles both JSON and XML responses)."""
        page = random.randint(0, 6)
        url = f"https://safebooru.org/index.php?page=dapi&s=post&q=index&json=1&tags=fumo&limit=40&pid={page}"
        async with self._session.get(url) as resp:
            text = await resp.text()
            if resp.status != 200:
                snippet = text[:150].replace("\n", " ")
                raise RuntimeError(f"HTTP {resp.status}: {snippet}")

            posts = []
            stripped = text.strip()

            # Handle XML fallback if Safebooru ignores json=1
            if stripped.startswith("<?xml") or stripped.startswith("<posts"):
                root = ET.fromstring(stripped)
                for item in root.findall("post"):
                    img_file = item.attrib.get("image", "")
                    dir_path = str(item.attrib.get("directory", "")).strip("/")
                    if not img_file:
                        continue
                    prefix = "" if dir_path.startswith("images") else "images/"
                    posts.append({
                        "id": f"safebooru_{item.attrib.get('id')}",
                        "file_url": f"https://safebooru.org/{prefix}{dir_path}/{img_file}",
                        "post_url": f"https://safebooru.org/index.php?page=post&s=view&id={item.attrib.get('id')}",
                        "tags": item.attrib.get("tags", ""),
                    })
                return url, posts, f"Parsed {len(posts)} posts via XML fallback"

            # Parse JSON
            data = json.loads(stripped)
            for item in data:
                img_file = item.get("image", "")
                dir_path = str(item.get("directory", "")).strip("/")
                if not img_file:
                    continue
                prefix = "" if dir_path.startswith("images") else "images/"
                posts.append({
                    "id": f"safebooru_{item.get('id')}",
                    "file_url": f"https://safebooru.org/{prefix}{dir_path}/{img_file}",
                    "post_url": f"https://safebooru.org/index.php?page=post&s=view&id={item.get('id')}",
                    "tags": item.get("tags", ""),
                })

            return url, posts, f"Parsed {len(posts)} posts via JSON"

    async def _fetch_tbib(self) -> Tuple[str, List[Dict[str, Any]], str]:
        """Fetch posts from TBIB (The Big ImageBoard)."""
        page = random.randint(0, 4)
        url = f"https://tbib.org/index.php?page=dapi&s=post&q=index&json=1&tags=fumo&limit=40&pid={page}"
        async with self._session.get(url) as resp:
            text = await resp.text()
            if resp.status != 200:
                snippet = text[:150].replace("\n", " ")
                raise RuntimeError(f"HTTP {resp.status}: {snippet}")

            stripped = text.strip()
            posts = []

            if stripped.startswith("<?xml") or stripped.startswith("<posts"):
                root = ET.fromstring(stripped)
                for item in root.findall("post"):
                    img_file = item.attrib.get("image", "")
                    dir_path = str(item.attrib.get("directory", "")).strip("/")
                    if not img_file:
                        continue
                    prefix = "" if dir_path.startswith("images") else "images/"
                    posts.append({
                        "id": f"tbib_{item.attrib.get('id')}",
                        "file_url": f"https://tbib.org/{prefix}{dir_path}/{img_file}",
                        "post_url": f"https://tbib.org/index.php?page=post&s=view&id={item.attrib.get('id')}",
                        "tags": item.attrib.get("tags", ""),
                    })
                return url, posts, f"Parsed {len(posts)} posts via XML"

            data = json.loads(stripped)
            for item in data:
                img_file = item.get("image", "")
                dir_path = str(item.get("directory", "")).strip("/")
                if not img_file:
                    continue
                prefix = "" if dir_path.startswith("images") else "images/"
                posts.append({
                    "id": f"tbib_{item.get('id')}",
                    "file_url": f"https://tbib.org/{prefix}{dir_path}/{img_file}",
                    "post_url": f"https://tbib.org/index.php?page=post&s=view&id={item.get('id')}",
                    "tags": item.get("tags", ""),
                })

            return url, posts, f"Parsed {len(posts)} posts via JSON"

    async def _fetch_danbooru(self) -> Tuple[str, List[Dict[str, Any]], str]:
        """Fetch posts from Danbooru."""
        url = "https://danbooru.donmai.us/posts.json?tags=fumo+order:random&limit=25"
        async with self._session.get(url) as resp:
            text = await resp.text()
            if resp.status != 200:
                snippet = text[:150].replace("\n", " ")
                raise RuntimeError(f"HTTP {resp.status}: {snippet}")

            data = json.loads(text)
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

            return url, posts, f"Parsed {len(posts)} posts via JSON"

    async def _fetch_gelbooru(self) -> Tuple[str, List[Dict[str, Any]], str]:
        """Fetch posts from Gelbooru."""
        url = "https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&tags=fumo+sort:random&limit=25"
        async with self._session.get(url) as resp:
            text = await resp.text()
            if resp.status != 200:
                snippet = text[:150].replace("\n", " ")
                raise RuntimeError(f"HTTP {resp.status}: {snippet}")

            data = json.loads(text)
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

            return url, posts, f"Parsed {len(posts)} posts via JSON"

    # -------------------------------------------------------------------------
    # Image Binary Downloader
    # -------------------------------------------------------------------------

    async def _download_image(self, file_url: str) -> Tuple[Optional[io.BytesIO], str]:
        """Download raw image bytes, verifying HTTP status and size."""
        try:
            headers = {"Referer": file_url}
            async with self._session.get(file_url, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return None, f"CDN HTTP {resp.status}: {body[:100]}"

                data = await resp.read()
                if len(data) < 512:
                    return None, f"Received corrupt payload ({len(data)} bytes)"

                buf = io.BytesIO(data)
                ext = "jpg"
                clean_url = file_url.split("?")[0]
                if "." in clean_url:
                    candidate = clean_url.split(".")[-1].lower()
                    if candidate in ["jpg", "jpeg", "png", "webp", "gif"]:
                        ext = candidate

                buf.name = f"fumo_{int(time.time())}.{ext}"
                buf.seek(0)
                return buf, f"OK ({len(data) / 1024:.1f} KB)"
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    # -------------------------------------------------------------------------
    # Core Aggregator & Diagnostic Collector
    # -------------------------------------------------------------------------

    async def _get_random_fumo(self) -> Tuple[Optional[Tuple[io.BytesIO, str, str, str]], List[Dict[str, Any]]]:
        """
        Runs query across all providers while tracking exact status diagnostics.
        Returns: ((buffer, post_url, tags, post_id), diagnostics_report)
        """
        providers = [
            ("Reddit r/FUMOFUMO", self._fetch_reddit_fumo),
            ("Safebooru", self._fetch_safebooru),
            ("TBIB", self._fetch_tbib),
            ("Danbooru", self._fetch_danbooru),
            ("Gelbooru", self._fetch_gelbooru),
        ]
        random.shuffle(providers)

        seen_ids: List[str] = self.db.get(self.strings["name"], "seen_ids", [])
        diagnostics = []

        for name, fetcher in providers:
            record: Dict[str, Any] = {
                "name": name,
                "url": "N/A",
                "status": "Unknown",
                "posts_count": 0,
                "detail": "None",
                "download_status": "Not attempted",
            }
            try:
                endpoint_url, posts, info_str = await fetcher()
                record["url"] = endpoint_url
                record["status"] = "HTTP 200 OK"
                record["posts_count"] = len(posts)
                record["detail"] = info_str

                if not posts:
                    record["status"] = "Warning"
                    record["detail"] = "API responded with 200 OK but returned 0 posts matching 'fumo'."
                    diagnostics.append(record)
                    continue

                # Filter seen IDs
                candidates = [p for p in posts if p["id"] not in seen_ids]
                if not candidates:
                    candidates = posts

                random.shuffle(candidates)

                downloaded = False
                for post in candidates[:3]:
                    buf, dl_info = await self._download_image(post["file_url"])
                    record["download_status"] = dl_info
                    if buf:
                        downloaded = True
                        diagnostics.append(record)
                        self._last_diagnostics = diagnostics
                        return (buf, post["post_url"], post["tags"], post["id"]), diagnostics

                if not downloaded:
                    record["status"] = "Download Failed"
                    record["detail"] = f"Failed to download image media from candidate CDN links: {record['download_status']}"

            except Exception as exc:
                record["status"] = "Failed"
                record["detail"] = f"{type(exc).__name__}: {exc}"

            diagnostics.append(record)

        self._last_diagnostics = diagnostics
        return None, diagnostics

    # -------------------------------------------------------------------------
    # Scheduler Loop
    # -------------------------------------------------------------------------

    async def _poster_loop(self):
        while True:
            try:
                enabled = self.db.get(self.strings["name"], "enabled", False)
                interval = self.db.get(self.strings["name"], "interval", 3600)
                last_post = self.db.get(self.strings["name"], "last_post_time", 0.0)
                now = time.time()

                if enabled and (now - last_post >= interval):
                    await self._execute_autopost()
                    self.db.set(self.strings["name"], "last_post_time", time.time())

                await asyncio.sleep(15)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._last_error_str = f"Loop error: {type(e).__name__}: {e}"
                logger.exception("Error in Fumo background loop: %s", e)
                await asyncio.sleep(30)

    async def _execute_autopost(self):
        target_chat = self.db.get(self.strings["name"], "target_chat", "me")
        result, diags = await self._get_random_fumo()

        if not result:
            self._last_error_str = "All providers failed during auto-post schedule."
            logger.warning("Fumo auto-post skipped. All providers failed.")
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
            self._last_error_str = "None"
            seen_ids = self.db.get(self.strings["name"], "seen_ids", [])
            seen_ids.append(post_id)
            self.db.set(self.strings["name"], "seen_ids", seen_ids[-100:])
        except errors.FloodWaitError as flood:
            self._last_error_str = f"FloodWait ({flood.seconds}s)"
            logger.warning("Fumo auto-poster hit FloodWait: %ds", flood.seconds)
            await asyncio.sleep(flood.seconds)
        except (errors.ChatWriteForbiddenError, errors.ChannelPrivateError) as perm:
            self._last_error_str = f"Permission error: {perm}"
            self.db.set(self.strings["name"], "enabled", False)
        except Exception as exc:
            self._last_error_str = f"{type(exc).__name__}: {exc}"
            logger.exception("Auto-post dispatch exception: %s", exc)

    def _build_caption(self, source_url: str, tags: str) -> str:
        default_caption = 'ᗜˬᗜ <b>Fumo Fumo!</b>\n<a href="{source}">Image Source</a>'
        template = self.db.get(self.strings["name"], "caption", default_caption)
        if not template.strip():
            return ""

        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        return template.format_map(
            SafeDict(
                source=source_url,
                date=now_str,
                tags=tags[:120] + ("..." if len(tags) > 120 else ""),
            )
        )

    # -------------------------------------------------------------------------
    # Utility Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _parse_time(time_str: str) -> Optional[int]:
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
        """Fetch and send a random Fumo photo immediately (includes inline debug on failure)."""
        status_msg = await utils.answer(message, self.strings["fetching"])

        result, diags = await self._get_random_fumo()
        if not result:
            lines = [
                "❌ <b>Failed to retrieve Fumo image from all providers!</b>\n",
                "<b>Diagnostic Breakdown:</b>",
            ]
            for d in diags:
                icon = "🟢" if "200" in d["status"] else "🔴"
                lines.append(
                    f"• <b>{d['name']}:</b> {icon} <code>{d['status']}</code>\n"
                    f"  └ <i>{utils.escape_html(d['detail'])}</i>"
                )
            lines.append("\n💡 <i>Run <code>.fumodebug</code> for a full active probe test.</i>")
            await utils.answer(status_msg, "\n".join(lines))
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

            seen = self.db.get(self.strings["name"], "seen_ids", [])
            seen.append(post_id)
            self.db.set(self.strings["name"], "seen_ids", seen[-100:])
        except Exception as e:
            await utils.answer(status_msg, f"❌ <b>Error sending media:</b> <code>{e}</code>")

    async def fumodebugcmd(self, message: Message):
        """Active live diagnostic probe: tests endpoints, latency, and CDN downloads."""
        status_msg = await utils.answer(message, "🔍 <b>Running active Fumo provider diagnostic probe...</b>")

        providers = [
            ("Reddit r/FUMOFUMO", self._fetch_reddit_fumo),
            ("Safebooru", self._fetch_safebooru),
            ("TBIB", self._fetch_tbib),
            ("Danbooru", self._fetch_danbooru),
            ("Gelbooru", self._fetch_gelbooru),
        ]

        report = ["🛠 <b><u>Fumo Providers Diagnostic Probe</u></b>\n"]

        for name, fetcher in providers:
            t0 = time.time()
            try:
                url, posts, info = await fetcher()
                latency = int((time.time() - t0) * 1000)

                dl_status = "⚪ <i>No posts</i>"
                if posts:
                    # Test-download the first post
                    test_post = posts[0]
                    buf, dl_info = await self._download_image(test_post["file_url"])
                    dl_status = f"🟢 <code>{dl_info}</code>" if buf else f"🔴 <code>{dl_info}</code>"

                report.append(
                    f"<b>{name}</b>\n"
                    f"• <b>Status:</b> 🟢 <code>200 OK</code> ({latency}ms)\n"
                    f"• <b>Posts Parsed:</b> <code>{len(posts)}</code>\n"
                    f"• <b>CDN Test:</b> {dl_status}\n"
                    f"• <b>Endpoint:</b> <code>{url[:60]}...</code>\n"
                )
            except Exception as e:
                latency = int((time.time() - t0) * 1000)
                report.append(
                    f"<b>{name}</b>\n"
                    f"• <b>Status:</b> 🔴 <code>Failed</code> ({latency}ms)\n"
                    f"• <b>Error:</b> <code>{type(e).__name__}: {utils.escape_html(str(e))}</code>\n"
                )

        report.append("🏁 <i>Diagnostic test complete.</i>")
        await utils.answer(status_msg, "\n".join(report))

    async def fumologcmd(self, message: Message):
        """Print the raw diagnostic dump of the last attempted fetch."""
        if not self._last_diagnostics:
            await utils.answer(message, "ℹ️ <i>No diagnostics recorded yet. Run <code>.fumo</code> or <code>.fumodebug</code> first.</i>")
            return

        formatted_dump = json.dumps(self._last_diagnostics, indent=2, ensure_ascii=False)
        if len(formatted_dump) > 3800:
            formatted_dump = formatted_dump[:3800] + "\n... (truncated)"

        await utils.answer(message, f"📋 <b><u>Last Raw Diagnostics:</u></b>\n<pre><code class=\"language-json\">{utils.escape_html(formatted_dump)}</code></pre>")

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
        """Configure auto-post target chat: .fumochat [chat_id|@username|me]"""
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
        """Configure posting interval: .fumointerval <value> (e.g. 30m, 2h, 1d)"""
        args = utils.get_args_raw(message).strip()
        if not args:
            await utils.answer(message, self.strings["interval_invalid"])
            return

        seconds = self._parse_time(args)
        if not seconds or seconds < 60:
            await utils.answer(message, "❌ <b>Interval must be at least 60 seconds (1m).</b>")
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
        """Set custom caption template (supports {source}, {date}, {tags}). Pass 'clear' to disable."""
        args = utils.get_args_raw(message).strip()

        if not args:
            await utils.answer(
                message,
                "ℹ️ <b>Usage:</b> <code>.fumocaption <template></code>\n"
                "• Placeholders: <code>{source}</code>, <code>{date}</code>, <code>{tags}</code>\n"
                "• Send <code>.fumocaption clear</code> to remove caption completely.",
            )
            return

        if args.lower() in ["clear", "none", "-"]:
            self.db.set(self.strings["name"], "caption", "")
            await utils.answer(message, self.strings["caption_cleared"])
        else:
            self.db.set(self.strings["name"], "caption", args)
            await utils.answer(message, self.strings["caption_set"].format(caption=utils.escape_html(args)))

    async def fumostatuscmd(self, message: Message):
        """Display current operational configuration, next post timer, and last error."""
        enabled = self.db.get(self.strings["name"], "enabled", False)
        interval = self.db.get(self.strings["name"], "interval", 3600)
        target = self.db.get(self.strings["name"], "target_chat", "me")
        last_post = self.db.get(self.strings["name"], "last_post_time", 0.0)
        caption = self.db.get(self.strings["name"], "caption", 'ᗜˬᗜ <b>Fumo Fumo!</b>\n<a href="{source}">Image Source</a>')
        seen_count = len(self.db.get(self.strings["name"], "seen_ids", []))

        if enabled:
            remaining = max(0, int(interval - (time.time() - last_post)))
            next_post = f"in {self._format_time(remaining)}" if remaining > 0 else "Pending loop execution..."
        else:
            next_post = "Disabled"

        text = self.strings["status"].format(
            status_icon="🟢" if enabled else "🔴",
            status_text="Active" if enabled else "Disabled",
            target=target,
            interval=self._format_time(interval),
            next_post=next_post,
            cached_count=seen_count,
            last_error=self._last_error_str,
            caption=utils.escape_html(caption) if caption.strip() else "<i>(None)</i>",
        )
        await utils.answer(message, text)
