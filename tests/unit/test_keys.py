from __future__ import annotations

from computer_use_linux.keys import chord_is_destructive, keysym, parse_chord, text_to_keysyms


def test_layout_safe_text_uses_keysyms_directly() -> None:
    text = "Hello @[]:_ 123"
    assert tuple(map(chr, text_to_keysyms(text))) == tuple(text)


def test_special_names_and_chords() -> None:
    assert keysym("Return") == 0xFF0D
    assert keysym("Shift_L") == 0xFFE1
    assert parse_chord("ctrl+shift+p") == (0xFFE3, 0xFFE1, ord("p"))


def test_destructive_chords_are_explicit() -> None:
    assert chord_is_destructive("super")
    assert chord_is_destructive("alt+F4")
    assert chord_is_destructive("ctrl+alt+t")
    assert chord_is_destructive("Delete")
    assert not chord_is_destructive("ctrl+shift+p")

