"""Macros: output texts with {commands} in braces.

    "Hallo{Enter}"                       text, then a key
    "{Win+R}{WaitWindow Run}notepad"     wait until the Run window is in front
    "{Loop 3}Hi{Wait 500}{EndLoop}"      repeat, may be nested
    "{Loop 3 n}[n] [= n*2+1]{EndLoop}"   counter n counts from 0 (name optional, default i); [= …] calculates
    "{Clipboard}"                        type what is on the clipboard (e.g. a KeePass copy)
    "{File C:\\Users\\me\\doc.pdf}"        paste the file or folder (several: path|path)

The whole macro is checked before anything happens, so a typo like {Crtl+C} stops it
with a message instead of typing half a macro into some window. German command names
work too ({Warte 500}, {Schleife 3}…{SchleifenEnde}, {Zwischenablage}, {Datei …}).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from . import jsontext

_I = re.IGNORECASE
_BRACE = re.compile(r"[{}]")
_ESCAPABLE = "[](){}"  # a backslash before one of these is an escape
_WAIT = re.compile(r"\s*(?:wait|warte|warten)\s*(\d{1,5})\s*", _I)
_WAIT_WINDOW = re.compile(r"\s*(?:waitwindow|wartefenster)\s+(.+?)\s*", _I)
_CLIPBOARD = re.compile(r"\s*(?:clipboard|zwischenablage)\s*", _I)
_FILES = re.compile(r"\s*(?:file|files|datei|dateien|folder|ordner)\s+(.+?)\s*", _I)
_LOOP = re.compile(r"\s*(?:loop|schleife)\s+(\d{1,5})(?:\s+([A-Za-z_][A-Za-z0-9_]*))?\s*", _I)
_END_LOOP = re.compile(r"\s*(?:endloop|schleifenende)\s*", _I)
_CALC = re.compile(r"\s*=(.*)", re.DOTALL)
_CALC_TOKEN = re.compile(r"\s*(?:([0-9]+)|([A-Za-z_][A-Za-z0-9_]*)|(.))", re.DOTALL)
# [x] is a counter, [= …] a calculation; any other brackets stay text
_BRACKETS = re.compile(r"\[([^\[\]]*)\]")

MAX_LOOP_COUNT = 1000
# nested loops multiply, so cap the result before it eats the memory
MAX_EXPANDED_LENGTH = 1_000_000
DEFAULT_COUNTER = "i"
MAX_CALC_LENGTH = 200
MAX_CALC_DEPTH = 32  # parentheses inside each other
MAX_VALUE = 10**15  # no number in a calculation, result or not, is larger than this

# ("text", str) | ("keys", combo) | ("wait", seconds) | ("window", title part)
# | ("clipboard", None) | ("files", [path, ...])
Step = tuple[str, object]


class MacroError(RuntimeError):
    """The macro is broken or was stopped; the message is shown to the user."""


class MacroStopped(MacroError):
    """The user switched Morse input off on purpose; nothing to report."""


def resolve_path(raw: str) -> str:
    """Absolute local path; ``%USERPROFILE%`` and ``~`` work. MacroError for a network path.

    Even looking whether ``\\\\server\\share`` exists makes Windows log in at that server and hand
    over the user's NTLM hash, so a macro from a stranger must never get that far.
    """
    expanded = os.path.expandvars(os.path.expanduser(raw))
    # looked at before abspath: only Windows reads a backslash as a separator, and a path that starts
    # with two of them must be refused whatever system the check runs on
    if expanded.startswith(("\\\\", "//")):
        raise MacroError(f"Netzwerkpfade sind nicht erlaubt: {raw}. Nichts getippt.")
    return os.path.abspath(expanded)


def commands_in_file(path: Path) -> list[str]:
    """Commands in the texts of an alphabet or smiley file that DO something when keyed.

    Key combinations (``Win+R``), ``File`` and ``Clipboard``. Waiting and loops are harmless, plain
    text too. An unreadable file gives [] because importing it reports that anyway.
    """
    try:
        data = jsontext.loads(Path(path).read_text(encoding="utf-8-sig"), dict)
        values = [v for v in data["codes"].values() if isinstance(v, str)]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []
    found: dict[str, None] = {}  # a dict keeps the order of first use
    for text in values:
        try:
            steps = parse(text, lambda _combo: None)  # every combo counts here, typos too
        except MacroError:
            continue  # a broken macro stops by itself before it does anything
        for kind, value in steps:
            if kind == "keys":
                found[f"{{{value}}}"] = None
            elif kind in ("files", "clipboard"):
                found["{File …}" if kind == "files" else "{Clipboard}"] = None
    return list(found)


def _split_paths(spec: str) -> list[str]:
    """``a|b`` gives two paths ('|' and '"' cannot occur in Windows paths); quotes around a path are dropped."""
    paths = [p.strip().strip('"').strip() for p in spec.split("|")]
    if not all(paths):
        raise MacroError(f"Leerer Pfad in {{File {spec}}}. Nichts getippt.")
    return paths


def _fail(message: str) -> MacroError:
    return MacroError(f"{message} Nichts getippt.")


class _CalcParser:
    """Recursive descent for integers, counters, ``+ - * / %``, parentheses and unary signs.

    Returns a tree of tuples. ``/`` and ``%`` cut toward zero like C: -7/2 is -3, -7%2 is -1.
    """

    def __init__(self, source: str, scope: frozenset[str]) -> None:
        self.source, self.scope, self.depth, self.pos = source.strip(), scope, 0, 0
        # (text, is a number, is a name)
        self.tokens = [(m[1] or m[2] or m[3], bool(m[1]), bool(m[2])) for m in _CALC_TOKEN.finditer(self.source)]

    def error(self, why: str) -> MacroError:
        return _fail(f"Fehler in der Rechnung [= {self.source}]: {why}.")

    def peek(self) -> str:
        return self.tokens[self.pos][0] if self.pos < len(self.tokens) else ""

    def take(self) -> str:
        self.pos += 1
        return self.tokens[self.pos - 1][0]

    def parse(self) -> tuple:
        if len(self.source) > MAX_CALC_LENGTH:
            raise self.error(f"länger als {MAX_CALC_LENGTH} Zeichen")
        tree = self.sum()
        if self.pos < len(self.tokens):
            raise self.error(f"unerwartet '{self.peek()}'")
        return tree

    def sum(self) -> tuple:
        left = self.product()
        while self.peek() in ("+", "-"):
            op = self.take()
            left = ("bin", op, left, self.product())
        return left

    def product(self) -> tuple:
        left = self.unary()
        while self.peek() in ("*", "/", "%"):
            op = self.take()
            left = ("bin", op, left, self.unary())
        return left

    def unary(self) -> tuple:
        if self.peek() in ("-", "+"):
            negate = self.take() == "-"
            operand = self.unary()
            return ("neg", operand) if negate else operand
        return self.atom()

    def atom(self) -> tuple:
        if self.pos >= len(self.tokens):
            raise self.error("es fehlt ein Wert")
        text, is_number, is_name = self.tokens[self.pos]
        self.pos += 1
        if is_number:
            if int(text) > MAX_VALUE:
                raise self.error("Zahl zu groß")
            return ("num", int(text))
        if is_name:
            if text not in self.scope:
                raise self.error(f"unbekannte Variable '{text}'")
            return ("var", text)
        if text != "(":
            raise self.error(f"unerwartet '{text}'")
        self.depth += 1
        if self.depth > MAX_CALC_DEPTH:
            raise self.error(f"mehr als {MAX_CALC_DEPTH} Klammern ineinander")
        inner = self.sum()
        if self.peek() != ")":
            raise self.error("Klammer nicht geschlossen")
        self.pos += 1
        self.depth -= 1
        return inner


def _calc(tree: tuple, env: dict[str, int]) -> int:
    kind = tree[0]
    if kind == "num":
        return tree[1]
    if kind == "var":
        return env[tree[1]]
    if kind == "neg":
        value = -_calc(tree[1], env)
    else:
        _, op, left, right = tree
        a, b = _calc(left, env), _calc(right, env)
        if op == "+":
            value = a + b
        elif op == "-":
            value = a - b
        elif op == "*":
            value = a * b
        elif b == 0:
            raise _fail("Division durch 0 in einer Rechnung.")
        else:
            quotient = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)
            value = quotient if op == "/" else a - b * quotient
    if abs(value) > MAX_VALUE:
        raise _fail(f"Ein Wert in einer Rechnung ist größer als {MAX_VALUE}.")
    return value


def _split(text: str) -> list:
    """Cut a macro into text and {token} parts, alternating, starting and ending with text.

    A text part is (text, mask). Escapes are resolved in the text: ``\\[`` gives ``[``, and ``\\\\`` before one
    of ``[](){}`` gives ``\\``. The mask has NUL where a bracket was escaped, so a search for [counter] runs on
    the mask and never sees it. The inside of a {token} is kept as it is because Windows paths contain backslashes.
    """
    parts: list = []
    text_out: list[str] = []
    mask: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        after = text[i + 1 : i + 2]
        if c == "\\" and after and after in _ESCAPABLE:
            text_out.append(after)
            mask.append("\0")
            i += 2
            continue
        if c == "\\" and after == "\\" and text[i + 2 : i + 3] and text[i + 2] in _ESCAPABLE:
            text_out.append(c)  # the bracket after \\ is not escaped
            mask.append(c)
            i += 2
            continue
        if c == "{" and (end := _BRACE.search(text, i + 1)) and end[0] == "}" and end.start() > i + 1:
            parts += [("".join(text_out), "".join(mask)), text[i + 1 : end.start()]]
            text_out, mask = [], []
            i = end.end()
            continue
        text_out.append(c)
        mask.append(c)
        i += 1
    parts.append(("".join(text_out), "".join(mask)))
    return parts


def _text_nodes(part: tuple[str, str], counters: list[str]) -> list[tuple]:
    """Plain text with the [counter] and [= calculation] parts cut out; outside loops all of it is text."""
    text, mask = part
    nodes: list[tuple] = []
    last = 0
    for m in _BRACKETS.finditer(mask) if counters else ():
        inner = text[m.start(1) : m.end(1)]
        calc = _CALC.fullmatch(inner)
        if calc and not (calc[1].lstrip("+- ")[:1].isalnum() or calc[1].lstrip("+- ")[:1] == "("):
            calc = None  # [=)] and [=-)] are smileys
        if inner in counters:
            node = ("var", inner)
        elif calc:
            node = ("calc", _CalcParser(calc[1], frozenset(counters)).parse())
        else:
            continue  # stays text
        nodes += [("text", text[last : m.start()]), node]
        last = m.end()
    nodes.append(("text", text[last:]))
    return [n for n in nodes if n != ("text", "")]


# tree nodes: ("text", str) | ("step", Step) | ("var", name) | ("calc", tree) | ("loop", count, name, body)
def _build(text: str, check_shortcut: Callable[[str], None]) -> list[tuple]:
    """Split a macro into a tree in which a loop holds its body. Every token is checked here."""
    root: list[tuple] = []
    open_loops: list[tuple[list[tuple], str]] = []  # (the list the loop sits in, its counter)
    body = root
    for i, part in enumerate(_split(text)):
        if not i % 2:
            body.extend(_text_nodes(part, [name for _, name in open_loops]))
        elif loop := _LOOP.fullmatch(part):
            count, name = int(loop[1]), loop[2] or DEFAULT_COUNTER
            if count > MAX_LOOP_COUNT:
                raise _fail(f"Schleife mit {count} Durchläufen – höchstens {MAX_LOOP_COUNT} erlaubt.")
            node = ("loop", count, name, [])
            body.append(node)
            open_loops.append((body, name))
            body = node[3]
        elif _END_LOOP.fullmatch(part):
            if not open_loops:
                raise _fail("Zu jedem {Loop …} gehört genau ein {EndLoop}.")
            body = open_loops.pop()[0]
        elif wait := _WAIT.fullmatch(part):
            body.append(("step", ("wait", int(wait[1]) / 1000)))
        elif window := _WAIT_WINDOW.fullmatch(part):
            body.append(("step", ("window", window[1])))
        elif _CLIPBOARD.fullmatch(part):
            body.append(("step", ("clipboard", None)))
        elif files := _FILES.fullmatch(part):
            body.append(("step", ("files", _split_paths(files[1]))))
        elif not any(c.isalnum() for c in part):
            body.append(("step", ("text", f"{{{part}}}")))  # {^_^} or { } is text, not a command
        else:
            try:
                check_shortcut(part)
            except ValueError as exc:
                raise MacroError(f"Unbekannter Befehl {{{part.strip()}}}: {exc}. Nichts getippt.") from None
            body.append(("step", ("keys", part.strip())))
    if open_loops:
        raise _fail("Zu jedem {Loop …} gehört genau ein {EndLoop}.")
    return root


def _expand(tree: list[tuple]) -> list[Step]:
    """Write out the loops. Counter values go in as plain text and are never read as commands."""
    out: list[tuple[str, object, bool]] = []  # (kind, value, plain text that may join its neighbours)
    spent = 0  # characters and loop rounds so far, so a huge result is stopped before it is built

    def spend(amount: int) -> None:
        nonlocal spent
        spent += amount
        if spent > MAX_EXPANDED_LENGTH:
            raise _fail("Die Schleifen ergeben zu viel Text.")

    def run(nodes: list[tuple], env: dict[str, int]) -> None:
        for node in nodes:
            if node[0] == "loop":
                for n in range(node[1]):
                    spend(1)
                    run(node[3], {**env, node[2]: n})
            elif node[0] == "step":
                spend(8)
                out.append((*node[1], False))
            else:
                if node[0] == "text":
                    value = node[1]
                else:
                    value = str(env[node[1]] if node[0] == "var" else _calc(node[1], env))
                spend(len(value))
                out.append(("text", value, True))

    run(tree, {})
    steps: list[Step] = []
    plain: list[str] = []  # plain text not yet added to steps; joined once, not piece by piece

    def flush() -> None:
        if plain:
            steps.append(("text", "".join(plain)))
            plain.clear()

    for kind, value, is_plain in out:
        if is_plain:
            plain.append(value)
        else:
            flush()
            steps.append((kind, value))
    flush()
    return [s for s in steps if s != ("text", "")]


def parse(text: str, check_shortcut: Callable[[str], None]) -> list[Step]:
    """Turn a macro into steps; MacroError if any part of it is broken.

    ``check_shortcut`` raises ValueError for anything that is no key combination.
    """
    return _expand(_build(text, check_shortcut))
