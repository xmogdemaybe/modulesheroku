# -*- coding: utf-8 -*-
# requires: aiohttp
# FumoQoder — random Touhou fumo autoposter for Hikka / Heroku.
#
# Sources (tried in random order, then fallbacks):
#   Safebooru / Konachan             -> boorus (safe-only)
#   Reddit (r/Fumofumo)              -> fallback, anonymous or OAuth (.fumoredditauth)
#   Flickr                           -> optional, only if API key is set (safe_search=1)
#
# Commands:
#   .fumo on|off                 toggle autoposting
#   .fumotarget [chat]           set target (arg / reply / current chat)
#   .fumointerval 30m|2h|90      set interval (minutes by default), 1m..7d
#   .fumocount <1-10>            how many pictures to send at once (album)
#   .fumocaption <text>          custom caption text (placeholders allowed)
#   .fumometa on|off             toggle auto metadata footer in caption
#   .fumotags [source] [tags]    per-source search tags (show if no args)
#   .fumoexclude <tags>          global minus-tag blacklist for boorus
#   .fumoreddit on|off           toggle Reddit fallback source
#   .fumoredditmedia pics|videos|all   what to fetch from Reddit
#   .fumoredditauth <id> <secret>      Reddit OAuth app credentials (403 fallback)
#   .fumosub <subreddit>         set subreddit (default Fumofumo)
#   .fumoflickrkey <key>         set Flickr API key (empty disables Flickr)
#   .fumotest [source] [count]   post fumo now (optionally from one source)
#   .fumostatus                  show all settings + enabled sources
#   .fumohelp                    command list with examples
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
EXT_MIME = {**SAFE_EXTS, ".mp4": "video/mp4"}
MAX_COUNT = 10

