"""A full autonomous run (the real app, Kokoro
commands fed to the real listener, real Groq / Open-Meteo / RSS): one
regression test per bug it found, with the words heard on the day.

Side effects are mocked as everywhere else (apps, Chrome, File Explorer,
the online classifier, Groq)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jarvis.actions import app_finder, find_file
from jarvis.actions.app_finder import ScoredMatch
from jarvis.actions.command_text import clean_command, split_glued
from jarvis.actions.file_index import clean_query
from jarvis.browser.commands import parse_search
from jarvis.controller import State
from jarvis.news.briefing import Briefing
from jarvis.news.feeds import Headline
from jarvis.router import IntentRouter
from jarvis.router.router import same_word
from jarvis.settings import SETTINGS
from jarvis.speech.claims import ClaimCheck
from jarvis.voice.listener import fragment_reason
from test_regressions_intent_router import _shown, jarvis  # noqa: F401 -- the shared rig (a pytest fixture)

_INTRO_START = "Hello everyone. I am JARVIS"


# -- 1. the router: a slip keeps its first letter ------------------------------------------------------


def test_a_fuzzy_cue_keeps_its_first_letter() -> None:
    """A fuzzy cue keeps its first letter."""
    assert not same_word("here", "where")  # "...to my friends here" ran as a memory recall
    assert same_word("brigthness", "brightness") and same_word("volumn", "volume")


# -- 2. intro phrasings (F1-05, -07, -08, -09, -10) ------------------------------------------------------


# "who are you" / "what are you" get the short
# identity answer (identity_who), the full intro only when asked to introduce.
@pytest.mark.parametrize("said, intent", [("JARVIS, Introduce yourself to my friends here", "intro"), ("Introduce yourself to the class", "intro"),
                                          ("I'm so uh, what are you exactly?", "identity_who"), ("Could you give us a quick introduction?", "intro"),
                                          ("Who am I talking to?", "identity_who"), ("Ugg, just tell me who you are already", "identity_who")])
def test_every_intro_phrasing_heard_on_the_day_is_the_intro(said, intent) -> None:
    """Every intro phrasing heard on the day is the intro."""
    verdict, routes = IntentRouter().decide(clean_command(said))
    assert verdict == "run" and routes[0].intent.name == intent, (said, verdict, routes)


@pytest.mark.parametrize("said", ["What are you doing?", "what is my name", "can I change your name"])
def test_intro_cues_do_not_swallow_other_questions(said) -> None:
    """Intro cues do not swallow other questions."""
    verdict, routes = IntentRouter().decide(clean_command(said))
    assert not (verdict == "run" and routes[0].intent.name == "intro")


def test_a_quick_introduction_says_the_locked_intro(jarvis) -> None:  # noqa: F811
    """A quick introduction says the locked intro."""
    assert jarvis["say"]("Could you give us a quick introduction?").startswith(_INTRO_START)
    assert jarvis["groq"].questions == []


# -- 3. sleep: "bedtime" sleeps; the classifier can't overrule "I need" (F15-06, -08) ---------------------


def test_bedtime_puts_her_to_sleep(jarvis) -> None:  # noqa: F811
    """Bedtime puts her to sleep."""
    jarvis["say"]("Okay, bedtime for you")
    assert jarvis["c"].state == State.SLEEPING


def test_the_classifier_cannot_overrule_an_avoided_phrase(jarvis) -> None:  # noqa: F811
    """The classifier cannot overrule an avoided phrase."""
    said = "I need some sleep tonight"
    jarvis["classifier"].answers[said.lower()] = '{"intent": "sleep", "slots": {}, "confidence": 0.95}'
    jarvis["say"](said)
    assert jarvis["c"].state == State.AWAKE
    assert "understood as: a general question" in _shown(jarvis)


def test_a_how_to_question_never_costs_a_classifier_call(jarvis) -> None:  # noqa: F811
    """A how to question never costs a classifier call."""
    said = "how do I turn the volume up"
    jarvis["classifier"].answers[said] = '{"intent": "volume_up", "slots": {}, "confidence": 0.95}'
    before = jarvis["pc"].volume
    jarvis["say"]("How do I turn the volume up?")
    assert jarvis["classifier"].calls == []
    assert jarvis["pc"].volume == before


def test_a_classified_command_shows_its_words_went_online(jarvis) -> None:  # noqa: F811
    """A classified command shows its words went online."""
    said = "could you be a dear and get the music app going"
    jarvis["classifier"].answers[said] = '{"intent": "open_app", "slots": {"rest": "spotify"}, "confidence": 0.92}'
    jarvis["say"](said)
    assert f'checking what that means online, sending "{said}"' in _shown(jarvis)
    record = jarvis["logbook"].last_completed()
    assert record.online_destination and "intent classifier" in record.online_destination
    assert "groq" not in _shown(jarvis).lower()  # Live Activity never names the service


# -- 4. glued "OpenX.com" and a page that was never opened (F3w-04, -08) ------------------------------------


@pytest.mark.parametrize("heard, out", [("OpenZorblex.com", "Open Zorblex.com"), ("OpenGetHub.com", "Open GetHub.com"),
                                        ("OpenAI is great", "OpenAI is great"), ("StartUp folder", "StartUp folder"),
                                        ("Opener", "Opener")])
def test_a_capital_letter_after_open_splits_the_glued_verb(heard, out) -> None:
    """A capital letter after open splits the glued verb."""
    assert split_glued(heard) == out


def test_an_unknown_glued_site_is_searched_never_sent_to_groq(jarvis) -> None:  # noqa: F811
    """An unknown glued site is searched never sent to Groq."""
    reply = jarvis["say"]("OpenZorblex.com")
    assert jarvis["groq"].questions == []
    assert jarvis["chrome"] and "google.com/search" in jarvis["chrome"][-1][1]
    assert "zorblex" in reply.lower()


def test_a_page_described_as_loaded_is_a_false_claim() -> None:
    """A page described as loaded is a false claim."""
    check = ClaimCheck(SETTINGS["claims"])
    said = "openzorblex.com loads now. the homepage shows a clean layout with a navigation bar."
    assert check.find(said, "openzorblex.com").name == "page"
    for fine in ["A website loads faster when images are compressed.", "The page loads slowly if your connection is weak.",
                 "Google.com opens in most browsers by default.", "I can't open a web page from here."]:
        assert check.find(fine.lower(), "q") is None, fine


# -- 5. app names: "Microsoft Word", "Google Maps" (F3a-04, F3w-07) ------------------------------------------


_DAY_APPS = [ScoredMatch("Word", 0.0, "app", r"C:\Start\Word.lnk"), ScoredMatch("Microsoft To Do", 0.0, "app", r"C:\Start\ToDo.lnk"),
             ScoredMatch("Microsoft Store", 0.0, "app", r"C:\Start\Store.lnk"), ScoredMatch("Maps", 0.0, "app", r"C:\Start\Maps.lnk")]


def test_open_microsoft_word_opens_word(jarvis, monkeypatch) -> None:  # noqa: F811
    """Open microsoft word opens word."""
    monkeypatch.setattr(app_finder, "enumerate_installed_apps", lambda store_apps=None: list(_DAY_APPS))
    assert jarvis["say"]("Open Microsoft Word") == "Opened Word."
    assert jarvis["launched"] == [r"C:\Start\Word.lnk"]


def test_open_google_maps_opens_the_site_not_the_maps_app(jarvis, monkeypatch) -> None:  # noqa: F811
    """Open google maps opens the site not the maps app."""
    monkeypatch.setattr(app_finder, "enumerate_installed_apps", lambda store_apps=None: list(_DAY_APPS))
    assert jarvis["say"]("Open Google Maps") == "Opening Google Maps in Chrome."
    assert jarvis["launched"] == [] and jarvis["chrome"][-1][1] == "https://maps.google.com"
    assert jarvis["say"]("Open Maps") == "Opened Maps."  # the app, named exactly, still opens


# -- 6. opening a found file never runs it and never blocks (F4-03, F4-08) -------------------------------------


def _found(tmp_path, name: str):
    folder = tmp_path / "files"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text("x = 1\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("name", ["budget.py", "setup.bat", "tool.ps1", "installer.exe", "settings.reg"])
def test_a_script_or_program_is_shown_in_explorer_never_opened(jarvis, tmp_path, name) -> None:  # noqa: F811
    """A script or program is shown in explorer never opened."""
    path = _found(tmp_path, name)
    ok, said = find_file.open_file_executor("open_file", {"path": str(path), "display_name": name})
    assert ok and said == f"{name} is a program or script, and opening it would run it, so I've shown it in File Explorer instead."
    assert jarvis["launched"] == []  # os.startfile never ran it
    assert jarvis["chrome"][-1] == ["explorer.exe", f"/select,{path}"]


def test_a_file_no_app_opens_is_shown_without_the_pick_an_app_dialog(jarvis, tmp_path, monkeypatch) -> None:  # noqa: F811
    """A file no app opens is shown without the pick an app dialog."""
    path = _found(tmp_path, "UmfPackSupport")
    monkeypatch.setattr(find_file, "_has_opener", lambda p: False)
    ok, said = find_file.open_file_executor("open_file", {"path": str(path), "display_name": "UmfPackSupport"})
    assert ok and said == "No app is set to open UmfPackSupport, so I've shown it in File Explorer instead."
    assert jarvis["launched"] == []


def test_an_ordinary_document_still_opens(jarvis, tmp_path, monkeypatch) -> None:  # noqa: F811
    """An ordinary document still opens."""
    path = _found(tmp_path, "zephyr_trip_itinerary.txt")
    monkeypatch.setattr(find_file, "_has_opener", lambda p: True)
    assert find_file.open_file_executor("open_file", {"path": str(path), "display_name": path.name}) == (True, f"Opened {path.name}.")
    assert jarvis["launched"] == [str(path)]


def test_the_association_check_knows_a_text_file_and_an_extensionless_one(tmp_path) -> None:
    """The association check knows a text file and an extensionless one."""
    pytest.importorskip("win32api")
    text, bare = tmp_path / "notes.txt", tmp_path / "UmfPackSupport"
    text.write_text("a", encoding="utf-8")
    bare.write_text("a", encoding="utf-8")
    assert find_file._has_opener(str(text)) and not find_file._has_opener(str(bare)) and find_file._has_opener(str(tmp_path))


# -- 7. file queries (F4-05, F4-07) --------------------------------------------------------------------------


def test_spoken_filler_never_reaches_the_file_query() -> None:
    """Spoken filler never reaches the file query."""
    assert clean_query("um, my budget stuff please") == "budget"


def test_not_found_names_what_was_searched_for(jarvis) -> None:  # noqa: F811
    """Not found names what was searched for."""
    assert jarvis["say"]("find the file called flamingo notes") == "I can't find any file called 'flamingo notes'."
    assert jarvis["say"]("find flamingo notes") == "I can't find anything called 'flamingo notes'."


# -- 8. "where is my <document>" finds it (F4-04) --------------------------------------------------------------


def test_where_is_my_report_is_a_file_search_not_a_memory(jarvis, tmp_path) -> None:  # noqa: F811
    """Where is my report is a file search not a memory."""
    reply = jarvis["say"]("Where is my quarterly budget report?")
    assert "haven't told me" not in reply
    assert "understood as: find file" in _shown(jarvis)


def test_where_is_my_appointment_is_still_a_memory_question(jarvis) -> None:  # noqa: F811
    """Where is my appointment is still a memory question."""
    assert jarvis["say"]("Where is my dentist appointment?") == "You haven't told me about your dentist appointment."


# -- 9. "why" skips earlier "why" questions, however they were worded (F5-05) -------------------------------------


def test_a_second_why_explains_the_action_not_the_first_why(jarvis) -> None:  # noqa: F811
    """A second why explains the action not the first why."""
    jarvis["say"]("open notepad")
    jarvis["say"]("What was the reason for that?")
    assert "open notepad" in jarvis["say"]("Explain what you just did.").lower()


# -- 10. fragments of someone else's sentence (room audio) -------------------------------------------------------


@pytest.mark.parametrize("heard", ["and other videos.", "and I thought since the interview at this.", "but the other ones."])
def test_a_lower_case_mid_sentence_fragment_is_ignored(heard) -> None:
    """A lower case mid sentence fragment is ignored."""
    assert fragment_reason(heard, 0.1) == "mid-sentence"


@pytest.mark.parametrize("heard", ["And open Notepad", "So what time is it?", "Open Notepad"])
def test_a_command_starting_with_a_capital_conjunction_still_counts(heard) -> None:
    """A command starting with a capital conjunction still counts."""
    assert fragment_reason(heard, 0.1) is None


# -- 11. briefing headlines are said whole when possible (F21-01) ------------------------------------------------


def _h(title: str, source: str) -> Headline:
    return Headline(title=title, link=f"https://x/{abs(hash(title))}", source=source, published=datetime.now(timezone.utc))


def test_the_briefing_prefers_headlines_that_fit_whole() -> None:
    """The briefing prefers headlines that fit whole."""
    long = _h("Despite concerns, the Trump administration still sends pregnant migrant minors to detention", "NPR")
    short = _h("Markets rally after rate decision", "NPR")
    found = [_h("US approves missile sale to Ukraine", "BBC"), long, short, _h("Storm hits the south coast", "BBC")]
    picked = Briefing._top_general(found)  # three of the four
    assert long not in picked and short in picked and len(picked) == 3


def test_a_long_headline_is_still_used_when_nothing_else_fits() -> None:
    """A long headline is still used when nothing else fits."""
    long = _h("Despite concerns, the Trump administration still sends pregnant migrant minors to detention", "NPR")
    assert long in Briefing._top_general([long])


# -- 12. searches: "$1,000" and "... online" (F16b-09, -10) --------------------------------------------------------


def test_thousands_and_dollars_survive_cleaning() -> None:
    """Thousands and dollars survive cleaning."""
    assert clean_command("Search online for the best laptops under $1,000") == "search online for the best laptops under 1000 dollars"


def test_look_up_x_online_searches_for_x() -> None:
    """Look up x online searches for x."""
    assert parse_search("look up the meaning of serendipity online") == "the meaning of serendipity"
    assert parse_search("search for how to stay online") == "how to stay online"


# -- 13. time and date stay local (TD-07, TD-09, TD-10) ---------------------------------------------------------


def _clock(said: str):
    from jarvis.actions.clock import ClockMatcher

    moment = datetime(2026, 10, 31, 18, 25, tzinfo=timezone.utc)
    answer = ClockMatcher(now=lambda: moment)(clean_command(said))
    return answer.text if answer else None


def test_got_the_time_is_the_time_not_a_city() -> None:
    """Got the time is the time not a city."""
    assert _clock("Got the time?") == "It's 6:25 PM."


def test_year_and_day_of_the_week_are_answered_from_the_clock() -> None:
    """Year and day of the week are answered from the clock."""
    assert _clock("What year is it?") == "It's 2026."
    assert _clock("Which day of the week is it?") == "It's Saturday, 31 October."


def test_a_real_city_still_gets_its_own_time() -> None:
    """A real city still gets its own time."""
    assert _clock("What time is it in London?").endswith("in London, on Saturday.")


# -- 14. Whisper's hesitations are filler (F5-09) -------------------------------------------------------------------


def test_hesitations_are_dropped_before_matching() -> None:
    """Hesitations are dropped before matching."""
    assert clean_command("Uhh, why did you Umm do that?") == "why did you do that"


@pytest.mark.parametrize("said", ["Explain what you just did", "What did you just do?"])
def test_explain_what_you_did_is_local(said) -> None:
    """Explain what you did is local."""
    verdict, routes = IntentRouter().decide(clean_command(said))
    assert verdict == "run" and routes[0].intent.name == "why"


# -- 15. weather: a length is never a city; comparisons are questions (F16g-07) ---------------------------------------


def test_weather_versus_climate_in_detail_is_a_question() -> None:
    """Weather versus climate in detail is a question."""
    from jarvis.actions.weather import parse_request

    assert parse_request(clean_command("What is the difference between weather and climate, in detail?")) is None


@pytest.mark.parametrize("said, city", [("What's the weather in Sydney in detail?", "sydney"), ("Weather in Hamilton, briefly", "hamilton")])
def test_a_length_phrase_is_not_part_of_the_city(said, city) -> None:
    """A length phrase is not part of the city."""
    from jarvis.actions.weather import parse_request

    assert parse_request(clean_command(said)).city == city


def test_a_bedtime_story_is_not_an_order_to_sleep() -> None:
    """A bedtime story is not an order to sleep."""
    assert IntentRouter().decide("tell me a bedtime story")[0] == "none"


# -- 16. memory (F10-05, F10-06, F10-10) --------------------------------------------------------------------------


def test_a_trailing_remember_that_is_not_stored() -> None:
    """A trailing remember that is not stored."""
    from jarvis.memory.commands import clean_fact

    assert clean_fact("my gym days are now tuesday and friday remember that") == "my gym days are tuesday and friday"


def test_forget_everything_about_me_needs_two_confirms(jarvis) -> None:  # noqa: F811
    """Forget everything about me needs two confirms."""
    jarvis["say"]("Remember that my gym days are Monday and Thursday")
    jarvis["say"]("Remember that my sister's birthday is on 12 March")
    reply = jarvis["say"]("Forget everything you remember about me")
    assert jarvis["c"].state == State.CONFIRMING and "deletes all 2 things" in reply
    jarvis["orch"].handle_typed_command("cancel")


def test_a_question_about_himself_is_answered_from_memory(jarvis) -> None:  # noqa: F811
    """A question about himself is answered from memory."""
    jarvis["say"]("Remember that my gym days are Tuesday and Friday")
    assert jarvis["say"]("What days do I go to the gym?").startswith("You told me your gym days are tuesday and friday.")
    assert jarvis["groq"].questions == []


def test_a_general_question_with_i_still_goes_online(jarvis) -> None:  # noqa: F811
    """A general question with i still goes online."""
    jarvis["say"]("Remember that my gym days are Tuesday and Friday")
    jarvis["say"]("What do I need to bake bread?")
    assert jarvis["groq"].questions == ["what do i need to bake bread"]


# -- 17. small talk stays local (F14-06) ----------------------------------------------------------------------------


@pytest.mark.parametrize("said", ["How are you doing today?", "Hey Jarvis, how's it going?", "How are you?"])
def test_how_are_you_is_answered_here(jarvis, said) -> None:  # noqa: F811
    """How are you is answered here."""
    reply = jarvis["say"](said)
    assert reply in SETTINGS["small_talk"]["how_are_you_replies"]
    assert jarvis["groq"].questions == [] and jarvis["chrome"] == []


def test_how_are_you_feeling_about_something_is_a_real_question(jarvis) -> None:  # noqa: F811
    """How are you feeling about something is a real question."""
    assert jarvis["say"]("How are you feeling about the election?") not in SETTINGS["small_talk"]["how_are_you_replies"]


# -- 18. screen names and finding words (F8-01, F8-08, F8-09) ---------------------------------------------------------


@pytest.mark.parametrize("title, process, said", [
    ("meeting_notes_marigold.txt - Notepad", "Notepad.exe", "a Notepad file called meeting notes marigold"),
    ("marigold_launch_plan.docx  -  Compatibility Mode - Word", "WINWORD.EXE", "a Word document called marigold launch plan"),
    ("123456789_11112222333344445_6666777788889990000_n.jpg - Google Chrome", "chrome.exe", "an image open in Chrome")])
def test_window_names_read_the_way_he_would_say_them(title, process, said) -> None:
    """Window names read the way he would say them."""
    from types import SimpleNamespace

    from jarvis.screen.commands import describe_window

    assert describe_window(SimpleNamespace(title=title, process=process))[0] == said


@pytest.mark.parametrize("said, word", [("is the word, Marigold, on my screen.", "marigold"), ("does my screen say error", "error")])
def test_more_ways_to_ask_for_a_word_on_the_screen(said, word) -> None:
    """More ways to ask for a word on the screen."""
    from jarvis.screen.commands import ScreenCommands

    request = ScreenCommands().matcher(clean_command(said))
    assert request.params["purpose"] == "find" and request.params["word"] == word


# -- 19. camera: a generic brand question is about what she saw (F9-05) ------------------------------------------------


def test_what_brand_is_it_is_about_the_main_object_seen() -> None:
    """What brand is it is about the main object seen."""
    from jarvis.vision.camera import Camera
    from jarvis.vision.commands import VisionCommands
    from jarvis.vision.detector import Detection

    vision = VisionCommands(Camera(opener=lambda index: None, blocked=lambda: False))
    vision._last = (vision._clock(), [Detection("person", 0.9, (0, 0, 100, 400)), Detection("refrigerator", 0.8, (200, 0, 600, 480)),
                                      Detection("cup", 0.6, (10, 10, 30, 30))], 640)
    assert vision._limits("what brand is it").text == "I can tell it's a refrigerator, but not the brand or model."


# -- 20. Office lock files are not files to find (F19-06, F19-07) -------------------------------------------------


def test_office_lock_files_are_never_indexed(tmp_path) -> None:
    """Office lock files are never indexed."""
    from jarvis.actions.file_index import FileIndex

    (tmp_path / "marigold_launch_plan.docx").write_bytes(b"x")
    (tmp_path / "~$rigold_launch_plan.docx").write_bytes(b"x")
    index = FileIndex(roots=[tmp_path])
    index.rebuild_now()
    assert [m.filename for m in index.search("marigold launch plan")] == ["marigold_launch_plan.docx"]


# -- 21. a misheard confirm with nothing pending stays local (F7-10) ---------------------------------------------------


def test_a_misheard_confirm_with_nothing_pending_is_answered_here(jarvis) -> None:  # noqa: F811
    """A misheard confirm with nothing pending is answered here."""
    assert jarvis["say"]("Yes, Confirmer") == "Nothing to confirm right now."
    assert jarvis["groq"].questions == []


def test_yes_still_answers_did_you_mean(jarvis) -> None:  # noqa: F811
    """Yes still answers did you mean."""
    jarvis["classifier"].answers["could you get the music app going"] = '{"intent": "open_app", "slots": {"rest": "spotify"}, "confidence": 0.6}'
    assert jarvis["say"]("could you get the music app going") == "Did you mean 'open spotify'?"
    assert jarvis["say"]("yes") == "Opened Spotify."


def test_a_question_about_cancelling_is_still_a_question(jarvis) -> None:  # noqa: F811
    """A question about cancelling is still a question."""
    jarvis["say"]("how do I cancel a subscription")
    assert jarvis["groq"].questions == ["how do i cancel a subscription"]


# -- 22. an expired confirmation is cancelled, not left waiting (F7-06) --------------------------------------------------


def test_an_expired_confirmation_is_cancelled_and_said(jarvis) -> None:  # noqa: F811
    """An expired confirmation is cancelled and said."""
    from stretch_rig import wait

    jarvis["c"]._confirmation_timeout = 0.3
    jarvis["orch"]._confirmation_seconds = 0.3
    jarvis["say"]("lock the screen")
    assert jarvis["c"].state == State.CONFIRMING
    assert wait(lambda: jarvis["c"].state == State.AWAKE, timeout=5.0)
    assert jarvis["speech"].texts[-1] == SETTINGS["conversation"]["confirmation_expired_reply"]
    assert jarvis["c"].pending_confirmation() is None
    assert jarvis["pc"].calls == [] or "lock" not in str(jarvis["pc"].calls)


def test_a_confirmation_answered_in_time_is_not_touched_by_the_timer(jarvis) -> None:  # noqa: F811
    """A confirmation answered in time is not touched by the timer."""
    from stretch_rig import wait

    jarvis["c"]._confirmation_timeout = 0.6
    jarvis["orch"]._confirmation_seconds = 0.6
    jarvis["say"]("lock the screen")
    jarvis["orch"].handle_typed_command("cancel")
    assert wait(lambda: jarvis["c"].state == State.AWAKE)
    import time

    time.sleep(1.5)
    assert SETTINGS["conversation"]["confirmation_expired_reply"] not in jarvis["speech"].texts


# -- 23. apps she opened are on the screen she reads (F8-01, F8-08, pass 2) ----------------------------------------------


def test_apps_she_started_are_not_hidden_as_her_own(monkeypatch) -> None:
    """Apps she started are not hidden as her own."""
    import psutil

    from jarvis.screen import capture

    class _P:
        def __init__(self, pid, name, kids=()):
            self.pid, self._name, self._kids = pid, name, list(kids)

        def name(self):
            return self._name

        def children(self, recursive=False):
            return self._kids

    me = _P(100, "python.exe", [_P(101, "flet.exe"), _P(102, "Notepad.exe"), _P(103, "WINWORD.EXE")])
    monkeypatch.setattr(psutil, "Process", lambda pid=None: me)
    assert capture._own_process_ids() == {100, 101}


# -- 24. the confirmation card reads as words (F6-05) -----------------------------------------------------------------------


def test_the_confirmation_card_shows_no_raw_dict() -> None:
    """The confirmation card shows no raw dict."""
    from jarvis.dashboard.app import plain_param_lines

    assert plain_param_lines({"display_name": "lock the screen"}) == ["Lock the screen"]
    assert plain_param_lines({"display_name": "turn wi-fi off", "on": False}) == ["Turn wi-fi off", "on: False"]


# -- 25. live information: schedules and "current" office holders (F16l-05, -06, -07) ----------------------------------------


@pytest.mark.parametrize("said", ["When is the next Formula One race?", "Who is the current prime minister of Australia?",
                                  "When's the next India match?"])
def test_schedules_and_current_office_holders_are_live(said) -> None:
    """Schedules and current office holders are live."""
    from jarvis.actions.web_query import needs_live_info

    assert needs_live_info(said)


def test_the_live_instructions_forbid_naming_the_search_tool() -> None:
    """The live instructions forbid naming the search tool."""
    assert "Never name the search tool" in SETTINGS["web"]["live_instructions"]


@pytest.mark.parametrize("answer, spoken", [
    ("Bitcoin is $77,186.05 today, October 31 2026. This comes from exa.ai.", "Bitcoin is $77,186.05 today, October 31 2026."),
    ("Real Madrid lost 1-2 to Barcelona. Source: espn.com", "Real Madrid lost 1-2 to Barcelona. Source: espn.com"),
    ("Examples of exams are here. Exa is a tool. Done.", "Examples of exams are here. Done.")])
def test_a_sentence_naming_the_search_service_is_never_spoken(answer, spoken) -> None:
    """A sentence naming the search service is never spoken."""
    from jarvis.actions.conversation import drop_service_sentences

    assert drop_service_sentences(answer) == spoken
