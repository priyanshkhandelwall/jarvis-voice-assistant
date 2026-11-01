"""Tests for F2's Groq-backed Q&A/small-talk piece (jarvis/speech/qa.py).

R14 + the missing-setup rule: every failure becomes a sentence that says
what to set up, never an exception. Groq is always mocked -- no test here
makes a real network call.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import groq
import httpx
import pytest

from jarvis.online import OnlineErrorKind, OnlineService, OnlineStatus
from jarvis.settings import SETTINGS
from jarvis.speech.qa import GroqAnswerer, build_system_prompt, classify_groq_error

_MESSAGES = SETTINGS["setup_messages"]
_REQUEST = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


@pytest.fixture(autouse=True)
def _no_real_groq_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # This developer's machine has a real GROQ_API_KEY in its environment.
    # Every test must run as if it weren't there, or a "no key" test would
    # quietly make a real call with a real key.
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


class FakeClock:
    """A clock the test moves by hand."""
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _service(clock: FakeClock | None = None, status: OnlineStatus | None = None) -> OnlineService:
    clock = clock or FakeClock()
    return OnlineService(
        "Groq",
        status or OnlineStatus(wall_clock=clock),
        timeout_seconds=10,
        max_retries=1,
        cooldown_seconds=300,
        clock=clock,
        wall_clock=clock,
    )


def _response(text: str) -> MagicMock:
    fake = MagicMock()
    fake.choices[0].message.content = text
    return fake


def _rate_limit_error(retry_after: str | None = None) -> groq.RateLimitError:
    headers = {"retry-after": retry_after} if retry_after else {}
    return groq.RateLimitError("rate limited", response=httpx.Response(429, headers=headers, request=_REQUEST), body=None)


def test_no_key_says_how_to_set_it_up_and_never_goes_online() -> None:
    """No key says how to set it up and never goes online."""
    went_online = []
    result = GroqAnswerer(api_key=None, service=_service()).answer("how are you", on_going_online=lambda: went_online.append(1))
    assert result.text == _MESSAGES["groq_no_key"]
    assert "GROQ_API_KEY" in result.text and ".env" in result.text
    assert result.error == OnlineErrorKind.NO_KEY
    assert result.went_online is False
    assert went_online == []  # "Checking online" must not be said when nothing goes online


def test_success_returns_plain_text_and_announces_first() -> None:
    """Success returns plain text and announces first."""
    order: list[str] = []
    with patch("groq.Groq") as groq_cls:
        groq_cls.return_value.chat.completions.create.side_effect = lambda **_: (order.append("request"), _response("  **Fine**, thanks!  "))[1]
        result = GroqAnswerer(api_key="k", service=_service()).answer("how are you", on_going_online=lambda: order.append("announce"))
    assert result.text == "Fine, thanks!"  # markdown stripped
    assert result.went_online is True and result.error is None
    assert order == ["announce", "request"]


def test_client_uses_r1_timeout_and_no_sdk_retries_and_settings_model() -> None:
    """Client uses R1 timeout and no sdk retries and settings model."""
    with patch("groq.Groq") as groq_cls:
        groq_cls.return_value.chat.completions.create.return_value = _response("ok")
        GroqAnswerer(api_key="k", service=_service()).answer("hi there")
    kwargs = groq_cls.call_args.kwargs
    assert kwargs["timeout"] == 10 and kwargs["max_retries"] == 0
    create_kwargs = groq_cls.return_value.chat.completions.create.call_args.kwargs
    assert create_kwargs["model"] == SETTINGS["groq"]["model"]
    assert create_kwargs["max_tokens"] == SETTINGS["groq"]["answer_max_tokens"]


@pytest.mark.parametrize(
    "error, kind, message_key",
    [
        (groq.AuthenticationError("bad key", response=httpx.Response(401, request=_REQUEST), body=None), OnlineErrorKind.KEY_REJECTED, "groq_key_rejected"),
        (groq.APIConnectionError(request=_REQUEST), OnlineErrorKind.CANNOT_REACH, "groq_cannot_reach"),
        (groq.APITimeoutError(request=_REQUEST), OnlineErrorKind.TIMEOUT, "groq_timeout"),
        (groq.InternalServerError("boom", response=httpx.Response(500, request=_REQUEST), body=None), OnlineErrorKind.SERVICE_ERROR, "groq_service_error"),
    ],
)
def test_each_failure_says_its_own_setup_message(error, kind, message_key) -> None:
    """Each failure says its own setup message."""
    with patch("groq.Groq") as groq_cls:
        groq_cls.return_value.chat.completions.create.side_effect = error
        result = GroqAnswerer(api_key="k", service=_service()).answer("question")
    assert result.error == kind
    assert result.text == _MESSAGES[message_key]


def test_empty_answer_says_so() -> None:
    """Empty answer says so."""
    with patch("groq.Groq") as groq_cls:
        groq_cls.return_value.chat.completions.create.return_value = _response("   ")
        result = GroqAnswerer(api_key="k", service=_service()).answer("question")
    assert result.error == OnlineErrorKind.EMPTY_RESPONSE
    assert result.text == _MESSAGES["groq_empty"]


def test_429_says_limit_with_time_then_later_calls_stay_offline() -> None:
    """429 says limit with time then later calls stay offline."""
    clock = FakeClock()
    status = OnlineStatus(wall_clock=clock)
    answerer = GroqAnswerer(api_key="k", service=_service(clock, status))
    with patch("groq.Groq") as groq_cls:
        groq_cls.return_value.chat.completions.create.side_effect = _rate_limit_error()
        first = answerer.answer("q1")
        announced = []
        second = answerer.answer("q2", on_going_online=lambda: announced.append(1))
    assert first.error == OnlineErrorKind.RATE_LIMITED
    assert "free online limit is reached" in first.text and "until" in first.text
    assert "Groq" not in first.text  # she never names the provider
    assert second.error == OnlineErrorKind.COOLING
    assert second.went_online is False and announced == []
    assert groq_cls.return_value.chat.completions.create.call_count == 1  # second call never touched the network


def test_system_prompt_lists_built_and_pending_features() -> None:
    """System prompt lists built and pending features."""
    prompt = build_system_prompt([("introduce yourself", True), ("read what is on the screen", False)])
    built_line = next(line for line in prompt.splitlines() if line.startswith("Things you can do"))
    pending_line = next(line for line in prompt.splitlines() if line.startswith("Things you cannot do"))
    assert "introduce yourself" in built_line and "screen" not in built_line
    assert "read what is on the screen" in pending_line
    assert "say plainly" in prompt


def test_system_prompt_persona_and_no_internal_names() -> None:
    """Female, confident, concise; abilities in plain words."""
    from jarvis.dashboard.module_status import BUILT, MODULE_STATUS
    from jarvis.settings import SETTINGS

    abilities = SETTINGS["groq"]["abilities"]
    prompt = build_system_prompt([(abilities[m.code], m.status == BUILT) for m in MODULE_STATUS if m.code in abilities])
    assert "female" in prompt and "confident" in prompt and "concise" in prompt
    lowered = prompt.lower()
    assert "never mention internal names" in lowered
    import re

    assert re.search(r"\bF\d+\b", prompt) is None  # no feature codes
    assert "trust level" not in lowered.replace("never mention internal names such as feature numbers, modules or trust levels", "")
    assert "go to sleep and wake up when asked" in prompt


def test_answerer_sends_the_capability_prompt() -> None:
    """Answerer sends the capability prompt."""
    with patch("groq.Groq") as groq_cls:
        groq_cls.return_value.chat.completions.create.return_value = _response("ok")
        GroqAnswerer(api_key="k", service=_service(), capabilities=[("F3 Open Any Application", True)]).answer("q")
    messages = groq_cls.return_value.chat.completions.create.call_args.kwargs["messages"]
    assert messages[0]["role"] == "system" and "F3 Open Any Application" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "q"}


def test_classifier_reads_retry_after() -> None:
    """Classifier reads retry after."""
    error = classify_groq_error(_rate_limit_error("120"))
    assert error.kind == OnlineErrorKind.RATE_LIMITED and error.retry_after_seconds == 120.0


def test_missing_groq_library_says_how_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing Groq library says how to install."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "groq":
            raise ImportError("No module named 'groq'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = GroqAnswerer(api_key="k", service=_service()).answer("q")
    assert result.error == OnlineErrorKind.LIBRARY_MISSING
    assert "pip install -r requirements.txt" in result.text


# -- Truncated answers ("3 tips" cut off mid-sentence) --


def _response_with_finish(text: str, finish_reason: str) -> MagicMock:
    fake = _response(text)
    fake.choices[0].finish_reason = finish_reason
    return fake


def _answer_with(response: MagicMock):
    with patch("groq.Groq") as groq_cls:
        groq_cls.return_value.chat.completions.create.return_value = response
        return GroqAnswerer(api_key="k", service=_service()).answer("give me 3 tips for studying")


def test_length_cut_answer_is_trimmed_to_the_last_full_sentence() -> None:
    """Length cut answer is trimmed to the last full sentence."""
    cut = "Space out your sessions. Test yourself often. Sleep well, it helps you tack"
    result = _answer_with(_response_with_finish(cut, "length"))
    assert result.text == "Space out your sessions. Test yourself often."
    assert result.trimmed is True
    assert result.error is None


def test_normal_answer_is_unchanged() -> None:
    """Normal answer is unchanged."""
    full = "Space out your sessions. Test yourself often. Sleep well, because it helps you remember"
    result = _answer_with(_response_with_finish(full, "stop"))
    assert result.text == full  # no trimming when Groq finished on its own
    assert result.trimmed is False


def test_length_cut_before_any_sentence_ended_says_so() -> None:
    """Length cut before any sentence ended says so."""
    result = _answer_with(_response_with_finish("Here are some ideas that might hel", "length"))
    assert result.text == _MESSAGES["groq_cut_off"]
    assert result.error == OnlineErrorKind.CUT_OFF


@pytest.mark.parametrize(
    "text, expected",
    [
        ("One. Two! Three? Fou", "One. Two! Three?"),
        ('He said "go." Then he', 'He said "go."'),
        ("Ends cleanly.", "Ends cleanly."),
        ("No end at all", ""),
        ("Version 1.5 is out. And th", "Version 1.5 is out."),
    ],
)
def test_trim_to_last_sentence(text, expected) -> None:
    """Trim to last sentence."""
    from jarvis.speech.qa import trim_to_last_sentence

    assert trim_to_last_sentence(text) == expected


def test_request_asks_for_low_reasoning_effort_and_the_bigger_budget() -> None:
    """Request asks for low reasoning effort and the bigger budget."""
    with patch("groq.Groq") as groq_cls:
        groq_cls.return_value.chat.completions.create.return_value = _response("ok")
        GroqAnswerer(api_key="k", service=_service()).answer("hi there")
    create_kwargs = groq_cls.return_value.chat.completions.create.call_args.kwargs
    assert create_kwargs["reasoning_effort"] == SETTINGS["groq"]["reasoning_effort"] == "low"
    assert create_kwargs["max_tokens"] >= 600


def test_prompt_keeps_answers_short_but_complete() -> None:
    """Prompt keeps answers short but complete."""
    prompt = build_system_prompt()
    assert "about three sentences" in prompt
    assert "exactly that many" in prompt


def test_prompt_asks_for_a_short_direct_first_sentence() -> None:
    """Prompt asks for a short direct first sentence."""
    # Her voice starts after the first sentence is made, so Groq
    # opens with at most eight words. Placed with the length rule: at the end
    # of the prompt only 1 of 5 real answers followed it.
    rule = SETTINGS["groq"]["opener_rule"]
    prompt = GroqAnswerer(api_key="x").system_prompt
    assert rule in prompt and "eight words" in rule and "accurate" in rule
    assert prompt.index("about three sentences") < prompt.index(rule) < prompt.index(SETTINGS["claims"]["prompt_rule"])