# Sensible per-source defaults (Safebooru uses fumo_(doll); Konachan needs a
# broader tag). Override any of them with: .fumotags <source> <tags>
DEFAULT_TAGS = {
    "safebooru": "fumo_(doll)",
    "konachan": "touhou doll",
    "flickr": "fumo",
}
BOORU_SOURCES = ("safebooru", "konachan")
DEFAULT_SUBREDDIT = "Fumofumo"
SOURCE_MAP = {
    "safebooru": "_src_safebooru",
    "konachan": "_src_konachan",
    "reddit": "_src_reddit",
    "flickr": "_src_flickr",
}


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
        async with session.get(url, params=params, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    # ---------- tag config ----------

    def _tags_for(self, source: str) -> str:
        return (self._get(f"tags_{source}", DEFAULT_TAGS.get(source, "fumo")) or "").strip()

    def _exclude(self) -> str:
        return (self._get("exclude", "") or "").strip()

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

    async def _reddit_token(self) -> str:
        """App-only OAuth token (client_credentials), cached in memory until expiry."""
        now = time.time()
        token = getattr(self, "_rd_token", None)
        if token and now < getattr(self, "_rd_token_exp", 0) - 60:
            return token
        session = await self._http()
        auth = aiohttp.BasicAuth(self._get("reddit_id", ""), self._get("reddit_secret", ""))
        async with session.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=auth,
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": REDDIT_USER_AGENT},
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        self._rd_token = payload["access_token"]
        self._rd_token_exp = now + int(payload.get("expires_in", 3600))
        return self._rd_token

    async def _src_reddit(self) -> list:
        sub = self._get("subreddit", DEFAULT_SUBREDDIT) or DEFAULT_SUBREDDIT
        media = self._get("reddit_media", "pics")
        params = {"limit": "50", "raw_json": "1"}
        payload = None

        # OAuth first when credentials are set: anonymous JSON is 403-blocked on
        # some networks (e.g. RU), an app-only token goes through oauth.reddit.com.
        if self._get("reddit_id", "") and self._get("reddit_secret", ""):
            try:
                token = await self._reddit_token()
                payload = await self._json(
                    f"https://oauth.reddit.com/r/{sub}/hot", params,
                    headers={
                        "User-Agent": REDDIT_USER_AGENT,
                        "Authorization": f"Bearer {token}",
                    },
                )
            except Exception as exc:
                logger.warning("[FumoQoder] reddit oauth failed: %r", exc)

        if payload is None:
            payload = await self._json(
                f"https://www.reddit.com/r/{sub}/hot.json", params,
                headers={"User-Agent": REDDIT_USER_AGENT},
            )

        children = (payload or {}).get("data", {}).get("children", []) if isinstance(payload, dict) else []
        posts = []
        for c in children:
            d = c.get("data", {}) if isinstance(c, dict) else {}
            img = d.get("url_overridden_by_dest") or d.get("url") or ""
            video = ((d.get("secure_media") or {}).get("reddit_video") or {}).get("fallback_url") or ""
            is_video = bool(video) and media in ("videos", "all")
            if is_video:
                img = video
            ext = self._ext(img)
            if not ext:
                continue
            if media == "pics" and (video or ext == ".mp4"):
                continue
            if media == "videos" and not is_video:
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
        for ext in EXT_MIME:
            if clean.endswith(ext):
                return ".jpg" if ext == ".jpeg" else ext
        return None

    def _source_by_name(self, name: str):
        return getattr(self, SOURCE_MAP[name.lower()])

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

    async def _pick_posts(self, count: int = 1, source: Optional[str] = None) -> list:
        """Return up to `count` fresh, unseen posts with an added 'ext' field.

        Raises only when nothing at all could be picked; a partial result is fine.
        """
        history = set(self._get("history", []) or [])
        picked, errors = [], []
        sources = [self._source_by_name(source)] if source else self._active_sources()
        for src in sources:
            if len(picked) >= count:
                break
            name = src.__name__
            added = 0
            try:
                posts = await src()
            except Exception as exc:
                errors.append(f"{name}:{type(exc).__name__}")
                logger.warning("[FumoQoder] source %s failed: %r", name, exc)
                continue
            random.shuffle(posts)
            chosen_ids = {p["id"] for p in picked}
            for p in posts:
                if len(picked) >= count:
                    break
                ext = self._ext(p["url"])
                if not ext or p["id"] in history or p["id"] in chosen_ids:
                    continue
                p["ext"] = ext
                chosen_ids.add(p["id"])
                self._push_history(p["id"])
                picked.append(p)
                added += 1
            if not added:
                errors.append(f"{name}:no-fresh")
        if not picked:
            raise RuntimeError("all sources failed: " + ", ".join(errors) if errors else "no fumo found")
        return picked

    def _push_history(self, post_id: str):
        history = self._get("history", []) or []
        history.append(post_id)
        self._set("history", history[-MAX_HISTORY:])

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

    async def _send(self, entity, source: Optional[str] = None, count: Optional[int] = None) -> list:
        if count is None:
            count = self._get("count", 1)
        count = max(1, min(MAX_COUNT, int(count or 1)))
        posts = await self._pick_posts(count, source)

        files, sent = [], []
        last_err = None
        for post in posts:
            data = None
            for _ in range(3):
                try:
                    data = await self._download(post["url"])
                    break
                except Exception as exc:
                    last_err = exc
                    logger.warning("[FumoQoder] download failed for %s: %r", post["url"], exc)
            if data is None:
                continue
            logger.info("[FumoQoder] RAW POST %r", post)
            file = BytesIO(data)
            file.name = f"fumo_{post['id']}{post['ext']}"
            files.append(file)
            sent.append(post)
        if not files:
            raise RuntimeError(f"download failed: {last_err}")

        kwargs = {}
        if len(files) == 1:
            kwargs["mime_type"] = EXT_MIME[sent[0]["ext"]]
        msg = await self.client.send_file(
            entity, files[0] if len(files) == 1 else files,
            caption=self._caption(sent), **kwargs,
        )

        now = time.time()
        self._set("last_post_ts", now)
        self._set("last_source", sent[0]["src"])
        self._set("last_url", sent[0]["url"])
        self._next_post = now + self._get("interval", DEFAULT_INTERVAL)
        logger.info(
            "[FumoQoder] SENT n=%d src=%s id=%s target=%s msg_id=%s",
            len(files), sent[0]["src"], sent[0]["id"],
            getattr(entity, "id", entity), getattr(msg, "id", "?"),
        )
        return sent

    def _caption(self, posts) -> str:
        if isinstance(posts, dict):
            posts = [posts]
        first = posts[0]
        now = datetime.now(timezone.utc)
        # Telegram parses captions as HTML, so every dynamic value must be escaped:
        # booru post URLs contain bare '&' (?page=post&s=view&id=..), which Telegram
        # rejects with "Failed to parse message" unless it is '&amp;'.
        vals = {
            "{source}": utils.escape_html(first["src"]),
            "{post}": utils.escape_html(first.get("post_url", "")),
            "{url}": utils.escape_html(first["url"]),
            "{tags}": utils.escape_html(first.get("tags", "")),
            "{id}": utils.escape_html(first["id"]),
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
            if len(posts) == 1:
                parts.append(
                    f"\U0001F338 {vals['{source}']} \u2022 \U0001F3F7 {vals['{tags}']}\n"
                    f"\U0001F517 {vals['{post}']}"
                )
            else:
                lines = [
                    f"{i}. \U0001F338 {utils.escape_html(p['src'])} "
                    f"\U0001F517 {utils.escape_html(p.get('post_url', ''))}"
                    for i, p in enumerate(posts, 1)
                ]
                parts.append("\n".join(lines))
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
        ru_doc="Сколько картинок за одну отправку: .fumocount 1-10",
        en_doc="How many pictures per send: .fumocount 1-10",
    )
    async def fumocount(self, message):
        """How many pictures to send at once (album), 1..10"""
        args = utils.get_args_raw(message).strip()
        try:
            count = int(args)
        except ValueError:
            await utils.answer(message, "\u26A0\uFE0F <b>Example:</b> <code>.fumocount 3</code>")
            return
        if not 1 <= count <= MAX_COUNT:
            await utils.answer(message, f"\u26A0\uFE0F <b>Count: 1 .. {MAX_COUNT}.</b>")
            return
        self._set("count", count)
        await utils.answer(message, f"\U0001F5BC <b>Per send:</b> <code>{count}</code>")

    @loader.command(
        ru_doc="Медиа с Reddit: .fumoredditmedia pics|videos|all",
        en_doc="Reddit media type: .fumoredditmedia pics|videos|all",
    )
    async def fumoredditmedia(self, message):
        """What to fetch from Reddit: pics, videos or all"""
        args = utils.get_args_raw(message).strip().lower()
        if args not in ("pics", "videos", "all"):
            state = self._get("reddit_media", "pics")
            await utils.answer(
                message, f"<b>Reddit media:</b> {state}\n"
                "<code>.fumoredditmedia pics|videos|all</code>"
            )
            return
        self._set("reddit_media", args)
        await utils.answer(message, f"\U0001F4E1 <b>Reddit media:</b> <code>{args}</code>")

    @loader.command(
        ru_doc="Reddit OAuth: .fumoredditauth <client_id> <client_secret> (пусто — сброс)",
        en_doc="Reddit OAuth app credentials: .fumoredditauth <client_id> <client_secret>",
    )
    async def fumoredditauth(self, message):
        """Reddit app-only OAuth credentials; used when anonymous JSON is blocked (403)"""
        parts = utils.get_args_raw(message).split()
        if not parts:
            self._set("reddit_id", "")
            self._set("reddit_secret", "")
            self._rd_token = None
            await utils.answer(message, "\U0001F4E1 <b>Reddit auth cleared (anonymous mode).</b>")
            return
        if len(parts) != 2:
            await utils.answer(
                message, "\u26A0\uFE0F <b>Usage:</b> <code>.fumoredditauth &lt;client_id&gt; &lt;client_secret&gt;</code>"
            )
            return
        self._set("reddit_id", parts[0])
        self._set("reddit_secret", parts[1])
        self._rd_token = None
        await utils.answer(
            message, f"\U0001F4E1 <b>Reddit auth set</b> (id <code>{utils.escape_html(parts[0])}</code>). "
            "Testing token...\n" + await self._test_reddit_token()
        )

    async def _test_reddit_token(self) -> str:
        try:
            await self._reddit_token()
            return "\u2705 <b>Token OK.</b>"
        except Exception as exc:
            logger.warning("[FumoQoder] reddit token test failed: %r", exc)
            return f"\u274C <b>Token failed:</b> <code>{utils.escape_html(repr(exc))}</code>"

    @loader.command(
        ru_doc="Отправить фумо сейчас: .fumotest [safebooru|konachan|reddit|flickr] [count]",
        en_doc="Post fumo now: .fumotest [source] [count]",
    )
    async def fumotest(self, message):
        """Post fumo right now; optional source (safebooru|konachan|reddit|flickr) and count"""
        args = utils.get_args_raw(message).split()
        source, count = None, None
        for a in args[:2]:
            if a.lower() in SOURCE_MAP:
                source = a.lower()
            elif a.isdigit():
                count = int(a)
            else:
                await utils.answer(
                    message, "\u26A0\uFE0F <b>Usage:</b> <code>.fumotest [safebooru|konachan|"
                    "reddit|flickr] [count]</code>"
                )
                return
        status = await utils.answer(message, "\U0001F50E <b>Fetching fumo...</b>")
        try:
            sent = await self._send(await message.get_chat(), source=source, count=count)
        except FloodWaitError as exc:
            await utils.answer(status, f"\U0001F40C <b>FloodWait:</b> <code>{exc.seconds}s</code>")
            return
        except Exception as exc:
            logger.error("[FumoQoder] fumotest failed (source=%s): %r", source, exc)
            await utils.answer(
                status, f"\u274C <b>Failed{' (' + source + ')' if source else ''}:</b>\n"
                f"<code>{utils.escape_html(str(exc))}</code>"
            )
            return
        srcs = ", ".join(dict.fromkeys(p["src"] for p in sent))
        await utils.answer(
            status, f"\u2705 <b>Sent {len(sent)}.</b> <i>Source:</i> {utils.escape_html(srcs)}"
        )

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
            f"<b>Per send:</b> <code>{self._get('count', 1)}</code>\n"
            f"<b>Sources:</b> <code>{utils.escape_html(', '.join(active))}</code>\n"
            f"<b>Reddit media:</b> <code>{utils.escape_html(self._get('reddit_media', 'pics'))}</code>\n"
            f"<b>Reddit auth:</b> {'set' if self._get('reddit_id', '') else 'anonymous'}\n"
            f"<b>Meta footer:</b> {'on' if self._get('meta', True) else 'off'}\n"
            f"<b>Caption:</b> <code>{utils.escape_html(self._get('caption', '') or '—')}</code>\n"
            f"<b>Exclude:</b> <code>{utils.escape_html(self._exclude() or '—')}</code>\n"
            f"<b>Last post:</b> <code>{last_str}</code> "
            f"({utils.escape_html(self._get('last_source', '') or '—')})",
        )

    @loader.command(
        ru_doc="Список команд FumoQoder с примерами",
        en_doc="FumoQoder command list with examples",
    )
    async def fumohelp(self, message):
        """Show all FumoQoder commands with examples"""
        await utils.answer(
            message,
            "\U0001F338 <b>FumoQoder — commands</b>\n\n"
            "<b>Autoposting</b>\n"
            "<code>.fumo on</code> / <code>.fumo off</code> — enable/disable\n"
            "<code>.fumotarget @mychannel</code> — where to post (or reply / empty = current chat)\n"
            "<code>.fumointerval 30m</code> — how often (30m / 2h / 90 = minutes)\n"
            "<code>.fumocount 3</code> — how many pictures per send (1..10, album)\n\n"
            "<b>Caption</b>\n"
            "<code>.fumocaption Fumo time! {source}</code> — custom text "
            "({source} {post} {url} {tags} {id} {date} {time}); empty = clear\n"
            "<code>.fumometa off</code> — hide source/tags/link footer\n\n"
            "<b>Sources & tags</b>\n"
            "<code>.fumotags</code> — show tags per source\n"
            "<code>.fumotags safebooru fumo_(doll) touhou</code> — set tags for one source\n"
            "<code>.fumoexclude nude gore</code> — minus-tag blacklist for boorus\n"
            "<code>.fumoflickrkey &lt;key&gt;</code> — enable Flickr (empty = off)\n\n"
            "<b>Reddit</b>\n"
            "<code>.fumoreddit on|off</code> — fallback source\n"
            "<code>.fumosub Fumofumo</code> — subreddit\n"
            "<code>.fumoredditmedia pics|videos|all</code> — what to fetch\n"
            "<code>.fumoredditauth &lt;client_id&gt; &lt;client_secret&gt;</code> — OAuth app "
            "(saves you when anonymous access is 403-blocked); empty = clear\n\n"
            "<b>Misc</b>\n"
            "<code>.fumotest</code> — post now\n"
            "<code>.fumotest reddit 2</code> — post 2 from a specific source\n"
            "<code>.fumostatus</code> — all settings\n",
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
