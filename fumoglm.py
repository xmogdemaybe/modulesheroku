# ═══════════════════════════════════════════════════════════════════════════════
#   FumoPoster · automatic fumo plushie delivery for Hikka-based userbots
#
#   Randomly fetches Touhou fumo images from a chain of booru APIs
#   (Safebooru / Gelbooru / Danbooru / yande.re, tried in random order)
#   and posts them to any chat or channel on a configurable schedule.
#
#   Install:    .dlmod <raw-url-to-this-file>
#   Help:       .help FumoPoster
#   Quickstart: .fumotarget me  ·  .fumointerval 2h  ·  .fumotoggle on
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import contextlib
import html
import io
import logging
import random
import re
import time
from datetime import datetime
from typing import Optional
from urllib.parse import quote, urlparse

import aiohttp
from telethon import errors
from telethon.errors import FloodWaitError

from .. import loader, utils

logger = logging.getLogger(__name__)

# ───────────────────────────────── settings ───────────────────────────────────

_NS = "FumoPoster"                    # database namespace

_DEFAULT_INTERVAL = 6 * 3600          # 6 hours
_MIN_INTERVAL = 60                    # 1 minute
_MAX_INTERVAL = 30 * 86400            # 30 days
_MAX_FAILURES = 3                     # consecutive failures before auto-disable
_RETRY_DELAY = 600                    # failed cycle retries in 10 minutes
_POLL_SECONDS = 60.0                  # scheduler wake-up granularity
_MAX_IMAGE_BYTES = 15 * 1024 * 1024   # safety cap for in-memory image downloads

_ALLOWED_EXTS = (".jpg", ".jpeg", ".png", ".gif")

_DEFAULT_CAPTION = (
    "🧸 <b>Fumo delivery</b>\n"
    '🔗 <a href="{source}">source</a> · ☁️ {api}\n'
    "📅 {date} · 🕒 {time}"
)

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_]+)\}")
_INTERVAL_RE = re.compile(r"^(\d+)\s*(s|m|h|d)?$", re.IGNORECASE)
_EXT_RE = re.compile(r"\.[a-z0-9]+$")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
        "Gecko/20100101 Firefox/126.0"
    ),
    "Accept": "application/json, text/plain, */*",
}

# Tags used per API while the user hasn't set custom ones.
# NB: Danbooru & Gelbooru allow only two tags per anonymous search,
#     so the defaults deliberately stay within that limit.
_API_DEFAULT_TAGS = {
    "safebooru": "fumo",
    "gelbooru": "fumo_fumo rating:general",
    "danbooru": "fumo_fumo",
    "yandere": "fumo",
}


# ══════════════════════════════════ module ═══════════════════════════════════

@loader.tds
class FumoPosterMod(loader.Module):
    """Fetches random Touhou fumo plushie images from booru APIs
(Safebooru / Gelbooru / Danbooru / yande.re) and automatically
posts them to any chat or channel on a schedule."""

    strings = {"name": "FumoPoster"}

    # ───────────────────────────── lifecycle ──────────────────────────────

    def __init__(self):
        super().__init__()
        self._task: Optional[asyncio.Future] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._wake = asyncio.Event()

    async def clientready(self, client, db=None) -> None:
        """Start the background scheduler as soon as the client is up."""
        self._client = client
        self._ensure_loop()
        logger.debug("FumoPoster: ready")

    async def on_unload(self) -> None:
        """Cancel the scheduler and free resources on unload/reload."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=5)
        await self._close_session()
        logger.debug("FumoPoster: unloaded")

    # ────────────────────────────── commands ──────────────────────────────

    async def fumocmd(self, message):
        """<b>Usage:</b> .fumo [&lt;chat | @username | id&gt;]
Fetch one random fumo plushie and send it here (or to the given chat) immediately."""
        self._ensure_loop()
        arg = (utils.get_args_raw(message) or "").strip()
        try:
            target = await self._resolve_target(arg) if arg else message.chat_id
        except ValueError as e:
            await self._edit(message, f"🚫 {html.escape(str(e))}")
            return
        status = None
        with contextlib.suppress(Exception):
            status = await utils.answer(message, "🧸 Summoning a fumo…")
        ok, err = await self._deliver(target)
        if ok:
            await self._delete_status(status)
        else:
            await self._edit(
                status,
                f"🚫 <b>Fumo delivery failed:</b> <code>{html.escape(err)}</code>",
            )

    async def fumotogglecmd(self, message):
        """<b>Usage:</b> .fumotoggle [on|off]
