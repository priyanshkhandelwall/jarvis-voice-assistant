"""F16a Weather (Stage 2 of F16, built): "what's the
weather", "weather in Melbourne", "will it rain today", "temperature
tomorrow", answered from Open-Meteo (free, no key).

- The one weather client in JARVIS (WeatherClient); F21 will call it too.
- Every number spoken comes from Open-Meteo's own reply. A reply missing a
  value is a service error, never a guess.
- L0: an answer to the developer's own words, or to the default city he
  typed into settings (R4's amendment). No fixed action, no permit.
- R3: "Searching online" once per command, ONLINE held until the answer is
  spoken, Live Activity shows the words sent. "Open-Meteo" appears on
  screen only in the credit line its CC BY 4.0 licence requires; the
  service name otherwise goes to the decision record and debug log.
- A default-city request is logged per R4: type, size, destination, time
  and a hash, never the city itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from jarvis.actions.command_text import PickSlot, normalize, pick_number
from jarvis.interpreter import Answer
from jarvis.online import OnlineError, OnlineErrorKind, OnlineService, OnlineStatus
from jarvis.online.http import build_url, classify_http_error, get_json
from jarvis.security.paths import PathIntent, check_path
from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)

SERVICE_NAME = "Open-Meteo"
_CFG = SETTINGS["weather"]
_ONLINE = SETTINGS["online"]
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CREDIT_LINE = str(_CFG["credit_line"])
_CHECKING_ONLINE_TEXT = str(SETTINGS["speech"]["checking_online_text"])

_CURRENT_VARS = ["temperature_2m", "apparent_temperature", "weather_code", "wind_speed_10m"]
_DAILY_VARS = ["weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_probability_max"]

# WMO weather interpretation codes (Open-Meteo's docs), as words that fit
# "it's 16 degrees and ___".
WMO_WORDS: Dict[int, str] = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy with frost",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers",
    85: "light snow showers", 86: "snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with heavy hail",
}
UNKNOWN_CONDITIONS = "unusual conditions"


def conditions(code: Optional[int]) -> str:
    """Words for a WMO code; an unknown code is "unusual conditions"."""
    return WMO_WORDS.get(code, UNKNOWN_CONDITIONS) if isinstance(code, int) else UNKNOWN_CONDITIONS


def default_cache_path() -> Path:
    """Where the default city's coordinates are cached in jarvis_data."""
    return _PROJECT_ROOT / str(_CFG["cache_path"])


@dataclass(frozen=True)
class Place:
    """One geocoded place: name, coordinates, time zone, country, region and population."""
    name: str
    latitude: float
    longitude: float
    timezone: str
    country: str = ""
    country_code: str = ""
    admin1: str = ""
    population: int = 0

    @property
    def region(self) -> str:
        """"England", "Alabama": what tells two places with one name apart."""
        return self.admin1 or self.country

    @property
    def label(self) -> str:
        """'Name, Region', or just the name."""
        return f"{self.name}, {self.region}" if self.region else self.name


@dataclass(frozen=True)
class Day:
    """One forecast day: weather code, high, low and chance of rain."""
    code: Optional[int]
    high: float
    low: float
    rain_chance: float


@dataclass(frozen=True)
class Forecast:
    """The current conditions and the forecast days for one place."""
    temperature: float
    feels_like: float
    code: Optional[int]
    days: Tuple[Day, ...]


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OnlineError(OnlineErrorKind.SERVICE_ERROR, f"expected a number, got {value!r}")
    return float(value)


