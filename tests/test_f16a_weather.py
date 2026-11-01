"""F16a Weather. Open-Meteo is faked at the fetch level
with reply shapes captured from the live service: Sydney (5 places, 4 countries), Leeds, Hamilton, and a
made-up name (no `results` key at all). No test touches the network."""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import List

import groq
import pytest

from jarvis.actions import app_finder, open_app
from jarvis.actions.file_index import FileIndex
from jarvis.actions.find_file import FileFinder
from jarvis.actions.weather import CREDIT_LINE, UNKNOWN_CONDITIONS, WeatherClient, WeatherMatcher, WeatherRequest, answer_text, choose_place, conditions, parse_forecast, parse_places, parse_request
from jarvis.controller import Controller, Event, EventType, State
from jarvis.dashboard.activity_feed import format_record
from jarvis.dashboard.app import register_matchers
from jarvis.interpreter import Interpreter
from jarvis.logbook import Logbook
from jarvis.logbook.debug_log import DebugLog
from jarvis.online import ConnectionMode, OnlineErrorKind, OnlineService, OnlineStatus
from jarvis.orchestrator import Orchestrator
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput
from jarvis.speech.qa import GroqAnswerer

_W = SETTINGS["weather"]
_T = _W["texts"]


def _geo(*rows):
    return {"results": [
        {"name": n, "latitude": lat, "longitude": lon, "admin1": a, "country": c, "country_code": cc, "population": pop, "timezone": tz}
        for n, a, c, cc, pop, lat, lon, tz in rows
    ], "generationtime_ms": 0.5}


SYDNEY = _geo(
    ("Sydney", "New South Wales", "Australia", "AU", 5638830, -33.86785, 151.20732, "Australia/Sydney"),
    ("Sidney", "Ohio", "United States", "US", 20858, 40.28, -84.15, "America/New_York"),
    ("Sydney", "Nova Scotia", "Canada", "CA", 105968, 46.14, -60.19, "America/Glace_Bay"),
    ("Sydney", "Florida", "United States", "US", None, 27.96, -82.21, "America/New_York"),
    ("Sydney", "North West", "South Africa", "ZA", None, -26.0, 27.0, "Africa/Johannesburg"),
)
LEEDS = _geo(
    ("Leeds", "England", "United Kingdom", "GB", 536280, 53.79, -1.55, "Europe/London"),
    ("Leeds", "Alabama", "United States", "US", 11936, 33.55, -86.54, "America/Chicago"),
    ("Hoopeston", "Illinois", "United States", "US", 5220, 40.47, -87.67, "America/Chicago"),
)
HAMILTON = _geo(
    ("Hamilton", "Ontario", "Canada", "CA", 569353, 43.25, -79.85, "America/Toronto"),
    ("Hamilton", "Waikato Region", "New Zealand", "NZ", 192100, -37.78, 175.28, "Pacific/Auckland"),
    ("Hamilton", "Ohio", "United States", "US", 62407, 39.4, -84.56, "America/New_York"),
    ("Hamilton", "Alabama", "United States", "US", 6772, 34.14, -87.99, "America/Chicago"),
)
NOTHING = {"generationtime_ms": 0.3}  # live shape: no `results` key at all


def _forecast(temp=20.2, feels=19.8, code=53, days=((53, 22.0, 17.5, 77), (45, 25.0, 16.9, 12))):
    return {
        "timezone": "Australia/Sydney",
        "current": {"time": "2026-10-23T10:00", "temperature_2m": temp, "apparent_temperature": feels, "weather_code": code, "wind_speed_10m": 15.0},
        "daily": {
            "time": ["2026-10-23", "2026-10-24"][: len(days)],
            "weather_code": [d[0] for d in days],
            "temperature_2m_max": [d[1] for d in days],
            "temperature_2m_min": [d[2] for d in days],
            "precipitation_probability_max": [d[3] for d in days],
        },
    }


class FakeOpenMeteo:
    """fetch(url, timeout): answers from canned replies by city name."""

    def __init__(self, places=None, forecast=None):
        self.places = {"sydney": SYDNEY, "leeds": LEEDS, "hamilton": HAMILTON, **(places or {})}
        self.forecast_reply = forecast or _forecast()
        self.calls: List[str] = []
        self.timeouts: List[float] = []
        self.fail: List[Exception] = []

    def __call__(self, url: str, timeout: float):
        self.calls.append(url)
        self.timeouts.append(timeout)
        if self.fail:
            raise self.fail.pop(0)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if "geocoding" in url:
            return self.places.get(query["name"].lower(), NOTHING)
        return self.forecast_reply

    def kinds(self) -> List[str]:
        """Fake kinds for the tests."""
        return ["geo" if "geocoding" in u else "forecast" for u in self.calls]