Enable or disable automatic fumo posting (no argument = toggle).
The first fumo is delivered right after enabling."""
        self._ensure_loop()
        arg = (utils.get_args_raw(message) or "").lower().strip()
        if arg in ("on", "enable", "enabled", "1", "true", "yes"):
            new_state = True
        elif arg in ("off", "disable", "disabled", "0", "false", "no"):
            new_state = False
        elif arg:
            await self._edit(message, "🚫 <b>Usage:</b> <code>.fumotoggle [on|off]</code>")
            return
        else:
            new_state = not bool(self._get("enabled", False))

        self._set("enabled", new_state)
        if new_state:
            self._set("fails", 0)
            self._set("next_post", time.time())  # deliver immediately
            self._wake.set()
            reply = (
                "🧸 <b>FumoPoster is enabled</b>\n"
                f"🎯 Target: {html.escape(self._target_label())}\n"
                f"⏱ Interval: {self._fmt_dur(self._get_interval())}\n"
                "🚀 First fumo is on its way!"
            )
        else:
            reply = "🧸 <b>FumoPoster is disabled</b> — no more scheduled fumos."
        await self._edit(message, reply)

    async def fumotargetcmd(self, message):
        """<b>Usage:</b> .fumotarget [&lt;chat id | @username | t.me link | me&gt;]
Choose where scheduled fumos are posted.
Without arguments — this chat. <code>me</code> — Saved Messages."""
        arg = (utils.get_args_raw(message) or "").strip()
        if not arg:
            try:
                chat = await message.get_chat()
            except Exception:
                chat = None
            self._set("target", str(message.chat_id))
            self._set(
                "target_name",
                self._entity_title(chat) if chat else str(message.chat_id),
            )
        elif arg.lower() in ("me", "self", "saved", "savedmessages"):
            self._set("target", "me")
            self._set("target_name", "Saved Messages")
        else:
            try:
                entity = await self._resolve_target(arg)
            except ValueError as e:
                await self._edit(message, f"🚫 {html.escape(str(e))}")
                return
            self._set("target", arg)
            self._set("target_name", self._entity_title(entity))
        await self._edit(
            message,
            f"🎯 <b>Fumo target:</b> {html.escape(self._target_label())}",
        )

    async def fumointervalcmd(self, message):
        """<b>Usage:</b> .fumointerval &lt;minutes | Ns | Nm | Nh | Nd&gt;
Set the auto-posting interval. Bare numbers mean minutes.
Limits: 1 minute – 30 days."""
        arg = (utils.get_args_raw(message) or "").strip().lower()
        if not arg:
            await self._edit(
                message,
                f"⏱ <b>Current interval:</b> {self._fmt_dur(self._get_interval())}\n"
                "Change it, e.g.: <code>.fumointerval 30m</code> / "
                "<code>.fumointerval 2h</code>",
            )
            return
        seconds = self._parse_interval(arg)
        if seconds is None:
            await self._edit(
                message,
                "🚫 Invalid interval. Examples: <code>45</code>, "
                "<code>30m</code>, <code>2h</code>, <code>1d</code>",
            )
            return
        clamped = max(_MIN_INTERVAL, min(seconds, _MAX_INTERVAL))
        note = "" if clamped == seconds else (
            f"\n⚠️ Value was clamped to {self._fmt_dur(clamped)}."
        )
        self._set("interval", clamped)
        if self._get("enabled", False):
            self._set("next_post", time.time() + clamped)
        await self._edit(message, f"⏱ <b>Interval set:</b> {self._fmt_dur(clamped)}{note}")

    async def fumocaptioncmd(self, message):
        """<b>Usage:</b> .fumocaption &lt;text | clear | reset&gt;