def _code(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_places(data) -> List[Place]:
    """Geocoding results. A missing `results` key means nothing matched
    (live check), the same as an empty list."""
    if not isinstance(data, dict):
        raise OnlineError(OnlineErrorKind.SERVICE_ERROR, "geocoding reply is not an object")
    results = data.get("results") or []
    if not isinstance(results, list):
        raise OnlineError(OnlineErrorKind.SERVICE_ERROR, "geocoding results is not a list")
    places = []
    for r in results:
        if not isinstance(r, dict) or not r.get("name") or not r.get("timezone"):
            continue
        try:
            places.append(
                Place(
                    name=str(r["name"]),
                    latitude=_number(r.get("latitude")),
                    longitude=_number(r.get("longitude")),
                    timezone=str(r["timezone"]),
                    country=str(r.get("country") or ""),
                    country_code=str(r.get("country_code") or ""),
                    admin1=str(r.get("admin1") or ""),
                    population=int(r.get("population") or 0),
                )
            )
        except OnlineError:
            continue  # one malformed entry doesn't spoil the others
    return places


def parse_forecast(data, days_wanted: int) -> Forecast:
    """Every value is required: a gap is a service error, never a guess."""
    if not isinstance(data, dict) or not isinstance(data.get("current"), dict) or not isinstance(data.get("daily"), dict):
        raise OnlineError(OnlineErrorKind.SERVICE_ERROR, "forecast reply has no current/daily block")
    current, daily = data["current"], data["daily"]
    columns = {}
    for var in _DAILY_VARS:
        column = daily.get(var)
        if not isinstance(column, list) or len(column) < days_wanted:
            raise OnlineError(OnlineErrorKind.SERVICE_ERROR, f"daily {var} missing")
        columns[var] = column
    days = tuple(
        Day(
            code=_code(columns["weather_code"][i]),
            high=_number(columns["temperature_2m_max"][i]),
            low=_number(columns["temperature_2m_min"][i]),
            rain_chance=_number(columns["precipitation_probability_max"][i]),
        )
        for i in range(days_wanted)
    )
    return Forecast(
        temperature=_number(current.get("temperature_2m")),
        feels_like=_number(current.get("apparent_temperature")),
        code=_code(current.get("weather_code")),
        days=days,
    )


Fetch = Callable[[str, float], object]


class WeatherClient:
    """Open-Meteo geocoding and forecast, through the shared OnlineService
    (R1: timeout, one retry, 429 cooldown). Geocoding is cached for the
    session by lower-cased name; the default city's coordinates are also
    kept in jarvis_data/weather_cache.json, saving one call per startup."""

    def __init__(self, service: OnlineService, fetch: Fetch = get_json, cache_path: Optional[Path] = None) -> None:
        self.service = service
        self._fetch = fetch
        self._cache_path = cache_path
        self._session: Dict[str, List[Place]] = {}

    def geocode_url(self, name: str) -> str:
        """The Open-Meteo geocoding URL for `name`."""
        return build_url(
            str(_CFG["geocode_url"]),
            {"name": name, "count": int(_CFG["geocode_result_count"]), "language": str(_CFG["language"]), "format": "json"},
        )

    def forecast_url(self, place: Place) -> str:
        """The Open-Meteo forecast URL for `place`."""
        return build_url(
            str(_CFG["forecast_url"]),
            {
                "latitude": place.latitude,
                "longitude": place.longitude,
                "current": _CURRENT_VARS,
                "daily": _DAILY_VARS,
                "timezone": place.timezone,  # required whenever daily= is asked for
                "forecast_days": int(_CFG["forecast_days"]),
                "temperature_unit": str(_CFG["temperature_unit"]),
                "wind_speed_unit": str(_CFG["wind_speed_unit"]),
            },
        )

    def cached(self, name: str) -> Optional[List[Place]]:
        """The places already known for `name` (this session, or the saved default city), or None."""
        key = " ".join(name.lower().split())
        if key in self._session:
            return self._session[key]
        saved = self._read_saved(key)
        if saved is not None:
            self._session[key] = [saved]
            return [saved]
        return None

    def geocode(self, name: str) -> List[Place]:
        """The places called `name` (cached, or looked up online). Raises OnlineError on failure."""
        key = " ".join(name.lower().split())
        hit = self.cached(key)
        if hit is not None:
            return hit
        places = parse_places(self.service.call(lambda timeout: self._fetch(self.geocode_url(name), timeout), classify_http_error))
        self._session[key] = places
        return places

    def forecast(self, place: Place) -> Forecast:
        """The forecast for `place`, online. Raises OnlineError on failure."""
        data = self.service.call(lambda timeout: self._fetch(self.forecast_url(place), timeout), classify_http_error)
        return parse_forecast(data, int(_CFG["forecast_days"]))

    def remember_default(self, name: str, place: Place) -> None:
        """Save the default city's coordinates (data folder, R18)."""
        if self._cache_path is None:
            return
        allowed, reason = check_path(str(self._cache_path), PathIntent.WRITE)
        if not allowed:
            logger.warning("Weather cache not saved: %s", reason)
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps({"city": " ".join(name.lower().split()), "place": asdict(place)}), encoding="utf-8")
        except OSError:
            logger.warning("Could not write the weather cache at %s", self._cache_path, exc_info=True)

    def _read_saved(self, key: str) -> Optional[Place]:
        if self._cache_path is None or not self._cache_path.exists():
            return None
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if data.get("city") != key:
                return None  # the default city was changed in settings
            return Place(**data["place"])
        except (OSError, ValueError, TypeError, KeyError):
            return None