def _service(status=None):
    return OnlineService("Open-Meteo", status or OnlineStatus(), timeout_seconds=10, max_retries=1, cooldown_seconds=300)


def _client(fake, status=None, cache_path=None):
    return WeatherClient(_service(status), fetch=fake, cache_path=cache_path)


# -- request building ----------------------------------------------------------------


def test_geocode_url_carries_name_count_and_language() -> None:
    """Geocode URL carries name count and language."""
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(_client(FakeOpenMeteo()).geocode_url("Leeds")).query))
    assert q["name"] == "Leeds" and q["count"] == "5" and q["language"] == "en"


def test_forecast_url_carries_timezone_daily_and_units_from_settings() -> None:
    """Forecast URL carries timezone daily and units from settings."""
    place = parse_places(SYDNEY)[0]
    url = _client(FakeOpenMeteo()).forecast_url(place)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["daily"] == "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    assert q["current"] == "temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
    assert q["timezone"] == "Australia/Sydney" and q["forecast_days"] == "2"
    assert q["temperature_unit"] == _W["temperature_unit"] and q["wind_speed_unit"] == _W["wind_speed_unit"]


# -- understanding the request -------------------------------------------------------


@pytest.mark.parametrize(
    "text, kind, day, city",
    [
        ("what's the weather", "weather", 0, None),
        ("weather in melbourne", "weather", 0, "melbourne"),
        ("will it rain today", "rain", 0, None),
        ("temperature tomorrow", "temperature", 1, None),
        ("what's the weather like in sydney tomorrow", "weather", 1, "sydney"),
        ("is it going to rain in leeds tomorrow", "rain", 1, "leeds"),
        ("how hot is it in perth", "temperature", 0, "perth"),
        ("melbourne weather", "weather", 0, "melbourne"),
        ("forecast for hamilton", "weather", 0, "hamilton"),
        ("whether in sydney", "weather", 0, "sydney"),
        ("is it raining right now", "rain", 0, None),
        ("tomorrow's weather", "weather", 1, None),
        ("weather in leeds england", "weather", 0, "leeds england"),
    ],
)
def test_weather_requests(text, kind, day, city) -> None:
    """Weather requests."""
    assert parse_request(text) == WeatherRequest(kind=kind, day=day, city=city)


@pytest.mark.parametrize(
    "text",
    ["open weather", "explain how weather forecasts work", "is there a weather warning for sydney", "what is photosynthesis",
     "play rain sounds", "whether or not", "find my rain jacket photos", "what time is it"],
)
def test_not_weather_requests(text) -> None:
    """Not weather requests."""
    assert parse_request(text) is None


# -- choosing the place ------------------------------------------------------------------


def test_sydney_is_dominant_so_no_question() -> None:
    """Sydney is dominant so no question."""
    assert choose_place(parse_places(SYDNEY), 10).admin1 == "New South Wales"  # 5.6M vs 106K


def test_live_leeds_is_dominant_too() -> None:
    """Live leeds is dominant too."""
    # Live data Leeds, England 536,280 vs Leeds, Alabama 11,936 (45x).
    assert choose_place(parse_places(LEEDS), 10).country_code == "GB"


def test_similar_sizes_in_different_countries_ask() -> None:
    """Similar sizes in different countries ask."""
    similar = _geo(("Leeds", "England", "United Kingdom", "GB", 50000, 0, 0, "Europe/London"),
                   ("Leeds", "Alabama", "United States", "US", 12000, 0, 0, "America/Chicago"))
    assert choose_place(parse_places(similar), 10) is None
    assert choose_place(parse_places(HAMILTON), 10) is None  # 569K vs 192K


def test_missing_population_never_counts_as_dominant() -> None:
    """Missing population never counts as dominant."""
    unknown = _geo(("Springfield", "Somewhere", "A", "AA", None, 0, 0, "UTC"), ("Springfield", "Other", "B", "BB", 10, 0, 0, "UTC"))
    assert choose_place(parse_places(unknown), 10) is None


