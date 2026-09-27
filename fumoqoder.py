# -*- coding: utf-8 -*-
# requires: aiohttp
# FumoQoder — random Touhou fumo autoposter for Hikka / Heroku.
#
# Sources (tried in random order, then fallbacks):
#   Safebooru / Konachan             -> boorus (safe-only)
#   Reddit (r/Fumofumo)              -> fallback, no API key (needs system VPN in RU)
#   Flickr                           -> optional, only if API key is set (safe_search=1)
#
# Commands:
#   .fumo on|off                 toggle autoposting
#   .fumotarget [chat]           set target (arg / reply / current chat)
#   .fumointerval 30m|2h|90      set interval (minutes by default), 1m..7d
#   .fumocaption <text>          custom caption text (placeholders allowed)
#   .fumometa on|off             toggle auto metadata footer in caption
#   .fumotags [source] [tags]    per-source search tags (show if no args)
#   .fumoexclude <tags>          global minus-tag blacklist for boorus
#   .fumoreddit on|off           toggle Reddit fallback source
#   .fumosub <subreddit>         set subreddit (default Fumofumo)
#   .fumoflickrkey <key>         set Flickr API key (empty disables Flickr)
#   .fumoproxy <url>             HTTP/SOCKS5 proxy for all module traffic
#                                (e.g. local WARP: socks5://127.0.0.1:40000)
#   .fumotest                    post one fumo into current chat right now
#   .fumostatus                  show all settings + enabled sources
#
# Caption placeholders: {source} {post} {url} {tags} {id} {date} {time}

import asyncio
import logging
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

logger = logging.getLogger(__name__)

MAX_HISTORY = 400
MAX_IMAGE_SIZE = 20 * 1024 * 1024
MIN_INTERVAL = 60
MAX_INTERVAL = 7 * 24 * 3600
DEFAULT_INTERVAL = 3600
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FumoQoder/1.2"
# Reddit blocks browser-like UAs; it requires a distinct bot-style one.
REDDIT_USER_AGENT = "linux:FumoQoder:1.2 (by /u/xmogdemaybe)"

SAFE_EXTS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# Sensible per-source defaults (Safebooru uses fumo_(doll); Konachan needs a
# broader tag). Override any of them with: .fumotags <source> <tags>
DEFAULT_TAGS = {
    "safebooru": "fumo_(doll)",
    "konachan": "touhou doll",
    "flickr": "fumo",
}
BOORU_SOURCES = ("safebooru", "konachan")
DEFAULT_SUBREDDIT = "Fumofumo"