def default_weather_service(status: Optional[OnlineStatus] = None) -> OnlineService:
    """The shared online helper (R1) configured for Open-Meteo."""
    return OnlineService(
        SERVICE_NAME,
        status or OnlineStatus(),
        timeout_seconds=float(_ONLINE["request_timeout_seconds"]),
        max_retries=int(_ONLINE["max_retries"]),
        cooldown_seconds=float(_ONLINE["rate_limit_cooldown_seconds"]),
    )


# -- understanding the request ----------------------------------------------------

_TIME_WORDS = r"(?:today|tomorrow|tonight|now|right now|this morning|this afternoon|this evening|later|later today)"
_RAIN = re.compile(r"\b(?:rain|raining|rainy|umbrella)\b")
# Only
# inside a rain question, so "Victoria's reign in England" is not weather.
_RAIN_MISHEARD = re.compile(r"\b(?:reign|reigning|rein|reining)\b")
_RAIN_QUESTION = re.compile(rf"\b(?:will it|is it|going to|gonna|chance of|should i|do i need|does it)\b|\brain(?:ing)? {_TIME_WORDS}\b")
_TEMPERATURE = re.compile(r"\b(?:temperature|temp|how (?:hot|cold|warm))\b")
_WEATHER = re.compile(r"\b(?:weather|forecast)\b")
_WHETHER = re.compile(r"\bwhether\b")  # a Whisper mishearing of "weather", counted only with a city or a day
# "Is it going to be hot tomorrow" went online and Groq made up
# "27 °C". A weather word in a question about a day (or "outside", or a city)
# is a weather request.
_FEEL = re.compile(r"\b(?:hot|cold|warm|chilly|freezing|humid|sunny|cloudy|windy|snowy|snowing|stormy)\b")
_FEEL_TEMPERATURE = re.compile(r"\b(?:hot|cold|warm|chilly|freezing)\b")
_FEEL_QUESTION = re.compile(r"\b(?:will it|is it|going to be|gonna be|will be|be)\b")
_TRAILING_TIME = re.compile(rf"(?:\s+{_TIME_WORDS})+$")
_CITY_AFTER = re.compile(r"\b(?:in|for|at)\s+(?P<city>[a-z][a-z' .-]*)$")
_CITY_BEFORE = re.compile(r"^(?P<city>[a-z][a-z' .-]*?)(?:'s)?\s+(?:weather|forecast|temperature)$")
_NOT_A_CITY_START = {"what", "whats", "what's", "how", "hows", "how's", "is", "will", "tell", "give", "check", "show",
                     "get", "the", "today", "today's", "todays", "tomorrow", "tomorrow's", "tomorrows", "current", "local", "any"}