def test_same_country_never_asks() -> None:
    """Same country never asks."""
    same = _geo(("Springfield", "Illinois", "United States", "US", 100, 0, 0, "UTC"), ("Springfield", "Missouri", "United States", "US", 200, 0, 0, "UTC"))
    assert choose_place(parse_places(same), 10).admin1 == "Illinois"


@pytest.mark.parametrize("reply", [NOTHING, {"results": []}])
def test_missing_or_empty_results_mean_not_found(reply) -> None:
    """Missing or empty results mean not found."""
    assert parse_places(reply) == []


# -- forecast and words ----------------------------------------------------------------


def test_wmo_codes() -> None:
    """Wmo codes."""
    assert conditions(0) == "clear" and conditions(63) == "rain" and conditions(95) == "thunderstorms"
    assert conditions(42) == UNKNOWN_CONDITIONS and conditions(None) == UNKNOWN_CONDITIONS


@pytest.mark.parametrize("broken", [
    {"current": {}, "daily": {}},
    {"current": {"temperature_2m": None, "apparent_temperature": 1, "weather_code": 0}, "daily": _forecast()["daily"]},
    {"current": _forecast()["current"], "daily": {**_forecast()["daily"], "temperature_2m_max": [22.0]}},
    ["not", "an", "object"],
])
def test_a_reply_with_a_gap_is_a_service_error_never_a_guess(broken) -> None:
    """A reply with a gap is a service error never a guess."""
    from jarvis.online import OnlineError

    with pytest.raises(OnlineError) as info:
        parse_forecast(broken, 2)
    assert info.value.kind == OnlineErrorKind.SERVICE_ERROR


def test_answers_use_only_the_forecast_numbers() -> None:
    """Answers use only the forecast numbers."""
    f = parse_forecast(_forecast(), 2)
    assert answer_text(WeatherRequest("weather", 0, None), "Sydney", f) == \
        "In Sydney it's 20 degrees and drizzle, feels like 20. Today: high 22, low 18, 77 percent chance of rain."
    assert answer_text(WeatherRequest("weather", 1, None), "Sydney", f) == \
        "Tomorrow in Sydney: foggy, high 25, low 17, 12 percent chance of rain."
    assert answer_text(WeatherRequest("rain", 0, None), "Sydney", f) == \
        "In Sydney there's a 77 percent chance of rain today. Right now it's drizzle and 20 degrees."
    assert answer_text(WeatherRequest("temperature", 1, None), "Sydney", f) == "Tomorrow in Sydney: high 25, low 17."


# -- the matcher on its own ------------------------------------------------------------


class Recorder:
    """Stands in for the orchestrator: records steps, destination, speech."""

    def __init__(self) -> None:
        self.steps: List[tuple] = []
        self.destinations: List[str] = []
        self.said: List[str] = []
        self.stop = False
        self.picks = None
        self.controller = SimpleNamespace(is_stop_requested=lambda: self.stop)

    def report_step(self, text, replaces=None, closing=False, session_only=False):
        """Fake report step for the tests."""
        self.steps.append((text, closing))

    def record_online_destination(self, d):
        """Fake record online destination for the tests."""
        self.destinations.append(d)

    def announce(self, text):
        """Fake announce for the tests."""
        self.said.append(text)

    def when_command_finishes(self, callback):
        """Fake when command finishes for the tests."""
        callback()


def _matcher(fake=None, status=None, cache_path=None):
    from jarvis.actions.command_text import PickSlot

    fake = fake or FakeOpenMeteo()
    status = status or OnlineStatus()
    m = WeatherMatcher(_client(fake, status, cache_path), status, now=lambda: datetime(2026, 10, 23, 10, 0))
    rec = Recorder()
    rec.picks = PickSlot()
    m._orchestrator, m._slot = rec, rec.picks
    return m, rec, fake


def test_default_city_one_geocode_one_forecast_and_r4_logging() -> None:
    """Default city one geocode one forecast and R4 logging."""
    m, rec, fake = _matcher()
    answer = m("what's the weather")
    assert fake.kinds() == ["geo", "forecast"]
    assert answer.text.startswith("In Sydney it's 20 degrees")
    steps = [s for s, _ in rec.steps]
    assert steps[0] == "searching online: weather, default city"
    assert "found default city" in steps and "forecast received" in steps
    assert (CREDIT_LINE, True) in rec.steps  # shown after what she says
    assert rec.said == ["Searching online."]  # once per command
    # R4: type, size, destination, time and hash -- never the city.
    assert "default city: text, 6 characters, sha256 " in rec.destinations[0] and "2026-10-23 10:00:00" in rec.destinations[0]
    assert all("sydney" not in d.lower() for d in rec.destinations)
    assert all("sydney" not in s.lower() for s, _ in rec.steps)


