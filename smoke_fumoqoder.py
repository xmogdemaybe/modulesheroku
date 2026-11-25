# -*- coding: utf-8 -*-
"""Smoke-тесты FumoQoder: замена из того же источника при 404, fast-fail 403/404."""
import asyncio
import sys
import types
import importlib

import aiohttp

# ---- стабы heroku/herokutl ----

loader_mod = types.ModuleType("heroku.loader")

def _command(**kw):
    def deco(fn):
        fn._cmd = True
        return fn
    return deco

class _ModuleMeta(type):
    pass

class _Module(metaclass=_ModuleMeta):
    strings = {}

loader_mod.command = _command
loader_mod.Module = _Module
loader_mod.tds = lambda cls: cls

utils_mod = types.ModuleType("heroku.utils")
utils_mod.escape_html = lambda s: s
utils_mod.get_args_raw = lambda m: ""
utils_mod.answer = lambda m, t: asyncio.coroutine(lambda: None)()
utils_mod.is_serializable = lambda v: True

heroku_pkg = types.ModuleType("heroku")
heroku_pkg.__path__ = []
sys.modules["heroku"] = heroku_pkg
sys.modules["heroku.loader"] = loader_mod
sys.modules["heroku.utils"] = utils_mod

# telethon stubs
tel = types.ModuleType("telethon")
tel_errors = types.ModuleType("telethon.errors")
class FloodWaitError(Exception):
    def __init__(self, seconds=0):
        self.seconds = seconds
class ChatWriteForbiddenError(Exception): pass
class ChatAdminRequiredError(Exception): pass
tel_errors.FloodWaitError = FloodWaitError
tel_errors.ChatWriteForbiddenError = ChatWriteForbiddenError
tel_errors.ChatAdminRequiredError = ChatAdminRequiredError
tel.errors = tel_errors
sys.modules["telethon"] = tel
sys.modules["telethon.errors"] = tel_errors

tel_tl = types.ModuleType("telethon.tl")
tel_tl_func = types.ModuleType("telethon.tl.functions")
tel_tl_func_msg = types.ModuleType("telethon.tl.functions.messages")
tel_tl_func_msg.GetScheduledHistoryRequest = None
sys.modules["telethon.tl"] = tel_tl
sys.modules["telethon.tl.functions"] = tel_tl_func
sys.modules["telethon.tl.functions.messages"] = tel_tl_func_msg

tel_utils = types.ModuleType("telethon.utils")
tel_utils.get_peer_id = lambda e: 0
sys.modules["telethon.utils"] = tel_utils

# Теперь грузим сам модуль как обычный пакет (не через loader)
spec = importlib.util.spec_from_file_location(
    "fumoqoder", "fumoqoder.py",
    submodule_search_locations=[],
)
# Подменяем относительный импорт: `from .. import loader, utils` → наши стабы
_real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level and fromlist:
        mapping = {"loader": loader_mod, "utils": utils_mod}
        result = types.ModuleType("heroku")
        for attr in fromlist:
            setattr(result, attr, mapping.get(attr, types.ModuleType(attr)))
        return result
    return _real_import(name, globals, locals, fromlist, level)

import builtins
_saved = builtins.__import__
builtins.__import__ = _fake_import
fumo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fumo)
builtins.__import__ = _saved

FumoQoder = fumo.FumoQoder

# ---- Вспомогательное ----

class FakeDB(dict):
    def get(self, mod, key, default=None):
        return self.setdefault(mod, {}).get(key, default)
    def set(self, mod, key, val):
        self.setdefault(mod, {})[key] = val

class FakeClient:
    """Стаб клиента — send_file просто сохраняет вызов."""
    def __init__(self):
        self.sent = []
    async def send_file(self, entity, files, caption="", **kw):
        self.sent.append((entity, files, caption, kw))
        return types.SimpleNamespace(id=42)