_NOT_PLACE = {str(w) for w in _CFG.get("not_place_words", [])}


def place_candidates(text: str) -> Tuple[str, ...]:
    """"how hot will Melbourne be tomorrow" -> ("melbourne",).
    Each run of words that are not question, weather or time words (at most
    three words), longest first, to be tried as a place."""
    runs, run = [], []
    for w in text.split() + [""]:
        if w and w not in _NOT_PLACE and not w.isdigit():
            run.append(w)
        elif run:
            runs.append(" ".join(run[:3]))
            run = []
    return tuple(sorted(runs, key=len, reverse=True))


_LENGTH_PHRASES = re.compile(r"\b(?:" + "|".join(re.escape(str(p)) for p in sorted(
    list(SETTINGS["length"]["one_sentence_phrases"]) + list(SETTINGS["length"]["detail_phrases"]), key=len, reverse=True)) + r")\b")


@dataclass(frozen=True)
class WeatherRequest:
    """What a weather question asks: its kind, which day, the city (None = default) and words that may name
    a place."""
    kind: str  # "weather" | "rain" | "temperature"
    day: int  # 0 today, 1 tomorrow
    city: Optional[str]  # None: use the default city
    maybe_city: Tuple[str, ...] = ()  # words that may name a place, tried before the default
    needs_place: bool = False  # weather only if one of maybe_city is a place ("will Brisbane be cold")


def parse_request(text: str) -> Optional[WeatherRequest]:
    """A weather request in the cleaned command, or None. Questions *about*
    weather ("explain how forecasts work") and weather warnings (a live
    web question) are left for the other matchers."""
    t = normalize(text)
    if not t or t.split()[0] in ("open", "find", "search", "close", "play"):
        return None
    if any(re.search(rf"\b{re.escape(str(p))}\b", t) for p in _CFG["skip_phrases"]):
        return None
    day = 1 if re.search(r"\btomorrow(?:'s)?\b", t) else 0
    # A length he asked for ("in detail", "briefly") is never part of a city.
    stripped = " ".join(_TRAILING_TIME.sub("", _LENGTH_PHRASES.sub(" ", t)).split())
    city = None
    m = _CITY_AFTER.search(stripped)
    if m:
        city = m.group("city").strip(" .'-")
    else:
        m = _CITY_BEFORE.match(stripped)
        if m and m.group("city").split()[0] not in _NOT_A_CITY_START:
            city = m.group("city").strip(" .'-")
    if city and (city in ("the", "it", "there", "here") or re.fullmatch(_TIME_WORDS, city)):
        city = None
    maybe = () if city else place_candidates(stripped)
    if _RAIN.search(t) or (_RAIN_MISHEARD.search(t) and re.search(r"\b(?:will it|is it|gonna|going to)\b", t)):
        kind = "rain"
    elif _TEMPERATURE.search(t):
        kind = "temperature"
    elif _WEATHER.search(t):
        kind = "weather"
    elif _WHETHER.search(t) and (city or re.search(rf"\b{_TIME_WORDS}\b", t)):
        kind = "weather"
    elif _FEEL.search(t) and _FEEL_QUESTION.search(t) and (city or maybe or re.search(rf"\b(?:{_TIME_WORDS}|outside)\b", t)):
        kind = "temperature" if _FEEL_TEMPERATURE.search(t) else "weather"
    else:
        return None
    if kind == "rain" and not (_RAIN_QUESTION.search(t) or _WEATHER.search(t) or city):
        return None  # "rain" in a sentence that isn't asking about the weather
    # "will the soup be hot": weather only if "soup" turns out to be a place.
    needs_place = bool(maybe) and not (_RAIN.search(t) or _TEMPERATURE.search(t) or _WEATHER.search(t)
                                       or re.search(rf"\b(?:{_TIME_WORDS}|outside)\b", t))
    return WeatherRequest(kind=kind, day=day, city=city or None, maybe_city=maybe, needs_place=needs_place)