Caption for the posted media. HTML markup is allowed.
Placeholders: <code>{source} {url} {tags} {id} {api} {date} {time}</code>
<code>clear</code> — no caption at all; <code>reset</code> — default caption."""
        arg = utils.get_args_raw(message)
        if not arg or not arg.strip():
            current = self._get("caption")
            if current is None:
                shown, note = _DEFAULT_CAPTION, " (default)"
            elif not current:
                shown, note = "— empty —", ""
            else:
                shown, note = current, ""
            await self._edit(
                message,
                f"📝 <b>Current caption{note}:</b>\n"
                f"<code>{html.escape(shown)}</code>\n\n"
                "Placeholders: <code>{source} {url} {tags} {id} {api} {date} {time}</code>\n"
                "HTML markup is supported.",
            )
            return
        arg = arg.strip()
        low = arg.lower()
        if low == "clear":
            self._set("caption", "")
            await self._edit(message, "📝 Caption cleared — fumos will arrive with no caption.")
        elif low in ("reset", "default"):
            self._set("caption", _DEFAULT_CAPTION)
            await self._edit(message, "📝 Caption reset to the default template.")
        else:
            self._set("caption", arg)
            await self._edit(
                message,
                f"📝 <b>Caption set:</b>\n<code>{html.escape(arg)}</code>",
            )

    async def fumotagscmd(self, message):
        """<b>Usage:</b> .fumotags &lt;tags | reset&gt;
Custom booru search tags (e.g. <code>fumo cirno</code>).
⚠️ Danbooru/Gelbooru allow max 2 tags for anonymous users.
<code>reset</code> restores the tuned per-API defaults."""
        arg = (utils.get_args_raw(message) or "").strip()
        if not arg:
            custom = self._get("tags")
            lines = ["🏷 <b>Fumo search tags</b>\n"]
            lines.append(
                f"• Custom: <code>"
                f"{html.escape(custom) if custom else '— (per-API defaults)'}</code>"
            )
            lines.append("\nPer-API defaults:")
            for api, tags in _API_DEFAULT_TAGS.items():
                lines.append(f"• {api.capitalize()}: <code>{html.escape(tags)}</code>")
            await self._edit(message, "\n".join(lines))
            return
        if arg.lower() in ("reset", "default"):
            self._set("tags", "")
            await self._edit(message, "🏷 Custom tags cleared — per-API defaults in effect.")
            return
        self._set("tags", arg)
        await self._edit(
            message,
            f"🏷 <b>Search tags set:</b> <code>{html.escape(arg)}</code>",
        )

    async def fumostatuscmd(self, message):
        """Show FumoPoster status: state, target, interval,
