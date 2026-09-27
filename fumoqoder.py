# -*- coding: utf-8 -*-
# requires: aiohttp
# FumoQoder — random Touhou fumo autoposter for Hikka / Heroku.
#
# Commands:
#   .fumo on|off          — toggle autoposting
#   .fumotarget [chat]    — set target (arg / reply / current chat)
#   .fumointerval 30m|2h  — set interval (1m .. 7d)
#   .fumocaption <text>   — caption template ({source} {date} {time} {url})
#   .fumotags <tags>      — custom booru tags (default: fumo)
#   .fumotest             — post one fumo into current chat right now
#   .fumostatus           — show current state

import asyncio
import random
import time
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Optional

import aiohttp

from telethon.errors import (
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
)

from .. import loader, utils

MAX_HISTORY = 400
MAX_IMAGE_SIZE = 20 * 1024 * 1024
MIN_INTERVAL = 60
MAX_INTERVAL = 7 * 24 * 3600
DEFAULT_INTERVAL = 3600
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FumoQoder/1.0"

SAFE_EXTS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


@loader.tds
class FumoQoder(loader.Module):
    """Random Touhou fumo autoposter (Safebooru / Gelbooru / Konachan / yande.re)"""

    strings = {"name": "FumoQoder"}

    async def client_ready(self, client, db):
        self.client = client
        self.db = db
        self._session: Optional[aiohttp.ClientSession] = None
        self._stopping = False
        self._next_post = self._get("last_post_ts", 0.0) + self._get(
            "interval", DEFAULT_INTERVAL
        )
        self._task = asyncio.create_task(self._loop())

    async def on_unload(self):
        self._stopping = True
        task = getattr(self, "_task", None)
        if task:
            task.cancel()
        session = getattr(self, "_session", None)
        if session:
            await session.close()

    # ---------- db ----------

    def _get(self, key: str, default: Any = None) -> Any:
        return self.db.get(self.strings["name"], key, default)

    def _set(self, key: str, value: Any):
        self.db.set(self.strings["name"], key, value)

    # ---------- http ----------

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"User-Agent": USER_AGENT},
            )
        return self._session

    async def _json(self, url: str, params: dict) -> Any:
        session = await self._http()
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    # ---------- providers ----------

    def _tags(self) -> str:
        return self._get("tags", "fumo").strip() or "fumo"

    @staticmethod
    def _page_candidates(rand_max: int, first: int) -> list:
        """Random page for variety, then the first page as a guaranteed fallback."""
        rnd = random.randint(first, rand_max)
        return [rnd, first] if rnd != first else [first]

    async def _dapi(self, url: str, tags: str, pid: int) -> list:
        params = {
            "page": "dapi", "s": "post", "q": "index", "json": "1",
            "limit": "100", "tags": tags, "pid": str(pid),
        }
        payload = await self._json(url, params)
        if isinstance(payload, dict):
            payload = payload.get("post", [])
        return payload if isinstance(payload, list) else []

    async def _src_safebooru(self) -> list:
        for pid in self._page_candidates(5, 0):
            posts = []
            for p in await self._dapi("https://safebooru.org/index.php", self._tags(), pid):
                url = p.get("file_url") or ""
                if url.startswith("//"):
                    url = "https:" + url
                elif url.startswith("/"):
                    url = "https://safebooru.org" + url
                if not url and p.get("directory") and p.get("image"):
                    url = f"https://safebooru.org/images/{p['directory']}/{p['image']}"
                if url:
                    posts.append({"id": f"sb_{p.get('id')}", "url": url, "src": "Safebooru"})
            if posts:
                return posts
        return []

    async def _src_gelbooru(self) -> list:
        tags = f"{self._tags()} rating:general"
        for pid in self._page_candidates(5, 0):
            posts = []
            for p in await self._dapi("https://gelbooru.com/index.php", tags, pid):
                url = p.get("file_url") or ""
                if url.startswith("//"):
                    url = "https:" + url
                if url:
                    posts.append({"id": f"gb_{p.get('id')}", "url": url, "src": "Gelbooru"})
            if posts:
                return posts
        return []

    async def _json_posts(self, url: str, tags: str, page: int) -> list:
        payload = await self._json(url, {"limit": "100", "tags": tags, "page": str(page)})
        return payload if isinstance(payload, list) else []

    async def _src_konachan(self) -> list:
        tags = f"{self._tags()} rating:safe"
        for page in self._page_candidates(3, 1):
            posts = [
                {"id": f"kn_{p.get('id')}", "url": p["file_url"], "src": "Konachan"}
                for p in await self._json_posts("https://konachan.com/post.json", tags, page)
                if isinstance(p, dict) and p.get("file_url") and p.get("rating") == "s"
            ]
            if posts:
                return posts
        return []

    async def _src_yandere(self) -> list:
        for page in self._page_candidates(3, 1):
            posts = [
                {"id": f"yd_{p.get('id')}", "url": p["file_url"], "src": "yande.re"}
                for p in await self._json_posts("https://yande.re/post.json", self._tags(), page)
                if isinstance(p, dict) and p.get("file_url") and p.get("rating") == "s"
            ]
            if posts:
                return posts
        return []


    # ---------- fetch ----------

    @staticmethod
    def _ext(url: str) -> Optional[str]:
        clean = url.lower().split("?", 1)[0]
        for ext in SAFE_EXTS:
            if clean.endswith(ext):
                return ".jpg" if ext == ".jpeg" else ext
        return None

    async def _pick_post(self) -> dict:
        """Return {id, url, ext, src} — a fresh, unseen, safe post."""
        sources = [self._src_safebooru, self._src_gelbooru,
                   self._src_konachan, self._src_yandere]
        random.shuffle(sources)
        history = self._get("history", []) or []
        history_set = set(history)
        errors = []

        for src in sources:
            try:
                posts = await src()
            except Exception as exc:
                errors.append(f"{type(exc).__name__}")
                continue

            random.shuffle(posts)
            for p in posts:
                ext = self._ext(p["url"])
                if not ext or p["id"] in history_set:
                    continue
                p["ext"] = ext
                self._push_history(p["id"])
                return p

            errors.append("no fresh posts")

        raise RuntimeError(
            ("all sources failed: " + ", ".join(errors)) if errors else "no fumo found"
        )

    def _push_history(self, post_id: str):
        history = self._get("history", []) or []
        history.append(post_id)
        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
        self._set("history", history)

    async def _download(self, url: str) -> bytes:
        session = await self._http()
        async with session.get(url, headers={
            "User-Agent": USER_AGENT,
            "Referer": "/".join(url.split("/")[:3]) + "/",
        }) as resp:
            resp.raise_for_status()
            data = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_IMAGE_SIZE:
                    raise RuntimeError("image > 20MB")
        if not data:
            raise RuntimeError("empty response")
        return bytes(data)

    async def _send(self, entity) -> dict:
        last_err = None
        for _ in range(3):
            post = await self._pick_post()
            try:
                data = await self._download(post["url"])
                break
            except Exception as exc:
                last_err = exc
        else:
            raise RuntimeError(f"download failed: {last_err}")

        file = BytesIO(data)
        file.name = f"fumo_{post['id']}{post['ext']}"

        await self.client.send_file(
            entity,
            file,
            caption=self._caption(post),
            mime_type=SAFE_EXTS[post["ext"]],
        )
        now = time.time()
        self._set("last_post_ts", now)
        self._set("last_source", post["src"])
        self._set("last_url", post["url"])
        self._next_post = now + self._get("interval", DEFAULT_INTERVAL)
        return post

    def _caption(self, post: dict) -> str:
        template = self._get("caption", "")
        if not template:
            return ""
        now = datetime.now(timezone.utc)
        return (
            template.replace("{source}", post["src"])
            .replace("{date}", now.strftime("%Y-%m-%d"))
            .replace("{time}", now.strftime("%H:%M:%S"))
            .replace("{url}", post["url"])
        )

    # ---------- target ----------

    async def _target(self):
        target = self._get("target", None)
        if target in (None, ""):
            return await self.client.get_entity("me")
        return await self.client.get_entity(target)

    # ---------- scheduler ----------

    async def _loop(self):
        await asyncio.sleep(10)
        while not self._stopping:
            try:
                if not self._get("enabled", False) or time.time() < self._next_post:
                    await asyncio.sleep(15)
                    continue

                try:
                    await self._send(await self._target())
                except FloodWaitError as exc:
                    self._next_post = time.time() + exc.seconds + 30
                except (ChatWriteForbiddenError, ChatAdminRequiredError):
                    self._next_post = time.time() + 300
                except Exception:
                    self._next_post = time.time() + 60
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(15)

    # ---------- commands ----------

    @loader.command(
        ru_doc="Включить/выключить автопостинг: .fumo on/off",
        en_doc="Toggle autoposting: .fumo on / .fumo off",
    )
    async def fumo(self, message):
        """Toggle autoposting: .fumo on / .fumo off"""
        args = utils.get_args_raw(message).lower()
        if args in ("on", "1", "enable"):
            self._set("enabled", True)
            await utils.answer(message, "🟢 <b>Autoposting enabled.</b>")
        elif args in ("off", "0", "disable"):
            self._set("enabled", False)
            await utils.answer(message, "🔴 <b>Autoposting disabled.</b>")
        else:
            state = "enabled" if self._get("enabled", False) else "disabled"
            await utils.answer(
                message,
                "<b>Usage:</b> <code>.fumo on|off</code>\n"
                f"<b>Current:</b> {state}",
            )

    @loader.command(
        ru_doc="Установить чат: .fumotarget @name|id, ответ на сообщение, или пусто для текущего",
        en_doc="Set target chat: .fumotarget @name|id, reply, or empty for current chat",
    )
    async def fumotarget(self, message):
        """Set target chat: .fumotarget @name|id, reply to a message, or empty for current chat"""
        args = utils.get_args_raw(message).strip()
        if args:
            raw: Any = int(args) if args.lstrip("-").isdigit() else args
            try:
                entity = await self.client.get_entity(raw)
            except Exception as exc:
                await utils.answer(
                    message,
                    f"❌ <b>Cannot resolve target:</b>\n"
                    f"<code>{utils.escape_html(str(exc))}</code>",
                )
                return
            stored = entity.id
        elif message.is_reply:
            reply = await message.get_reply_message()
            entity, stored = reply.chat, reply.chat_id
        else:
            entity, stored = await message.get_chat(), message.chat_id

        self._set("target", stored)
        title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(stored)
        await utils.answer(
            message, f"🎯 <b>Target:</b> <code>{utils.escape_html(str(title))}</code>"
        )

    @loader.command(
        ru_doc="Интервал постинга: .fumointerval 30m / 2h / 90 (=минуты)",
        en_doc="Set interval: .fumointerval 30m / 2h / 90 (=minutes)",
    )
    async def fumointerval(self, message):
        """Set interval: .fumointerval 30m / 2h / 90 (=minutes)"""
        args = utils.get_args_raw(message).strip().lower()
        try:
            if args.endswith("h"):
                seconds = int(float(args[:-1]) * 3600)
            elif args.endswith("m"):
                seconds = int(float(args[:-1]) * 60)
            elif args.endswith("s"):
                seconds = int(float(args[:-1]))
            else:
                seconds = int(float(args) * 60)
        except (ValueError, TypeError):
            await utils.answer(message, "⚠️ <b>Example:</b> <code>.fumointerval 30m</code>")
            return

        if not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
            await utils.answer(message, "⚠️ <b>Interval: 1 minute .. 7 days.</b>")
            return

        self._set("interval", seconds)
        self._next_post = self._get("last_post_ts", 0.0) + seconds
        await utils.answer(message, f"⏱ <b>Interval:</b> <code>{fmt_interval(seconds)}</code>")

    @loader.command(
        ru_doc="Шаблон подписи ({source} {date} {time} {url}); пусто — очистить",
        en_doc="Set caption template ({source} {date} {time} {url}); empty clears it",
    )
    async def fumocaption(self, message):
        """Set caption template ({source} {date} {time} {url}); empty clears it"""
        caption = utils.get_args_raw(message)
        self._set("caption", caption)
        await utils.answer(
            message, "📝 <b>Caption updated.</b>" if caption else "📝 <b>Caption cleared.</b>"
        )

    @loader.command(
        ru_doc="Свои теги booru: .fumotags fumo cirno (пусто — сброс на 'fumo')",
        en_doc="Set custom booru tags: .fumotags fumo cirno (empty resets to 'fumo')",
    )
    async def fumotags(self, message):
        """Set custom booru tags: .fumotags fumo cirno (empty resets to 'fumo')"""
        tags = utils.get_args_raw(message).strip()
        self._set("tags", tags or "fumo")
        await utils.answer(message, f"🏷 <b>Tags:</b> <code>{utils.escape_html(tags or 'fumo')}</code>")

    @loader.command(
        ru_doc="Отправить одно фумо в текущий чат прямо сейчас",
        en_doc="Post one fumo into the current chat right now",
    )
    async def fumotest(self, message):
        """Post one fumo into the current chat right now"""
        status = await utils.answer(message, "🔎 <b>Fetching fumo...</b>")
        try:
            post = await self._send(await message.get_chat())
        except FloodWaitError as exc:
            await utils.answer(status, f"🐌 <b>FloodWait:</b> <code>{exc.seconds}s</code>")
            return
        except Exception as exc:
            await utils.answer(
                status,
                f"❌ <b>Failed:</b>\n<code>{utils.escape_html(str(exc))}</code>",
            )
            return
        await utils.answer(status, f"✅ <b>Sent.</b> <i>Source:</i> {post['src']}")

    @loader.command(
        ru_doc="Показать состояние автопостера",
        en_doc="Show autoposter status",
    )
    async def fumostatus(self, message):
        """Show autoposter status"""
        target = self._get("target", None)
        target_str = str(target) if target else "Saved Messages"
        last_time = self._get("last_post_ts", 0)
        last_str = (
            datetime.fromtimestamp(last_time, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if last_time else "never"
        )
        if self._get("enabled", False):
            remain = max(0, int(self._next_post - time.time()))
            state = f"🟢 enabled, next post in <code>{fmt_interval(remain)}</code>"
        else:
            state = "🔴 disabled"
        await utils.answer(
            message,
            "🌸 <b>FumoQoder</b>\n\n"
            f"<b>Status:</b> {state}\n"
            f"<b>Target:</b> <code>{utils.escape_html(target_str)}</code>\n"
            f"<b>Interval:</b> <code>{fmt_interval(self._get('interval', DEFAULT_INTERVAL))}</code>\n"
            f"<b>Tags:</b> <code>{utils.escape_html(self._tags())}</code>\n"
            f"<b>Caption:</b> <code>{utils.escape_html(self._get('caption', '') or '—')}</code>\n"
            f"<b>Last post:</b> <code>{last_str}</code>\n"
            f"<b>Last source:</b> <code>{utils.escape_html(self._get('last_source', '') or '—')}</code>",
        )


def fmt_interval(seconds: int) -> str:
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
