# -*- coding: utf-8 -*-
# Calculator — автопосчёт выражений в отправляемых сообщениях (Hikka / Heroku).
#
# Как работает: watcher ловит ТВОИ отправленные сообщения, и если ВСЁ сообщение
# целиком является математическим выражением — дописывает к нему результат.
# «240*15+199» ➜ «240*15+199 = 3799». Обычный текст не трогается: разбор strict,
# любое непонятное слово («15*4 рубля») = ошибка = не трогаем.
# Никакого eval(): свой токенайзер + рекурсивный спуск, с ограничением на размер.
#
# Про откат: после каждой правки в чат логов улетает форма с кнопками
# «↩️ откатить» / «ок». В терминал (heroku logs) пишется всё через logging, в TG
# из модуля приходит только эта форма — остальное Hikka сама дублирует лишь при
# фатальной ошибке.
#
# Команды:
#   .calc                      статус и справка
#   .calctoggle [on|off]       вкл/выкл автопосчёт
#   .calcsafe [on|off]         фильтр «похоже на жизнь»: телефон, счёт матча, года
#   .calclogs [чат|id]         куда кидать кнопку отката (без аргумента — текущий чат)
#   .calctest <выражение>      проверить без отправки и без правки
#   .calcundo                  откатить последний посчёт вручную
#
# Фильтр «жизни» пропускает «+7 912 345-67-89», «2-1» (счёт), «1991-2025» (года).
# Если написал «2-1 =» или «2-1?» — считаем всегда: знак на конце = явная просьба.
#
# Что понимает: + - * / // % ^ (), скобки, унарный минус, «x»/«х»/«×»/«·» как
# умножение, «÷», проценты (199+15% = 228.85, 500-10%, 20% of 50), % как остаток
# (10%3), sqrt/cbrt/abs и приставные √/∛ (√16), π, десятичная запятая (2,5*4),
# группы разрядов пробелом (1 000 000/3), точка-группировка только через две
# («1.234.567» = 1234567; «1.500» = 1,5).

import logging
import math
import re
import time

from .. import loader, utils

logger = logging.getLogger(__name__)

MODNAME = "Calculator"
MAX_EXPR = 200
MAX_DEPTH = 24
MAX_RESULT = 1e30
MAX_EXP = 256
FORM_TTL = 900
UNDO_TTL = 3600

# выражение из двух 4-значных чисел через минус — это обычно годы, а не пример
YEAR_RANGE = re.compile(r"^\d{4}\s*[-−–—]\s*\d{4}$")
# счёт матча: «2-1», «1x0», «3:2» — однозначные с обеих сторон
SCORE = re.compile(r"^\d\s*[-xх×:]\s*\d$")
# всё, что остаётся цифрами после вырезания телефонных разделителей
PHONE_MARKS = re.compile(r"[\s()\-\u2212\u2013\u2014+]")
GROUPED_DOT = re.compile(r"^\d{1,3}(\.\d{3}){2,}$")
NUM_CHARS = "0123456789.,"
LOG_MODULES = ("hikka_logs", "tester")

FUNCS = {
    "sqrt": (math.sqrt, lambda v: v < 0, "корень из отрицательного"),
    "cbrt": (lambda v: math.copysign(abs(v) ** (1 / 3), v), None, None),
    "abs": (abs, None, None),
}

CHAR_MAP = {
    "x": "*", "X": "*",
    "×": "*", "·": "*", "∙": "*", "✕": "*", "✖": "*", "х": "*", "Х": "*",
    "÷": "/", "∕": "/", "⁄": "/",
    "−": "-", "–": "-", "—": "-", "‐": "-", "‑": "-",
    "√": " sqrt ", "∛": " cbrt ",
    "π": " 3.14159265358979 ",
    "（": "(", "）": ")", "［": "(", "］": ")", "【": "(", "】": ")",
    "\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u2007": " ", "\u3000": " ",
    "_": " ",
}


class CalcError(Exception):
    pass


# ---------- разбор ----------


def _normalize(text):
    return "".join(CHAR_MAP.get(ch, ch) for ch in text)


