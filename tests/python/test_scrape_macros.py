"""The {Scrape} / {Ausschnitt} command: parsing, cutting text out of the clipboard, limits and the controller step."""

from __future__ import annotations

import json

import pytest
from mourse_decoder import macros
from mourse_decoder.decoders import Emission
from mourse_decoder.macros import MAX_EXPANDED_LENGTH, MAX_SCRAPE_INPUT, MAX_SCRAPE_RESULT, MacroError
from test_delay_rollback import make_controller, run_macro


def no_shortcut(combo):
    raise ValueError(combo)  # a scrape must never be read as a key combination


def parse(text):
    return macros.parse(text, no_shortcut)


def scrape(macro, clipboard):
    """Parse a macro that holds one {Scrape} and cut it out of ``clipboard``."""
    ((kind, value),) = parse(macro)
    assert kind == "scrape"
    return macros.scrape_text(value, clipboard)


def write_set(tmp_path, codes):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"id": "x", "codes": codes}), encoding="utf-8")
    return path


# --- parsing -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "macro",
    [
        "{Scrape a | b | => $1}",
        "{scrape a|b|=>$1}",
        "{SCRAPE  a  |  b  |  =>  $1  }",
        "{ Scrape a | b | => $1 }",
        "{Ausschnitt a | b | => $1}",
        "{ausschnitt a|b|=>$1}",
        "{AUSSCHNITT a | b | => $1}",
    ],
)
def test_spellings_give_one_scrape_step(macro):
    assert parse(macro) == [("scrape", macros.Scrape((("a", "b"),), (1,)))]


def test_two_pairs_and_a_template_with_text_around_the_placeholders():
    ((kind, value),) = parse("{Scrape Name: | \\n | Preis: | € | => $1 zahlt $2 Euro}")
    assert kind == "scrape"
    assert value.pairs == (("Name:", "\n"), ("Preis:", "€"))
    assert value.template == (1, " zahlt ", 2, " Euro")


def test_one_pair_with_an_empty_end_marker():
    ((_, value),) = parse("{Scrape Preis: | | => $1}")
    assert value.pairs == (("Preis:", ""),)


def test_scrape_between_text_and_keys():
    steps = parse("a{Scrape x|y|=>$1}b")
    assert [kind for kind, _ in steps] == ["text", "scrape", "text"]
    assert steps[0] == ("text", "a") and steps[2] == ("text", "b")


def test_whitespace_inside_a_marker_stays():
    ((_, value),) = parse("{Scrape  Total  price: | EUR | => $1}")
    assert value.pairs == (("Total  price:", "EUR"),)


def test_double_dollar_is_a_literal_dollar_and_a_lone_dollar_stays():
    ((_, value),) = parse("{Scrape a | b | => $$1 costs $1$$ or 5$ or $}")
    assert value.template == ("$1 costs ", 1, "$ or 5$ or $")


@pytest.mark.parametrize(
    ("macro", "markers", "template"),
    [
        ("{Scrape a\\|b | c | => $1}", (("a|b", "c"),), (1,)),  # \| is a pipe, not a separator
        ("{Scrape a | b | => x\\|y}", (("a", "b"),), ("x|y",)),
        ("{Scrape a | \\n | => $1\\n$1}", (("a", "\n"),), (1, "\n", 1)),
        ("{Scrape a | \\t | => \\t$1}", (("a", "\t"),), ("\t", 1)),
        ("{Scrape a\\\\ | b | => \\\\}", (("a\\", "b"),), ("\\",)),
        ("{Scrape a | \\x7D | => \\x7B$1\\x7D}", (("a", "}"),), ("{", 1, "}")),
        ("{Scrape \\x7Bid\\x7D: | ; | => $1}", (("{id}:", ";"),), (1,)),
        ("{Scrape a | b | => \\x24 1 \\x241}", (("a", "b"),), ("$ 1 $1",)),  # \x24 is text, no placeholder
        ("{Scrape C:\\a | b | => $1}", (("C:\\a", "b"),), (1,)),  # an unknown escape keeps its backslash
    ],
)
def test_escapes_in_markers_and_template(macro, markers, template):
    ((_, value),) = parse(macro)
    assert value.pairs == markers
    assert value.template == template