def _region_matches(place: Place, region_words: Sequence[str]) -> bool:
    region = " ".join(region_words)
    aliases = {str(k).lower(): str(v).upper() for k, v in _CFG.get("region_aliases", {}).items()}
    if aliases.get(region) and aliases[region] == place.country_code.upper():
        return True
    return region in (place.admin1.lower(), place.country.lower(), place.country_code.lower())


def choose_place(places: Sequence[Place], ratio: float) -> Optional[Place]:
    """The top result, unless another country's result is close enough in
    size that she must ask (spec F16a, 10x population rule). None = ask."""
    if not places:
        return None
    top = places[0]
    others = [p for p in places[1:] if p.country_code != top.country_code]
    if not others:
        return top
    biggest_other = max(p.population for p in others)
    if top.population > 0 and top.population >= ratio * biggest_other:
        return top
    return None


def choices(places: Sequence[Place], limit: int) -> List[Place]:
    """One place per country, in the order the service ranked them."""
    picked: List[Place] = []
    seen = set()
    for p in places:
        if p.country_code in seen:
            continue
        seen.add(p.country_code)
        picked.append(p)
        if len(picked) == limit:
            break
    return picked


def _join_or(items: List[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + ", or " + items[-1]


def _r(value: float) -> int:
    return int(round(value))


def answer_text(request: WeatherRequest, spoken_place: str, forecast: Forecast) -> str:
    """The spoken answer, built only from the forecast's own numbers."""
    day = forecast.days[min(request.day, len(forecast.days) - 1)]
    values = dict(
        place=spoken_place,
        temp=_r(forecast.temperature),
        feels=_r(forecast.feels_like),
        now=conditions(forecast.code),
        conditions=conditions(day.code),
        high=_r(day.high),
        low=_r(day.low),
        rain=_r(day.rain_chance),
    )
    when = "tomorrow" if request.day == 1 else "today"
    return str(_CFG["texts"][f"{request.kind}_{when}"]).format(**values)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class WeatherMatcher:
    """Registered after the clock and before F4/F3 and the Groq
    conversation. A question about several places with one name ("Hamilton
    in Ontario, Hamilton in Waikato Region, or Hamilton in Ohio?") is
    answered by the next command only -- a number ("the second one") or
    the region ("Ontario") -- like F4's pick list."""

    def __init__(
        self,
        client: WeatherClient,
        online_status: Optional[OnlineStatus] = None,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._client = client
        self._online_status = online_status
        self._now = now
        self._orchestrator = None
        self._slot: Optional[PickSlot] = None
        self._pending: Optional[Tuple[WeatherRequest, List[Place]]] = None

    # -- matchers ---------------------------------------------------------------

    def pending_matcher(self, text: str) -> Optional[Answer]:
        """The answer to "X in A, or X in B?" -- valid for one command."""
        if self._pending is None:
            return None
        request, options = self._pending
        self._pending = None
        owns = self._slot is None or self._slot.owned_by(self)
        if self._slot is not None:
            self._slot.release(self)
        if not owns:
            return None
        words = normalize(text).split()
        selector = " ".join(w for w in words if w not in ("open", "the", "one", "in", "weather", "for", "please")) or " ".join(words)
        number = pick_number(" ".join(words)) or pick_number(selector)
        chosen = None
        if number is not None and 1 <= number <= len(options):
            chosen = options[number - 1]
        else:
            for place in options:
                region = [w for w in selector.split() if w != place.name.lower()]
                if region and _region_matches(place, region):
                    chosen = place
                    break
        if chosen is None:
            return None  # not an answer to the question: handled as a new command
        return self._answer_for(request, chosen, chosen.label)

    def matcher(self, text: str) -> Optional[Answer]:
        """F16a: answer a weather, rain or temperature question for the named or default city. None if it
        isn't one."""
        request = parse_request(text)
        if request is None:
            return None
        default = str(_CFG["default_city"]).strip()
        if request.city is None and request.maybe_city:
            named = self._named_place(request.maybe_city)
            if named is None and request.needs_place:
                return None  # "will the soup be hot": not about the weather
            if named is not None:
                request = WeatherRequest(request.kind, request.day, named)
        city = request.city or default
        if not city:
            return Answer(str(_CFG["texts"]["which_city"]))
        using_default = request.city is None

        held: List[bool] = []
        try:
            cooling = self._client.service.cooling_until_wall()
            if cooling is not None:
                return self._failure(OnlineError(OnlineErrorKind.COOLING, until_wall=cooling))
            cached_before = self._client.cached(city)
            if cached_before is None:
                self._going_online(city, using_default, held)
            try:
                place, spoken, question = self._resolve(request, city, using_default)
            except OnlineError as error:
                return self._failure(error)
            if self._stopped():
                return Answer("")  # Stop: dropped, never handed on to the online conversation
            if question is not None:
                self._report("asking which place")
                return Answer(question)
            if place is None:
                self._report("weather: no such place found")
                return Answer(str(_CFG["texts"]["not_found"]).format(city=city.title()))
            if not held:
                # Place from the cache: only the forecast call goes online. The
                # default city is logged as the setting itself, never by name (R4).
                self._going_online(city if using_default else place.label, using_default, held, forecast_only=True)
            if using_default:
                self._client.remember_default(city, place)
                self._report("found default city")
            else:
                self._report(f"found {place.label}")
                if self._orchestrator is not None and cached_before is None:
                    self._orchestrator.record_online_destination(f"{SERVICE_NAME}, geocoding+forecast")
            return self._answer_for(request, place, spoken, held=held)
        finally:
            self._release_after_reply(held)

    # -- internals --------------------------------------------------------------

    def _named_place(self, candidates: Sequence[str]) -> Optional[str]:
        """The first of `candidates` the geocoder knows, or None (then the
        default city is used). A failing geocoder: None."""
        for name in candidates:
            if self._client.cached(name) is None:
                self._report(f"checking if '{name}' is a place (online)")
            try:
                places = self._client.geocode(name)
            except OnlineError:
                return None
            if places:
                return name
        return None

    def _resolve(self, request: WeatherRequest, city: str, using_default: bool) -> Tuple[Optional[Place], str, Optional[str]]:
        """(place, how to say it, None), or (None, "", the "which one?"
        question), or (None, "", None) when nothing matched."""
        places = self._client.geocode(city)
        words = city.split()
        if not places and len(words) > 1:
            # "Leeds England", "Hamilton New Zealand": the last word(s) name the region.
            for cut in (1, 2):
                if len(words) <= cut:
                    break
                head, region = " ".join(words[:-cut]), words[-cut:]
                matching = [p for p in self._client.geocode(head) if _region_matches(p, region)]
                if matching:
                    return matching[0], matching[0].label, None
        if not places:
            return None, "", None
        chosen = choose_place(places, float(_CFG["dominant_population_ratio"]))
        if chosen is not None:
            return chosen, chosen.name, None
        if using_default:
            # His own setting: no question, the service's first match.
            return places[0], places[0].name, None
        options = choices(places, int(_CFG["max_choices"]))
        self._pending = (request, options)
        return None, "", _join_or([f"{p.name} in {p.region}" for p in options]) + "?"

    def _answer_for(
        self,
        request: WeatherRequest,
        place: Place,
        spoken_place: str,
        held: Optional[List[bool]] = None,
    ) -> Answer:
        own_hold: List[bool] = []
        if held is None:
            held = own_hold
        try:
            if not held:
                # A pick from the "which one?" list: only the forecast call is left.
                self._going_online(place.label, False, held, forecast_only=True)
            try:
                forecast = self._client.forecast(place)
            except OnlineError as error:
                return self._failure(error)
            if self._stopped():
                return Answer("")
            self._report("forecast received")
            self._report(CREDIT_LINE, closing=True)
            return Answer(answer_text(request, spoken_place, forecast))
        finally:
            self._release_after_reply(own_hold)

    def _going_online(self, words: str, using_default: bool, held: List[bool], forecast_only: bool = False) -> None:
        if self._online_status is not None and not held:
            self._online_status.hold(SERVICE_NAME)
            held.append(True)
        calls = "forecast" if forecast_only else "geocoding+forecast"
        if using_default:
            self._report("searching online: weather, default city")
            # R4: a settings value -- type, size, destination, time, hash; never the city.
            detail = (f"{SERVICE_NAME}, {calls}; default city: text, {len(words)} characters, "
                      f"sha256 {_hash(words)}, {self._now().strftime('%Y-%m-%d %H:%M:%S')}")
        elif forecast_only:
            self._report(f"searching online: forecast for {words}")
            detail = f"{SERVICE_NAME}, {calls}"
        else:
            self._report(f'searching online, sending "{words}"')
            detail = f"{SERVICE_NAME}, geocoding"  # "+forecast" is added once that call is made
        if self._orchestrator is not None:
            self._orchestrator.record_online_destination(detail)
            self._orchestrator.announce(_CHECKING_ONLINE_TEXT)

    def _release_after_reply(self, held: List[bool]) -> None:
        if not held or self._online_status is None:
            return
        held.clear()
        release = lambda: self._online_status.release(SERVICE_NAME)  # noqa: E731
        if self._orchestrator is not None:
            self._orchestrator.when_command_finishes(release)
        else:
            release()

    def _failure(self, error: OnlineError) -> Answer:
        texts = _CFG["texts"]
        kind = error.kind
        until = datetime.fromtimestamp(error.until_wall).strftime("%H:%M") if error.until_wall else "later"
        if kind in (OnlineErrorKind.CANNOT_REACH, OnlineErrorKind.TIMEOUT):
            step, text = ("weather: could not connect" if kind == OnlineErrorKind.CANNOT_REACH else "weather: timed out"), texts["cannot_reach"]
        elif kind == OnlineErrorKind.RATE_LIMITED:
            step, text = "weather: free limit reached", str(texts["rate_limited"]).format(until=until)
        elif kind == OnlineErrorKind.COOLING:
            step, text = "weather: still in free-limit cooldown, not sent", str(texts["cooling"]).format(until=until)
        else:
            step, text = "weather: service error", texts["service_error"]
        logger.info("Weather unavailable (%s: %s)", kind.name, error.detail)
        self._report(step)
        return Answer(str(text))

    def _stopped(self) -> bool:
        return self._orchestrator is not None and self._orchestrator.controller.is_stop_requested()

    def _report(self, step: str, closing: bool = False) -> None:
        if self._orchestrator is not None:
            self._orchestrator.report_step(step, closing=closing)

    def __call__(self, text: str) -> Optional[Answer]:
        result = self.matcher(text)
        if self._pending is not None and self._slot is not None:
            self._slot.claim(self)  # "the second one" now picks from this list
        return result

    def register_pick(self, interpreter, orchestrator) -> None:
        """The app puts the "which place?" answer with the other list picks,
        first of all matchers."""
        self._orchestrator = orchestrator
        self._slot = getattr(orchestrator, "picks", None)
        interpreter.register("weather_pick", self.pending_matcher)
        self._pick_registered = True

    def register(self, interpreter, orchestrator) -> None:
        """Register the weather matcher (and its 'which place?' pick) with the interpreter."""
        self._orchestrator = orchestrator
        self._slot = getattr(orchestrator, "picks", None)
        if not getattr(self, "_pick_registered", False):
            interpreter.register("weather_pick", self.pending_matcher)
        interpreter.register("weather", self)
