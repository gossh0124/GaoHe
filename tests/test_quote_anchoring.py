import unicodedata

import pytest

from gaohe.analysis import locate_quote


def span_text(text, span):
    assert span is not None
    return text[span[0]:span[1]]


def test_locate_quote_is_reexported_from_providers():
    from gaohe import analysis, providers

    assert analysis.locate_quote is providers.locate_quote


def test_exact_unique_quote_returns_its_span():
    text = "The ministry said 100 units arrived on Monday."

    span = locate_quote(text, "100 units arrived")

    assert span == (text.index("100 units"), text.index("100 units") + len("100 units arrived"))


def test_exact_cjk_quote_returns_its_span():
    text = "國防部表示，今年共有 1,200 名官兵參與演習。"

    span = locate_quote(text, "今年共有 1,200 名官兵參與演習")

    assert span_text(text, span) == "今年共有 1,200 名官兵參與演習"


def test_surrounding_whitespace_in_the_quote_is_not_part_of_the_span():
    text = "Before. The claim here. After."

    assert span_text(text, locate_quote(text, "  The claim here.\n")) == "The claim here."


@pytest.mark.parametrize(
    ("text", "quote", "expected"),
    [
        # A line break and a double space inside the article, single spaces in the quote.
        (
            "邀集近 500 位\n國內外官員與  專家出席。",
            "邀集近 500 位 國內外官員與 專家",
            "邀集近 500 位\n國內外官員與  專家",
        ),
        # The model dropped the spaces around a number in CJK text.
        ("大會邀集近 500 位國內外官員。", "邀集近500位國內外官員", "邀集近 500 位國內外官員"),
        # The model added spaces the article does not have.
        ("大會邀集近500位國內外官員。", "邀集 近 500 位", "邀集近500位"),
        # Ideographic space and no-break space count as whitespace.
        ("部長　表示：補助\xa01,000 萬元。", "部長 表示：補助 1,000 萬元", "部長　表示：補助\xa01,000 萬元"),
        ("The minister\n said   the plan\twill work.", "said the plan will work", "said   the plan\twill work"),
    ],
)
def test_whitespace_insensitive_match_maps_back_to_the_original_text(text, quote, expected):
    span = locate_quote(text, quote)

    assert span_text(text, span) == expected
    assert not text[span[0]].isspace()
    assert not text[span[1] - 1].isspace()


def test_repeated_quote_without_occurrence_is_ambiguous():
    text = "補助 100 萬元。另一項補助 100 萬元。"

    assert locate_quote(text, "補助 100 萬元") is None


def test_occurrence_disambiguates_repeated_quotes():
    text = "補助 100 萬元。另一項補助 100 萬元。"
    first = text.index("補助 100 萬元")
    second = text.index("補助 100 萬元", first + 1)

    assert locate_quote(text, "補助 100 萬元", 1) == (first, first + len("補助 100 萬元"))
    assert locate_quote(text, "補助 100 萬元", 2) == (second, second + len("補助 100 萬元"))
    assert locate_quote(text, "補助 100 萬元", 3) is None


def test_occurrence_also_disambiguates_whitespace_insensitive_matches():
    text = "補助 100\n萬元。另一項補助 100  萬元。"

    assert locate_quote(text, "補助 100 萬元") is None
    assert span_text(text, locate_quote(text, "補助 100 萬元", 2)) == "補助 100  萬元"


@pytest.mark.parametrize("occurrence", [0, -1, True, "2", 1.0, None])
def test_invalid_occurrence_counts_as_absent(occurrence):
    unique = "Only once here."
    repeated = "Twice. Twice."

    assert span_text(unique, locate_quote(unique, "Only once", occurrence)) == "Only once"
    assert locate_quote(repeated, "Twice", occurrence) is None


def test_unique_quote_with_an_occurrence_beyond_it_is_rejected():
    text = "Only once here."

    assert locate_quote(text, "Only once", 1) == (0, len("Only once"))
    assert locate_quote(text, "Only once", 2) is None


def test_overlapping_repeats_are_ambiguous():
    assert locate_quote("aaa", "aa") is None
    assert locate_quote("aaa", "aa", 2) == (1, 3)


def test_an_exact_match_wins_over_whitespace_insensitive_candidates():
    text = "a b then ab"

    assert locate_quote(text, "ab") == (text.index("ab"), text.index("ab") + 2)


@pytest.mark.parametrize("quote", ["", "   ", "\n　", None, 42])
def test_empty_or_non_text_quotes_are_rejected(quote):
    assert locate_quote("Some article text.", quote) is None


def test_unknown_quote_and_non_text_article_are_rejected():
    assert locate_quote("Some article text.", "not present") is None
    assert locate_quote(None, "text") is None


def test_quote_in_a_different_unicode_normal_form_still_anchors():
    text = unicodedata.normalize("NFC", "Café opening drew 200 people.")
    decomposed = unicodedata.normalize("NFD", "Café opening")

    assert span_text(text, locate_quote(text, decomposed)) == "Café opening"


def test_contains_quote_is_verbatim_and_whitespace_insensitive_but_never_empty():
    from gaohe.providers import contains_quote

    excerpt = "官方紀錄：本次共 200\n名官員出席。"

    assert contains_quote(excerpt, "共 200\n名官員出席") is True
    assert contains_quote(excerpt, "共 200 名官員出席") is True
    assert contains_quote(excerpt, "共200名官員出席") is True
    assert contains_quote(excerpt, "共 300 名官員出席") is False
    assert contains_quote(excerpt, "   ") is False
    assert contains_quote(excerpt, None) is False
    assert contains_quote(None, "200") is False
