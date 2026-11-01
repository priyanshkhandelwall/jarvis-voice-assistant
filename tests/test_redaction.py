"""Tests for redact_sensitive()/redact_value() (R11/F15).

Covers both matching modes required by the developer's review: word-
boundary matching for short markers (must NOT fire on lookalike ordinary
words), and phrase/substring matching for the longer markers (kept as
plain substring containment, by instruction -- including its known gap).
"""

from __future__ import annotations

from jarvis.logbook.records import redact_sensitive, redact_value

REDACTED = "[redacted]"


# -- boundary markers (otp, pin): true positives -------------------------


def test_boundary_marker_otp_is_redacted() -> None:
    """Boundary marker otp is redacted."""
    assert redact_sensitive("what is my otp") == REDACTED
    assert redact_sensitive("OTP please") == REDACTED
    assert redact_sensitive("send the otp.") == REDACTED  # trailing punctuation


def test_boundary_marker_pin_is_redacted() -> None:
    """Boundary marker pin is redacted."""
    assert redact_sensitive("my pin is 1234") == REDACTED
    assert redact_sensitive("what's the PIN") == REDACTED


# -- boundary markers: false positives must NOT fire ---------------------


def test_boundary_marker_otp_does_not_fire_on_lookalike_words() -> None:
    """Boundary marker otp does not fire on lookalike words."""
    assert redact_sensitive("I ordered a hotpot for dinner") == "I ordered a hotpot for dinner"
    assert redact_sensitive("walk down the footpath") == "walk down the footpath"


def test_boundary_marker_pin_does_not_fire_on_lookalike_words() -> None:
    """Boundary marker pin does not fire on lookalike words."""
    assert redact_sensitive("that's just my opinion") == "that's just my opinion"
    assert redact_sensitive("I bought a pineapple") == "I bought a pineapple"
    assert redact_sensitive("put it on spin cycle") == "put it on spin cycle"


# -- phrase markers: true positives ---------------------------------------


def test_phrase_markers_are_redacted() -> None:
    """Phrase markers are redacted."""
    assert redact_sensitive("my password is hunter2") == REDACTED
    assert redact_sensitive("what's the passcode") == REDACTED
    assert redact_sensitive("here is the api key") == REDACTED
    assert redact_sensitive("here is the api_key") == REDACTED
    assert redact_sensitive("that's a secret") == REDACTED
    assert redact_sensitive("my access token") == REDACTED


def test_phrase_markers_keep_the_known_substring_gap() -> None:
    """Documented, kept-on-purpose limit: phrase markers use plain
    substring matching, so a word that merely contains one still
    redacts. The developer asked to keep this trade-off for the longer
    markers (only otp/pin got word-boundary matching)."""
    assert redact_sensitive("she works as a secretary") == REDACTED
    assert redact_sensitive("use the tokenizer") == REDACTED


# -- non-sensitive text passes through unchanged ---------------------------


def test_ordinary_text_is_unchanged() -> None:
    """Ordinary text is unchanged."""
    text = "open chrome and find my resume"
    assert redact_sensitive(text) == text


def test_none_passes_through() -> None:
    """None passes through."""
    assert redact_sensitive(None) is None


# -- redact_value: recursive redaction of nested structures ----------------


def test_redact_value_redacts_nested_dict_strings() -> None:
    """Redact value redacts nested dict strings."""
    value = {"action_name": "lock_screen", "params": {"text": "my password is hunter2"}}
    redacted = redact_value(value)
    assert redacted == {"action_name": "lock_screen", "params": {"text": REDACTED}}


def test_redact_value_redacts_list_of_strings() -> None:
    """Redact value redacts list of strings."""
    assert redact_value(["open chrome", "my otp is 5"]) == ["open chrome", REDACTED]


def test_redact_value_leaves_non_strings_alone() -> None:
    """Redact value leaves non strings alone."""
    assert redact_value({"count": 3, "verified": True, "score": 0.9}) == {
        "count": 3,
        "verified": True,
        "score": 0.9,
    }


# -- Setup messages (settings.toml [setup_messages]) are JARVIS's own fixed
# sentences: they name a setting, never its value, so they are shown in
# full. Only an exact match counts. ----------------------------------------


def test_every_setup_message_without_placeholders_is_shown_in_full() -> None:
    """Every setup message without placeholders is shown in full."""
    from jarvis.settings import SETTINGS

    for key, message in SETTINGS["setup_messages"].items():
        if "{" not in message:
            assert redact_sensitive(message) == message, key


def test_groq_key_messages_are_not_redacted() -> None:
    """Groq key messages are not redacted."""
    from jarvis.settings import SETTINGS

    for key in ("groq_no_key", "groq_key_rejected"):
        message = SETTINGS["setup_messages"][key]
        assert "GROQ_API_KEY" in message
        assert redact_sensitive(message) == message


def test_setup_message_with_its_placeholder_filled_is_shown() -> None:
    """Setup message with its placeholder filled is shown."""
    from jarvis.settings import SETTINGS

    limited = SETTINGS["setup_messages"]["groq_rate_limited"].format(until="14:32")
    assert redact_sensitive(limited) == limited
    zira = SETTINGS["setup_messages"]["tts_zira_missing"].format(voice="Microsoft David Desktop - English (United States)")
    assert redact_sensitive(zira) == zira


def test_setup_message_with_anything_added_is_still_redacted() -> None:
    """Setup message with anything added is still redacted."""
    from jarvis.settings import SETTINGS

    message = SETTINGS["setup_messages"]["groq_no_key"]
    assert redact_sensitive(message + " My key is gsk_abc123.") == "[redacted]"
    assert redact_sensitive("my api_key is gsk_abc123. " + message) == "[redacted]"
    assert redact_sensitive(message.lower()) == "[redacted]"  # exact text only


def test_placeholder_cannot_carry_other_text() -> None:
    """{until} only takes a clock time: free text in its place, even if
    it holds a sensitive marker, is redacted as normal."""
    from jarvis.settings import SETTINGS

    template = SETTINGS["setup_messages"]["groq_rate_limited"]
    assert redact_sensitive(template.format(until="my token abc")) == "[redacted]"


def test_real_sensitive_text_is_still_redacted() -> None:
    """Real sensitive text is still redacted."""
    assert redact_sensitive("add my api key sk-123 to the file") == "[redacted]"
    assert redact_sensitive("my password is hunter2") == "[redacted]"
