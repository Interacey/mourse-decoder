"""Loop counters ({Loop 5 i}[i]) and calculations ([= i*2+1]) in macros."""

from __future__ import annotations

import glob
import json
import os
import random
import re
import string

import pytest
from mourse_decoder import macros
from mourse_decoder.macros import MacroError, parse

PACKAGE = os.path.join(os.path.dirname(macros.__file__))


def text_of(macro: str) -> str:
    steps = parse(macro, lambda _combo: None)
    assert all(kind == "text" for kind, _ in steps), steps
    return "".join(value for _, value in steps)


@pytest.mark.parametrize(
    ("macro", "expected"),
    [
        ("{Loop 5 i}[i] {EndLoop}", "0 1 2 3 4 "),  # counts from 0
        ("{Loop 5}[i] {EndLoop}", "0 1 2 3 4 "),  # no name: i
        ("{Loop 5 i}[= i*2+1] {EndLoop}", "1 3 5 7 9 "),
        ("{Loop 3 n}[n][= n]{EndLoop}", "001122"),
        ("{Schleife 3 i}[i]{SchleifenEnde}", "012"),
        ("{loop 2 Zaehler_1}[Zaehler_1]{ENDLOOP}", "01"),
        ("{ Loop 2 i }[i][= i + 5 ]{ EndLoop }", "0516"),
        ("{Loop 0 i}[i]{EndLoop}x", "x"),
        ("a{Loop 2}[i]{EndLoop}b", "a01b"),  # joins the text around it
        # nesting: every loop has its own counter
        ("{Loop 3 j}{Loop 2 i}[= i+j*10],{EndLoop}{EndLoop}", "0,1,10,11,20,21,"),
        ("{Loop 2 j}{Loop 2 i}[j][i] {EndLoop}{EndLoop}", "00 01 10 11 "),
        # the inner counter hides the outer one of the same name
        ("{Loop 2 i}{Loop 2 i}[i]{EndLoop}-{EndLoop}", "01-01-"),
        ("{Loop 2}{Loop 3}[i]{EndLoop}|[i]|{EndLoop}", "012|0|012|1|"),
        # precedence, parentheses, signs
        ("{Loop 1}[= 2+3*4]{EndLoop}", "14"),
        ("{Loop 1}[= (2+3)*4]{EndLoop}", "20"),
        ("{Loop 1}[= 10-4-3]{EndLoop}", "3"),
        ("{Loop 1}[= 100/10/5]{EndLoop}", "2"),
        ("{Loop 1}[= 2*3%4]{EndLoop}", "2"),
        ("{Loop 1}[= -3+5]{EndLoop}", "2"),
        ("{Loop 1}[= - -3]{EndLoop}", "3"),
        ("{Loop 1}[= +3--2]{EndLoop}", "5"),
        ("{Loop 1}[= -(1+2)*2]{EndLoop}", "-6"),
        ("{Loop 1}[= ((((7))))]{EndLoop}", "7"),
        # division and remainder cut toward zero (like C)
        ("{Loop 1}[= 7/2],[= -7/2],[= 7/-2],[= -7/-2]{EndLoop}", "3,-3,-3,3"),
        ("{Loop 1}[= 7%2],[= -7%2],[= 7%-2],[= -7%-2]{EndLoop}", "1,-1,1,-1"),
        ("{Loop 1}[= 1000000000000000]{EndLoop}", "1000000000000000"),
        ("{Loop 1}[= 1000000000000000*-1]{EndLoop}", "-1000000000000000"),
        ("{Loop 1}[= -3+5],[=-3],[= +3--2],[= -(1+2)*2]{EndLoop}", "2,-3,5,-6"),
        # any identifier is a name, also one that is a command or key name
        ("{Loop 2 Tab}[Tab]{EndLoop}", "01"),
        ("{Loop 2 Wait}[Wait][= Wait+1]{EndLoop}", "0112"),
        ("{Loop 2 F5}[F5]{EndLoop}", "01"),
        # brackets that are no counter or calculation stay text, in a loop and outside of it
        ("[i] [= i] [= 1+1] [[i]] [x] [1]", "[i] [= i] [= 1+1] [[i]] [x] [1]"),
        (
            "{Loop 2}[1][x][][ i ][I][=][=)][=-)][= ][i x][[1]]{EndLoop}",
            "[1][x][][ i ][I][=][=)][=-)][= ][i x][[1]]" * 2,
        ),
        ("{Loop 2 n}[i]-[n]{EndLoop}", "[i]-0[i]-1"),  # [i] is no counter here
        ("{Loop 2}[[i]]{EndLoop}", "[0][1]"),  # the counter, in brackets
        ("{Loop 2}[[i][i]{EndLoop}", "[00[11"),
    ],
)
def test_counter_and_calculation(macro, expected):
    assert text_of(macro) == expected