def _parse_number(raw):
    """Вернуть (значение, была_ли_запятая)."""
    digits = raw.replace(" ", "").replace("\u00a0", "")
    if not re.fullmatch(r"\d*(?:[.,]\d*)*", digits) or not any(
        c.isdigit() for c in digits
    ):
        raise CalcError(f"не число «{raw.strip()}»")
    if GROUPED_DOT.match(digits):
        return int(digits.replace(".", "")), False
    comma = False
    if "," in digits and "." in digits:
        # «1.500,25» / «1,500.25» — последняя из точек/запятых и есть разделитель
        cut = max(digits.rfind(","), digits.rfind("."))
        whole = digits[:cut].replace(".", "").replace(",", "")
        frac = digits[cut + 1 :]
        comma = digits[cut] == ","
    elif "," in digits:
        parts = digits.split(",")
        if len(parts) > 2:
            raise CalcError("несколько запятых в числе")
        whole, frac = parts[0], parts[1]
        comma = True
    else:
        parts = digits.split(".")
        if len(parts) > 2:
            raise CalcError("не понимаю число «%s»" % raw.strip())
        whole, frac = parts[0], parts[1] if len(parts) > 1 else ""
    whole = whole or "0"
    if not frac:
        return int(whole), comma
    return float(whole + "." + frac), comma


def _tokenize(s):
    tokens = []
    comma = False
    index = 0
    length = len(s)
    while index < length:
        ch = s[index]
        if ch.isspace():
            index += 1
        elif ch in NUM_CHARS:
            end = index
            buf = []
            while end < length and (s[end].isdigit() or s[end] in "., \u00a0"):
                buf.append(s[end])
                end += 1
            raw = "".join(buf).strip().rstrip(".,")
            if not raw or not any(c.isdigit() for c in raw):
                raise CalcError("не понимаю, что за «%s»" % "".join(buf).strip())
            value, is_comma = _parse_number(raw)
            comma = comma or is_comma
            tokens.append(("num", value))
            index = end
        elif ch.isalpha():
            end = index
            while end < length and s[end].isalpha():
                end += 1
            word = s[index:end].lower()
            if word in FUNCS:
                tokens.append(("fn", word))
            elif word == "of":
                tokens.append(("of", None))
            else:
                raise CalcError("не понимаю слово «%s»" % s[index:end])
            index = end
        elif ch in "+*/^%":
            if ch == "%":
                tail = s[index + 1 :].lstrip()
                if tail[:1].isdigit() or tail[:1] == "(":
                    tokens.append(("op", "%"))
                else:
                    tokens.append(("pct", None))
            elif ch == "*" and s[index + 1 : index + 2] == "*":
                tokens.append(("op", "^"))
                index += 1
            elif ch == "/" and s[index + 1 : index + 2] == "/":
                tokens.append(("op", "//"))
                index += 1
            else:
                tokens.append(("op", ch))
            index += 1
        elif ch == "-":
            tokens.append(("op", "-"))
            index += 1
        elif ch in "([":
            tokens.append(("lp", None))
            index += 1
        elif ch in ")]":
            tokens.append(("rp", None))
            index += 1
        elif ch == "=":
            raise CalcError("несколько «=»")
        else:
            raise CalcError("не понимаю символ «%s»" % ch)
    return tokens, comma


def _cap(value):
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise CalcError("результат не число")
    if abs(value) > MAX_RESULT:
        raise CalcError("слишком большое число")
    return value


def _power(base, exp):
    if base == 0 and exp < 0:
        raise CalcError("деление на ноль")
    if isinstance(exp, float):
        if not exp.is_integer():
            if base < 0:
                raise CalcError("отрицательное в нецелой степени")
            return _cap(float(base) ** exp)
        exp = int(exp)
    if abs(exp) > MAX_EXP:
        raise CalcError("слишком большая степень")
    if exp > 0 and abs(base) > 1:
        if exp * math.log10(max(abs(base), 1.000001)) > 30:
            raise CalcError("слишком большое число")
    return _cap(base ** exp)


def _call(name, value):
    func, bad, reason = FUNCS[name]
    if bad is not None and bad(value):
        raise CalcError(reason)
    return _cap(func(value))