class FakeSession:
    """aiohttp-сессия с программируемыми ответами."""
    def __init__(self, responses):
        # responses: dict url → (status, body_bytes)
        self._responses = responses
        self.closed = False
    def get(self, url, **kw):
        return _FakeResp(self._responses.get(url, (200, b"\xff\xd8fake-jpg")))
    async def close(self):
        self.closed = True

class _FakeResp:
    def __init__(self, pair):
        self.status, self._body = pair
        self.content_length = len(self._body) if self.status == 200 else 0
        self.content = self
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        pass
    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=aiohttp.RequestInfo(url="", method="GET", headers={}, real_url=""),
                history=(),
                status=self.status,
                message=f"HTTP {self.status}",
            )
    async def iter_chunked(self, _):
        yield self._body


def _make_mod(db=None, session=None) -> FumoQoder:
    """Создать инстанс модуля без client_ready (тесты управляют вручную)."""
    obj = FumoQoder.__new__(FumoQoder)
    obj.db = db or FakeDB()
    obj.client = FakeClient()
    obj._session = session
    obj._stopping = False
    obj._inflight = set()
    obj._source_status = {}
    obj._next_post = 0
    return obj


# ---- Тесты ----

passed = 0
failed = 0

def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}")


async def test_same_source_fallback():
    """Слот Reddit падает с 404 → замена другим постом Reddit, а не Konachan."""
    print("\n[test_same_source_fallback]")

    mod = _make_mod()
    # 2 Reddit-поста, 1 Konachan
    reddit_ok_url = "https://i.redd.it/good.jpg"
    reddit_dead_url = "https://i.redd.it/dead.jpg"
    konachan_url = "https://konachan.net/image/ok.jpg"

    reddit_posts = [
        {"id": "rd_dead", "url": reddit_dead_url, "src": "Reddit",
         "post_url": "https://reddit.com/r/Fumofumo/dead", "tags": "r/Fumofumo"},
        {"id": "rd_good", "url": reddit_ok_url, "src": "Reddit",
         "post_url": "https://reddit.com/r/Fumofumo/good", "tags": "r/Fumofumo"},
    ]
    konachan_posts = [
        {"id": "kn_1", "url": konachan_url, "src": "Konachan",
         "post_url": "https://konachan.net/post/show/1", "tags": "touhou doll"},
    ]

    async def fake_reddit():
        return reddit_posts
    async def fake_konachan():
        return konachan_posts
    fake_reddit.__name__ = "_src_reddit"
    fake_konachan.__name__ = "_src_konachan"

    mod._active_sources = lambda: [fake_reddit, fake_konachan]
    mod._ffmpeg = lambda: ""

    # Программируем HTTP: dead → 404, good → ok, konachan → ok
    session = FakeSession({
        reddit_dead_url: (404, b""),
        reddit_ok_url: (200, b"\xff\xd8good-reddit"),
        konachan_url: (200, b"\xff\xd8good-konachan"),
    })
    mod._session = session

    entity = types.SimpleNamespace(id=123)
    sent = await mod._send(entity, count=1, is_test=True)

    check("sent exactly 1 post", len(sent) == 1)
    check("sent post is from Reddit (same-source fallback)", sent[0]["src"] == "Reddit")
    check("sent post is rd_good, not rd_dead", sent[0]["id"] == "rd_good")
    check("_inflight is empty after send", len(mod._inflight) == 0)


