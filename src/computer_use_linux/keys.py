from __future__ import annotations

from collections.abc import Iterable

_KEYSYMS: dict[str, int] = {
    "backspace": 0xFF08,
    "tab": 0xFF09,
    "return": 0xFF0D,
    "enter": 0xFF0D,
    "escape": 0xFF1B,
    "esc": 0xFF1B,
    "home": 0xFF50,
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    "pageup": 0xFF55,
    "pagedown": 0xFF56,
    "end": 0xFF57,
    "insert": 0xFF63,
    "delete": 0xFFFF,
    "pause": 0xFF13,
    "print": 0xFF61,
    "space": 0x20,
    "shift": 0xFFE1,
    "shift_l": 0xFFE1,
    "shift_r": 0xFFE2,
    "ctrl": 0xFFE3,
    "control": 0xFFE3,
    "control_l": 0xFFE3,
    "control_r": 0xFFE4,
    "capslock": 0xFFE5,
    "caps_lock": 0xFFE5,
    "alt": 0xFFE9,
    "alt_l": 0xFFE9,
    "alt_r": 0xFFEA,
    "super": 0xFFEB,
    "meta": 0xFFEB,
    "super_l": 0xFFEB,
    "super_r": 0xFFEC,
    "menu": 0xFF67,
}
for _index in range(1, 13):
    _KEYSYMS[f"f{_index}"] = 0xFFBD + _index

_MODIFIER_NAMES = {"shift", "shift_l", "shift_r", "ctrl", "control", "control_l", "control_r", "alt", "alt_l", "alt_r", "super", "super_l", "super_r", "meta"}


def _normalise(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(" ", "")


def keysym(name: str | int) -> int:
    if isinstance(name, int):
        return name
    name = name.strip()
    if len(name) == 1:
        return ord(name)
    normalised = _normalise(name)
    if normalised.startswith("u+"):
        return 0x1000000 | int(normalised[2:], 16)
    if normalised in _KEYSYMS:
        return _KEYSYMS[normalised]
    raise ValueError(f"unknown keysym: {name}")


def parse_chord(chord: str | Iterable[str]) -> tuple[int, ...]:
    if isinstance(chord, str):
        parts = [part for part in chord.split("+") if part.strip()]
    else:
        parts = list(chord)
    if not parts:
        raise ValueError("key chord cannot be empty")
    return tuple(keysym(part) for part in parts)


def text_to_keysyms(text: str) -> tuple[int, ...]:
    """Return direct X keysyms for the keysym-safe ASCII typing path."""

    if not text.isascii():
        raise ValueError("non-ASCII text requires clipboard mode")
    result: list[int] = []
    for char in text:
        if char == "\n" or char == "\r":
            result.append(_KEYSYMS["return"])
        elif char == "\t":
            result.append(_KEYSYMS["tab"])
        else:
            result.append(ord(char))
    return tuple(result)


def is_modifier(key: str | int) -> bool:
    if isinstance(key, int):
        return key in {0xFFE1, 0xFFE2, 0xFFE3, 0xFFE4, 0xFFE9, 0xFFEA, 0xFFEB, 0xFFEC}
    return _normalise(key) in _MODIFIER_NAMES


def chord_is_destructive(chord: str | Iterable[str]) -> bool:
    names = {_normalise(part) for part in (chord.split("+") if isinstance(chord, str) else chord)}
    if any(name in {"super", "super_l", "super_r", "meta", "delete"} for name in names):
        return True
    if "alt" in names or "alt_l" in names or "alt_r" in names:
        if any(name in {"f4", "delete"} for name in names):
            return True
    controls = {"ctrl", "control", "control_l", "control_r"}
    alts = {"alt", "alt_l", "alt_r"}
    if names & controls and names & alts:
        return True
    return False