def test_named_city_shows_the_words_sent_and_never_the_provider() -> None:
    """Named city shows the words sent and never the provider."""
    fake = FakeOpenMeteo(places={"melbourne": _geo(("Melbourne", "Victoria", "Australia", "AU", 4246375, -37.8, 144.9, "Australia/Melbourne"))})
    m, rec, _ = _matcher(fake)
    answer = m("weather in melbourne")
    steps = [s for s, _ in rec.steps]
    assert steps[0] == 'searching online, sending "melbourne"' and "found Melbourne, Victoria" in steps
    assert answer.text.startswith("In Melbourne it's")
    shown = " ".join(s for s, closing in rec.steps if not closing).lower()
    assert "open-meteo" not in shown and "meteo" not in shown
    assert rec.destinations[-1] == "Open-Meteo, geocoding+forecast"  # record and debug log only


def test_hamilton_asks_then_a_number_picks_with_one_forecast_call() -> None:
    """Hamilton asks then a number picks with one forecast call."""
    m, rec, fake = _matcher()
    question = m("weather in hamilton")
    assert question.text == "Hamilton in Ontario, Hamilton in Waikato Region, or Hamilton in Ohio?"
    assert fake.kinds() == ["geo"]  # no forecast call before he answers
    answer = m.pending_matcher("the second one")
    assert fake.kinds() == ["geo", "forecast"]
    assert answer.text.startswith("In Hamilton, Waikato Region it's")
    assert "lat" in fake.calls[-1] and "-37.78" in fake.calls[-1]


def test_hamilton_answered_by_region_name() -> None:
    """Hamilton answered by region name."""
    m, _, fake = _matcher()
    m("weather in hamilton")
    answer = m.pending_matcher("Ontario")
    assert answer.text.startswith("In Hamilton, Ontario it's") and "43.25" in fake.calls[-1]


def test_the_question_is_valid_for_one_command_only() -> None:
    """The question is valid for one command only."""
    m, _, fake = _matcher()
    m("weather in hamilton")
    assert m.pending_matcher("open chrome") is None  # not an answer: handled as a new command
    assert m.pending_matcher("the second one") is None  # and the question is gone
    assert fake.kinds() == ["geo"]


def test_made_up_city_is_not_found_and_no_forecast_call() -> None:
    """Made up city is not found and no forecast call."""
    m, rec, fake = _matcher()
    answer = m("weather in zqxworth")
    assert answer.text == _T["not_found"].format(city="Zqxworth")
    assert fake.kinds() == ["geo"]
    assert rec.destinations == ["Open-Meteo, geocoding"]  # only what really went out


def test_country_after_the_city_picks_without_asking() -> None:
    """Country after the city picks without asking."""
    m, _, fake = _matcher()
    answer = m("weather in hamilton new zealand")
    assert answer.text.startswith("In Hamilton, Waikato Region it's")
    assert fake.kinds() == ["geo", "geo", "geo", "forecast"]  # full name, then two shorter tries


