# -*- coding: utf-8 -*-
# requires: aiohttp curl_cffi
# FumoQoder — random Touhou fumo autoposter for Hikka / Heroku.
#
# Sources (all fetched together and mixed, unless a single one is requested):
#   Safebooru / Konachan             -> boorus (safe-only)
#   Reddit (r/Fumofumo)              -> toggleable source; listing via direct JSON or
#                                       the Arctic Shift archive (.fumoredditmode)
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
#   .fumokonachan net|com        konachan.net (safe) / konachan.com (R-18, filtered)
#   .fumoreddit on|off           toggle the Reddit source
#   .fumoredditmedia pics|videos|all   what to fetch from Reddit
#   .fumoredditauth <id> <secret>      Reddit OAuth app credentials (403 fallback)
#   .fumoredditcookie <cookie>         browser cookie for Reddit (403 fallback)
#   .fumoredditmode auto|json|arctic   listing via direct JSON or Arctic Shift archive
#   .fumosub <subreddit>         set subreddit (default Fumofumo)
#   .fumoschedule on|off         use Telegram scheduled messages for autoposting
#   .fumotime 07:00|off          wall-clock publish time for scheduled posts (local)
#   .fumonum [N]                 post counter (#N in caption); set your own number
#   .fumotest [source] [count]   post fumo now (optionally from one source)
#   .fumoreset                   clear sent-images history (settings are kept)
#   .fumostatus                  show all settings + enabled sources
#   .fumohelp                    command list with examples
#
# Caption placeholders: {source} {post} {url} {tags} {id} {num} {date} {time}

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Optional

import aiohttp

try:
    # Reddit's anti-scraper checks the TLS fingerprint (JA3) + HTTP/2, which
    # aiohttp cannot fake; curl_cffi impersonates a real Chrome down to TLS.
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

from telethon.errors import (
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
)

try:
    from telethon.tl.functions.messages import GetScheduledHistoryRequest
except ImportError:  # very old forks
    GetScheduledHistoryRequest = None

from telethon import utils as tl_utils

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
# 2026: Reddit 403s non-browser clients on listing endpoints even with valid
# cookies (anti-AI-scraper), so the JSON path mimics a real Chrome navigation.
REDDIT_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Cache-Control": "max-age=0",
    "Priority": "u=0, i",
    "Sec-Ch-Ua": '"Chromium";v="154", "Google Chrome";v="154", "Not A(Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

SAFE_EXTS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
EXT_MIME = {**SAFE_EXTS, ".mp4": "video/mp4"}
MAX_COUNT = 10
# Telegram rejects photos larger than 10 MB (UploadPhoto); such images are sent
# as documents instead. Albums also can't mix photos with stickers (.webp) or
# GIFs, so only these extensions are eligible for a multi-file album.
MAX_PHOTO_SIZE = 10 * 1024 * 1024
PHOTO_EXTS = {".jpg", ".png"}
ALBUM_OK_EXTS = {".jpg", ".png", ".mp4"}
CAPTION_LIMIT = 1024

# Sensible per-source defaults (Safebooru uses fumo_(doll); Konachan needs a
# broader tag). Override any of them with: .fumotags <source> <tags>
DEFAULT_TAGS = {
    "safebooru": "fumo_(doll)",
    "konachan": "touhou doll",
}
BOORU_SOURCES = ("safebooru", "konachan")
DEFAULT_SUBREDDIT = "Fumofumo"
SOURCE_MAP = {
    "safebooru": "_src_safebooru",
    "konachan": "_src_konachan",
    "reddit": "_src_reddit",
}