def test_braces_stay_keys_and_brackets_are_the_counter():
    steps = parse("{i}{Loop 2}{i}[i]{EndLoop}{i}[i]", lambda _combo: None)
    assert steps == [
        ("keys", "i"), ("keys", "i"), ("text", "0"), ("keys", "i"), ("text", "1"), ("keys", "i"), ("text", "[i]"),
    ]  # fmt: skip


def test_other_steps_in_a_loop_split_the_text():
    steps = parse("{Loop 2 n}a[n]{Wait 5}{Enter}{EndLoop}", lambda _combo: None)
    assert steps == [
        ("text", "a0"), ("wait", 0.005), ("keys", "Enter"),
        ("text", "a1"), ("wait", 0.005), ("keys", "Enter"),
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("macro", "message"),
    [
        ("{Loop 2 i}[= j]{EndLoop}", "unbekannte Variable 'j'"),
        ("{Loop 2 i}{Loop 2 j}{EndLoop}[= j]{EndLoop}", "unbekannte Variable 'j'"),
        ("{Loop 2 i}[= I]{EndLoop}", "unbekannte Variable 'I'"),  # case matters
        ("{Loop 2}[= 1/0]{EndLoop}", "Division durch 0"),
        ("{Loop 2}[= 5%0]{EndLoop}", "Division durch 0"),
        ("{Loop 3}[= 1/(i-1)]{EndLoop}", "Division durch 0"),  # only at the third round
        ("{Loop 1}[= 1+]{EndLoop}", "es fehlt ein Wert"),
        ("{Loop 1}[= (1+2]{EndLoop}", "Klammer nicht geschlossen"),
        ("{Loop 1}[= 1+2)]{EndLoop}", "unerwartet ')'"),
        ("{Loop 1}[= 1 2]{EndLoop}", "unerwartet '2'"),
        ("{Loop 1}[= 2**3]{EndLoop}", "unerwartet '*'"),
        ("{Loop 1}[= 2^3]{EndLoop}", "unerwartet '^'"),
        ("{Loop 1}[= i.5]{EndLoop}", "unerwartet '.'"),
        ("{Loop 1}[= abs(1)]{EndLoop}", "unbekannte Variable 'abs'"),
        ("{Loop 1}[= x__import__]{EndLoop}", "unbekannte Variable"),
        ("{Loop 1}[= 1;2]{EndLoop}", "unerwartet ';'"),
        ("{Loop 1}[= 1000000000000001]{EndLoop}", "Zahl zu groß"),
        ("{Loop 1}[= 1000000000000*1000000]{EndLoop}", "größer als"),
        ("{Loop 1}[= 1000000000000000+1]{EndLoop}", "größer als"),
        ("{Loop 1}[= (1000000000000000*10)/100]{EndLoop}", "größer als"),  # an in-between value counts too
        ("{Loop 1}[= " + "(" * 33 + "1" + ")" * 33 + "]{EndLoop}", "mehr als 32 Klammern"),
        ("{Loop 1}[= 1" + "+1" * 100 + "]{EndLoop}", "länger als 200 Zeichen"),  # 201 characters
        ("{Loop 2}{= i}{EndLoop}", "Unbekannter Befehl"),  # {= …} is no longer a command
        ("{Loop 1001 i}{EndLoop}", "höchstens 1000"),
        ("{Loop 2 i}[i]", "{EndLoop}"),
        ("{Loop 2 3x}{EndLoop}", "Unbekannter Befehl"),
    ],
)
def test_broken_calculations_are_rejected_before_anything_happens(macro, message):
    with pytest.raises(MacroError, match=re.escape(message)) as info:
        parse(macro, _strict_shortcut)
    assert "Nichts getippt" in str(info.value)