class _Parser:
    def __init__(self, tokens):
        self.t = tokens
        self.i = 0
        self.depth = 0

    def _enter(self):
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise CalcError("слишком много скобок")
        self.i += 1

    def peek(self, offset=0):
        position = self.i + offset
        if position < len(self.t):
            return self.t[position]
        return (None, None)

    def parse(self):
        value = self.addsub()
        kind, extra = self.peek()
        if kind is not None:
            raise CalcError("не понимаю хвост «%s»" % extra)
        return value

    def addsub(self):
        value = self.muldiv()
        while True:
            kind, op = self.peek()
            if kind != "op" or op not in ("+", "-"):
                break
            self.i += 1
            # относительный процент: «199 + 15%» = 199 * 1.15
            after = self.peek(2)
            if (
                self.peek()[0] == "num"
                and self.peek(1)[0] == "pct"
                and (after[0] in (None, "rp") or after in (("op", "+"), ("op", "-")))
            ):
                part = value * self.peek()[1] / 100
                self.i += 2
                value = _cap(value + part if op == "+" else value - part)
                continue
            right = self.muldiv()
            value = _cap(value + right) if op == "+" else _cap(value - right)
        return value

    def muldiv(self):
        value = self.factor()
        while True:
            kind, op = self.peek()
            if kind == "pct":
                self.i += 1
                value = _cap(value / 100)
                continue
            if kind == "of":
                self.i += 1
                value = _cap(value * self.factor())
                continue
            if kind == "op" and op in ("*", "/", "//", "%"):
                self.i += 1
                right = self.factor()
                if right == 0 and op in ("/", "//", "%"):
                    raise CalcError("деление на ноль")
                if op == "*":
                    value = _cap(value * right)
                elif op == "/":
                    value = _cap(value / right)
                elif op == "//":
                    value = _cap(value // right)
                else:
                    value = _cap(value % right)
                continue
            if kind in ("num", "fn", "lp"):
                # неявное умножение: «2(3+1)»
                value = _cap(value * self.factor())
                continue
            break
        return value

    def factor(self):
        kind, op = self.peek()
        if kind == "op" and op in ("+", "-"):
            self.i += 1
            value = self.factor()
            return -value if op == "-" else value
        return self.power()

    def power(self):
        base = self.atom()
        if self.peek() == ("op", "^"):
            self.i += 1
            return _power(base, self.factor())
        return base

    def atom(self):
        kind, value = self.peek()
        if kind == "num":
            self.i += 1
            return value
        if kind == "fn":
            self.i += 1
            if self.peek()[0] == "lp":
                self._enter()
                inner = self.addsub()
                if self.peek()[0] != "rp":
                    raise CalcError("нет закрывающей скобки")
                self.i += 1
                self.depth -= 1
                return _call(value, inner)
            return _call(value, self.factor())
        if kind == "lp":
            self._enter()
            inner = self.addsub()
            if self.peek()[0] != "rp":
                raise CalcError("нет закрывающей скобки")
            self.i += 1
            self.depth -= 1
            return inner
        if kind is None:
            raise CalcError("пустое выражение")
        raise CalcError("неожиданный «%s»" % value)


def _strip_markers(text):
    result = text.strip()
    changed = True
    while changed:
        changed = False
        for tail in ("=?", "?=", "=", "?"):
            if result.endswith(tail):
                result = result[: -len(tail)].rstrip()
                changed = True
                break
    while result.startswith("="):
        result = result[1:].lstrip()
    return result


def _looks_like_life(text):
    """Телефон / счёт матча / диапазон годов — строки, похожие на пример, но бытовые."""
    stripped = text.strip()
    if YEAR_RANGE.match(stripped):
        return "диапазон годов"
    if SCORE.match(stripped):
        return "счёт матча"
    digits = re.sub(r"\D", "", stripped)
    if PHONE_MARKS.sub("", stripped).isdigit() and len(digits) in (10, 11):
        if digits[:1] in ("7", "8") or stripped.startswith("+"):
            return "телефон"
    return None


def evaluate(text):
    """Вернуть (готовая строка результата, None) либо (None, текст ошибки)."""
    try:
        return _evaluate(text)
    except CalcError as exc:
        return None, str(exc)
    except (OverflowError, ValueError, ZeroDivisionError, TypeError) as exc:
        return None, "не посчиталось: %s" % exc


def _evaluate(text):
    body = _strip_markers(text)
    if not body:
        return None, "пусто"
    if len(body) > MAX_EXPR:
        return None, "слишком длинно"
    if not any(ch.isdigit() for ch in body):
        return None, "нет чисел"
    tokens, comma = _tokenize(_normalize(body))
    nums = sum(1 for kind, _ in tokens if kind == "num")
    has_fn = any(kind == "fn" for kind, _ in tokens)
    if not any(kind in ("op", "pct", "fn", "of") for kind, _ in tokens):
        return None, "нет действия"
    if nums < 2 and not has_fn:
        # одиночное число с минусом/процентом: «-5», «50%» — не пример
        return None, "нет действия"
    value = _Parser(tokens).parse()
    if isinstance(value, int):
        shown = str(value)
    else:
        magnitude = abs(value)
        if magnitude != 0 and (magnitude >= 1e16 or magnitude < 1e-9):
            shown = "%.6g" % value
        elif float(value).is_integer() and magnitude < 1e15:
            shown = str(int(value))
        else:
            shown = ("%.10f" % value).rstrip("0").rstrip(".")
    if comma:
        shown = shown.replace(".", ",")
    return shown, None


# ---------- модуль ----------


@loader.tds
class CalculatorMod(loader.Module):
    """Автопосчёт выражений в отправляемых сообщениях, с откатом в логи"""

    strings = {"name": MODNAME}

    async def client_ready(self, client, db):
        self.client = client
        self.db = db
        self._undo = {}
        self._edited = {}
        if self._get("enabled") is None:
            self._set("enabled", True)
        if self._get("safe") is None:
            self._set("safe", True)

    # ---------- БД ----------

    def _get(self, key, default=None):
        try:
            value = self.db.get(MODNAME, key)
            return default if value is None else value
        except Exception as exc:
            logger.error("[Calculator] db read %s: %r", key, exc)
            return default

    def _set(self, key, value):
        try:
            self.db.set(MODNAME, key, value)
        except Exception as exc:
            logger.error("[Calculator] db write %s: %r", key, exc)

    # ---------- чат логов ----------

    def _logchat(self):
        """Куда кидать откат. Свой чат важнее, иначе берём hikka-logs."""
        own = self._get("logchat")
        if isinstance(own, int) and own:
            return own
        for name in LOG_MODULES:
            try:
                mod = self.lookup(name)
            except Exception:
                continue
            chat = getattr(mod, "logchat", None)
            if isinstance(chat, int) and chat:
                return chat
        return None

    def _chat_name(self, message):
        chat = getattr(message, "chat", None)
        if chat is None:
            return "?"
        title = getattr(chat, "title", None)
        if title:
            return title
        first = getattr(chat, "first_name", None)
        return ("л/с %s" % first) if first else "?"

    # ---------- откат ----------

    def _prune(self):
        cutoff = time.time() - UNDO_TTL
        for key in [k for k, v in self._undo.items() if v["ts"] < cutoff]:
            self._undo.pop(key, None)

    async def _notify(self, message, text, shown, key):
        """Всё остальное — только в терминал; в TG летит только форма с откатом."""
        chat = self._logchat()
        if not chat:
            logger.info(
                "[Calculator] %s = %s (msg %s), но чат логов не задан — .calclogs",
                text, shown, message.id,
            )
            return
        header = (
            "🧮 <b>Calculator</b>\n"
            f"<code>{utils.escape_html(text)}</code> = <b>{utils.escape_html(shown)}</b>\n"
            f"💬 <i>{utils.escape_html(self._chat_name(message))}</i>"
        )
        try:
            form = await self.inline.form(
                header + "\n\nОткатить правку?",
                message=chat,
                reply_markup=[
                    [
                        {"text": "↩️ откатить", "callback": self._inline_undo,
                         "args": (key,)},
                        {"text": "ок", "callback": self._inline_keep, "args": (key,)},
                    ]
                ],
                ttl=FORM_TTL,
                force_me=True,
                on_unload=lambda *_: self._undo.pop(key, None),
            )
        except Exception as exc:
            logger.warning("[Calculator] inline form failed: %r", exc)
            form = None
        if form:
            return
        try:
            await self.client.send_message(
                chat,
                header + f"\n\nОткат: <code>.calcundo</code> (кнопки не работают — "
                "инлайн-бот не настроен)",
            )
        except Exception as exc:
            logger.warning("[Calculator] logchat send failed: %r", exc)

    async def _revert(self, key):
        entry = self._undo.pop(key, None)
        if entry is None:
            return False, "уж поздно — посчёт забыт"
        try:
            await entry["msg"].edit(entry["orig"])
        except Exception as exc:
            logger.warning("[Calculator] revert failed (msg %s): %r", entry["id"], exc)
            return False, "не откатилось: %s" % exc
        return True, "откатил"

    async def _inline_undo(self, call, key):
        done, note = await self._revert(key)
        await call.answer(note, alert=not done)
        await call.delete()

    async def _inline_keep(self, call, key):
        self._undo.pop(key, None)
        await call.answer("ок")
        await call.delete()

    # ---------- watcher ----------

    @loader.watcher()
    async def watcher(self, message):
        try:
            if not getattr(message, "out", False):
                return
            if not self._get("enabled", True):
                return
            if getattr(message, "fwd_from", None) or getattr(message, "via_bot_id", None):
                return
            if getattr(message, "photo", None) or getattr(message, "file", None):
                return
            text = getattr(message, "text", None) or ""
            if not isinstance(text, str) or "\n" in text:
                return
            for prefix in self.get_prefixes():
                if prefix and text.startswith(prefix):
                    return
            ts = self._edited.get(message.id)
            if ts is not None and time.time() - ts < 300:
                return
            stripped = text.strip()
            # «=» или «?» на конце = явная просьба посчитать, фильтр молчит
            if self._get("safe", True) and not stripped.endswith(("=", "?")):
                life = _looks_like_life(stripped)
                if life:
                    logger.debug("[Calculator] пропуск %r: %s", text, life)
                    return

            shown, error = evaluate(text)
            if error:
                # не сработало — это не событие, только в терминал
                logger.debug("[Calculator] пропуск %r: %s", text, error)
                return

            base = stripped
            glue = "" if base.endswith("=") else " ="
            new_text = base + glue + " " + shown
            if not 0 < len(new_text) <= 4096:
                return
            try:
                await message.edit(new_text)
            except Exception as exc:
                logger.warning("[Calculator] edit failed (msg %s): %r", message.id, exc)
                return

            self._edited[message.id] = time.time()
            if len(self._edited) > 500:
                cutoff = time.time() - 600
                self._edited = {k: v for k, v in self._edited.items() if v > cutoff}
            key = "%s:%s" % (getattr(message, "chat_id", 0), message.id)
            self._prune()
            self._undo[key] = {
                "msg": message, "orig": text, "id": message.id, "ts": time.time(),
            }
            logger.info("[Calculator] %s = %s (msg %s)", base, shown, message.id)
            await self._notify(message, base, shown, key)
        except Exception as exc:
            logger.exception("[Calculator] watcher error: %r", exc)

    # ---------- команды ----------

    @loader.command(
        ru_doc="Статус и справка по Calculator",
        en_doc="Calculator status and help",
    )
    async def calc(self, message):
        """Show status and help"""
        state = "🟢 вкл" if self._get("enabled", True) else "🔴 выкл"
        safe = "🟢 вкл" if self._get("safe", True) else "🔴 выкл"
        chat = self._logchat()
        logs = f"<code>{chat}</code>" if chat else "⚠️ не задан (<code>.calclogs</code>)"
        await utils.answer(
            message,
            f"🧮 <b>Calculator</b> — {state}, фильтр «жизни» — {safe}\n"
            f"📁 Откаты улетают в — {logs}\n\n"
            "Пишешь <code>240*15+199</code> ➜ становится <code>240*15+199 = 3799</code>\n\n"
            "<code>.calctoggle [on|off]</code> — вкл/выкл\n"
            "<code>.calcsafe [on|off]</code> — фильтр телефон/счёт/года\n"
            "<code>.calclogs [чат]</code> — куда кидать кнопку отката\n"
            "<code>.calctest 2,5*4</code> — проверить, ничего не правя\n"
            "<code>.calcundo</code> — откатить последний посчёт\n\n"
            "Считает <b>только если всё сообщение — пример</b>. Можно: "
            "<code>+ - * / // % ^ ()</code>, <code>x</code>/<code>х</code>/<code>×</code> "
            "как умножение, <code>÷</code>, проценты (<code>199+15%</code>, "
            "<code>20% of 50</code>), <code>sqrt</code>/<code>√</code>/<code>cbrt</code>/"
            "<code>abs</code>, <code>π</code>, запятая (<code>2,5</code>) и пробелы "
            "в числах (<code>1 000 000</code>). «=» на конце не обязателен, но можно.",
        )

    @loader.command(
        ru_doc="Вкл/выкл автопосчёт: .calctoggle [on|off]; без аргументов — переключить",
        en_doc="Toggle auto-calc: .calctoggle [on|off]; no args flips",
    )
    async def calctoggle(self, message):
        """Toggle the calculator on/off"""
        new = _switch(utils.get_args_raw(message).strip().lower(), self._get("enabled", True))
        if isinstance(new, str):
            await utils.answer(message, new)
            return
        self._set("enabled", new)
        await utils.answer(
            message,
            "🟢 Автопосчёт включен." if new else "🔴 Автопосчёт выключен.",
        )

    @loader.command(
        ru_doc="Вкл/выкл фильтр телефон/счёт/года: .calcsafe [on|off]",
        en_doc="Toggle phone/score/year guard: .calcsafe [on|off]",
    )
    async def calcsafe(self, message):
        """Toggle the looks-like-life guard"""
        new = _switch(utils.get_args_raw(message).strip().lower(), self._get("safe", True))
        if isinstance(new, str):
            await utils.answer(message, new)
            return
        self._set("safe", new)
        note = (
            "🛡 Фильтр включен: «+7 912 345-67-89», «2-1», «1991-2025» не тронем. "
            "Знак «=» или «?» на конце обходит фильтр."
            if new
            else "🛡 Фильтр выключен: посчитаем и телефон, и счёт матча."
        )
        await utils.answer(message, note)

    @loader.command(
        ru_doc="Проверить выражение без правки: .calctest 240*15+199",
        en_doc="Evaluate without editing: .calctest 240*15+199",
    )
    async def calctest(self, message):
        """Evaluate an expression without touching anything"""
        raw = utils.get_args_raw(message)
        if not raw.strip():
            await utils.answer(
                message, "❌ Укажи выражение: <code>.calctest 240*15+199</code>"
            )
            return
        shown, error = evaluate(raw)
        if error:
            await utils.answer(
                message,
                f"❌ Не считаю: <code>{utils.escape_html(error)}</code>\n"
                f"<code>{utils.escape_html(raw.strip())}</code>",
            )
            return
        await utils.answer(
            message,
            f"<code>{utils.escape_html(raw.strip())}</code> = <b>{utils.escape_html(shown)}</b>",
        )

    @loader.command(
        ru_doc="Куда кидать кнопку отката: .calclogs [чат|off]; без аргумента — текущий чат",
        en_doc="Where to send the undo form: .calclogs [chat|off]; no args = current chat",
    )
    async def calclogs(self, message):
        """Set the chat that receives undo forms"""
        raw = utils.get_args_raw(message).strip().lower()
        if raw in ("off", "выкл", "auto"):
            self._set("logchat", None)
            await utils.answer(
                message,
                "📁 Свой чат сброшен — буду брать чат логов Hikka (hikka-logs).",
            )
            return
        target = message.chat_id
        if raw:
            resolved = None
            if re.fullmatch(r"-?\d+", raw):
                resolved = int(raw)
            else:
                try:
                    resolved = await utils.get_target(message, 0)
                except Exception as exc:
                    logger.debug("[Calculator] get_target: %r", exc)
                    resolved = None
            if not resolved:
                await utils.answer(
                    message,
                    "❌ Не понял чат. Варианты: id числом, @юзернейм, ссылка-приглашение, "
                    "или без аргумента (текущий чат).",
                )
                return
            target = resolved
        self._set("logchat", target)
        await utils.answer(
            message,
            f"📁 Откаты буду кидать сюда: <code>{target}</code>. Проверка — следующим "
            "сообщением-примером.",
        )

    @loader.command(
        ru_doc="Откатить последний посчёт: .calcundo",
        en_doc="Revert the last calculation: .calcundo",
    )
    async def calcundo(self, message):
        """Revert the most recent calculation edit"""
        self._prune()
        if not self._undo:
            await utils.answer(message, "ℹ️ Откатывать нечего — свежих правок нет.")
            return
        key = max(self._undo, key=lambda k: self._undo[k]["ts"])
        done, note = await self._revert(key)
        await utils.answer(
            message,
            ("↩️ " if done else "❌ ") + note,
        )


def _switch(raw, current):
    if raw in ("on", "1", "вкл", "enable"):
        return True
    if raw in ("off", "0", "выкл", "disable"):
        return False
    if not raw:
        return not current
    return "❌ Аргумент: <code>on</code> / <code>off</code> / пусто."
