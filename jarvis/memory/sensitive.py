"""F10 never-store check (R11): passwords and PINs, card numbers (Luhn),
bank account numbers, and government ID / passport / licence numbers.

Used twice:
- by the memory commands, which refuse to store a match ("I won't store
  that. It looks like a <type>.");
- by redact_sensitive() (jarvis/logbook/records.py), so the refused text is
  never written anywhere: not in Live Activity, not in a decision record,
  not in debug.log.

Deliberately cautious: a sensitive word (password, PIN,
card number, account number, BSB, passport number, ...) together with ANY
digit, spoken digit or value ("is hunter2") is refused, whatever the digits
are -- testing once stored "my card number 411 111 111 111" because it
failed the Luhn check. The word alone is fine ("remember to change my
password"). A bare number needs its own evidence: a card number must pass
the Luhn check, an ID number needs its keyword next to it.

Pure text, no I/O.
"""

from __future__ import annotations

import re
from typing import Optional

from jarvis.settings import SETTINGS

_CFG = SETTINGS["memory"]["never_store"]


def _words_pattern(words) -> Optional[re.Pattern]:
    words = sorted((str(w).lower() for w in words), key=len, reverse=True)
    if not words:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in words) + r")(?![a-z0-9])")


_PASSWORD_WORDS = _words_pattern(_CFG["password_words"])
_PIN_WORDS = _words_pattern(_CFG["pin_words"])
_CARD_WORDS = _words_pattern(_CFG["card_words"])
_BANK_WORDS = _words_pattern(_CFG["bank_words"])
_ID_KEYWORDS = [(_words_pattern(words), str(kind)) for kind, words in _CFG["id_words"].items()]
_ID_NUMBER_KEYWORDS = [(_words_pattern(words), str(kind)) for kind, words in _CFG["id_number_words"].items()]
_NUMBER_WORDS = _words_pattern(_CFG["number_words"])
_LINKS = sorted((str(w).lower() for w in _CFG["value_link_words"]), key=len, reverse=True)
_DIGIT = re.compile(r"[0-9]")

# 13-19 digits, optionally grouped by spaces or hyphens ("4111 1111 1111 1111").
_CARD_CANDIDATE = re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])")
# An Australian BSB with an account number after it ("062-000 12345678").
_BSB_ACCOUNT = re.compile(r"(?<![0-9])[0-9]{3}[- ]?[0-9]{3}[ ,]+[0-9]{4,10}(?![0-9])")
_IBAN = re.compile(r"(?<![a-z0-9])[a-z]{2}[0-9]{2}(?: ?[a-z0-9]){11,30}(?![a-z0-9])")
_US_SSN = re.compile(r"(?<![0-9])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![0-9])")
# A number-like token: at least `min_id_digits` digits, letters allowed ("PA1234567").
_ID_NUMBER = re.compile(r"(?<![a-z0-9])(?=(?:[a-z -]*[0-9]){%d})[a-z0-9][a-z0-9 -]{3,24}[0-9](?![a-z0-9])" % int(_CFG["min_id_digits"]))
_DIGIT_RUN = re.compile(r"[0-9][0-9 -]{2,}[0-9]")


def luhn_valid(digits: str) -> bool:
    """The card-number checksum: double every second digit from the right."""
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _has_card_number(text: str) -> bool:
    for match in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            return True
    return False


def _value_after(pattern: re.Pattern, text: str) -> bool:
    """A value follows the word: "password is hunter2", "pin: ...",
    "change my password to fluffy". Nothing after it, or no linking word
    ("remember to change my password", "password tomorrow"), is no value."""
    for match in pattern.finditer(text):
        rest = text[match.end():].lstrip(" ,'\"")
        for link in _LINKS:
            if rest.startswith(link):
                after = rest[len(link):]
                if (link in "=:" or not after[:1].isalnum()) and re.search(r"[a-z0-9]", after):
                    return True
    return False


def _with_value(pattern: Optional[re.Pattern], text: str) -> bool:
    """The word is there AND any digit, spoken digit or value is too."""
    if pattern is None or not pattern.search(text):
        return False
    if _DIGIT.search(text) or (_NUMBER_WORDS is not None and _NUMBER_WORDS.search(text)):
        return True
    return _value_after(pattern, text)


