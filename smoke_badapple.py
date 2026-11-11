"""Smoke test for badapple.py with stubbed heroku/herokutl APIs.

Usage:
    python smoke_badapple.py            # expect all PASS
    python smoke_badapple.py --mutate   # cursor mutation: expect exactly 2 predicted FAILs
"""

import asyncio
import importlib
import logging
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

logging.basicConfig(level=logging.CRITICAL)

MUTATE = "--mutate" in sys.argv
MODULE_SRC = Path(__file__).with_name("badapple.py").read_text(encoding="utf-8")

if MUTATE:
    old = 'offset_id=self.get("last_id", 0)'
    assert old in MODULE_SRC, "mutation target not found"
    MODULE_SRC = MODULE_SRC.replace(old, "offset_id=0")


def build_env():
    tmp = Path(tempfile.mkdtemp(prefix="badapple_smoke_"))
    outer = tmp / "outer"
    pkg = outer / "bapkg"
    pkg.mkdir(parents=True)
    (outer / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "badapple.py").write_text(MODULE_SRC, encoding="utf-8")
    (outer / "loader.py").write_text(
        "class _DB(dict):\n"
        "    def get(self, owner, key, default=None):\n"
        "        try:\n"
        "            return self[owner][key]\n"
        "        except KeyError:\n"
        "            return default\n"
        "    def set(self, owner, key, value):\n"
        "        self.setdefault(owner, {})[key] = value\n"
        "        self.save()\n"
        "    def save(self):\n"
        "        return True\n"
        "class Module:\n"
        "    strings = {}\n"
        "    def __init__(self):\n"
        "        self._db = _DB()\n"
        "        self.tg_id = 0\n"
        "        self.get_prefix = lambda: '.'\n"
        "    def get(self, key, default=None):\n"
        "        return self._db.get(self.__class__.__name__, key, default)\n"
        "    def set(self, key, value):\n"
        "        self._db.set(self.__class__.__name__, key, value)\n"
        "def command(*a, **k):\n"
        "    def deco(f):\n"
        "        return f\n"
        "    return deco\n"
        "def loop(*a, **k):\n"
        "    def deco(f):\n"
        "        return f\n"
        "    return deco\n",
        encoding="utf-8",
    )
    (outer / "utils.py").write_text(
        "def get_args_raw(message):\n"
        "    return message.args\n"
        "async def answer(message, text):\n"
        "    message.replies.append(text)\n",
        encoding="utf-8",
    )

    herokutl = types.ModuleType("herokutl")
    errors = types.ModuleType("herokutl.errors")
    tl = types.ModuleType("herokutl.tl")
    functions = types.ModuleType("herokutl.tl.functions")
    photos = types.ModuleType("herokutl.tl.functions.photos")
    tl_types = types.ModuleType("herokutl.tl.types")

    class FloodWaitError(Exception):
        def __init__(self, seconds):
            super().__init__(f"flood {seconds}")
            self.seconds = seconds

    class UploadProfilePhotoRequest:
        def __init__(self, file=None):
            self.file = file

    errors.FloodWaitError = FloodWaitError
    photos.UploadProfilePhotoRequest = UploadProfilePhotoRequest
    tl_types.Message = type("Message", (), {})
    herokutl.errors = errors
    herokutl.tl = tl
    tl.functions = functions
    tl.types = tl_types
    functions.photos = photos
    for name, mod in {
        "herokutl": herokutl,
        "herokutl.errors": errors,
        "herokutl.tl": tl,
        "herokutl.tl.functions": functions,
        "herokutl.tl.functions.photos": photos,
        "herokutl.tl.types": tl_types,
    }.items():
        sys.modules[name] = mod

    sys.path.insert(0, str(tmp))
    return tmp, importlib.import_module("outer.bapkg.badapple"), FloodWaitError


TMP, badapple, FloodWaitError = build_env()


class FakeMsg:
    def __init__(self, id, photo):
        self.id = id
        self.photo = photo

    async def download_media(self, fmt):
        return b"imgdata" if self.photo else None


class FakeClient:
    def __init__(self, msgs, flood_times=0, fail_times=0):
        self.msgs = msgs
        self.flood_times = flood_times
        self.fail_times = fail_times
        self.uploaded = []
        self.iter_calls = []
        self.sent = []

    async def iter_messages(self, channel, offset_id=0):
        self.iter_calls.append((channel, offset_id))
        for m in self.msgs:
            if offset_id == 0 or m.id < offset_id:
                yield m

    async def upload_file(self, file, file_name=None):
        return ("file", file_name)

    async def __call__(self, request):
        if self.flood_times:
            self.flood_times -= 1
            raise FloodWaitError(42)
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("upload failed")
        self.uploaded.append(request.file[1])

    async def send_message(self, peer, text):
        self.sent.append((peer, text))


class FakeMessage:
    def __init__(self, args=""):
        self.args = args
        self.replies = []


def make_mod(client, store):
    mod = badapple.BadAppleMod()
    mod._db[mod.__class__.__name__] = store
    mod._client = client
    mod.tg_id = 42
    return mod


def tick(store):
    store["next_at"] = 0


MSGS = [FakeMsg(10, True), FakeMsg(9, False), FakeMsg(8, True), FakeMsg(7, True)]


async def test_order_and_cursor():
    client = FakeClient(MSGS)
    store = {"enabled": True}
    mod = make_mod(client, store)
    for _ in range(4):
        tick(store)
        await mod._worker()
    assert client.uploaded == [
        "badapple_10.jpg",
        "badapple_8.jpg",
        "badapple_7.jpg",
    ], client.uploaded
    assert [c[1] for c in client.iter_calls] == [0, 10, 8, 7], client.iter_calls
    assert store["last_id"] == 7 and store["uploaded"] == 3, store
    assert store["done"] is True
    assert len(client.sent) == 1 and client.sent[0][0] == 42, client.sent
    tick(store)
    await mod._worker()
    assert len(client.sent) == 1, "duplicate done notification"


