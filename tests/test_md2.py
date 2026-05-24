from shared.md2 import bold, esc, plain


def test_esc_backslash_before_closing_bold():
    assert bold("hello\\") == r"*hello\\*"


def test_bold_special_chars():
    assert bold("AC*DC") == r"*AC\*DC*"
    assert bold("Dr. Dre") == r"*Dr\. Dre*"


def test_plain_escapes_punctuation():
    assert plain("Стр. 1/2") == r"Стр\. 1/2"