@loader.tds
class FumoQoder(loader.Module):
    """Random Touhou fumo autoposter (Safebooru / Konachan / Reddit, mixed)"""

    strings = {"name": "FumoQoder"}

    async def client_ready(self, client, db):
        self.client = client
        self.db = db
        self._session: Optional[aiohttp.ClientSession] = None
        self._stopping = False
        self._inflight: set = set()
        # On a fresh install last_post_ts is 0; anchor to now so enabling autopost
        # doesn't instantly fire a post before the user has set a target.
        last_ts = self._get("last_post_ts", 0.0) or time.time()
        if self._get("schedule_mode", False):
            # Check Telegram's real queue on the first tick (it won't double-post:
            # _tick_schedule no-ops while a future post is already queued).
            self._next_post = 0
        else:
            self._next_post = last_ts + self._get("interval", DEFAULT_INTERVAL)
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
        # konachan.net = safe-only mirror, konachan.com = R-18 (filtered by rating).
        domain = self._get("konachan_domain", "konachan.net") or "konachan.net"
        tags = self._booru_tags("konachan")
        for page in self._page_candidates(3, 1):
            posts = []
            for p in await self._moebooru(f"https://{domain}/post.json", tags, page):
                if not isinstance(p, dict) or not p.get("file_url"):
                    continue
                if domain != "konachan.net" and p.get("rating") != "s":
                    continue
                url = p["file_url"]
                if url.startswith("//"):
                    url = "https:" + url
                elif url.startswith("/"):
                    url = f"https://{domain}" + url
                posts.append({
                    "id": f"kn_{p.get('id')}", "url": url, "src": "Konachan",
                    "post_url": f"https://{domain}/post/show/{p.get('id')}",
                    "tags": tags,
                })
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
        if not isinstance(payload, dict) or "access_token" not in payload:
            err = payload.get("error") if isinstance(payload, dict) else payload
            raise RuntimeError(f"reddit oauth failed: {err!r}")
        self._rd_token = payload["access_token"]
        self._rd_token_exp = now + int(payload.get("expires_in", 3600))
        return self._rd_token

    def _reddit_parse(self, items: list, sub: str) -> list:
        """Shared parser for reddit post dicts (listing JSON children data or Arctic Shift rows)."""
        media = self._get("reddit_media", "pics")
        posts = []
        for d in items:
            if not isinstance(d, dict):
                continue
            # Never leak 18+ posts: the module is safe-only by design, and the user
            # can point .fumosub at any subreddit.
            if d.get("over_18"):
                continue
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

    async def _reddit_listing(self, sub: str) -> Any:
        """www hot.json via curl_cffi (real Chrome TLS fingerprint) or aiohttp fallback."""
        url = f"https://www.reddit.com/r/{sub}/hot.json"
        params = {"limit": "50", "raw_json": "1"}
        cookie = (self._get("reddit_cookie", "") or "").strip()
        if cffi_requests is not None:
            # impersonate supplies its own consistent Chrome headers; only add the cookie.
            async with cffi_requests.AsyncSession(impersonate="chrome") as session:
                resp = await session.get(
                    url, params=params,
                    headers={"Cookie": cookie} if cookie else None,
                    timeout=30,
                )
                resp.raise_for_status()
                return resp.json()
        headers = dict(REDDIT_BROWSER_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        return await self._json(url, params, headers=headers)

    async def _src_reddit_json(self, sub: str) -> list:
        """Direct Reddit listing: OAuth if creds are set, else www JSON with full browser headers (+cookie)."""
        payload = None
        if self._get("reddit_id", "") and self._get("reddit_secret", ""):
            try:
                token = await self._reddit_token()
                payload = await self._json(
                    f"https://oauth.reddit.com/r/{sub}/hot", {"limit": "50", "raw_json": "1"},
                    headers={
                        "User-Agent": REDDIT_USER_AGENT,
                        "Authorization": f"Bearer {token}",
                    },
                )
            except Exception as exc:
                logger.warning("[FumoQoder] reddit oauth failed: %r", exc)
        if payload is None:
            payload = await self._reddit_listing(sub)
        children = (payload or {}).get("data", {}).get("children", []) if isinstance(payload, dict) else []
        items = [c.get("data", {}) for c in children if isinstance(c, dict)]
        return self._reddit_parse(items, sub)

    async def _src_reddit_arctic(self, sub: str) -> list:
        """Arctic Shift (arctic-shift.photon-reddit.com): a third-party Reddit archive.

        Reddit blocks its own listing endpoints (.json/.rss/old) by IP reputation in
        some networks, but this archive is a different host, and the image CDN
        (i.redd.it) is not blocked at all — so listing via archive + download via
        CDN works where direct Reddit does not. Data is newest-first, slightly delayed.
        """
        payload = await self._json(
            "https://arctic-shift.photon-reddit.com/api/posts/search",
            {"subreddit": sub, "limit": "100", "sort": "desc"},
            headers={"User-Agent": USER_AGENT},
        )
        items = payload.get("data", []) if isinstance(payload, dict) else []
        return self._reddit_parse(items, sub)

    async def _src_reddit(self) -> list:
        sub = self._get("subreddit", DEFAULT_SUBREDDIT) or DEFAULT_SUBREDDIT
        mode = self._get("reddit_mode", "auto")
        if mode == "arctic":
            return await self._src_reddit_arctic(sub)
        try:
            posts = await self._src_reddit_json(sub)
        except Exception as exc:
            if mode == "json":
                raise
            logger.warning("[FumoQoder] reddit json failed (%r), trying Arctic Shift", exc)
            posts = []
        if posts:
            return posts
        return await self._src_reddit_arctic(sub)

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
        """Boorus plus the optional Reddit source; all are fetched and mixed."""
        boorus = [self._src_safebooru, self._src_konachan]
        random.shuffle(boorus)
        sources = list(boorus)
        if self._get("reddit", True):
            sources.append(self._src_reddit)
        return sources

    async def _pick_posts(self, count: int = 1, source: Optional[str] = None) -> list:
        """Return up to `count` fresh, unseen posts with an added 'ext' field.

        With no explicit source, every active source is fetched concurrently and
        their posts are pooled and shuffled together, so a plain `.fumotest` mixes
        Safebooru / Konachan / Reddit instead of draining one source first.

        Raises only when nothing at all could be picked; a partial result is fine.
        """
        history = set(self._get("history", []) or [])
        self._inflight = getattr(self, "_inflight", set())
        picked, errors = [], []
        sources = [self._source_by_name(source)] if source else self._active_sources()

        # Fetch all sources in parallel; a failing one is skipped, not fatal.
        results = await asyncio.gather(
            *[self._safe_fetch(src) for src in sources], return_exceptions=True
        )
        pool = []
        for src, res in zip(sources, results):
            name = src.__name__
            if isinstance(res, Exception):
                errors.append(f"{name}:{type(res).__name__}")
                logger.warning("[FumoQoder] source %s failed: %r", name, res)
                continue
            pool.extend(res)
        random.shuffle(pool)

        chosen_ids = set()
        for p in pool:
            if len(picked) >= count:
                break
            ext = self._ext(p["url"])
            pid = p["id"]
            if not ext or pid in history or pid in chosen_ids or pid in self._inflight:
                continue
            p["ext"] = ext
            chosen_ids.add(pid)
            # Reserve in memory only; the id is written to the db history in _send
            # AFTER a successful send, so a failed download/post can be retried.
            self._inflight.add(pid)
            picked.append(p)
        if not picked:
            errors.append("no-fresh")
            raise RuntimeError("all sources failed: " + ", ".join(errors) if errors else "no fumo found")
        return picked

    async def _safe_fetch(self, src):
        return await src()

    def _commit_history(self, ids: list):
        """Persist sent-post ids in a single db write (called after a successful send)."""
        ids = [i for i in ids if i]
        if not ids:
            return
        history = self._get("history", []) or []
        history.extend(ids)
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

    async def _send(self, entity, source: Optional[str] = None, count: Optional[int] = None,
                    schedule: Optional[float] = None) -> list:
        if count is None:
            count = self._get("count", 1)
        count = max(1, min(MAX_COUNT, int(count or 1)))
        posts = await self._pick_posts(count, source)
        reserved = [p["id"] for p in posts]

        # Albums can't mix photos with stickers (.webp) or GIFs — keep only the
        # Telegram-safe set for multi-file sends.
        album = count > 1
        if album:
            compatible = [p for p in posts if p["ext"] in ALBUM_OK_EXTS]
            if compatible:
                posts = compatible
                album = len(posts) > 1
            else:
                # Nothing album-safe was picked: send a single one (as a document if
                # it's .webp/oversized) rather than crashing on an invalid media group.
                posts = posts[:1]
                album = False

        try:
            files, sent, sizes = [], [], []
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
                size = len(data)
                # In an album a photo over 10 MB can't be sent and can't be turned
                # into a document (that would break the media group) — skip it.
                if album and post["ext"] in PHOTO_EXTS and size > MAX_PHOTO_SIZE:
                    logger.info("[FumoQoder] skipping %s: photo > 10MB in album", post["id"])
                    continue
                logger.debug("[FumoQoder] RAW POST %r", post)
                file = BytesIO(data)
                file.name = f"fumo_{post['id']}{post['ext']}"
                files.append(file)
                sent.append(post)
                sizes.append(size)
            if not files:
                raise RuntimeError(f"download failed: {last_err}")

            kwargs = {}
            if len(files) == 1:
                ext = sent[0]["ext"]
                # .webp is treated as a sticker (no caption) and an oversized photo
                # is rejected by UploadPhoto — send both as a document instead.
                as_doc = ext == ".webp" or (ext in PHOTO_EXTS and sizes[0] > MAX_PHOTO_SIZE)
                if not as_doc:
                    kwargs["mime_type"] = EXT_MIME.get(ext)
                kwargs["force_document"] = as_doc
            if schedule:
                # Telegram publishes it server-side at that moment, even if the userbot
                # is offline — this is the native "Scheduled Messages" feature.
                kwargs["schedule"] = datetime.fromtimestamp(schedule, timezone.utc)
            num = int(self._get("post_num", 0) or 0) + 1
            msg = await self.client.send_file(
                entity, files[0] if len(files) == 1 else files,
                caption=self._caption(sent, num), **kwargs,
            )
        finally:
            # Release the in-memory reservation regardless of outcome; only ids we
            # actually sent get persisted to history below.
            for pid in reserved:
                self._inflight.discard(pid)

        # For a scheduled post, "now" is really its future publish time.
        stamp = schedule if schedule else time.time()
        self._commit_history([p["id"] for p in sent])
        self._set("post_num", num)
        self._set("last_post_ts", stamp)
        self._set("last_source", sent[0]["src"])
        self._set("last_url", sent[0]["url"])
        self._next_post = stamp + self._get("interval", DEFAULT_INTERVAL)
        logger.info(
            "[FumoQoder] %s n=%d src=%s id=%s target=%s msg_id=%s",
            "SCHEDULED" if schedule else "SENT",
            len(files), sent[0]["src"], sent[0]["id"],
            getattr(entity, "id", entity), getattr(msg, "id", "?"),
        )
        return sent

    def _caption(self, posts, num: Optional[int] = None) -> str:
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
            "{num}": str(num if num is not None else self._get("post_num", 0)),
            "{date}": now.strftime("%Y-%m-%d"),
            "{time}": now.strftime("%H:%M:%S"),
        }

        def render(t: str) -> str:
            for k, v in vals.items():
                t = t.replace(k, str(v))
            return t

        parts = []
        # Escape the user's template too, so a literal '<' (e.g. "<3") can't break
        # Telegram's HTML caption parser; placeholders are then filled with
        # already-escaped values.
        raw_custom = self._get("caption", "") or ""
        custom = render(utils.escape_html(raw_custom))
        # Post counter: prepended unless the custom caption already uses {num}.
        if num is not None and "{num}" not in raw_custom:
            parts.append(f"<b>#{num}</b>")
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
        caption = "\n\n".join(parts)
        # Media captions are capped at 1024 chars; a 10-post album footer can exceed it.
        if len(caption) > CAPTION_LIMIT:
            caption = caption[:CAPTION_LIMIT - 1] + "\u2026"
        return caption

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
                # Gate BOTH modes on _next_post so we never hit Telegram every 15s:
                # in schedule mode a tick does get_entity + GetScheduledHistory RPCs,
                # which would otherwise run 5760 times a day and trigger FloodWait.
                if self._get("enabled", False) and time.time() >= self._next_post:
                    if self._get("schedule_mode", False):
                        await self._tick_schedule()
                    else:
                        await self._tick_live()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(15)

    async def _tick_live(self):
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

    def _parse_post_time(self):
        """Return (hh, mm) from the stored '.fumotime' value, or None if unset/invalid."""
        raw = (self._get("post_time", "") or "").strip()
        if not raw or ":" not in raw:
            return None
        try:
            hh, mm = raw.split(":", 1)
            hh, mm = int(hh), int(mm)
        except ValueError:
            return None
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
        return None

    def _next_fire(self, base: float, now: Optional[float] = None) -> float:
        """When to publish the next post, given the last (already queued) one at `base`.

        With a `.fumotime` HH:MM set, the post goes at that local wall-clock time on
        the next date it falls in the future (spacing in whole days derived from the
        interval); otherwise it is simply `base + interval`.
        """
        interval = self._get("interval", DEFAULT_INTERVAL)
        hm = self._parse_post_time()
        if hm is None:
            fire = base + interval
            if now is not None and fire <= now:
                fire = now + interval
            return fire
        hh, mm = hm
        days = max(1, int(round(interval / 86400)))
        base_dt = datetime.fromtimestamp(base, tz=timezone.utc).astimezone()
        # Start from the SAME date at HH:MM, then roll forward in whole-day steps
        # until it's strictly in the future — so an empty queue at 06:00 with
        # .fumotime 07:00 fires today at 07:00, not tomorrow.
        cand = base_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        floor = now if now is not None else base
        while cand.timestamp() <= floor:
            cand += timedelta(days=days)
        return cand.timestamp()

    async def _last_scheduled_ts(self, entity) -> float:
        """Publish time of the latest message in Telegram's Scheduled queue (0 if none).

        Reading the real queue (not just our own db) lets the module chain after posts
        the user scheduled by hand, and stays correct across reloads.
        """
        if GetScheduledHistoryRequest is None:
            return self._get("sched_pending", 0) or 0
        try:
            peer = await self.client.get_input_entity(entity)
            res = await self.client(GetScheduledHistoryRequest(peer=peer, hash=0))
            stamps = [
                m.date.timestamp() for m in getattr(res, "messages", [])
                if getattr(m, "date", None) is not None
            ]
            return max(stamps) if stamps else 0
        except Exception as exc:
            logger.warning("[FumoQoder] cannot read scheduled history: %r", exc)
            return self._get("sched_pending", 0) or 0

    async def _tick_schedule(self):
        """Keep exactly one post queued in Telegram's Scheduled Messages.

        The chain is anchored to the newest message already in the queue: while that
        publish time is still in the future we do nothing; once it has fired (or the
        queue is empty) we queue the next one via `_next_fire`. Telegram publishes it
        server-side even if the userbot is offline in between.
        """
        now = time.time()
        entity = await self._target()
        last = await self._last_scheduled_ts(entity)
        if last > now:
            # A post is already pending in the queue — just track it.
            self._set("sched_pending", last)
            self._next_post = last
            return
        fire_at = self._next_fire(last if last else now, now)
        if fire_at <= now:
            fire_at = now + self._get("interval", DEFAULT_INTERVAL)
        try:
            await self._send(entity, schedule=fire_at)
            self._set("sched_pending", fire_at)
            self._next_post = fire_at
        except FloodWaitError as exc:
            logger.warning("[FumoQoder] FloodWait %ss", exc.seconds)
            self._backoff(now + exc.seconds + 30)
        except (ChatWriteForbiddenError, ChatAdminRequiredError) as exc:
            logger.warning("[FumoQoder] cannot write to target: %r", exc)
            self._backoff(now + 300)
        except Exception as exc:
            logger.error("[FumoQoder] schedule failed: %r", exc)
            self._backoff(now + 60)

    def _backoff(self, ts: float):
        """Push the loop gate forward so a failing tick doesn't retry every 15s."""
        self._set("sched_pending", ts)
        self._next_post = ts

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
            # Store the marked peer id (-100... for channels), not the bare entity.id,
            # or get_entity() would later resolve it as a user and fail after a restart.
            stored = tl_utils.get_peer_id(entity)
        elif message.is_reply:
            reply = await message.get_reply_message()
            if reply is None:
                entity, stored = await message.get_chat(), message.chat_id
            else:
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
        except (ValueError, TypeError, OverflowError):
            await utils.answer(message, "\u26A0\uFE0F <b>Example:</b> <code>.fumointerval 30m</code>")
            return
        if not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
            await utils.answer(message, "\u26A0\uFE0F <b>Interval: 1 minute .. 7 days.</b>")
            return
        self._set("interval", seconds)
        self._next_post = (self._get("last_post_ts", 0.0) or time.time()) + seconds
        await utils.answer(message, f"\u23F1 <b>Interval:</b> <code>{fmt_interval(seconds)}</code>")

    @loader.command(
        ru_doc="Свой текст подписи (плейсхолдеры {source}{post}{url}{tags}{num}{date}{time}); пусто — очистить",
        en_doc="Custom caption text (placeholders allowed); empty clears it",
    )
    async def fumocaption(self, message):
        """Custom caption text; placeholders {source}{post}{url}{tags}{id}{num}{date}{time}"""
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
        ru_doc="Теги по источникам: .fumotags <safebooru|konachan> <теги>; без аргументов — показать",
        en_doc="Per-source tags: .fumotags <source> <tags>; no args shows current",
    )
    async def fumotags(self, message):
        """Per-source tags: .fumotags <source> <tags>; no args shows all"""
        raw = utils.get_args_raw(message).strip()
        parts = raw.split(None, 1)
        all_src = BOORU_SOURCES
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
        ru_doc="Вкл/выкл Reddit как источник (миксуется с booru): .fumoreddit on/off",
        en_doc="Toggle the Reddit source (mixed with the boorus): .fumoreddit on/off",
    )
    async def fumoreddit(self, message):
        """Toggle the Reddit source (mixed in with the boorus)"""
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
        await utils.answer(message, f"\U0001F4E1 <b>Reddit source:</b> {state}")

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
        ru_doc="Автопостинг через отложенные сообщения Telegram: .fumoschedule on/off",
        en_doc="Autopost via Telegram Scheduled Messages: .fumoschedule on/off",
    )
    async def fumoschedule(self, message):
        """Use Telegram's native Scheduled Messages: the post is queued server-side
        and published even if the userbot is offline at that moment."""
        args = utils.get_args_raw(message).lower()
        if args in ("on", "1", "enable"):
            self._set("schedule_mode", True)
            self._set("sched_pending", 0)
            self._next_post = 0  # let the gated loop queue the first post promptly
            await utils.answer(
                message, "\U0001F4C5 <b>Schedule mode ON.</b> Next posts go to "
                "Telegram's Scheduled Messages queue (published even if the bot is offline)."
            )
        elif args in ("off", "0", "disable"):
            self._set("schedule_mode", False)
            self._set("sched_pending", 0)
            self._next_post = time.time() + self._get("interval", DEFAULT_INTERVAL)
            await utils.answer(
                message, "\U0001F4C5 <b>Schedule mode OFF.</b> Posts are sent live when due. "
                "<i>Note: messages already in Telegram's Scheduled queue are not removed.</i>"
            )
        else:
            state = "on" if self._get("schedule_mode", False) else "off"
            await utils.answer(
                message, f"<b>Schedule mode:</b> {state}\n<code>.fumoschedule on|off</code>"
            )

    @loader.command(
        ru_doc="Во сколько слать отложенные посты: .fumotime 07:00 (off — сброс); время по часовому поясу машины",
        en_doc="Wall-clock time for scheduled posts: .fumotime 07:00 (off resets); machine local time",
    )
    async def fumotime(self, message):
        """Set the time of day (HH:MM, machine local time) scheduled posts are published at.

        Each next post goes on the following date at this time, chained after the last
        message already in Telegram's Scheduled queue. 'off' (or empty) clears it and
        falls back to plain interval spacing.
        """
        args = utils.get_args_raw(message).strip()
        if args.lower() in ("off", "clear", "none", ""):
            self._set("post_time", "")
            await utils.answer(
                message, "\u23F0 <b>Post time cleared.</b> Scheduled posts now use plain interval spacing."
            )
            return
        parts = args.split(":")
        ok = len(parts) == 2 and all(p.strip().isdigit() for p in parts)
        if ok:
            hh, mm = int(parts[0]), int(parts[1])
            ok = 0 <= hh <= 23 and 0 <= mm <= 59
        if not ok:
            await utils.answer(
                message, "\u26A0\uFE0F <b>Example:</b> <code>.fumotime 07:00</code> "
                "(24h format, machine local time)"
            )
            return
        val = f"{hh:02d}:{mm:02d}"
        self._set("post_time", val)
        await utils.answer(
            message, f"\u23F0 <b>Scheduled posts at</b> <code>{val}</code> (local time). "
            "Each next date after the last queued post."
        )

    @loader.command(
        ru_doc="Счётчик постов (#N в подписи): .fumonum 57 — задать своё число; без аргумента — показать",
        en_doc="Post counter (#N in caption): .fumonum 57 sets your own number; no args shows it",
    )
    async def fumonum(self, message):
        """Show or set the post counter that appears as #N at the top of each caption."""
        args = utils.get_args_raw(message).strip()
        if not args:
            num = int(self._get("post_num", 0) or 0)
            await utils.answer(
                message, f"\U0001F522 <b>Post counter:</b> <code>#{num}</code> "
                "(next post = <code>#{}</code>)\n<i>Set your own:</i> <code>.fumonum 57</code>".format(num + 1)
            )
            return
        try:
            num = int(args)
        except ValueError:
            await utils.answer(message, "\u26A0\uFE0F <b>Example:</b> <code>.fumonum 57</code>")
            return
        if num < 0:
            await utils.answer(message, "\u26A0\uFE0F <b>Number must be >= 0.</b>")
            return
        self._set("post_num", num)
        await utils.answer(
            message, f"\U0001F522 <b>Counter set to</b> <code>#{num}</code>, "
            f"next post will be <code>#{num + 1}</code>."
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
        ru_doc="Куки Reddit из браузера: .fumoredditcookie <cookie> (пусто — сброс)",
        en_doc="Browser cookies for Reddit: .fumoredditcookie <cookie> (empty clears)",
    )
    async def fumoredditcookie(self, message):
        """Cookie header for anonymous Reddit requests (copy from a logged-in browser)"""
        cookie = utils.get_args_raw(message).strip()
        self._set("reddit_cookie", cookie)
        if not cookie:
            await utils.answer(message, "\U0001F36A <b>Reddit cookie cleared.</b>")
            return
        await utils.answer(
            message, f"\U0001F36A <b>Reddit cookie set</b> ({len(cookie)} chars). "
            "Test it: <code>.fumotest reddit</code>"
        )

    @loader.command(
        ru_doc="Способ брать список постов Reddit: .fumoredditmode auto|json|arctic",
        en_doc="Reddit listing method: .fumoredditmode auto|json|arctic",
    )
    async def fumoredditmode(self, message):
        """Where to get the Reddit post list: json (direct), arctic (archive mirror), auto"""
        args = utils.get_args_raw(message).strip().lower()
        if args not in ("auto", "json", "arctic"):
            state = self._get("reddit_mode", "auto")
            await utils.answer(
                message, f"<b>Reddit mode:</b> {state}\n"
                "<code>.fumoredditmode auto|json|arctic</code>\n"
                "<i>auto = direct JSON, on failure Arctic Shift archive;\n"
                "arctic = archive only (works where Reddit listings are IP-blocked)</i>"
            )
            return
        self._set("reddit_mode", args)
        await utils.answer(message, f"\U0001F4E1 <b>Reddit mode:</b> <code>{args}</code>")

    @loader.command(
        ru_doc="Домен Konachan: .fumokonachan net|com (net — safe, com — R-18 с фильтром)",
        en_doc="Konachan domain: .fumokonachan net|com (net = safe, com = R-18 filtered)",
    )
    async def fumokonachan(self, message):
        """Switch Konachan domain: konachan.net (safe) or konachan.com (R-18, rating-filtered)"""
        args = utils.get_args_raw(message).strip().lower().replace("konachan.", "")
        if args not in ("net", "com"):
            cur = (self._get("konachan_domain", "konachan.net") or "konachan.net").split(".")[-1]
            await utils.answer(
                message, f"<b>Konachan:</b> konachan.{cur}\n<code>.fumokonachan net|com</code>"
            )
            return
        self._set("konachan_domain", f"konachan.{args}")
        note = " (safe)" if args == "net" else " (R-18, only rating:safe is fetched)"
        await utils.answer(message, f"\U0001F338 <b>Konachan:</b> <code>konachan.{args}</code>{note}")

    @loader.command(
        ru_doc="Отправить фумо сейчас: .fumotest [safebooru|konachan|reddit] [count]",
        en_doc="Post fumo now: .fumotest [source] [count]",
    )
    async def fumotest(self, message):
        """Post fumo right now; optional source (safebooru|konachan|reddit) and count"""
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
                    "reddit] [count]</code>"
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
        ru_doc="Очистить историю отправленных картинок: .fumoreset",
        en_doc="Clear the sent-images history: .fumoreset",
    )
    async def fumoreset(self, message):
        """Clear the seen-posts history so old pictures can be posted again (settings are kept)"""
        count = len(self._get("history", []) or [])
        self._set("history", [])
        await utils.answer(
            message, f"\U0001F9F9 <b>History cleared:</b> <code>{count}</code> ids. "
            "Previously sent posts can appear again."
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
            mode = " (scheduled)" if self._get("schedule_mode", False) else ""
            state = f"\U0001F7E2 enabled{mode}, next post in <code>{fmt_interval(remain)}</code>"
        else:
            state = "\U0001F534 disabled"

        active = ["safebooru", f"konachan({(self._get('konachan_domain', 'konachan.net') or 'konachan.net').split('.')[-1]})"]
        if self._get("reddit", True):
            active.append(f"reddit(r/{self._get('subreddit', DEFAULT_SUBREDDIT)})")

        await utils.answer(
            message,
            "\U0001F338 <b>FumoQoder</b>\n\n"
            f"<b>Status:</b> {state}\n"
            f"<b>Target:</b> <code>{utils.escape_html(target_str)}</code>\n"
            f"<b>Interval:</b> <code>{fmt_interval(self._get('interval', DEFAULT_INTERVAL))}</code>\n"
            f"<b>Per send:</b> <code>{self._get('count', 1)}</code>\n"
            f"<b>Post counter:</b> <code>#{int(self._get('post_num', 0) or 0)}</code>\n"
            f"<b>Schedule mode:</b> {'on' if self._get('schedule_mode', False) else 'off'}\n"
            f"<b>Post time:</b> <code>{utils.escape_html(self._get('post_time', '') or '—')}</code>\n"
            f"<b>Sources:</b> <code>{utils.escape_html(', '.join(active))}</code>\n"
            f"<b>Reddit media:</b> <code>{utils.escape_html(self._get('reddit_media', 'pics'))}</code>\n"
            f"<b>Reddit mode:</b> <code>{utils.escape_html(self._get('reddit_mode', 'auto'))}</code>\n"
            f"<b>Reddit client:</b> <code>{'curl_cffi (chrome TLS)' if cffi_requests else 'aiohttp (install curl_cffi!)'}</code>\n"
            f"<b>Reddit auth:</b> {'set' if self._get('reddit_id', '') else 'anonymous'}\n"
            f"<b>Reddit cookie:</b> {'set' if (self._get('reddit_cookie', '') or '').strip() else 'none'}\n"
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
            "<code>.fumocount 3</code> — how many pictures per send (1..10, album)\n"
            "<code>.fumoschedule on</code> — queue posts as Telegram Scheduled Messages "
            "(published even if the bot is offline)\n"
            "<code>.fumotime 07:00</code> — publish scheduled posts at this local time, "
            "chained after the last queued post (off = plain interval)\n"
            "<code>.fumonum 57</code> — set the post counter (#57 → #58 …); no args shows it\n\n"
            "<b>Caption</b>\n"
            "<code>.fumocaption Fumo time! {source}</code> — custom text "
            "({source} {post} {url} {tags} {id} {num} {date} {time}); empty = clear\n"
            "<code>.fumometa off</code> — hide source/tags/link footer\n\n"
            "<b>Sources & tags</b>\n"
            "<code>.fumotags</code> — show tags per source\n"
            "<code>.fumotags safebooru fumo_(doll) touhou</code> — set tags for one source\n"
            "<code>.fumoexclude nude gore</code> — minus-tag blacklist for boorus\n"
            "<code>.fumokonachan net|com</code> — konachan.net (safe) / konachan.com (R-18, filtered)\n\n"
            "<b>Reddit</b>\n"
            "<code>.fumoreddit on|off</code> — Reddit source (mixed with the boorus)\n"
            "<code>.fumosub Fumofumo</code> — subreddit\n"
            "<code>.fumoredditmedia pics|videos|all</code> — what to fetch\n"
            "<code>.fumoredditauth &lt;client_id&gt; &lt;client_secret&gt;</code> — OAuth app "
            "(saves you when anonymous access is 403-blocked); empty = clear\n"
            "<code>.fumoredditcookie &lt;cookie&gt;</code> — browser cookie fallback "
            "(dev tools → Network → any reddit request → Cookie header); empty = clear\n"
            "<code>.fumoredditmode auto|json|arctic</code> — listing source: direct JSON "
            "or Arctic Shift archive (works where Reddit listings are IP-blocked)\n\n"
            "<b>Misc</b>\n"
            "<code>.fumotest</code> — post now\n"
            "<code>.fumotest reddit 2</code> — post 2 from a specific source\n"
            "<code>.fumoreset</code> — clear sent-images history (settings are kept)\n"
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