# ("Remember my bin 44555" was stored -- Whisper's
# "PIN"). One-word keywords, matched with Whisper's slips, need a long number.
_FUZZY_KEYWORDS = [(str(w).lower(), kind) for kind, key in (("pin", "pin_words"), ("password", "password_words"))
                   for w in _CFG[key] if " " not in str(w) and "-" not in str(w) and len(str(w)) >= 3]
_FUZZY_DISTANCE = int(_CFG.get("fuzzy_max_distance", 1))
_LONG_NUMBER = re.compile(r"(?<![0-9])[0-9]{%d,}(?![0-9])" % int(_CFG.get("fuzzy_min_digits", 4)))
_SAFE_NUMBER_WORDS = {str(w).lower() for w in _CFG.get("safe_number_words", [])}
_CODE_WORDS = {str(w).lower() for w in _CFG.get("code_words", [])}
_MY_WORD_CODE = re.compile(r"\bmy\s+((?:[a-z']+\s+)?[a-z']+)\s+(?:is\s+|=\s*|:\s*)?([0-9]{%d,%d})(?![0-9])\s*$"
                           % (int(_CFG.get("code_min_digits", 4)), int(_CFG.get("code_max_digits", 8))))


def _fuzzy_keyword(text: str) -> Optional[str]:
    """"bin"/"pen"/"pinn" -> "pin", "passward" -> "password", next to a long
    number; never a word shorter than the keyword."""
    if not _LONG_NUMBER.search(text):
        return None
    from jarvis.actions.command_text import edit_distance  # actions imports the interpreter, not this

    for word in re.findall(r"[a-z]+", text):
        for keyword, kind in _FUZZY_KEYWORDS:
            if len(word) >= len(keyword) and edit_distance(word, keyword) <= _FUZZY_DISTANCE:
                return kind
    return None


def _looks_like_a_code(text: str) -> bool:
    """"my bin 44555", "my gym code 1234": a bare 4-8 digit number right
    after "my" and one or two words (a code word always counts)."""
    match = _MY_WORD_CODE.search(text)
    if match is None:
        return False
    named = set(match.group(1).split())
    return bool(named & _CODE_WORDS) or not named & _SAFE_NUMBER_WORDS


def sensitive_type(text: Optional[str]) -> Optional[str]:
    """What kind of secret `text` looks like ("password", "card number",
    ...), or None when it looks safe to store."""
    if not text:
        return None
    lowered = text.lower().replace("’", "'")
    names = _CFG["type_names"]
    for pattern, kind in ((_PIN_WORDS, "pin"), (_PASSWORD_WORDS, "password"), (_CARD_WORDS, "card"), (_BANK_WORDS, "bank")):
        if _with_value(pattern, lowered):
            return str(names[kind])
    fuzzy = _fuzzy_keyword(lowered)
    if fuzzy is not None:
        return str(names[fuzzy])
    for pattern, kind in _ID_NUMBER_KEYWORDS:
        if _with_value(pattern, lowered):
            return str(_CFG["id_words_types"][kind])
    if _has_card_number(lowered):
        return str(names["card"])
    if _BSB_ACCOUNT.search(lowered) or _IBAN.search(lowered):
        return str(names["bank"])
    if _US_SSN.search(lowered):
        return str(_CFG["id_words_types"]["social_security"])
    for pattern, kind in _ID_KEYWORDS:
        if pattern is not None and pattern.search(lowered) and _ID_NUMBER.search(lowered):
            return str(_CFG["id_words_types"][kind])
    if _looks_like_a_code(lowered):
        return str(names["code"])
    return None


def refusal_text(kind: str) -> str:
    """"I won't store that. It looks like a <type>." (an for a vowel sound)."""
    if kind == str(_CFG["type_names"]["code"]):
        return str(SETTINGS["memory"]["replies"]["refuse_code"])
    article = "an" if kind[:1].lower() in "aeiou" else "a"
    return str(SETTINGS["memory"]["replies"]["refuse"]).format(article=article, type=kind)