def test_square_brackets_in_the_template_are_plain_text():
    ((_, value),) = parse("{Scrape a | b | => [1] [= 2] $1}")
    assert value.template == ("[1] [= 2] ", 1)


@pytest.mark.parametrize(
    "macro",
    [
        "{Scrape}",
        "{Scrape a | b}",  # no template
        "{Scrape a | b | $1}",  # template without =>
        "{Scrape => $1}",  # no pair
        "{Scrape a | => $1}",  # a pair needs both markers (an empty one is written as an empty field)
        "{Scrape a | b | c | => $1}",
        "{Scrape a | b | c | d | => $1 $3}",  # only two pairs
        "{Scrape a | b | => $0}",
        "{Scrape a | b | => $2}",
        "{Scrape  | b | => $1}",  # empty from
        "{Scrape a | b |  | | => $1}",  # second pair has an empty from
        "{Ausschnitt a | b}",
    ],
)
def test_broken_scrapes_are_errors_before_anything_happens(macro):
    with pytest.raises(MacroError, match="Nichts getippt"):
        parse(macro)


def test_an_unescaped_pipe_in_the_template_is_an_error():
    with pytest.raises(MacroError):
        parse("{Scrape a | b | => x | y}")


def test_scrape_is_expanded_inside_a_loop():
    steps = parse("{Loop 3}{Scrape a|b|=>$1}{EndLoop}")
    assert [kind for kind, _ in steps] == ["scrape"] * 3


def test_a_counter_in_the_template_is_not_replaced():
    ((kind, value),) = parse("{Loop 1 i}{Scrape a|b|=>[i]}{EndLoop}")
    assert kind == "scrape"
    assert value.template == ("[i]",)


# --- cutting -------------------------------------------------------------------------------------------

CLIP = "Name: Anna Weiß\nPreis: 12,50 €\n"


def test_example_from_the_spec():
    macro = "{Scrape Name: | \\n | Preis: | € | => $1 zahlt $2 Euro}"
    assert scrape(macro, CLIP) == "Anna Weiß zahlt 12,50 Euro"


def test_example_user_in_a_single_line():
    assert scrape("{ausschnitt user= | ; | => Hallo $1!}", "ID=77; user=bob; mail=x") == "Hallo bob!"


def test_markers_are_found_in_any_case():
    assert scrape("{Scrape NAME: | \\n | => $1}", CLIP) == "Anna Weiß"
    assert scrape("{Scrape name: | WEIß | => $1}", CLIP) == "Anna"


def test_the_result_is_stripped_but_markers_are_exact():
    assert scrape("{Scrape a: | ; | => <$1>}", "a:   x y  ;") == "<x y>"
    with pytest.raises(MacroError):
        scrape("{Scrape a: | \\x20b | => $1}", "a:b")  # a space in a marker must be there


def test_first_match_only():
    assert scrape("{Scrape x= | ; | => $1}", "x=1; x=2; x=3;") == "1"


def test_the_end_marker_is_searched_after_the_start_marker():
    assert scrape("{Scrape = | ; | => $1}", "; ;a=5;") == "5"


def test_the_second_pair_is_searched_after_the_first():
    # "id=" occurs before the first pair's end, only the later one may be used
    assert scrape("{Scrape a= | ; | id= | ; | => $1/$2}", "id=0; a=1; id=2;") == "1/2"
    with pytest.raises(MacroError, match="id="):
        scrape("{Scrape a= | ; | id= | ; | => $1/$2}", "id=0; a=1;")


def test_a_pair_can_be_used_twice_and_out_of_order():
    assert scrape("{Scrape a= | ; | b= | ; | => $2 $1 $2}", "a=1;b=2;") == "2 1 2"