@loader.tds
class FumoQoder(loader.Module):
    """Random Touhou fumo autoposter (Safebooru / Konachan + Reddit/Flickr fallbacks)"""

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

    async def _json(self, url: str, params: dict, headers: Optional[dict] = None) -> Any:
        session = await self._http()
        async with session.get(url, params=params, headers=headers, proxy=self._proxy()) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    # ---------- tag config ----------

    def _tags_for(self, source: str) -> str:
        return (self._get(f"tags_{source}", DEFAULT_TAGS.get(source, "fumo")) or "").strip()

    def _exclude(self) -> str:
        return (self._get("exclude", "") or "").strip()

    def _proxy(self) -> Optional[str]:
        return (self._get("proxy", "") or "").strip() or None

    def _booru_tags(self, source: str) -> str:
        """Search tags + site rating filter + global minus-tag blacklist."""
        parts = [self._tags_for(source)]
        if source == "konachan":
            parts.append("rating:safe")
        for t in self._exclude().split():
            parts.append(t if t.startswith("-") else f"-{t}")
        return " ".join(p for p in parts if p)

    # ---------- pagination helpers ----------

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

    async def _moebooru(self, url: str, tags: str, page: int) -> list:
        payload = await self._json(url, {"limit": "100", "tags": tags, "page": str(page)})
        return payload if isinstance(payload, list) else []

    # ---------- sources (each returns canonical post dicts) ----------

    async def _src_safebooru(self) -> list:
        tags = self._booru_tags("safebooru")
        for pid in self._page_candidates(5, 0):
            posts = []
            for p in self._dapi_parse(await self._dapi("https://safebooru.org/index.php", tags, pid)):
                url = p.get("file_url") or ""
                if url.startswith("//"):
                    url = "https:" + url
                elif url.startswith("/"):
                    url = "https://safebooru.org" + url
                if not url and p.get("directory") and p.get("image"):
                    url = f"https://safebooru.org/images/{p['directory']}/{p['image']}"
                if url:
                    posts.append({
                        "id": f"sb_{p.get('id')}", "url": url, "src": "Safebooru",
                        "post_url": f"https://safebooru.org/index.php?page=post&s=view&id={p.get('id')}",
                        "tags": tags,
                    })
            if posts:
                return posts
        return []

    async def _src_konachan(self) -> list:
        tags = self._booru_tags("konachan")
        for page in self._page_candidates(3, 1):
            posts = [
                {
                    "id": f"kn_{p.get('id')}", "url": p["file_url"], "src": "Konachan",
                    "post_url": f"https://konachan.com/post/show/{p.get('id')}",
                    "tags": tags,
                }
                for p in await self._moebooru("https://konachan.com/post.json", tags, page)
                if isinstance(p, dict) and p.get("file_url") and p.get("rating") == "s"
            ]
            if posts:
                return posts
        return []

    async def _src_reddit(self) -> list:
        sub = self._get("subreddit", DEFAULT_SUBREDDIT) or DEFAULT_SUBREDDIT
        payload = await self._json(
            f"https://www.reddit.com/r/{sub}/hot.json", {"limit": "50", "raw_json": "1"},
            headers={"User-Agent": REDDIT_USER_AGENT},
        )
        children = (payload or {}).get("data", {}).get("children", []) if isinstance(payload, dict) else []
        posts = []
        for c in children:
            d = c.get("data", {}) if isinstance(c, dict) else {}
            img = d.get("url_overridden_by_dest") or d.get("url") or ""
            if not self._ext(img):
                continue
            posts.append({
                "id": f"rd_{d.get('id')}", "url": img, "src": "Reddit",
                "post_url": "https://reddit.com" + (d.get("permalink") or ""),
                "tags": f"r/{sub}",
            })
        return posts

    async def _src_flickr(self) -> list:
        key = self._get("flickr_key", "")
        if not key:
            return []
        params = {
            "method": "flickr.photos.search", "api_key": key,
            "text": self._tags_for("flickr"), "safe_search": "1", "content_type": "1",
            "media": "photos", "per_page": "100", "format": "json", "nojsoncallback": "1",
            "extras": "url_o,url_l,url_m,owner_name,tags",
        }
        payload = await self._json("https://www.flickr.com/services/rest/", params)
        photos = (payload or {}).get("photos", {}).get("photo", []) if isinstance(payload, dict) else []
        posts = []
        for p in photos:
            if not isinstance(p, dict):
                continue
            img = p.get("url_o") or p.get("url_l") or p.get("url_m")
            if not img or not self._ext(img):
                continue
            posts.append({
                "id": f"fl_{p.get('id')}", "url": img, "src": "Flickr",
                "post_url": f"https://www.flickr.com/photos/{p.get('owner')}/{p.get('id')}",
                "tags": (p.get("tags") or "")[:120],
            })
        return posts

    @staticmethod
    def _dapi_parse(payload: Any) -> list:
        if isinstance(payload, dict):
            payload = payload.get("post", [])
        return payload if isinstance(payload, list) else []

    # ---------- fetch ----------

    @staticmethod
    def _ext(url: str) -> Optional[str]:
        clean = (url or "").lower().split("?", 1)[0]
        for ext in SAFE_EXTS:
            if clean.endswith(ext):
                return ".jpg" if ext == ".jpeg" else ext
        return None

    def _active_sources(self) -> list:
        """Boorus (shuffled) first, then optional fallbacks: Flickr, Reddit."""
        boorus = [self._src_safebooru, self._src_konachan]
        random.shuffle(boorus)
        sources = list(boorus)
        if self._get("flickr_key", ""):
            sources.append(self._src_flickr)
        if self._get("reddit", True):
            sources.append(self._src_reddit)
        return sources

    async def _pick_post(self) -> dict:
        """Return a fresh, unseen, safe canonical post with an added 'ext' field."""
        history = set(self._get("history", []) or [])
        errors = []
        for src in self._active_sources():
            name = src.__name__
            try:
                posts = await src()
            except Exception as exc:
                errors.append(f"{name}:{type(exc).__name__}")
                logger.warning("[FumoQoder] source %s failed: %r", name, exc)
                continue
            random.shuffle(posts)
            for p in posts:
                ext = self._ext(p["url"])
                if not ext or p["id"] in history:
                    continue
                p["ext"] = ext
                self._push_history(p["id"])
                return p
            errors.append(f"{name}:no-fresh")
        raise RuntimeError("all sources failed: " + ", ".join(errors) if errors else "no fumo found")

    def _push_history(self, post_id: str):
        history = self._get("history", []) or []
        history.append(post_id)
        self._set("history", history[-MAX_HISTORY:])

    async def _download(self, url: str) -> bytes:
        session = await self._http()
        async with session.get(url, headers={
            "User-Agent": USER_AGENT,
            "Referer": "/".join(url.split("/")[:3]) + "/",
        }, proxy=self._proxy()) as resp:
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
        post = None
        last_err = None
        for _ in range(3):
            post = await self._pick_post()
            try:
                data = await self._download(post["url"])
                break
            except Exception as exc:
                last_err = exc
                logger.warning("[FumoQoder] download failed for %s: %r", post["url"], exc)
        else:
            raise RuntimeError(f"download failed: {last_err}")

        logger.info("[FumoQoder] RAW POST %r", post)

        file = BytesIO(data)
        file.name = f"fumo_{post['id']}{post['ext']}"
        msg = await self.client.send_file(
            entity, file, caption=self._caption(post), mime_type=SAFE_EXTS[post["ext"]],
        )

        now = time.time()
        self._set("last_post_ts", now)
        self._set("last_source", post["src"])
        self._set("last_url", post["url"])
        self._next_post = now + self._get("interval", DEFAULT_INTERVAL)
        logger.info(
            "[FumoQoder] SENT src=%s id=%s bytes=%d target=%s msg_id=%s",
            post["src"], post["id"], len(data),
            getattr(entity, "id", entity), getattr(msg, "id", "?"),
        )
        return post

    def _caption(self, post: dict) -> str:
        now = datetime.now(timezone.utc)
        # Telegram parses captions as HTML, so every dynamic value must be escaped:
        # booru post URLs contain bare '&' (?page=post&s=view&id=..), which Telegram
        # rejects with "Failed to parse message" unless it is '&amp;'.
        vals = {
            "{source}": utils.escape_html(post["src"]),
            "{post}": utils.escape_html(post.get("post_url", "")),
            "{url}": utils.escape_html(post["url"]),
            "{tags}": utils.escape_html(post.get("tags", "")),
            "{id}": utils.escape_html(post["id"]),
            "{date}": now.strftime("%Y-%m-%d"),
            "{time}": now.strftime("%H:%M:%S"),
        }

        def render(t: str) -> str:
            for k, v in vals.items():
                t = t.replace(k, str(v))
            return t

        parts = []
        custom = render(self._get("caption", "") or "")
        if custom:
            parts.append(custom)
        if self._get("meta", True):
            parts.append(
                f"\U0001F338 {vals['{source}']} \u2022 \U0001F3F7 {vals['{tags}']}\n"
                f"\U0001F517 {vals['{post}']}"
            )
        return "\n\n".join(parts)

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
                    logger.warning("[FumoQoder] FloodWait %ss", exc.seconds)
                    self._next_post = time.time() + exc.seconds + 30
                except (ChatWriteForbiddenError, ChatAdminRequiredError) as exc:
                    logger.warning("[FumoQoder] cannot write to target: %r", exc)
                    self._next_post = time.time() + 300
                except Exception as exc:
                    logger.error("[FumoQoder] autopost failed: %r", exc)
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
            await utils.answer(message, "\U0001F7E2 <b>Autoposting enabled.</b>")
        elif args in ("off", "0", "disable"):
            self._set("enabled", False)
            await utils.answer(message, "\U0001F534 <b>Autoposting disabled.</b>")
        else:
            state = "enabled" if self._get("enabled", False) else "disabled"
            await utils.answer(
                message, f"<b>Usage:</b> <code>.fumo on|off</code>\n<b>Current:</b> {state}"
            )

    @loader.command(
        ru_doc="Установить чат: .fumotarget @name|id, ответ, или пусто для текущего",
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
                    message, f"\u274C <b>Cannot resolve target:</b>\n"
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
            message, f"\U0001F3AF <b>Target:</b> <code>{utils.escape_html(str(title))}</code>"
        )

    @loader.command(
        ru_doc="Интервал: .fumointerval 30m / 2h / 90 (=минуты)",
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
            await utils.answer(message, "\u26A0\uFE0F <b>Example:</b> <code>.fumointerval 30m</code>")
            return
        if not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
            await utils.answer(message, "\u26A0\uFE0F <b>Interval: 1 minute .. 7 days.</b>")
            return
        self._set("interval", seconds)
        self._next_post = self._get("last_post_ts", 0.0) + seconds
        await utils.answer(message, f"\u23F1 <b>Interval:</b> <code>{fmt_interval(seconds)}</code>")

    @loader.command(
        ru_doc="Свой текст подписи (плейсхолдеры {source}{post}{url}{tags}{date}{time}); пусто — очистить",
        en_doc="Custom caption text (placeholders allowed); empty clears it",
    )
    async def fumocaption(self, message):
        """Custom caption text; placeholders {source}{post}{url}{tags}{id}{date}{time}"""
        caption = utils.get_args_raw(message)
        self._set("caption", caption)
        await utils.answer(
            message, "\U0001F4DD <b>Caption updated.</b>" if caption
            else "\U0001F4DD <b>Caption cleared.</b>"
        )

    @loader.command(
        ru_doc="Вкл/выкл авто-футер (источник, теги, ссылка) под картинкой",
        en_doc="Toggle auto metadata footer under the image",
    )
    async def fumometa(self, message):
        """Toggle the auto metadata footer (source, tags, post link)"""
        args = utils.get_args_raw(message).lower()
        if args in ("on", "1", "enable"):
            self._set("meta", True)
        elif args in ("off", "0", "disable"):
            self._set("meta", False)
        else:
            state = "on" if self._get("meta", True) else "off"
            await utils.answer(message, f"<b>Meta footer:</b> {state}\n<code>.fumometa on|off</code>")
            return
        state = "on" if self._get("meta", True) else "off"
        await utils.answer(message, f"\U0001F4DD <b>Meta footer:</b> {state}")

    @loader.command(
        ru_doc="Теги по источникам: .fumotags <safebooru|konachan|flickr> <теги>; без аргументов — показать",
        en_doc="Per-source tags: .fumotags <source> <tags>; no args shows current",
    )
    async def fumotags(self, message):
        """Per-source tags: .fumotags <source> <tags>; no args shows all"""
        raw = utils.get_args_raw(message).strip()
        parts = raw.split(None, 1)
        all_src = BOORU_SOURCES + ("flickr",)
        if not parts:
            lines = [f"<code>{s}</code>: {utils.escape_html(self._tags_for(s))}" for s in all_src]
            await utils.answer(message, "\U0001F3F7 <b>Tags per source:</b>\n" + "\n".join(lines))
            return
        source = parts[0].lower()
        if source not in all_src:
            await utils.answer(
                message, f"\u26A0\uFE0F <b>Unknown source.</b> Use: <code>{', '.join(all_src)}</code>"
            )
            return
        if len(parts) == 1:
            await utils.answer(
                message, f"\U0001F3F7 <b>{source}:</b> <code>{utils.escape_html(self._tags_for(source))}</code>"
            )
            return
        self._set(f"tags_{source}", parts[1])
        await utils.answer(
            message, f"\U0001F3F7 <b>{source} tags:</b> <code>{utils.escape_html(parts[1])}</code>"
        )

    @loader.command(
        ru_doc="Блэклист минус-тегов для booru: .fumoexclude nude gore (пусто — очистить)",
        en_doc="Global minus-tag blacklist for boorus: .fumoexclude nude gore",
    )
    async def fumoexclude(self, message):
        """Global minus-tag blacklist for boorus: .fumoexclude nude gore (empty clears)"""
        raw = utils.get_args_raw(message).strip()
        self._set("exclude", raw)
        shown = utils.escape_html(raw) if raw else "\u2014"
        await utils.answer(message, f"\U0001F6AB <b>Exclude:</b> <code>{shown}</code>")

    @loader.command(
        ru_doc="Вкл/выкл Reddit как страховочный источник: .fumoreddit on/off",
        en_doc="Toggle Reddit fallback source: .fumoreddit on/off",
    )
    async def fumoreddit(self, message):
        """Toggle the Reddit fallback source"""
        args = utils.get_args_raw(message).lower()
        if args in ("on", "1", "enable"):
            self._set("reddit", True)
        elif args in ("off", "0", "disable"):
            self._set("reddit", False)
        else:
            state = "on" if self._get("reddit", True) else "off"
            await utils.answer(message, f"<b>Reddit:</b> {state}\n<code>.fumoreddit on|off</code>")
            return
        state = "on" if self._get("reddit", True) else "off"
        await utils.answer(message, f"\U0001F4E1 <b>Reddit fallback:</b> {state}")

    @loader.command(
        ru_doc="Сабреддит: .fumosub Fumofumo (пусто — сброс)",
        en_doc="Set subreddit: .fumosub Fumofumo (empty resets)",
    )
    async def fumosub(self, message):
        """Set the subreddit for the Reddit source (default Fumofumo)"""
        sub = utils.get_args_raw(message).strip().replace("r/", "").replace("/", "")
        self._set("subreddit", sub or DEFAULT_SUBREDDIT)
        await utils.answer(
            message, f"\U0001F4E1 <b>Subreddit:</b> <code>r/{utils.escape_html(sub or DEFAULT_SUBREDDIT)}</code>"
        )

    @loader.command(
        ru_doc="Flickr API-ключ: .fumoflickrkey <key> (пусто — выключить Flickr)",
        en_doc="Set Flickr API key: .fumoflickrkey <key> (empty disables Flickr)",
    )
    async def fumoflickrkey(self, message):
        """Set the Flickr API key (Flickr stays off until a key is set)"""
        key = utils.get_args_raw(message).strip()
        self._set("flickr_key", key)
        await utils.answer(
            message, "\U0001F4F7 <b>Flickr enabled.</b>" if key
            else "\U0001F4F7 <b>Flickr disabled (no key).</b>"
        )

    @loader.command(
        ru_doc="Прокси для трафика модуля: .fumoproxy socks5://127.0.0.1:40000 (пусто — выключить)",
        en_doc="Proxy for module traffic: .fumoproxy socks5://127.0.0.1:40000 (empty disables)",
    )
    async def fumoproxy(self, message):
        """HTTP/SOCKS5 proxy for all module requests (e.g. local WARP socks5)"""
        proxy = utils.get_args_raw(message).strip()
        if proxy and not proxy.startswith(("http://", "https://", "socks5://", "socks5h://", "socks4://")):
            await utils.answer(
                message, "\u26A0\uFE0F <b>Proxy URL must start with http://, https:// or socks5://</b>"
            )
            return
        self._set("proxy", proxy)
        await utils.answer(
            message, f"\U0001F310 <b>Proxy:</b> <code>{utils.escape_html(proxy)}</code>" if proxy
            else "\U0001F310 <b>Proxy disabled.</b>"
        )

    @loader.command(
        ru_doc="Отправить одно фумо в текущий чат прямо сейчас",
        en_doc="Post one fumo into the current chat right now",
    )
    async def fumotest(self, message):
        """Post one fumo into the current chat right now"""
        status = await utils.answer(message, "\U0001F50E <b>Fetching fumo...</b>")
        try:
            post = await self._send(await message.get_chat())
        except FloodWaitError as exc:
            await utils.answer(status, f"\U0001F40C <b>FloodWait:</b> <code>{exc.seconds}s</code>")
            return
        except Exception as exc:
            logger.error("[FumoQoder] fumotest failed: %r", exc)
            await utils.answer(
                status, f"\u274C <b>Failed:</b>\n<code>{utils.escape_html(str(exc))}</code>"
            )
            return
        await utils.answer(status, f"\u2705 <b>Sent.</b> <i>Source:</i> {post['src']}")

    @loader.command(
        ru_doc="Показать все настройки и активные источники",
        en_doc="Show all settings and enabled sources",
    )
    async def fumostatus(self, message):
        """Show all settings and which sources are active"""
        target = self._get("target", None)
        target_str = str(target) if target else "Saved Messages"
        last_time = self._get("last_post_ts", 0)
        last_str = (
            datetime.fromtimestamp(last_time, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if last_time else "never"
        )
        if self._get("enabled", False):
            remain = max(0, int(self._next_post - time.time()))
            state = f"\U0001F7E2 enabled, next post in <code>{fmt_interval(remain)}</code>"
        else:
            state = "\U0001F534 disabled"

        active = ["safebooru", "konachan"]
        if self._get("flickr_key", ""):
            active.append("flickr")
        if self._get("reddit", True):
            active.append(f"reddit(r/{self._get('subreddit', DEFAULT_SUBREDDIT)})")

        await utils.answer(
            message,
            "\U0001F338 <b>FumoQoder</b>\n\n"
            f"<b>Status:</b> {state}\n"
            f"<b>Target:</b> <code>{utils.escape_html(target_str)}</code>\n"
            f"<b>Interval:</b> <code>{fmt_interval(self._get('interval', DEFAULT_INTERVAL))}</code>\n"
            f"<b>Sources:</b> <code>{utils.escape_html(', '.join(active))}</code>\n"
            f"<b>Meta footer:</b> {'on' if self._get('meta', True) else 'off'}\n"
            f"<b>Caption:</b> <code>{utils.escape_html(self._get('caption', '') or '—')}</code>\n"
            f"<b>Exclude:</b> <code>{utils.escape_html(self._exclude() or '—')}</code>\n"
            f"<b>Proxy:</b> <code>{utils.escape_html(self._get('proxy', '') or '—')}</code>\n"
            f"<b>Last post:</b> <code>{last_str}</code> "
            f"({utils.escape_html(self._get('last_source', '') or '—')})",
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