def test_session_cache_skips_the_second_geocode() -> None:
    """Session cache skips the second geocode."""
    m, rec, fake = _matcher()
    m("what's the weather")
    m("will it rain tomorrow")
    assert fake.kinds() == ["geo", "forecast", "forecast"]
    assert "searching online: weather, default city" in [s for s, _ in rec.steps[len(rec.steps) // 2:]]  # still never names it


def test_default_city_file_cache_saves_a_call_after_restart(tmp_path, monkeypatch) -> None:
    """Default city file cache saves a call after restart."""
    # The real cache is in the project folder (approved, R9); here the test's
    # own temp folder stands in for it.
    import jarvis.actions.weather as weather_module

    monkeypatch.setattr(weather_module, "check_path", lambda raw, intent: (Path(raw).parent == tmp_path, "test folder"))
    cache = tmp_path / "weather_cache.json"
    m1, _, fake1 = _matcher(cache_path=cache)
    m1("what's the weather")
    assert cache.exists()
    m2, _, fake2 = _matcher(cache_path=cache)
    m2("what's the weather")
    assert fake2.kinds() == ["forecast"]


def test_cache_outside_an_approved_folder_is_not_written(tmp_path) -> None:
    """Cache outside an approved folder is not written."""
    cache = tmp_path / "weather_cache.json"  # the temp folder is not approved (R9)
    m, _, _ = _matcher(cache_path=cache)
    assert m("what's the weather").text.startswith("In Sydney")  # the answer still works
    assert not cache.exists()


def test_no_city_and_no_default_asks_which_city(monkeypatch) -> None:
    """No city and no default asks which city."""
    monkeypatch.setitem(_W, "default_city", "")
    m, _, fake = _matcher()
    assert m("what's the weather").text == _T["which_city"]
    assert fake.calls == []


# -- failures (R1, R14) ------------------------------------------------------------------


def test_timeout_retries_once_then_says_so() -> None:
    """Timeout retries once then says so."""
    fake = FakeOpenMeteo()
    fake.fail = [socket.timeout("timed out"), socket.timeout("timed out")]
    m, rec, _ = _matcher(fake)
    answer = m("what's the weather")
    assert answer.text == _T["cannot_reach"] and len(fake.calls) == 2
    assert all(t == 10 for t in fake.timeouts)  # the shared timeout on every call
    assert ("weather: timed out", False) in rec.steps


def test_no_internet_says_what_to_do() -> None:
    """No internet says what to do."""
    fake = FakeOpenMeteo()
    fake.fail = [urllib.error.URLError(OSError("getaddrinfo failed")), urllib.error.URLError(OSError("getaddrinfo failed"))]
    m, rec, _ = _matcher(fake)
    assert m("what's the weather").text == _T["cannot_reach"]
    assert "Check your internet connection" in _T["cannot_reach"]


def test_429_cools_down_and_the_next_call_makes_no_request() -> None:
    """429 cools down and the next call makes no request."""
    fake = FakeOpenMeteo()
    fake.fail = [urllib.error.HTTPError("u", 429, "Too Many Requests", {"Retry-After": "60"}, None)]
    status = OnlineStatus()
    m, rec, _ = _matcher(fake, status)
    first = m("what's the weather")
    assert first.text.startswith("The free limit for weather is reached; it is off until ")
    second = m("weather in leeds")
    assert len(fake.calls) == 1  # no retry after a 429, nothing during the cooldown
    assert second.text.startswith("Weather is still off until")
    assert status.snapshot().mode == ConnectionMode.LIMITED


def test_bad_reply_is_a_service_error() -> None:
    """Bad reply is a service error."""
    m, _, _ = _matcher(FakeOpenMeteo(forecast={"current": {}}))
    assert m("what's the weather").text == _T["service_error"]


def test_failure_texts_follow_the_missing_setup_rule() -> None:
    """Failure texts follow the missing setup rule."""
    for key in ("which_city", "not_found", "cannot_reach", "service_error"):
        assert "not available" not in _T[key].lower()
    assert "settings.toml" in _T["which_city"] and "Check" in _T["not_found"]


def test_online_is_held_during_the_call() -> None:
    """Online is held during the call."""
    status = OnlineStatus()
    modes = []
    status.add_listener(lambda snap: modes.append(snap.mode))
    m, _, _ = _matcher(status=status)
    m("what's the weather")
    assert ConnectionMode.ONLINE in modes and status.snapshot().mode == ConnectionMode.LOCAL


def test_stop_between_calls_drops_the_answer_without_handing_on() -> None:
    """Stop between calls drops the answer without handing on."""
    m, rec, fake = _matcher()
    original = m._client.geocode

    def geocode_then_stop(name):
        rec.stop = True
        return original(name)

    m._client.geocode = geocode_then_stop
    assert m("what's the weather").text == ""  # never None: that would reach the online conversation
    assert fake.kinds() == ["geo"]


def test_only_one_weather_client_exists() -> None:
    """Only one weather client exists."""
    root = Path(__file__).resolve().parent.parent / "jarvis"
    users = [p.name for p in root.rglob("*.py") if '["forecast_url"]' in p.read_text(encoding="utf-8")]
    assert users == ["weather.py"]  # F21 must call WeatherClient, not build its own


# -- end to end through the production matcher order -----------------------------------


class FakeSpeech(SpeechOutput):
    """Speech output stand-in: records what she would say; nothing is played."""
    def __init__(self) -> None:
        self.texts: List[str] = []

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self.texts.append(text)


class NoGroq:
    """Groq stand-in that records calls and never answers (proves nothing went online)."""
    calls: List[dict] = []

    def __init__(self, **kw):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **k: NoGroq.calls.append(k)))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Fixture: the components this file's tests drive, with every side effect mocked."""
    NoGroq.calls = []
    monkeypatch.setattr(groq, "Groq", NoGroq)
    monkeypatch.setattr(open_app, "STORE_APPS", SimpleNamespace(prefetch=lambda: None, apps=lambda wait_seconds=0: []))
    monkeypatch.setattr(app_finder, "enumerate_installed_apps", lambda store_apps=None: [
        app_finder.ScoredMatch("Weather", 0.0, "app", r"C:\Start\Weather.lnk")])
    launched = []
    monkeypatch.setattr(open_app.os, "startfile", launched.append)
    monkeypatch.setattr(open_app, "launch_and_verify_with_retry", lambda launch, t, p: (launch(), True))
    c = Controller(start_state=State.AWAKE)
    logbook = Logbook(debug_log=DebugLog(path=tmp_path / "debug.log"))
    c.set_transition_listener(logbook.on_transition)
    c.start()
    interpreter = Interpreter()
    speech = FakeSpeech()
    orch = Orchestrator(c, interpreter, speech, logbook=logbook)
    index = FileIndex(roots=[tmp_path / "empty"])
    index.rebuild_now()
    status = OnlineStatus()
    fake = FakeOpenMeteo()
    weather = WeatherMatcher(_client(fake, status), status)
    answerer = GroqAnswerer(api_key="k", service=OnlineService("Groq", status, 10, 1, 300))
    register_matchers(interpreter, orch, logbook, FileFinder(index), answerer, status, weather=weather)

    def say(text):
        c.submit(Event(EventType.SPEECH_STARTED, {"source": "voice"}))
        deadline = time.monotonic() + 3
        while c.state != State.LISTENING and time.monotonic() < deadline:
            time.sleep(0.01)
        orch.handle_command(text, "voice")

    yield SimpleNamespace(c=c, logbook=logbook, speech=speech, say=say, fake=fake, launched=launched, debug=tmp_path / "debug.log")
    c.shutdown()