async def test_no_retry_on_404():
    """403/404/410 не ретраятся — ровно 1 вызов download, потом переход к следующему."""
    print("\n[test_no_retry_on_404]")

    mod = _make_mod()
    dead_url = "https://i.redd.it/dead404.jpg"
    download_count = 0
    original_download = mod._download

    async def counting_download(url, max_size=20*1024*1024):
        nonlocal download_count
        download_count += 1
        raise aiohttp.ClientResponseError(
            request_info=aiohttp.RequestInfo(url=url, method="GET", headers={}, real_url=url),
            history=(),
            status=404,
            message="Not Found",
        )

    mod._download = counting_download

    # Один источник с одним мёртвым постом и одним живым
    good_url = "https://i.redd.it/alive.jpg"

    async def fake_src():
        return [
            {"id": "rd_d1", "url": dead_url, "src": "Reddit",
             "post_url": "", "tags": "r/Fumofumo"},
            {"id": "rd_ok", "url": good_url, "src": "Reddit",
             "post_url": "", "tags": "r/Fumofumo"},
        ]
    fake_src.__name__ = "_src_reddit"
    mod._active_sources = lambda: [fake_src]
    mod._ffmpeg = lambda: ""

    # Переопределяем download: первый вызов (dead_url) кидает 404,
    # второй (good_url) возвращает данные.
    call_log = []
    async def smart_download(url, max_size=20*1024*1024):
        call_log.append(url)
        if url == dead_url:
            raise aiohttp.ClientResponseError(
                request_info=aiohttp.RequestInfo(url=url, method="GET", headers={}, real_url=url),
                history=(),
                status=404,
                message="Not Found",
            )
        return b"\xff\xd8ok"
    mod._download = smart_download

    session = FakeSession({})  # не используется — download подменён
    mod._session = session

    entity = types.SimpleNamespace(id=123)
    sent = await mod._send(entity, count=1, is_test=True)

    # dead_url вызван ровно 1 раз (а не 3)
    dead_calls = [u for u in call_log if u == dead_url]
    check("404 URL called exactly once (no retry)", len(dead_calls) == 1)
    check("good URL eventually downloaded", good_url in call_log)
    check("sent 1 post", len(sent) == 1)


async def test_inflight_released_on_failure():
    """Если все посты отвалились — _inflight всё равно чистится."""
    print("\n[test_inflight_released_on_failure]")

    mod = _make_mod()

    async def fake_src():
        return [
            {"id": "rd_x", "url": "https://i.redd.it/x.jpg", "src": "Reddit",
             "post_url": "", "tags": "r/Fumofumo"},
        ]
    fake_src.__name__ = "_src_reddit"
    mod._active_sources = lambda: [fake_src]
    mod._ffmpeg = lambda: ""

    async def fail_download(url, max_size=20*1024*1024):
        raise aiohttp.ClientResponseError(
            request_info=aiohttp.RequestInfo(url=url, method="GET", headers={}, real_url=url),
            history=(),
            status=404,
            message="Not Found",
        )
    mod._download = fail_download
    mod._session = FakeSession({})

    entity = types.SimpleNamespace(id=123)
    err = None
    try:
        await mod._send(entity, count=1, is_test=True)
    except RuntimeError as e:
        err = e

    check("RuntimeError raised on total failure", err is not None)
    check("_inflight empty after failure", len(mod._inflight) == 0)


async def test_5xx_still_retried():
    """500/502/503 по-прежнему ретраятся до 3 раз."""
    print("\n[test_5xx_still_retried]")

    mod = _make_mod()
    call_count = 0
    target_url = "https://safebooru.org/images/1/test.jpg"

    async def flaky_download(url, max_size=20*1024*1024):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise aiohttp.ClientResponseError(
                request_info=aiohttp.RequestInfo(url=url, method="GET", headers={}, real_url=url),
                history=(),
                status=502,
                message="Bad Gateway",
            )
        return b"\xff\xd8ok"
    mod._download = flaky_download

    async def fake_src():
        return [
            {"id": "sb_1", "url": target_url, "src": "Safebooru",
             "post_url": "", "tags": "fumo_(doll)"},
        ]
    fake_src.__name__ = "_src_safebooru"
    mod._active_sources = lambda: [fake_src]
    mod._ffmpeg = lambda: ""
    mod._session = FakeSession({})

    entity = types.SimpleNamespace(id=123)
    sent = await mod._send(entity, count=1, is_test=True)

    check("502 retried 3 times total", call_count == 3)
    check("eventually sent", len(sent) == 1)


async def main():
    await test_same_source_fallback()
    await test_no_retry_on_404()
    await test_inflight_released_on_failure()
    await test_5xx_still_retried()
    print(f"\n{'='*40}")
    print(f"Passed: {passed}, Failed: {failed}")
    if failed:
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