def test_empty_end_marker_means_end_of_line():
    assert scrape("{Scrape Preis: | | => $1}", "Name: x\nPreis: 12 €\nrest") == "12 €"
    assert scrape("{Scrape Preis: | | => $1}", "Preis: 12 €\r\nrest") == "12 €"


def test_empty_end_marker_at_the_end_of_the_text():
    assert scrape("{Scrape Preis: | | => $1}", "Preis: 12") == "12"


def test_a_following_pair_starts_after_the_end_of_the_line():
    assert scrape("{Scrape a: | | b: | | => $1,$2}", "a: 1\nb: 2\nc: 3") == "1,2"


def test_text_that_looks_like_a_regex_is_plain_text():
    assert scrape("{Scrape (a+) | .* | => $1}", "(a+) x.*") == "x"


@pytest.mark.parametrize(
    ("macro", "missing"),
    [
        ("{Scrape Preis: | € | => $1}", "Preis:"),
        ("{Scrape Name: | ZZZ | => $1}", "ZZZ"),
        ("{Scrape Name: | \\n | Gewicht: | kg | => $1 $2}", "Gewicht:"),
    ],
)
def test_marker_not_found_is_an_error_that_names_it(macro, missing):
    with pytest.raises(MacroError, match=f"Marke '{missing}' nicht in der Zwischenablage gefunden. Nichts getippt."):
        scrape(macro, "Name: Anna\n")


def test_a_newline_marker_is_shown_readably_in_the_error():
    with pytest.raises(MacroError, match=r"Marke '\\n\\n'"):
        scrape("{Scrape a | \\n\\n | => $1}", "a b")


def test_a_result_with_commands_or_counters_stays_literal_text():
    text = "x={Enter}[i]{Loop 3}[= 1+1]{Wait 9}\\n;"
    assert scrape("{Scrape x= | ; | => $1}", text) == "{Enter}[i]{Loop 3}[= 1+1]{Wait 9}\\n"


def test_limit_for_the_clipboard_text():
    assert scrape("{Scrape a | ; | => $1}", "a;" + "z" * (MAX_SCRAPE_INPUT - 2)) == ""  # exactly at the limit
    with pytest.raises(MacroError, match=f"mehr als {MAX_SCRAPE_INPUT} Zeichen"):
        scrape("{Scrape a | ; | => $1}", "a;" + "z" * (MAX_SCRAPE_INPUT - 1))


def test_limit_for_the_result():
    assert len(scrape("{Scrape a | ; | => $1}", "a" + "b" * MAX_SCRAPE_RESULT + ";")) == MAX_SCRAPE_RESULT
    with pytest.raises(MacroError, match=str(MAX_SCRAPE_RESULT)):
        scrape("{Scrape a | ; | => $1}", "a" + "b" * (MAX_SCRAPE_RESULT + 1) + ";")
    with pytest.raises(MacroError):  # the template counts too
        scrape("{Scrape a | ; | => $1$1}", "a" + "b" * (MAX_SCRAPE_RESULT // 2 + 1) + ";")


def test_each_scrape_in_a_loop_is_charged_in_full_against_the_expansion_limit():
    allowed = MAX_EXPANDED_LENGTH // MAX_SCRAPE_RESULT
    assert len(parse(f"{{Loop {allowed - 1}}}{{Scrape a|b|=>$1}}{{EndLoop}}")) == allowed - 1
    with pytest.raises(MacroError, match="zu viel Text"):
        parse(f"{{Loop {allowed + 1}}}{{Scrape a|b|=>$1}}{{EndLoop}}")
    with pytest.raises(MacroError, match="zu viel Text"):  # nested loops multiply
        parse("{Loop 20}{Loop 20}{Scrape a|b|=>$1}{EndLoop}{EndLoop}")


# --- the controller ------------------------------------------------------------------------------------


def test_typing_a_scrape_then_a_key(fake_engine, injector):
    injector.clipboard = CLIP
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "{Scrape Name: | \\n | Preis: | € | => $1 zahlt $2 Euro}{Enter}")
    assert injector.calls == [("text", "Anna Weiß zahlt 12,50 Euro"), ("keys", "Enter")]


def test_scraped_text_is_not_added_to_the_live_history(fake_engine, injector):
    injector.clipboard = "pw=secret;"
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "ab{Scrape pw= | ; | => $1}cd")
    assert injector.buffer == "absecretcd"
    assert controller.typed_text == "abcd"