def test_whats_the_weather_end_to_end(rig) -> None:
    """Whats the weather end to end."""
    rig.say("What's the weather?")
    assert NoGroq.calls == []
    assert rig.speech.texts[0] == "Searching online."
    assert rig.speech.texts[1].startswith("In Sydney it's 20 degrees and drizzle")
    lines = format_record(rig.logbook.last_completed()).lines
    assert lines[:4] == ["heard: What's the weather", "searching online: weather, default city", "found default city", "forecast received"]
    assert lines[4].startswith("saying: In Sydney") and lines[5] == CREDIT_LINE
    assert CREDIT_LINE in rig.debug.read_text(encoding="utf-8")  # the credit line is not private


def test_hamilton_question_and_pick_end_to_end(rig) -> None:
    """Hamilton question and pick end to end."""
    rig.say("weather in Hamilton")
    assert rig.speech.texts[-1] == "Hamilton in Ontario, Hamilton in Waikato Region, or Hamilton in Ohio?"
    rig.say("the second one")
    assert rig.speech.texts[-1].startswith("In Hamilton, Waikato Region it's")
    assert rig.fake.kinds() == ["geo", "forecast"] and NoGroq.calls == []


def test_open_weather_still_opens_the_app(rig) -> None:
    """Open weather still opens the app."""
    rig.say("open weather")
    assert rig.launched == [r"C:\Start\Weather.lnk"] and rig.fake.calls == []


def test_weather_warning_is_left_for_live_search(rig) -> None:
    """Weather warning is left for live search."""
    assert parse_request("is there a weather warning for sydney today") is None


def test_cached_default_city_still_goes_online_first_and_hashes_the_setting() -> None:
    """Cached default city still goes online first and hashes the setting."""
    m, rec, fake = _matcher()
    m("what's the weather")
    first_hash = rec.destinations[-1].split("sha256 ")[1].split(",")[0]
    rec.steps.clear()
    m("temperature tomorrow")
    steps = [s for s, _ in rec.steps]
    assert steps[:2] == ["searching online: weather, default city", "found default city"]
    assert "6 characters" in rec.destinations[-1] and first_hash in rec.destinations[-1]