def _strict_shortcut(combo: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9]|enter|tab|esc|f5", combo.strip(), re.IGNORECASE):
        raise ValueError(f"keine Taste: {combo!r}")


def test_expression_of_200_characters_and_depth_32_are_allowed():
    assert text_of("{Loop 1}[= 1" + "+1" * 99 + "]{EndLoop}") == "100"  # 199 characters
    assert text_of("{Loop 1}[= " + "(" * 32 + "5" + ")" * 32 + "]{EndLoop}") == "5"


def test_limits_apply_after_the_values_are_put_in():
    # 1000 x 1000 rounds with a body that is empty in the source would be 0 characters in the old way of counting
    with pytest.raises(MacroError, match="zu viel Text"):
        parse("{Loop 1000}{Loop 1000}{Loop 2}{EndLoop}{EndLoop}{EndLoop}", lambda _c: None)
    # 6 characters in the source per round, 2 to 6 after the numbers are in
    with pytest.raises(MacroError, match="zu viel Text"):
        parse("{Loop 1000}{Loop 1000}[i][i]{EndLoop}{EndLoop}", lambda _c: None)
    assert len(text_of("{Loop 1000}[i]{EndLoop}")) == 2890  # 10 + 180 + 2700


def test_huge_nested_loop_stops_early(monkeypatch):
    rounds = 0
    original = macros._calc

    def counting(tree, env):
        nonlocal rounds
        rounds += 1
        return original(tree, env)

    monkeypatch.setattr(macros, "_calc", counting)
    with pytest.raises(MacroError):
        parse("{Loop 1000}{Loop 1000}{Loop 1000}[= i]{EndLoop}{EndLoop}{EndLoop}", lambda _c: None)
    assert rounds <= macros.MAX_EXPANDED_LENGTH  # stopped at the cap, not after a billion rounds


def test_values_are_never_read_as_commands():
    steps = parse("{Loop 2}{[i]{EndLoop}}", lambda _combo: pytest.fail("a value was read as a command"))
    assert steps == [("text", "{0{1}")]  # a value and the braces around it never form {0}
    # a value does not combine with the brackets around it to a new counter either
    assert text_of("{Loop 2 i}[[[i]]]{EndLoop}") == "[[0]]" + "[[1]]"
    assert text_of("{Loop 12 n}[n]{EndLoop}") == "01234567891011"  # 1 and 0 stay digits, never [1][0]


def test_a_counter_value_is_text_even_if_it_looks_like_a_key():
    steps = parse("{Loop 2 e}<[e]>{EndLoop}", lambda _combo: pytest.fail("read as a command"))
    assert steps == [("text", "<0><1>")]


# --- shipped alphabets and smiley sets did not change meaning ---


def test_all_shipped_codes_parse_as_plain_text():
    files = glob.glob(os.path.join(PACKAGE, "alphabets", "*.json")) + glob.glob(
        os.path.join(PACKAGE, "smiley_sets", "*.json")
    )
    assert len(files) >= 7
    count = 0
    for path in files:
        with open(path, encoding="utf-8-sig") as handle:
            codes = json.load(handle)["codes"]
        for sequence, text in codes.items():
            if isinstance(text, str) and text:
                assert parse(text, lambda _combo: None) == [("text", text)], (path, sequence)
                count += 1
    assert count > 250  # 292 when written


# --- fuzz and comparison with Python ---


def test_random_calculations_only_raise_macro_error():
    rng = random.Random(20240601)
    alphabet = "0123456789+-*/%() ij=.,;_ab[]" + string.ascii_letters[:5] + "\t€"
    for _ in range(3000):
        junk = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        macro = "{Loop 3 i}{Loop 2 j}[= " + junk + "]{EndLoop}{EndLoop}"
        if "{" in junk or "}" in junk:
            continue
        try:
            parse(macro, lambda _combo: None)
        except MacroError:
            pass


def _random_expression(rng: random.Random, depth: int = 0) -> str:
    if depth > 3 or rng.random() < 0.3:
        return rng.choice(["i", "j", str(rng.randint(0, 50))])
    left, right = _random_expression(rng, depth + 1), _random_expression(rng, depth + 1)
    return f"({left}{rng.choice('+-*')}{right})"


def test_calculations_match_python_integers():
    rng = random.Random(7)
    for _ in range(300):
        expr = _random_expression(rng)
        macro = "{Loop 3 j}{Loop 4 i}[= " + expr + "],{EndLoop}{EndLoop}"
        expected = "".join(f"{eval(expr, {}, {'i': i, 'j': j})}," for j in range(3) for i in range(4))
        assert text_of(macro) == expected, expr


def test_division_matches_python_for_non_negative_numbers():
    for a in range(0, 30):
        for b in range(1, 8):
            assert text_of(f"{{Loop 1}}[= {a}/{b}],[= {a}%{b}]{{EndLoop}}") == f"{a // b},{a % b}"


# --- a counter exists only in the loop body of one macro ---


def test_macros_do_not_influence_each_other():
    a, b = "{Loop 3 i}[i]{EndLoop}", "[i] [= i+1]"
    first = (text_of(a), text_of(b), text_of(a))
    second = (text_of(b), text_of(a), text_of(b))
    assert first[0] == first[2] == "012"
    assert first[1] == second[0] == second[2] == "[i] [= i+1]"
    assert second[1] == "012"


@pytest.mark.parametrize(
    ("macro", "expected"),
    [
        ("{Loop 3 i}[i]{EndLoop}[i][= i+1]", "012[i][= i+1]"),  # gone after {EndLoop}
        ("{Loop 2 i}[i]{EndLoop}-{Loop 3 i}[i]{EndLoop}", "01-012"),
        ("{Loop 2 a}[a]{EndLoop}{Loop 2 b}[a][b]{EndLoop}", "01[a]0[a]1"),  # siblings share nothing
        (
            "{Loop 2 a}{Loop 2 b}[a][b] {EndLoop}[b][a]{EndLoop}",
            "00 01 [b]010 11 [b]1",
        ),  # outer seen inside, not inner outside
    ],
)
def test_counter_lives_only_in_its_loop_body(macro, expected):
    assert text_of(macro) == expected


def test_same_text_gives_the_same_steps_every_time():
    macro = "{Loop 3 i}x[i]{Wait 5}{EndLoop}"
    assert parse(macro, lambda _c: None) == parse(macro, lambda _c: None)


def test_inner_counter_is_unknown_in_a_calculation_after_its_loop():
    with pytest.raises(MacroError, match="unbekannte Variable 'b'"):
        parse("{Loop 2 a}{Loop 2 b}{EndLoop}[= b]{EndLoop}", lambda _c: None)


# --- backslash escapes for ( ) [ ] { } ---

BS = chr(92)


def KEYS(_combo):
    pytest.fail("read as a command")


@pytest.mark.parametrize("char", list("[](){}"))
def test_escaped_bracket_is_the_plain_character(char):
    assert text_of(f"a{BS}{char}b") == f"a{char}b"
    assert text_of(f"{{Loop 2}}a{BS}{char}b{{EndLoop}}") == f"a{char}ba{char}b"