def test_a_missing_marker_types_nothing_not_even_the_text_before_it(fake_engine, injector):
    injector.clipboard = "Name: Anna\n"
    controller = make_controller(fake_engine, injector)
    with pytest.raises(MacroError, match="Marke 'Preis:' nicht in der Zwischenablage gefunden. Nichts getippt."):
        controller._emit_steps(Emission("before{Scrape Preis: | | => $1}{Enter}"))
    assert injector.calls == []
    assert controller.typed_text == ""


def test_the_error_of_a_slot_reaches_the_listener(fake_engine, injector, listener):
    injector.clipboard = "Name: Anna\n"
    controller = make_controller(fake_engine, injector)
    controller.set_listener(listener)
    run_macro(controller, "before{Scrape Preis: | | => $1}{Enter}")
    assert len(listener.errors) == 1 and "Marke 'Preis:'" in listener.errors[0]
    assert injector.calls == []


@pytest.mark.parametrize("clipboard", [None, ""])
def test_an_empty_clipboard_types_nothing(fake_engine, injector, clipboard):
    injector.clipboard = clipboard
    controller = make_controller(fake_engine, injector)
    with pytest.raises(MacroError, match="Zwischenablage"):
        controller._emit_steps(Emission("x{Scrape a | | => $1}"))
    assert injector.calls == []


def test_the_clipboard_is_not_read_without_a_scrape(fake_engine, injector):
    injector.clipboard = "x"
    reads = []
    injector.clipboard_text = lambda: reads.append(1)
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "plain{Enter}")
    assert reads == []


def test_scrape_in_a_loop_types_the_cut_text_every_round(fake_engine, injector):
    injector.clipboard = "a=7;"
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "{Loop 3 n}[n]:{Scrape a= | ; | => $1}|{EndLoop}")
    assert injector.buffer == "0:7|1:7|2:7|"


def test_a_scraped_result_with_commands_is_typed_literally(fake_engine, injector):
    injector.clipboard = "x={Enter}[i]{File C:\\nope};"
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "{Scrape x= | ; | => $1}")
    assert injector.calls == [("text", "{Enter}[i]{File C:\\nope}")]


def test_a_slot_with_a_broken_scrape_is_reported():
    from mourse_decoder.function_keys import FunctionKeys

    keys = FunctionKeys()
    keys = keys.replace_slot(keys.slot(13).replace(macro="{Scrape a | b}"))
    errors = keys.validate(None, lambda combo: None)
    assert any(e.startswith("F13: ") and "Vorlage" in e for e in errors)


# --- the import warning --------------------------------------------------------------------------------


def test_scrape_is_listed_like_the_clipboard_for_the_import_warning(tmp_path):
    path = write_set(
        tmp_path,
        {
            "--..--..": "{Scrape a | b | => $1}",
            "--..-.-.": "x{ausschnitt a|b|=>$1}{Clipboard}",
            "--...-..": "{Clipboard}",
        },
    )
    assert macros.commands_in_file(path) == ["{Scrape …}", "{Clipboard}"]


# --- review fixes --------------------------------------------------------------------------------------


def test_a_scrape_after_ctrl_c_reads_the_clipboard_at_its_position(fake_engine, injector):
    injector.clipboard = "old: x;"
    controller = make_controller(fake_engine, injector)
    injector.press_shortcut = lambda combo: (injector.calls.append(("keys", combo)), setattr(injector, "clipboard", "Name: Anna\n"))
    run_macro(controller, "{Ctrl+C}{Wait 1}{Scrape Name: | \n | => $1}")
    assert injector.calls == [("keys", "Ctrl+C"), ("text", "Anna")]