next post, caption template, tags and scheduler health."""
        self._ensure_loop()
        enabled = bool(self._get("enabled", False))
        interval = self._get_interval()
        nxt = float(self._get("next_post", 0) or 0)
        fails = int(self._get("fails", 0) or 0)
        caption = self._get("caption")
        if caption is None:
            caption = _DEFAULT_CAPTION
        custom_tags = self._get("tags")

        chunks = [
            "🧸 <b>FumoPoster</b>\n",
            f"⚙️ Auto-posting: <b>{'✅ enabled' if enabled else '⛔ disabled'}</b>",
            f"🎯 Target: <b>{html.escape(self._target_label())}</b>",
            f"⏱ Interval: <b>{self._fmt_dur(interval)}</b>",
        ]
        if enabled:
            remaining = max(0, int(nxt - time.time()))
            when = datetime.fromtimestamp(nxt).strftime("%H:%M:%S") if nxt else "—"
            chunks.append(
                f"⏭ Next post: <b>in {self._fmt_dur(remaining)}</b> ({when})"
            )
        if fails:
            chunks.append(f"❗ Consecutive failures: <b>{fails}</b>/{_MAX_FAILURES}")
        chunks.append(f"📝 Caption: <code>{html.escape(caption) or '(empty)'}</code>")
        chunks.append(
            "🏷 Tags: <code>"
            f"{html.escape(custom_tags) if custom_tags else 'per-API defaults'}</code>"
        )
        alive = self._task is not None and not self._task.done()
        chunks.append(f"🫀 Scheduler: <b>{'✅ running' if alive else '⚠️ not running'}</b>")
        chunks.append("☁️ Sources: Safebooru · Gelbooru · Danbooru · yande.re")
        await self._edit(message, "\n".join(chunks))

    # ─────────────────────────── background loop ──────────────────────────

    def _ensure_loop(self) -> None:
        """(Re)start the scheduler exactly once."""
        if self._task is not None and not self._task.done():
            return
        # A unique token lets a stale loop from a previous module instance
        # (e.g. after a buggy reload) terminate by itself.
        token = f"{time.time_ns():x}{random.randrange(1 << 32):08x}"
        self._set("loop_token", token)
        self._task = asyncio.ensure_future(self._poster_loop(token))

    async def _poster_loop(self, token: str) -> None:
        logger.debug("FumoPoster: scheduler started")
        try:
            while True:
                if self._get("loop_token") != token:
                    logger.debug("FumoPoster: superseded by a newer instance — stopping")
                    return
                poll = _POLL_SECONDS
                try:
                    if self._get("enabled", False):
                        interval = float(self._get_interval())
                        poll = min(_POLL_SECONDS, max(5.0, interval / 4.0))
                        if time.time() >= float(self._get("next_post", 0) or 0):
                            await self._cycle()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("FumoPoster: scheduler iteration failed")
                # interruptible sleep — commands can wake us up instantly
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=poll)
                    self._wake.clear()
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            logger.debug("FumoPoster: scheduler cancelled")
            raise

    async def _cycle(self) -> None:
        """One scheduled delivery, with failure accounting."""
        interval = self._get_interval()
        try:
            entity = await self._resolve_target(self._get("target") or "me")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._register_failure(f"invalid target: {e}", interval)
            return
        try:
            ok, err = await self._deliver(entity)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("FumoPoster: unexpected delivery error")
            ok, err = False, f"internal error: {e}"
        if ok:
            self._set("fails", 0)
            self._set("next_post", time.time() + interval)
        else:
            await self._register_failure(err, interval)

    async def _register_failure(self, reason: str, interval: int) -> None:
        """Count a failed cycle; auto-disable the module after repeated failures."""
        fails = int(self._get("fails", 0) or 0) + 1
        self._set("fails", fails)
        logger.warning(
            "FumoPoster: delivery failed (%d/%d): %s", fails, _MAX_FAILURES, reason
        )
        if fails < _MAX_FAILURES:
            self._set("next_post", time.time() + min(interval, _RETRY_DELAY))
            return
        self._set("enabled", False)
        self._set("fails", 0)
        self._set("next_post", time.time() + interval)
        logger.error("FumoPoster: auto-disabled after %d consecutive failures", fails)
        with contextlib.suppress(Exception):
            await self._client.send_message(
                "me",
                "⛔ <b>FumoPoster</b> was auto-disabled after "
                f"{fails} consecutive delivery failures.\n"
                f"Last error: <code>{html.escape(str(reason)[:300])}</code>\n"
                "Check <code>.fumostatus</code>, fix the issue and re-enable "
                "with <code>.fumotoggle on</code>.",
            )

    # ───────────────────────────── delivery ───────────────────────────────

    async def _deliver(self, entity) -> tuple:
        """Fetch a random fumo and send it to `entity`. Returns (ok, error)."""
        post = await self._fetch_random()
        if post is None:
            return False, "all image sources failed"

        data = await self._download(post["url"])
        if not data:
            return False, f"failed to download {post['url']}"

        ext = self._ext_for(post["url"], data)
        if not ext:
            return False, "unsupported image format"

        caption = self._render_caption(post)
        buf = io.BytesIO(data)
        buf.name = f"fumo_{post.get('id') or 'post'}{ext}"
        last_error = ""

        for attempt in (1, 2):
            buf.seek(0)
            try:
                await self._client.send_file(
                    entity,
                    buf,
                    caption=caption if caption.strip() else None,
                )
                logger.debug("FumoPoster: delivered %s", post["url"])
                return True, ""
            except FloodWaitError as e:
                # also covers SlowModeWaitError
                wait = int(getattr(e, "seconds", 0) or 0)
                last_error = f"flood wait {wait}s"
                if attempt == 1 and 0 < wait <= 120:
                    await asyncio.sleep(wait + 2)
                    continue
                return False, last_error
            except errors.RPCError as e:
                # ChatWriteForbiddenError, UserBannedInChannelError,
                # ChannelPrivateError and friends all land here
                return False, f"telegram error: {e}"
            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                last_error = f"network error: {e}"
                if attempt == 1:
                    await asyncio.sleep(3)
                    continue
                return False, last_error
            except (ValueError, TypeError) as e:
                return False, f"cannot send to target: {e}"
        return False, last_error or "send failed"

    # ─────────────────────────── image sources ────────────────────────────

    async def _fetch_random(self) -> Optional[dict]:
        """Try every booru source in random order until one yields a fumo."""
        sources = [
            ("Safebooru", self._src_safebooru),
            ("Gelbooru", self._src_gelbooru),
            ("Danbooru", self._src_danbooru),
            ("yande.re", self._src_yandere),
        ]
        random.shuffle(sources)
        for name, fetcher in sources:
            try:
                post = await fetcher()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("FumoPoster: source %s crashed", name)
                post = None
            if post is not None:
                logger.debug(
                    "FumoPoster: picked %s post #%s", post.get("api"), post.get("id")
                )
                return post
            logger.debug("FumoPoster: source %s gave nothing", name)
        return None

    async def _src_safebooru(self) -> Optional[dict]:
        return await self._fetch_dapi(
            "https://safebooru.org/index.php",
            "Safebooru",
            self._safebooru_url,
            lambda post_id: f"https://safebooru.org/index.php?page=post&s=view&id={post_id}",
        )

    async def _src_gelbooru(self) -> Optional[dict]:
        return await self._fetch_dapi(
            "https://gelbooru.com/index.php",
            "Gelbooru",
            self._gelbooru_url,
            lambda post_id: f"https://gelbooru.com/index.php?page=post&s=view&id={post_id}",
        )

    async def _src_danbooru(self) -> Optional[dict]:
        tags = (self._tags_for("danbooru") + " order:random").strip()
        data = await self._fetch_json(
            "https://danbooru.donmai.us/posts.json",
            {"limit": "25", "tags": tags},
        )
        if not isinstance(data, list):
            return None
        return self._collect(
            data,
            "Danbooru",
            lambda post: post.get("file_url") or post.get("large_file_url"),
            lambda post_id: f"https://danbooru.donmai.us/posts/{post_id}",
            "tag_string",
        )

    async def _src_yandere(self) -> Optional[dict]:
        tags = (self._tags_for("yandere") + " order:random").strip()
        data = await self._fetch_json(
            "https://yande.re/post.json",
            {"limit": "25", "tags": tags},
        )
        if not isinstance(data, list):
            return None
        return self._collect(
            data,
            "yande.re",
            lambda post: post.get("file_url"),
            lambda post_id: f"https://yande.re/post/show/{post_id}",
            "tags",
        )

    async def _fetch_dapi(self, base: str, api: str, url_of, page_of) -> Optional[dict]:
        """Query a Gelbooru-style `dapi` endpoint (Safebooru / Gelbooru)."""
        limit = 100
        params = {
            "page": "dapi",
            "s": "post",
            "q": "index",
            "json": "1",
            "limit": str(limit),
            "tags": self._tags_for(api.lower()),
        }
        data = await self._fetch_json(base, params)
        posts, count = self._parse_dapi(data)
        if not posts:
            return None
        # Jump to a random result page so picks aren't limited to the newest posts
        try:
            if count and int(count) > limit:
                params["pid"] = str(random.randint(0, (int(count) - 1) // limit))
                retry_data = await self._fetch_json(base, params)
                if retry_data:
                    retry_posts, _ = self._parse_dapi(retry_data)
                    if retry_posts:
                        posts = retry_posts
        except (TypeError, ValueError):
            pass
        return self._collect(posts, api, url_of, page_of)

    @staticmethod
    def _parse_dapi(payload):
        """Normalize a Gelbooru-style `dapi` JSON payload.

        Handles both the bare-list shape (Safebooru) and the
        {"@attributes": ..., "post": [...]} shape (Gelbooru API 0.2+).
        Returns (posts, total_count)."""
        if isinstance(payload, list):
            count = None
            if payload and isinstance(payload[0], dict) and "@attributes" in payload[0]:
                count = payload[0].get("@attributes", {}).get("count")
                payload = payload[1:]
            return [p for p in payload if isinstance(p, dict)], count
        if isinstance(payload, dict):
            attrs = payload.get("@attributes")
            count = attrs.get("count") if isinstance(attrs, dict) else None
            posts = payload.get("post") or []
            if isinstance(posts, dict):
                posts = [posts]
            if not isinstance(posts, list):
                posts = []
            return [p for p in posts if isinstance(p, dict)], count
        return [], None

    def _collect(self, posts: list, api: str, url_of, page_of, tags_key: str = "tags"):
        """Normalize a list of API posts into one randomly picked candidate."""
        candidates = []
        for post in posts:
            if not isinstance(post, dict):
                continue
            url = url_of(post)
            if not (isinstance(url, str) and url.startswith("http")):
                continue
            post_id = post.get("id")
            candidates.append(
                {
                    "api": api,
                    "url": url,
                    "page": page_of(post_id) if post_id is not None else "",
                    "tags": str(post.get(tags_key) or ""),
                    "id": post_id,
                }
            )
        return self._pick(candidates)

    @staticmethod
    def _safebooru_url(post: dict) -> Optional[str]:
        url = post.get("file_url")
        if isinstance(url, str) and url.startswith("http"):
            return url
        directory, image = post.get("directory"), post.get("image")
        if directory and image:
            return "https://safebooru.org/images/" + quote(f"{directory}/{image}", safe="/")
        return None

    @staticmethod
    def _gelbooru_url(post: dict) -> Optional[str]:
        url = post.get("file_url")
        if isinstance(url, str) and url.startswith("http"):
            return url
        directory, image = post.get("directory"), post.get("image")
        if directory and image:
            return "https://cdn.gelbooru.com/images/" + quote(f"{directory}/{image}", safe="/")
        return None

    def _pick(self, candidates: list) -> Optional[dict]:
        """Random choice, image-extension filter, no immediate repeats."""
        if not candidates:
            return None
        images = [c for c in candidates if self._is_image_url(c["url"])]
        if not images:
            return None
        last = self._get("last_url")
        fresh = [c for c in images if c["url"] != last]
        pool = fresh or images
        choice = random.choice(pool)
        self._set("last_url", choice["url"])
        return choice

    @staticmethod
    def _is_image_url(url: str) -> bool:
        ext = _EXT_RE.search(urlparse(url).path.lower())
        if not ext:
            return True  # no extension — magic bytes will be checked later
        return ext.group(0) in _ALLOWED_EXTS

    def _tags_for(self, api: str) -> str:
        custom = self._get("tags")
        if isinstance(custom, str) and custom.strip():
            return custom.strip()
        return _API_DEFAULT_TAGS.get(api, "fumo")

    # ───────────────────────────── networking ─────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=_HEADERS,
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
                trust_env=True,  # respect HTTP(S)_PROXY environment variables
            )
        return self._session

    async def _close_session(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            with contextlib.suppress(Exception):
                await session.close()

    async def _fetch_json(self, url: str, params: Optional[dict] = None):
        """GET a JSON document with one retry on transient errors."""
        for attempt in (1, 2):
            try:
                async with self._get_session().get(url, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    logger.debug("FumoPoster: %s → HTTP %s", url, resp.status)
                    if resp.status not in (429, 500, 502, 503, 504):
                        return None  # e.g. 404 — retrying is pointless
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                logger.debug(
                    "FumoPoster: %s failed (attempt %d)", url, attempt, exc_info=True
                )
            if attempt == 1:
                await asyncio.sleep(1.5)
        return None

    async def _download(self, url: str) -> Optional[bytes]:
        """Download an image into memory (capped at _MAX_IMAGE_BYTES)."""
        try:
            async with self._get_session().get(
                url,
                timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_read=60),
            ) as resp:
                if resp.status != 200:
                    logger.debug("FumoPoster: image %s → HTTP %s", url, resp.status)
                    return None
                if resp.content_length and resp.content_length > _MAX_IMAGE_BYTES:
                    return None
                buffer = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buffer.extend(chunk)
                    if len(buffer) > _MAX_IMAGE_BYTES:
