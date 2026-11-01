"""Regressions from a live voice run (Kokoro speech fed
into the real listener and Whisper). Each test uses the words Whisper
actually wrote. Side effects mocked as in test_regressions_core_rig."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jarvis.actions import file_index
from jarvis.actions.greeting import GreetingMatcher, SmallTalkMatcher
from jarvis.controller import State
from jarvis.router.router import IntentRouter
from jarvis.settings import SETTINGS
from jarvis.speech.rudeness import RudenessFilter
from jarvis.system.commands import parse
from jarvis.vision.detector import Detection
from test_regressions_core_rig import PERSON, THANK_YOU, jarvis  # noqa: F401 -- the fixture

BIKE = Detection("bicycle", 0.95, (40, 100, 300, 400))
DOG = Detection("dog", 0.92, (350, 200, 600, 470))


def _bike_and_dog(rig) -> None:
    rig["detector"].frames = [[BIKE, DOG]] * 3


def test_good_afternoon_to_you_is_a_greeting() -> None:
    """Good afternoon to you is a greeting."""
    assert GreetingMatcher()("good afternoon to you").text.startswith("Good ")
    assert GreetingMatcher()("morning everyone").text.startswith("Good ")


def test_great_work_today_is_a_compliment(jarvis) -> None:  # noqa: F811
    """Great work today is a compliment."""
    assert jarvis["say"]("Great work today, Jarvis") in THANK_YOU
    assert jarvis["groq"].questions == [] and jarvis["groq"].live == []


def test_you_are_absolutely_useless_you_idiot_asks_nothing(jarvis) -> None:  # noqa: F811
    """You are absolutely useless you idiot asks nothing."""
    from jarvis.speech.rudeness import RudenessFilter

    jarvis["orch"]._rudeness = RudenessFilter(SETTINGS["rudeness"]["words"])
    assert jarvis["say"]("You are absolutely useless, you idiot.") == "That wasn't a nice way to say it."
    assert jarvis["groq"].questions == []


@pytest.mark.parametrize("said, left", [("you are so dumb, open paint", "open paint"), ("you're truly stupid", "")])
def test_intensifiers_between_you_are_and_the_insult(said, left) -> None:
    """Intensifiers between you are and the insult."""
    assert RudenessFilter(SETTINGS["rudeness"]["words"]).check(said).cleaned == left


@pytest.mark.parametrize("said", ["Explain what's on my display", "explain what is on my monitor"])
def test_explain_my_display_is_the_screen_explain(jarvis, said) -> None:  # noqa: F811
    """Explain my display is the screen explain."""
    assert jarvis["say"](said).startswith("I've put exactly what I'd send")
    assert jarvis["c"].state == State.CONFIRMING
    jarvis["orch"].handle_typed_command("cancel")


@pytest.mark.parametrize("said", ["What do you see in front of you?", "Have a look at this.", "what can you see right now"])
def test_look_phrases_with_extra_words_open_the_camera(jarvis, said) -> None:  # noqa: F811
    """Look phrases with extra words open the camera."""
    assert jarvis["say"](said) == "I can see a person and a cup."
    assert jarvis["detector"].calls == 3


def test_what_brand_is_the_bike(jarvis) -> None:  # noqa: F811
    """What brand is the bike."""
    _bike_and_dog(jarvis)
    jarvis["say"]("Have a look")
    assert jarvis["say"]("What brand is the bike?") == "I can tell it's a bicycle, but not the brand or model."
    assert jarvis["groq"].questions == []


def test_could_you_scout_out_whats_around_me_with_the_webcam(jarvis) -> None:  # noqa: F811
    """Could you scout out whats around me with the webcam."""
    assert jarvis["say"]("Could you scout out what's around me with the webcam?") == "I can see a person and a cup."
    assert jarvis["classifier"].calls == [] and jarvis["groq"].questions == []


def test_watch_the_weather_in_perth_is_weather() -> None:
    """Watch the weather in perth is weather."""
    verdict, routes = IntentRouter().decide("Watch the weather in Perth")
    assert verdict == "run" and routes[0].intent.name == "weather" and routes[0].slot == "perth"
    verdict, routes = IntentRouter().decide("watch the news on youtube")
    assert verdict == "run" and routes[0].intent.name == "youtube_play"


def test_turn_the_sound_down_a_notch(jarvis) -> None:  # noqa: F811
    """Turn the sound down a notch."""
    jarvis["say"]("Turn the sound down a notch")
    assert jarvis["pc"].volume == 20


@pytest.mark.parametrize("heard", ["Make it Max-er.", "make it maxed out", "make it max"])
def test_make_it_max_as_whisper_writes_it(heard) -> None:
    """Make it max as Whisper writes it."""
    assert parse(heard) == ("volume", "set", 100)


def test_jarvis_e2e_results_are_not_indexed() -> None:
    """JARVIS e2e results are not indexed."""
    root = Path(file_index.__file__).resolve().parents[2]
    assert os.path.normcase(str(root / "tests" / "e2e" / "results")) in file_index._EXCLUDED_PATHS


def test_small_talk_tails_do_not_make_other_sentences_compliments() -> None:
    """Small talk tails do not make other sentences compliments."""
    matcher = SmallTalkMatcher()
    assert matcher("good luck today") is None
    assert matcher("great work today") is not None


def test_have_a_look_key_never_opens_an_app(jarvis) -> None:  # noqa: F811
    """Whisper wrote "Have a look, key." and the classifier said open_app("key"):
    the On-Screen Keyboard opened. No open-word was said: she asks instead."""
    said = "Have a look, key."
    jarvis["classifier"].answers["have a look, key"] = '{"intent": "open_app", "slots": {"rest": "key"}, "confidence": 0.9}'
    reply = jarvis["say"](said)
    assert jarvis["classifier"].calls, "expected the classifier to be asked"
    assert reply == "Did you mean 'open key'?"
    assert jarvis["launched"] == [] and jarvis["chrome"] == []


@pytest.mark.parametrize("query, opens", [("key", None), ("board", None), ("keyboard", "On-Screen Keyboard"), ("calc", "Calculator")])
def test_short_fragments_never_launch(query, opens) -> None:
    """Short fragments never launch."""
    from jarvis.actions.app_finder import ScoredMatch, confident, find_app

    apps = [ScoredMatch("On-Screen Keyboard", 0.0, "app", "osk.lnk"), ScoredMatch("Calculator", 0.0, "app", "calc.lnk")]
    found = find_app(query, apps)
    assert (found[0].display_name if found and confident(found) else None) == opens


def test_alright_then_is_local(jarvis) -> None:  # noqa: F811
    """Whisper: "Alright then" -- it went online."""
    assert jarvis["say"]("Alright then") in SETTINGS["small_talk"]["ack_replies"]
    assert jarvis["groq"].questions == []


def test_take_a_look_through_the_camera(jarvis) -> None:  # noqa: F811
    """Take a look through the camera."""
    assert jarvis["say"]("Take a look through the camera") == "I can see a person and a cup."


@pytest.mark.parametrize("heard", ["All right, thens", "okays", "alright then"])
def test_whispers_stray_plural_on_small_talk(heard) -> None:
    """Whispers stray plural on small talk."""
    assert SmallTalkMatcher()(heard) is not None


@pytest.mark.parametrize("heard, dropped", [("and you open ChatGPT.", False), ("so set the volume to 20", False),
                                            ("and other videos.", True), ("and you know what I mean", True),
                                            ("but open it later", True)])
def test_and_you_open_chatgpt_is_his_command(heard, dropped) -> None:
    """Whisper wrote "Can you open ChatGPT?" as "and you open ChatGPT." twice
    and it was silently dropped as mid-sentence."""
    from jarvis.voice.listener import fragment_reason

    assert (fragment_reason(heard, 0.1) == "mid-sentence") is dropped