def test_a_scrape_before_any_key_is_still_cut_before_anything_is_typed(fake_engine, injector):
    injector.clipboard = "Name: Anna\n"
    controller = make_controller(fake_engine, injector)
    with pytest.raises(MacroError, match="Marke 'Preis:'"):
        controller._emit_steps(Emission("ab{Wait 1}{Scrape Preis: | | => $1}{Ctrl+C}{Scrape Name: | | => $1}"))
    assert injector.calls == []


def test_a_missing_marker_after_a_key_stops_there_and_earlier_output_stays(fake_engine, injector):
    injector.clipboard = "Name: Anna\n"
    controller = make_controller(fake_engine, injector)
    with pytest.raises(MacroError, match="Marke 'Preis:'"):
        controller._emit_steps(Emission("ab{Ctrl+C}{Scrape Preis: | | => $1}cd"))
    assert injector.calls == [("text", "ab"), ("keys", "Ctrl+C")]


def test_an_empty_clipboard_after_a_key_stops_there(fake_engine, injector):
    injector.clipboard = "x"
    controller = make_controller(fake_engine, injector)
    injector.press_shortcut = lambda combo: (injector.calls.append(("keys", combo)), setattr(injector, "clipboard", ""))
    with pytest.raises(MacroError, match="Zwischenablage"):
        controller._emit_steps(Emission("a{Ctrl+X}{Scrape a | | => $1}"))
    assert injector.calls == [("text", "a"), ("keys", "Ctrl+X")]


def test_a_scrape_after_a_file_step_is_cut_late(fake_engine, injector, tmp_path):
    injector.clipboard = "a=1;"
    controller = make_controller(fake_engine, injector)
    injector.paste_files = lambda paths: (injector.calls.append(("files", paths)), setattr(injector, "clipboard", "a=2;"))
    run_macro(controller, "{File " + str(tmp_path) + "}{Scrape a= | ; | => $1}")
    assert injector.calls[-1] == ("text", "2")


def test_a_huge_placeholder_number_is_a_macro_error_not_a_crash():
    for digits in (10, 4300, 4301, 100_000):
        with pytest.raises(MacroError, match="gibt es nicht"):
            parse("{Scrape a | b | => $" + "9" * digits + "}")


def test_a_huge_placeholder_number_is_reported_for_a_slot_and_for_a_run(fake_engine, injector):
    from mourse_decoder.function_keys import FunctionKeys

    macro = "{Scrape a | b | => $" + "1" * 4301 + "}"
    keys = FunctionKeys()
    keys = keys.replace_slot(keys.slot(13).replace(macro=macro))
    assert any(e.startswith("F13: ") for e in keys.validate(None, lambda combo: None))
    controller = make_controller(fake_engine, injector)
    with pytest.raises(MacroError):
        controller._emit_steps(Emission(macro))


def test_the_real_limit_is_99_scrapes_in_a_loop():
    assert len(parse("{Loop 99}{Scrape a|b|=>$1}{EndLoop}")) == 99
    with pytest.raises(MacroError, match="zu viel Text"):
        parse("{Loop 100}{Scrape a|b|=>$1}{EndLoop}")


def test_readme_names_the_real_loop_limit():
    from pathlib import Path

    readme = (Path(__file__).parents[2] / "README.md").read_text(encoding="utf-8")
    assert "at most 99 of them fit in loops" in readme
    assert "at most 100 of them" not in readme


def test_dry_run_does_not_read_the_clipboard_and_does_not_abort_on_a_scrape(fake_engine, injector, capsys):
    from mourse_decoder.app import _PrintInjector

    controller = make_controller(fake_engine, _PrintInjector())
    controller._emit_steps(Emission("a{Scrape Name: | | => $1}{Ctrl+C}{Scrape Name: | | => $1}"))
    out = capsys.readouterr().out
    assert out.count("<Ausschnitt>") == 2 and "Marke" not in out