@pytest.mark.parametrize(
    ("macro", "expected"),
    [
        (BS + "{Wait 100}", "{Wait 100}"),  # no token, so nothing to wait for
        (BS + "{Wait 100" + BS + "}", "{Wait 100}"),
        ("x" + BS + "{Enter}y", "x{Enter}y"),
        ("{Loop 2 i}" + BS + "[i]{EndLoop}", "[i][i]"),
        ("{Loop 2 i}" + BS + "[= i+1]{EndLoop}", "[= i+1][= i+1]"),
        ("{Loop 2 i}" + BS + "[i" + BS + "][i]{EndLoop}", "[i]0[i]1"),
        ("{Loop 2 i}[i" + BS + "]{EndLoop}", "[i]" * 2),  # the closing bracket is escaped, so no counter
        ("{Loop 2 i}(" + BS + "(" + BS + ")[= (i+1)*2]){EndLoop}", "(()2)(()4)"),
        # \\ before a bracket is one backslash and the bracket stays active
        ("{Loop 2 i}" + BS * 2 + "[i]{EndLoop}", BS + "0" + BS + "1"),
        ("{Loop 1 i}" + BS * 2 + "[= i+5]{EndLoop}", BS + "5"),
        # everything else is untouched
        (BS + "a C:" + BS + "Users" + BS + "x", BS + "a C:" + BS + "Users" + BS + "x"),
        (BS * 2 + "a", BS * 2 + "a"),
        (BS * 3 + "a", BS * 3 + "a"),
        ("{Loop 2 i}C:" + BS + "Users" + BS + "[i]{EndLoop}", ("C:" + BS + "Users[i]") * 2),
        ("end" + BS, "end" + BS),
        (BS + "{" + BS + "{" + BS + "}", "{{}"),
    ],
)
def test_backslash_escapes(macro, expected):
    assert text_of(macro) == expected


def test_a_value_is_never_searched_for_escapes():
    # the loop body ends in a backslash, the values start with digits: nothing combines
    assert text_of("{Loop 12 i}[i]" + BS + " {EndLoop}") == "".join(f"{i}{BS} " for i in range(12))
    assert text_of("{Loop 3 i}" + BS + "[i]" + BS + "[{EndLoop}") == "[i][" * 3


def test_a_token_still_works_and_text_around_an_escape_stays_one_step():
    steps = parse("a" + BS + "[b{Wait 5}c" + BS + "]d", lambda _c: None)
    assert steps == [("text", "a[b"), ("wait", 0.005), ("text", "c]d")]
    steps = parse("{Loop 2}x" + BS + "[[i]" + BS + "]y{EndLoop}", lambda _c: None)
    assert steps == [("text", "x[0]y" + "x[1]y")]


def test_paths_and_window_titles_keep_their_backslashes():
    steps = parse("{File C:" + BS + "Users" + BS + "me" + BS + "a.txt}{WaitWindow C:" + BS + "x" + BS + "}", KEYS)
    assert steps == [("files", ["C:" + BS + "Users" + BS + "me" + BS + "a.txt"]), ("window", "C:" + BS + "x" + BS)]


def test_escaped_brace_inside_a_token_does_not_help():
    # known limit: a token ends at the first closing brace, a backslash before it does not change that
    with pytest.raises(MacroError):
        parse("{Wait 100" + BS + "}", _strict_shortcut)


def test_escape_with_long_expansion_still_hits_the_limit():
    with pytest.raises(MacroError, match="zu viel Text"):
        parse("{Loop 1000}{Loop 1000}" + BS + "[i" + BS + "]{EndLoop}{EndLoop}", lambda _c: None)


def test_random_text_with_backslashes_and_brackets_only_raises_macro_error():
    rng = random.Random(99)
    alphabet = BS * 3 + "[]{}()=i0 1+-Wait"
    for _ in range(3000):
        junk = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        for macro in (junk, "{Loop 2 i}" + junk + "{EndLoop}"):
            try:
                parse(macro, lambda _c: None)
            except MacroError:
                pass


def test_backslash_before_a_command_escapes_its_brace():
    with pytest.raises(MacroError, match="EndLoop"):
        parse("{Loop 2}end" + BS + "{EndLoop}", lambda _c: None)
    assert text_of("{Loop 2}end" + BS + " {EndLoop}") == ("end" + BS + " ") * 2