async def test_resume_after_restart():
    client = FakeClient(MSGS)
    store = {"last_id": 8, "uploaded": 2, "enabled": True}
    mod = make_mod(client, store)
    tick(store)
    await mod._worker()
    assert client.uploaded == ["badapple_7.jpg"], client.uploaded
    assert client.iter_calls == [("badappletg", 8)], client.iter_calls


async def test_flood_wait():
    client = FakeClient(MSGS, flood_times=1)
    store = {"enabled": True}
    mod = make_mod(client, store)
    tick(store)
    await mod._worker()
    assert client.uploaded == []
    assert store.get("last_id", 0) == 0
    assert store["next_at"] >= time.time() + 42, store
    assert store.get("err_streak", 0) == 0, "flood must not count as error"
    await mod._worker()
    assert len(client.iter_calls) == 1, "next_at gate ignored"
    tick(store)
    await mod._worker()
    assert client.uploaded == ["badapple_10.jpg"], client.uploaded


async def test_pause():
    client = FakeClient(MSGS)
    store = {"enabled": False}
    mod = make_mod(client, store)
    tick(store)
    await mod._worker()
    assert client.iter_calls == [] and client.uploaded == []


async def test_default_paused():
    client = FakeClient(MSGS)
    store = {}
    mod = make_mod(client, store)
    tick(store)
    await mod._worker()
    assert client.iter_calls == [] and client.uploaded == []


async def test_error_streak_notify():
    client = FakeClient(MSGS, fail_times=100)
    store = {"enabled": True}
    mod = make_mod(client, store)
    for _ in range(12):
        tick(store)
        await mod._worker()
    assert store.get("err_streak", 0) == 12, store
    assert len(client.sent) == 1, client.sent


async def test_error_streak_reset():
    client = FakeClient(MSGS, fail_times=3)
    store = {"enabled": True}
    mod = make_mod(client, store)
    for _ in range(3):
        tick(store)
        await mod._worker()
    assert store.get("err_streak", 0) == 3, store
    assert client.sent == []
    tick(store)
    await mod._worker()
    assert store["err_streak"] == 0
    assert client.uploaded == ["badapple_10.jpg"], client.uploaded


async def test_interval_command():
    assert badapple.DEFAULT_INTERVAL == 15
    store = {}
    mod = make_mod(FakeClient(MSGS), store)
    msg = FakeMessage("3")
    await mod.badapple_interval(msg)
    assert "interval" not in store and msg.replies
    msg = FakeMessage("120")
    await mod.badapple_interval(msg)
    assert store["interval"] == 120


async def test_channel_command():
    store = {"last_id": 555, "uploaded": 9, "done": True}
    mod = make_mod(FakeClient(MSGS), store)
    await mod.badapple_channel(FakeMessage("https://t.me/somechan"))
    assert store["channel"] == "somechan", store
    assert store["last_id"] == 0 and store["done"] is False


async def test_wipe_requires_confirm():
    mod = make_mod(FakeClient(MSGS), {})
    assert hasattr(mod, "badapple_wipe")
    store = {"channel": "x", "interval": 99, "enabled": True, "last_id": 555}
    mod = make_mod(FakeClient(MSGS), store)
    msg = FakeMessage("")
    await mod.badapple_wipe(msg)
    assert store["last_id"] == 555 and store["interval"] == 99, store
    assert msg.replies and "confirm" in msg.replies[0]


async def test_wipe_confirm():
    mod = make_mod(FakeClient(MSGS), {})
    assert hasattr(mod, "badapple_wipe")
    store = {
        "channel": "x",
        "interval": 99,
        "enabled": True,
        "last_id": 555,
        "uploaded": 9,
        "done": True,
    }
    mod = make_mod(FakeClient(MSGS), store)
    await mod.badapple_wipe(FakeMessage("confirm"))
    assert mod.get("last_id", 0) == 0
    assert mod.get("uploaded", 0) == 0
    assert mod.get("enabled", False) is False
    assert mod.get("done", False) is False
    assert mod.get("interval", badapple.DEFAULT_INTERVAL) == badapple.DEFAULT_INTERVAL
    assert mod.get("channel", badapple.DEFAULT_CHANNEL) == badapple.DEFAULT_CHANNEL


TESTS = [
    test_order_and_cursor,
    test_resume_after_restart,
    test_flood_wait,
    test_pause,
    test_default_paused,
    test_error_streak_notify,
    test_error_streak_reset,
    test_interval_command,
    test_channel_command,
    test_wipe_requires_confirm,
    test_wipe_confirm,
]
PREDICTED_MUTATION_FAILURES = {"test_order_and_cursor", "test_resume_after_restart"}


async def main():
    failed = set()
    for test in TESTS:
        try:
            await test()
            print(f"PASS {test.__name__}")
        except AssertionError as e:
            failed.add(test.__name__)
            print(f"FAIL {test.__name__}: {e}")
    shutil.rmtree(TMP, ignore_errors=True)
    if MUTATE:
        ok = failed == PREDICTED_MUTATION_FAILURES
        print(f"mutation run: failed={sorted(failed)} predicted={sorted(PREDICTED_MUTATION_FAILURES)} -> {'OK' if ok else 'MISMATCH'}")
        return 0 if ok else 1
    print(f"{len(TESTS) - len(failed)}/{len(TESTS)} passed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))