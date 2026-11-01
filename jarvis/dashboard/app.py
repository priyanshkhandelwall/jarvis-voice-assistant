"""F6: the native dashboard window (Flet). Locked visual design,
developer-approved -- colors/fonts/layout in jarvis/dashboard/
theme.py, module status in jarvis/dashboard/module_status.py, live-data
helpers in gauges.py/activity_feed.py, kept out of this file so the
design and the data plumbing can be read/changed separately.

Every panel shows real data, never decoration (F6's own rule): the
sensor rows, gauges, Live Activity, the Internet panel and the Webcam Feed
all read live state.

Threading (R8/4.2): the dashboard's own background thread (refresh_loop
below) is the only thing that touches Flet controls or calls page.update().
The controller's transition listener never does UI work -- it only pushes
onto DashboardBridge's bounded, non-blocking queue (jarvis/dashboard/
bridge.py) and feeds F5's Logbook, both of which are safe to call from the
controller's own worker thread; refresh_loop drains/reads them on its own
schedule.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, List, NamedTuple, Tuple

import flet as ft

from jarvis.actions import open_app
from jarvis.actions.conversation import Conversation
from jarvis.router.commands import RouterCommands
from jarvis.actions.explain import ExplainMatcher
from jarvis.actions.clock import ClockMatcher
from jarvis.actions.weather import WeatherClient, WeatherMatcher, default_cache_path, default_weather_service
from jarvis.actions.wake_greeting import WakeGreeter, default_state_path
from jarvis.actions.sleep_wake import AlreadyAwakeMatcher, SleepMatcher
from jarvis.actions.file_index import FileIndex
from jarvis.actions.find_file import FileFinder
from jarvis.actions.greeting import GreetingMatcher, SmallTalkMatcher
from jarvis.actions.intro import intro_matcher
from jarvis.controller import Controller, State
from jarvis.controller.permits import PendingConfirmation
from jarvis.dashboard import theme
from jarvis.dashboard.activity_feed import NewRecordTracker, format_record, format_session_log
from jarvis.dashboard.bridge import DashboardBridge
from jarvis.dashboard.gauges import (
    LowMemoryWarning,
    NetworkRateSampler,
    cpu_status_word,
    disk_status_word,
    gauges_setup_message,
    is_memory_low,
    low_memory_warning_text,
    ram_status_word,
    read_gauges,
)
from jarvis.dashboard.internet_status import InternetPanelBinding, internet_display
from jarvis.dashboard.module_status import BUILT, IN_PROGRESS, MODULE_STATUS
from jarvis.dashboard.native_titlebar import apply_caption_color_async
from jarvis.platform import setup_message
from jarvis.dashboard.state_display import StateDisplay
from jarvis.dashboard.visibility import WindowVisibility
from jarvis.dashboard.background_start import STARTING, BackgroundVoice, start_in_background
from jarvis import startup_timing
from jarvis.interpreter import Interpreter
from jarvis.logbook import Logbook
from jarvis.logbook.debug_log import DebugLog
from jarvis.browser.commands import BrowserCommands
from jarvis.memory.commands import MemoryCommands
from jarvis.news.feeds import NewsClient, default_news_service
from jarvis.news.topics import TopicCommands, TopicMonitor, TopicStore
from jarvis.news.briefing import Briefing
from jarvis.system.commands import SystemCommands
from jarvis.youtube.commands import YouTubeCommands, default_youtube_service
from jarvis.files.commands import SEND_ACTION as FILE_SEND_ACTION, FileCommands
from jarvis.screen.commands import ScreenCommands
from jarvis.sensors import CAMERA, SCREEN, SensorFlags
from jarvis.vision.camera import Camera
from jarvis.vision.commands import VisionCommands
from jarvis.memory.store import MemoryStore
from jarvis.online import OnlineStatus
from jarvis.orchestrator import Orchestrator
from jarvis.settings import SETTINGS, save_string_setting
from jarvis.online.budget import TokenLedger, default_usage_path
from jarvis.speech.qa import GroqAnswerer, default_groq_service
from jarvis.speech.mood import MoodWatcher
from jarvis.speech.rudeness import RudenessFilter
from jarvis.speech.neural_tts import make_speech_output
from jarvis.speech.output import SpeechOutput
from jarvis.voice.audio_input import list_input_device_names
from jarvis.voice.system import VoiceSystem, start_voice

logger = logging.getLogger(__name__)

# Errors from the refresh loop only (never per-tick data): plain file
# append, not the `logging` module, since this needs to survive even if
# logging isn't configured anywhere in this process, and needs a path the
# user can actually open with no console available to print to. Lives in
# the project's own data folder (settings: [dashboard] diag_log_path), never
# the system temp folder: no JARVIS files outside the project.
_DIAG_LOG_PATH = Path(__file__).resolve().parents[2] / str(SETTINGS["dashboard"]["diag_log_path"])


def _diag_log(message: str) -> None:
    try:
        _DIAG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_DIAG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(message + "\n")
    except OSError:
        pass


_STATE_COLORS = {
    State.SLEEPING: theme.TEXT_DIM,
    State.AWAKE: theme.TEXT_SECONDARY,
    State.LISTENING: theme.CYAN_ACCENT,
    State.UNDERSTANDING: theme.CYAN_ACCENT,
    State.CONFIRMING: theme.STATUS_ELEVATED,
    State.ACTING: theme.CYAN_ACCENT,
    State.VERIFYING: theme.CYAN_ACCENT,
    State.SPEAKING: theme.CYAN_ACCENT,
}

_MODULE_STATUS_COLORS = {
    BUILT: theme.STATUS_OK,
    IN_PROGRESS: theme.STATUS_ELEVATED,
    "pending": theme.TEXT_DIM,
}
_MODULE_STATUS_LABELS = {BUILT: "BUILT", IN_PROGRESS: "IN PROGRESS", "pending": "PENDING"}


# Column proportions are flex weights (ft.Row's `expand` as an int is a
# flex factor, same idea as CSS flex-grow), not fixed pixel widths -- a
# fixed width on column1's Column previously silently overrode every
# resize of that number (see git history / prior report) because the
# three _panel() Containers inside it had their own fixed width= that won
# the layout, not the Column's. Flex weights on the three body columns
# plus horizontal_alignment=STRETCH on column1/column3 (so their panels,
# which set no width of their own, fill whatever pixel width the flex
# split actually gives that column) removes fixed pixel widths from this
# layout entirely, so there is no number left for a future edit to change
# without effect.
_COL1_FLEX, _COL2_FLEX, _COL3_FLEX = 3, 10, 4

_ACTIVITY_FEED_MAX_ROWS = int(SETTINGS["dashboard"]["activity_feed_max_rows"])
_STATE_DWELL_MIN = float(SETTINGS["dashboard"]["state_dwell_min_seconds"])
_STATE_DWELL_MAX = float(SETTINGS["dashboard"]["state_dwell_max_seconds"])
_STATE_CHAIN_BUDGET = float(SETTINGS["dashboard"]["state_chain_budget_seconds"])
_LIVE_ACTIVITY_TOP_SPACER = float(SETTINGS["dashboard"]["live_activity_top_spacer"])
_WINDOW_MIN_WIDTH = float(SETTINGS["dashboard"]["window_min_width"])
_WINDOW_MIN_HEIGHT = float(SETTINGS["dashboard"]["window_min_height"])


class AppComponents(NamedTuple):
    """Everything make_app_components() wires together, so the window (and the tests) can reach each part."""
    controller: Controller
    logbook: Logbook
    interpreter: Interpreter
    orchestrator: Orchestrator
    bridge: DashboardBridge
    file_index: FileIndex
    online_status: OnlineStatus
    speech: SpeechOutput
    memory: MemoryCommands | None = None
    browser: BrowserCommands | None = None
    screen: ScreenCommands | None = None
    sensors: SensorFlags | None = None
    vision: VisionCommands | None = None
    topics: TopicCommands | None = None
    briefing: Briefing | None = None
    system: SystemCommands | None = None
    youtube: YouTubeCommands | None = None
    files: FileCommands | None = None


def register_matchers(interpreter, orchestrator, logbook, file_finder, answerer, online_status, weather: WeatherMatcher | None = None, prefetch_apps: bool = True, memory: MemoryCommands | None = None,
                      browser: BrowserCommands | None = None, screen: ScreenCommands | None = None,
                      vision: VisionCommands | None = None, topics: TopicCommands | None = None,
                      briefing: Briefing | None = None, system: SystemCommands | None = None,
                      youtube: YouTubeCommands | None = None, files: FileCommands | None = None,
                      router: RouterCommands | None = None) -> None:
    """Every matcher, in first-match-wins order (see make_app_components).
    Separate so the tests run commands through exactly this order. Without
    `weather`, the real Open-Meteo client is used; without `memory`, the
    real jarvis_data/memory.json (read only when a memory command is used).
    Without `browser`, F16b's commands and fallback are not registered (the
    app passes the real one: a Google search opened in his Chrome)."""
    if memory is None:
        memory = MemoryCommands(MemoryStore())
    if router is None:
        router = RouterCommands()  # offline only: the app passes one with the online classifier
    # Picks from a pending list come first of all: "the second
    # file please" or the file's name must never be taken by another command
    # or sent online. Only the list shown last answers (orchestrator.picks).
    router.register_pick(interpreter, orchestrator)  # The answer to "Did you mean...?"
    open_app.CLARIFIER = router.clarifier  # "Did you mean Claude?" for a name that matches nothing
    app_picker = open_app.AppPicker(getattr(orchestrator, "picks", None))
    interpreter.register("open_app_pick", app_picker.selection_matcher)
    file_finder.register_pick(interpreter, orchestrator)
    interpreter.register("memory_pick", memory.pick_matcher)  # F10: "forget 2" after a list
    if files is not None:
        files.register_pick(interpreter, orchestrator)  # F19: "the second one" / "the middle" after its question
    # Weather's "which place?" list answers its own picks, then a
    # pick with no list at all is answered here -- never an app called "one".
    if weather is None:
        weather = WeatherMatcher(WeatherClient(default_weather_service(online_status), cache_path=default_cache_path()), online_status)
    weather.register_pick(interpreter, orchestrator)
    interpreter.register("no_list_pick", open_app.no_list_matcher(getattr(orchestrator, "picks", None)))
    interpreter.register("intro", intro_matcher)  # F1
    interpreter.register("already_awake", AlreadyAwakeMatcher())  # "wake up" while awake: local
    interpreter.register("small_talk", SmallTalkMatcher())  # "thank you", "ok" -- never online
    interpreter.register("greeting", GreetingMatcher())  # F2, answered locally
    interpreter.register("clock", ClockMatcher())  # time and date from the laptop clock, never online
    ExplainMatcher(logbook).register(interpreter)  # F5/R13: "why did you do that?", never Groq
    interpreter.register("sleep", SleepMatcher())  # "go to sleep": local, never sent online
    if briefing is not None:
        briefing.register(interpreter, orchestrator)  # F21: exact phrases only -- before F10 ("what's my daily update" was a memory question) and F16a ("morning briefing" is not a weather question)
    memory.register(interpreter, orchestrator)  # F10: local only, never online -- before weather/files/apps
    if topics is not None:
        topics.register(interpreter, orchestrator)  # F20: "track X", "any news on my topics" -- before weather/files/apps/Groq
    if youtube is not None:
        youtube.register(interpreter, orchestrator)  # F18: before F16b ("search youtube for X") and F4/F19 ("summarise")
    if browser is not None:
        browser.register(interpreter, orchestrator)  # F16b: before F16a/F4/F3 ("open chrome and search X")
    if screen is not None:
        screen.register(interpreter, orchestrator)  # F8: before F4 ("find X on my screen" is not a file search)
    if vision is not None:
        vision.register(interpreter, orchestrator)  # F9: before F3 ("turn on the camera" is not the Camera app)
    if files is not None:
        files.register(interpreter, orchestrator)  # F19: after F8 ("read my screen"), before F4 and Groq
    if system is not None:
        system.register(interpreter, orchestrator)  # F22: after F9 (camera), before F3 ("turn off the laptop" is not an app)
    weather.register(interpreter, orchestrator)  # F16a -- before F4/F3 ("open weather" is an app) and Groq
    file_finder.register(interpreter, orchestrator)  # F4 -- before F3, see docstring above
    open_app.register(interpreter, orchestrator, picker=app_picker, prefetch=prefetch_apps)  # F3
    # "when is my presentation due" -> memory only, never Groq.
    interpreter.register("personal_facts", memory.personal_matcher, raw=True)
    # The general intent router -- after every fixed phrase, before Groq.
    router.register(interpreter, orchestrator)
    conversation = Conversation(answerer, online_status)
    if browser is not None:  # F16: a failed live search opens the Google results in his Chrome
        conversation.browser_fallback = BrowserCommands.request
    conversation.register(interpreter, orchestrator)  # F2 -- must stay last


def make_app_components() -> AppComponents:
    """Wires the controller, F5's logbook, F7's confirmation engine
    (already inside Controller), the interpreter and the orchestrator
    together, and registers the pluggable modules against the real app.

    Matcher order is the interpreter's first-match-wins order: F1 intro,
    F2 greeting, F5's "why did you do that?" (R13), F4's open_selection_matcher before F3's open_app_matcher
    (so a pending "open 1"/"open <name>" selection is tried first, per
    jarvis/actions/find_file.py), and F2's Groq conversation strictly
    last -- deterministic first, a model only if needed (4.1)."""
    controller = Controller(start_state=State.SLEEPING)  # conversation mode: she starts asleep
    logbook = Logbook(debug_log=DebugLog())  # raw record detail goes to the debug log, not the UI
    bridge = DashboardBridge()

    def on_transition(entry) -> None:
        # bridge.push is O(1)/non-blocking and runs first, unconditionally,
        # so a logbook bug can never stop the dashboard from seeing an
        # update.
        bridge.push(entry)
        try:
            logbook.on_transition(entry)
        except Exception:
            logger.exception("logbook.on_transition raised inside the dashboard's combined listener")

    controller.set_transition_listener(on_transition)

    interpreter = Interpreter()
    # F2: [speech] engine -- Kokoro af_bella, loading and warming up on its
    # own thread (R15). A missing Kokoro package or model degrades to Zira,
    # or to text only, with a setup note (R14).
    speech = make_speech_output()
    startup_timing.mark("speech engine ready")
    rudeness = RudenessFilter(SETTINGS["rudeness"]["words"])
    greeter = WakeGreeter(state_path=default_state_path())  # varied wake greeting, remembered across restarts
    mood = MoodWatcher(SETTINGS["mood"])  # F14: session only, never saved
    orchestrator = Orchestrator(controller, interpreter, speech, logbook=logbook, rudeness=rudeness, greeter=greeter, mood=mood)

    # Started by the background start, after voice (R15: the wake-word model
    # loads 2.4 s alone but 4.3 s while the index scan competes for the CPU).
    file_index = FileIndex()
    online_status = OnlineStatus()
    abilities = SETTINGS["groq"]["abilities"]
    capabilities = [(str(abilities[m.code]), m.status == BUILT) for m in MODULE_STATUS if m.code in abilities]
    # F16 Stage 1: every Groq call's tokens go to a daily ledger (numbers only).
    ledger = TokenLedger(default_usage_path())
    answerer = GroqAnswerer(service=default_groq_service(online_status), capabilities=capabilities, ledger=ledger, style_hint=mood.prompt_hint)
    startup_timing.mark("online answerer ready")
    # F10: what he told her to remember. Loaded now (a small file) so a
    # damaged file is reported at startup, shown and spoken.
    memory = MemoryCommands(MemoryStore())
    memory.store.count()
    # F16b: searches open Google in his own Chrome, no automation.
    browser = BrowserCommands()
    # F8: the Sensor Array's Screen Capture row is on only while a capture runs.
    sensors = SensorFlags()
    screen = ScreenCommands(answerer, sensors, online_status=online_status)
    # F9: the camera is opened only when asked; the stream's detections go to Live Activity.
    vision = VisionCommands(Camera(sensors), status=logbook.record_status)
    # F16a: the one weather client, shared with F21's briefing (one cooldown).
    weather_client = WeatherClient(default_weather_service(online_status), cache_path=default_cache_path())
    weather = WeatherMatcher(weather_client, online_status)
    # F20: tracked topics, checked only on "any news on my topics" (or a
    # briefing he asked for) -- never on a wake, never by a timer.
    news_client = NewsClient(default_news_service(online_status))
    topics = TopicCommands(TopicMonitor(TopicStore(), news_client), online_status)
    topics.store.topics()  # a damaged file is found (and kept) now, and said on the next check
    # F22: volume, brightness, Wi-Fi, lock, restart, shutdown (no terminal, R10).
    system = SystemCommands()
    # F18: YouTube search in his Chrome; transcript summaries online (L0).
    youtube = YouTubeCommands(answerer, default_youtube_service(online_status))
    # F19: files read locally; a summary goes online only after the R4 preview.
    files = FileCommands(file_index, answerer)
    # F21: the morning briefing, ONLY when he asks ("brief me").
    # the first-wake trigger is gone -- nothing runs by itself on a wake.
    briefing = Briefing(weather_client, news_client, topics, online_status=online_status)
    register_matchers(interpreter, orchestrator, logbook, FileFinder(file_index), answerer, online_status, weather=weather,
                      prefetch_apps=False, memory=memory, browser=browser, screen=screen, vision=vision, topics=topics,
                      briefing=briefing, system=system, youtube=youtube, files=files,
                      router=RouterCommands(classify=answerer.classify))
    startup_timing.mark("commands registered")

    def _cut_speech_on_stop(entry) -> None:
        # R8: Stop (button, Esc, global hotkey) ends speech at once: the
        # current sentence is cut and queued sentences are dropped.
        if entry.event_type is None:
            speech.stop()

    controller.add_transition_listener(_cut_speech_on_stop)

    controller.start()
    return AppComponents(controller, logbook, interpreter, orchestrator, bridge, file_index, online_status, speech, memory, browser,
                         screen, sensors, vision, topics, briefing, system, youtube, files)


def _border_all(width: float, color: str) -> ft.Border:
    """This Flet version has no ft.border.all() shorthand -- a uniform
    Border built from one BorderSide per side."""
    side = ft.BorderSide(width, color)
    return ft.Border(top=side, right=side, bottom=side, left=side)


def _panel(*, width: float | None = None, expand: bool | int = False, content: ft.Control, padding: ft.Padding | int = 14) -> ft.Container:
    return ft.Container(
        content=content,
        width=width,
        expand=expand,
        bgcolor=theme.PANEL_BG,
        border=_border_all(1, theme.PANEL_BORDER),
        border_radius=10,
        padding=padding,
    )


# Column 1's panels keep the 14px top padding every panel has (so the three
# columns' titles still line up) but a smaller bottom padding: at the normal
# 1280x820 window the Modules panel had room for its title only and none of
# its rows. The space saved here goes to the Modules list.
_COL1_PANEL_PADDING = ft.Padding(14, 14, 14, 8)


def _one_line(text: ft.Text) -> ft.Text:
    """(half-screen width: "Micropho / ne", "Camera off - only
    when you ask" wrapping): a label is one line, cut with "..." when it
    doesn't fit -- never broken mid-word. Size, font and colour unchanged."""
    text.max_lines = 1
    text.no_wrap = True
    text.overflow = ft.TextOverflow.ELLIPSIS
    return text


def _live_activity_header(copy_button: ft.Control) -> ft.Row:
    """The Live Activity title takes the room that is left and is cut with
    "..." first; COPY LOG never shrinks, so it is always fully visible."""
    return ft.Row(
        [ft.Container(content=_one_line(_panel_title("Live Activity")), expand=True), copy_button],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=8,
    )


def _panel_title(text: str) -> ft.Text:
    return ft.Text(
        text.upper(),
        font_family=theme.FONT_ORBITRON,
        size=13,
        weight=ft.FontWeight.W_700,
        color=theme.TEXT_SECONDARY,
        style=ft.TextStyle(letter_spacing=2),
    )


def _kbd(label: str) -> ft.Container:
    return ft.Container(
        content=ft.Text(
            label,
            font_family=theme.FONT_ORBITRON,
            size=11,
            weight=ft.FontWeight.W_700,
            color=theme.CYAN_ACCENT,
        ),
        padding=ft.Padding(6, 2, 6, 2),
        border=_border_all(1, theme.CHIP_BORDER),
        border_radius=4,
        bgcolor=theme.TILE_BG,
        shadow=ft.BoxShadow(blur_radius=6, color="#4000E5FF"),
    )


def _quick_command_button(label: str, icon: str, on_click) -> ft.Button:
    """Item C of the second batch fix: plain ft.Button(bgcolor=TILE_BG) was
    nearly invisible against the panel -- TILE_BG (#081B29) and PANEL_BG
    (#061826) are too close in value for a flat fill alone to read as a
    button. Explicit border + hover/press states (the same CHIP_BORDER ->
    CYAN_ACCENT treatment used for focus elsewhere) makes it unmistakably
    a clickable control instead of a text list.

    Third-round fix: the border-only resting state was still reading as an
    outlined box rather than a filled button. Resting fill is now a ~16%
    cyan tint (#29 alpha on CYAN_ACCENT) instead of TILE_BG, clearly
    distinct from the panel/tile backgrounds; hover/press stay stronger.

    Item 5 (locked design spec): a small icon in a circular badge to the
    left of the label -- alignment=CENTER on the badge is required, not
    decorative, per the earlier Sensor Array toggle bug: a fixed-size
    Container with a child and no alignment stretches that child to fill
    it instead of centering it at its own size."""
    icon_badge = ft.Container(
        content=ft.Icon(icon, size=14, color=theme.CYAN_ACCENT),
        width=22,
        height=22,
        border_radius=11,
        bgcolor=theme.CHIP_BORDER,
        alignment=ft.Alignment.CENTER,
    )
    return ft.Button(
        content=ft.Row(
            [icon_badge, ft.Text(label, font_family=theme.FONT_RAJDHANI, size=13, color=theme.TEXT_PRIMARY)],
            spacing=10,
        ),
        on_click=on_click,
        style=ft.ButtonStyle(
            bgcolor={
                ft.ControlState.DEFAULT: "#2900E5FF",
                ft.ControlState.HOVERED: theme.CHIP_BORDER,
                ft.ControlState.PRESSED: theme.CHIP_BORDER,
            },
            side={
                ft.ControlState.DEFAULT: ft.BorderSide(1, theme.CHIP_BORDER),
                ft.ControlState.HOVERED: ft.BorderSide(1, theme.CYAN_ACCENT),
            },
            shape=ft.RoundedRectangleBorder(radius=8),
            padding=ft.Padding(14, 12, 14, 12),
            elevation={ft.ControlState.DEFAULT: 0, ft.ControlState.HOVERED: 2},
        ),
    )


def _schedule_page_update(page: ft.Page, ui_lock: threading.Lock, on_closed=None) -> None:
    """Send pending control changes to the window, from ANY thread.

    page.update() must run on Flet's own asyncio loop: it queues the outgoing
    message with put_nowait(), which does not wake a sleeping loop when called
    from another thread, so the update sits unsent until something else (a
    mouse move) wakes it -- see build()'s comment on push_update. page.run_task
    hands the call to that loop in a wake-safe way."""

    async def send() -> None:
        with ui_lock:
            try:
                page.update()
            except Exception:
                if on_closed is not None:
                    on_closed()  # the window was closed

    try:
        page.run_task(send)
    except Exception:
        if on_closed is not None:
            on_closed()


def _sensor_row(
    icon: str,
    label: str,
    page: ft.Page,
    ui_lock: threading.Lock,
    on_click=None,
    subtitle: ft.Text | None = None,
    refs: dict | None = None,
) -> ft.Container:
    """Styled indicator row for a sensor (mic/camera/screen) -- honestly OFF, not a
    decorative "on". Clickable through on_click: nothing silently ignores a
    click.

    The pill uses a high-contrast thumb (TEXT_SECONDARY) for a clear
    switch shape, and a hover state -- border and thumb shift to
    CYAN_ACCENT with a soft glow -- matching the same affordance language
    the Quick Commands buttons already use. Hover mutates real Flet
    controls from a page-loop callback, so it goes through the same
    ui_lock + page.update() pattern as every other control-touching
    handler in this module (see build()'s own docstring/comment on why)."""
    thumb = ft.Container(
        width=14,
        height=14,
        border_radius=7,
        bgcolor=theme.TEXT_SECONDARY,
        margin=ft.Margin(3, 0, 0, 0),
        shadow=ft.BoxShadow(blur_radius=3, color="#80000000"),
    )
    # Root cause of the "flat uniform capsule, no visible track/thumb
    # contrast" regression (found by sampling actual rendered pixel colors,
    # not just re-reading this code -- they came back a single uniform
    # value across the whole 38px width, with zero trace of TILE_BG
    # anywhere): a Flet/Flutter Container that has both a fixed size AND a
    # `content` child, but no `alignment`, stretches that child to fill it
    # completely -- overriding the child's own width/height entirely. The
    # 14x14 thumb was being forced to fill all 38x20px instead of sitting
    # as a small circle to one side, hiding the dark track color under it.
    # alignment=CENTER_LEFT makes Flutter wrap the child in an Align
    # instead, letting it render at its own declared size.
    track = ft.Container(
        width=38,
        height=20,
        border_radius=10,
        bgcolor=theme.TILE_BG,
        border=_border_all(1, theme.CHIP_BORDER),
        content=thumb,
        alignment=ft.Alignment.CENTER_LEFT,
        animate=ft.Animation(150, ft.AnimationCurve.EASE_OUT),
    )

    def _hover(e: ft.HoverEvent) -> None:
        hovering = bool(e.data)
        live = bool(refs and refs.get("live"))
        with ui_lock:
            track.border = _border_all(1, theme.CYAN_ACCENT if hovering or live else theme.CHIP_BORDER)
            track.shadow = ft.BoxShadow(blur_radius=10, color=theme.CYAN_ACCENT) if hovering else None
            thumb.bgcolor = theme.CYAN_ACCENT if hovering or live else theme.TEXT_SECONDARY
        _schedule_page_update(page, ui_lock)

    track.on_hover = _hover

    icon_control = ft.Icon(icon, color=theme.TEXT_DIM, size=16)
    label_text = _one_line(ft.Text(label, font_family=theme.FONT_RAJDHANI, size=14, color=theme.TEXT_SECONDARY))
    if subtitle is not None:
        _one_line(subtitle)
    # F2: a real sensor (the microphone) shows a second line underneath --
    # the live device name, or that it needs setting up.
    label_block = ft.Column([label_text, subtitle], spacing=0, expand=True) if subtitle is not None else ft.Container(content=label_text, expand=True)
    if refs is not None:
        refs.update(track=track, thumb=thumb, icon=icon_control, live=False)
    row = ft.Row([icon_control, label_block, track])
    # 2px top/bottom (was 4): part of the room given to the Modules list.
    return ft.Container(content=row, on_click=on_click, ink=True, border_radius=6, padding=ft.Padding(4, 2, 4, 2))


def _metric_tile(icon: str, label: str) -> Tuple[ft.Container, ft.Text, ft.ProgressBar, ft.Text]:
    value_text = ft.Text("--", font_family=theme.FONT_ORBITRON, size=16, color=theme.TEXT_PRIMARY)
    bar = ft.ProgressBar(value=0, color=theme.CYAN_ACCENT, bgcolor=theme.CHIP_BORDER, bar_height=3)
    status_text = ft.Text("--", font_family=theme.FONT_RAJDHANI, size=11, color=theme.TEXT_DIM)
    # Item 1 (System Vitals too tall), round 3: pushed further than round 2
    # (was padding 8/3, spacing 1, status_text size 12) -- all four metrics
    # (icon, label, value, bar, status word) still present, just tighter.
    tile = ft.Container(
        bgcolor=theme.TILE_BG,
        border=_border_all(1, theme.CHIP_BORDER),
        border_radius=8,
        padding=ft.Padding(8, 2, 8, 2),
        content=ft.Column(
            [
                ft.Row([ft.Icon(icon, color=theme.ICON_INACTIVE, size=16), ft.Text(label, font_family=theme.FONT_RAJDHANI, size=13, color=theme.TEXT_SECONDARY), value_text], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                bar,
                status_text,
            ],
            spacing=0,
        ),
    )
    return tile, value_text, bar, status_text


def build(
    page: ft.Page,
    controller: Controller,
    orchestrator: Orchestrator,
    logbook: Logbook,
    bridge: DashboardBridge,
    online_status: OnlineStatus | None = None,
    notices: List[str] | None = None,
    voice: VoiceSystem | None = None,
    notice_hook: dict | None = None,
    visibility: WindowVisibility | None = None,
    warn: Callable[[str], None] | None = None,
    sensors: SensorFlags | None = None,
    screen: ScreenCommands | None = None,
    vision: VisionCommands | None = None,
    system: SystemCommands | None = None,
    files: FileCommands | None = None,
) -> None:
    """`visibility` says when the window is minimised or hidden (R15:
    animations pause); `warn` shows and speaks a warning (R15's low-memory
    warning). `sensors` drives the live capture rows (F8 screen); `screen`
    gives the R4 preview card its exact image and text."""
    sensors = sensors or SensorFlags()
    online_status = online_status or OnlineStatus()
    visibility = visibility or WindowVisibility()
    # Blank, not "JARVIS" -- the in-app centered J.A.R.V.I.S. wordmark is
    # the real title; page.title is the only thing that drives the native
    # OS title-bar text in this Flet version (no separate Window.title),
    # and an empty string blanks that text while keeping the window fully
    # native: real drag-to-move, native resize handles, native min/max/
    # close buttons all still work. Deliberately NOT using
    # window.title_bar_hidden/frameless -- that's a real tradeoff (loses
    # native drag/resize) the developer asked not to make silently.
    page.title = ""
    page.window.min_width = _WINDOW_MIN_WIDTH
    page.window.min_height = _WINDOW_MIN_HEIGHT
    page.window.width = max(1280, _WINDOW_MIN_WIDTH)
    page.window.height = max(820, _WINDOW_MIN_HEIGHT)
    # flet.exe is the same shared client binary for every Flet app on this
    # machine, and Windows was observed restoring a maximized state left
    # over from some other app's last run instead of the size set above --
    # column 3 rendered past the edge of a 1536px-wide screen as a result.
    # Forcing maximized=False after the explicit size above makes the
    # launch geometry deterministic regardless of leftover native window
    # state. (Not calling page.window.center() -- it's async-only in this
    # Flet version and this function isn't; the OS's default placement for
    # a non-maximized window has been fine.)
    page.window.maximized = False

    # Column pixel widths, computed from the real values just set above
    # (not asserted from the flex weights alone) so the sphere below can be
    # sized off column 2's actual width rather than a guessed constant.
    # Mirrors body's own container: Padding(20, 16, 20, 0) (left+right=40),
    # a Row with spacing=12 between its 5 children (4 gaps=48), and two
    # ft.VerticalDivider()s at their default width of 16px each (32) --
    # every number below comes from those same controls, not a separate
    # estimate.
    _body_fixed_overhead = 40 + 4 * 12 + 2 * 16  # = 120
    _flex_total = page.window.width - _body_fixed_overhead
    _flex_unit = _flex_total / (_COL1_FLEX + _COL2_FLEX + _COL3_FLEX)
    _col1_px = _flex_unit * _COL1_FLEX
    _col2_px = _flex_unit * _COL2_FLEX
    _col3_px = _flex_unit * _COL3_FLEX
    logger.info(
        "dashboard column layout at window=%.0fpx: col1=%.0fpx col2=%.0fpx col3=%.0fpx (flex %d:%d:%d)",
        page.window.width, _col1_px, _col2_px, _col3_px, _COL1_FLEX, _COL2_FLEX, _COL3_FLEX,
    )
    # Replaces Flet's default framework icon (generated once via
    # scripts/-style struct/zlib, no Pillow dependency added -- see the
    # asset itself) with a small cyan orb glyph matching the core-orb
    # motif used elsewhere in this dashboard. Windows-only per Flet's own
    # docs for window.icon; harmless no-op elsewhere.
    page.window.icon = "icon/jarvis.ico"
    page.bgcolor = theme.BG_BASE
    page.padding = 0
    page.fonts = theme.FONT_PATHS

    # Item 1a of the batch fix: recolor the still-fully-native title bar
    # (DWMWA_CAPTION_COLOR, Windows 11 only) instead of going frameless --
    # see native_titlebar.py for why/how this is reachable at all. Runs on
    # its own daemon thread and is a no-op everywhere it can't work.
    apply_caption_color_async(theme.BG_BASE, theme.TEXT_PRIMARY)

    # Flet's Page/session is driven by its own asyncio event loop; calling
    # page.update() (or mutating a control's properties) concurrently from
    # a raw background thread (refresh_loop below) AND from a click
    # handler (which runs on that asyncio loop) is not safe -- confirmed
    # live: clicking "Find a File" reproducibly crashed the whole app
    # (both the python and flet.exe processes, no traceback -- a native
    # crash, not a Python exception) because its handler mutated
    # command_box and called page.update() directly, racing refresh_loop's
    # own page.update() a few times a second. Flet's own answer to this is
    # page.run_task()/page.run_thread() for scheduling background work
    # onto the page's loop; the more surgical fix here, since refresh_loop
    # is a long-lived loop rather than one-shot background work, is a
    # single lock that both refresh_loop and every control-touching click
    # handler hold before touching any Flet control or calling
    # page.update().
    ui_lock = threading.Lock()

    # ROOT CAUSE of the clock (and every other value) freezing on screen and
    # then jumping several seconds at once -- found by capturing the real
    # window's pixels, not by reading Python values, which were correct the
    # whole time: Flet hands each outgoing message to an asyncio queue with
    # put_nowait(), which is NOT thread-safe and does not wake a sleeping
    # event loop when called from another thread. refresh_loop/clock_loop are
    # plain threads, so their page.update() messages just sat in the queue
    # until something else (a mouse move, a click) happened to wake Flet's
    # loop -- then everything flushed at once. The fix is to never call
    # page.update() from those threads: hand it to Flet's own loop with
    # page.run_task (run_coroutine_threadsafe, which does wake the loop).
    window_closed = threading.Event()

    def push_update() -> None:
        """Call from a background thread, after releasing ui_lock."""
        _schedule_page_update(page, ui_lock, window_closed.set)

    # -- top bar -----------------------------------------------------
    top_date_text = ft.Text("--", font_family=theme.FONT_MONO, size=13, color=theme.TEXT_SECONDARY)
    top_clock_text = ft.Text("--", font_family=theme.FONT_MONO, size=13, color=theme.CYAN_ACCENT)

    def _glow_dot() -> ft.Container:
        return ft.Container(width=10, height=10, border_radius=5, bgcolor=theme.CYAN_ACCENT, shadow=ft.BoxShadow(blur_radius=10, color=theme.CYAN_ACCENT))

    top_bar = ft.Stack(
        [
            ft.Row(
                [
                    _glow_dot(),
                    ft.Text(
                        "J.A.R.V.I.S.",
                        font_family=theme.FONT_ORBITRON,
                        size=26,
                        weight=ft.FontWeight.W_800,
                        color=theme.TEXT_PRIMARY,
                        style=ft.TextStyle(letter_spacing=8, shadow=[ft.BoxShadow(blur_radius=16, color=theme.CYAN_ACCENT)]),
                    ),
                    _glow_dot(),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=14,
            ),
            ft.Row(
                [top_date_text, ft.Container(width=16), top_clock_text],
                alignment=ft.MainAxisAlignment.END,
            ),
        ]
    )
    top_bar_container = ft.Container(content=top_bar, padding=ft.Padding(20, 14, 20, 14), border=ft.Border(bottom=ft.BorderSide(1, theme.PANEL_BORDER)))

    # -- column 1: sensors + gauges + modules -------------------------
    cpu_tile, cpu_value, cpu_bar, cpu_status = _metric_tile(ft.Icons.MEMORY, "CPU")
    ram_tile, ram_value, ram_bar, ram_status = _metric_tile(ft.Icons.SD_STORAGE, "RAM")
    disk_tile, disk_value, disk_bar, disk_status = _metric_tile(ft.Icons.STORAGE, "DISK")
    gauges_missing = gauges_setup_message()
    if gauges_missing:
        for tile in (cpu_tile, ram_tile, disk_tile):
            tile.tooltip = gauges_missing

    # Column (Row header + Text below), not one Row -- at column1's 220px
    # width, "NETWORK" + icon + "UP 999.9 KB/s / DOWN 999.9 KB/s" on one
    # unwrapped mono-font line doesn't fit in any of 320/270/240/220px and
    # would overflow the tile's right edge (see column1 width fix above for
    # why an overflowing child, not the declared width, is what a screenshot
    # actually shows).
    # Item 4: matches the CPU/RAM/Disk tiles' own row language (small icon +
    # label + value, ft.Icons.ARROW_* for direction) instead of one flat
    # mono-font line -- upload and download each get their own row and
    # their own accent color (cyan for outbound, status-green for inbound)
    # so they read as distinct at a glance, not just two numbers.
    net_up_value = ft.Text("--", font_family=theme.FONT_MONO, size=13, color=theme.TEXT_PRIMARY)
    net_down_value = ft.Text("--", font_family=theme.FONT_MONO, size=13, color=theme.TEXT_PRIMARY)
    net_tile = ft.Container(
        bgcolor=theme.TILE_BG,
        border=_border_all(1, theme.CHIP_BORDER),
        border_radius=8,
        padding=ft.Padding(8, 2, 8, 2),
        content=ft.Column(
            [
                ft.Row([ft.Icon(ft.Icons.NETWORK_CHECK, color=theme.ICON_INACTIVE, size=16), ft.Text("NETWORK", font_family=theme.FONT_RAJDHANI, size=13, color=theme.TEXT_SECONDARY)], spacing=8),
                ft.Row(
                    [
                        ft.Icon(ft.Icons.ARROW_UPWARD, color=theme.CYAN_ACCENT, size=14),
                        ft.Text("UP", font_family=theme.FONT_RAJDHANI, size=12, color=theme.TEXT_SECONDARY, expand=True),
                        net_up_value,
                    ],
                    spacing=6,
                ),
                ft.Row(
                    [
                        ft.Icon(ft.Icons.ARROW_DOWNWARD, color=theme.STATUS_OK, size=14),
                        ft.Text("DOWN", font_family=theme.FONT_RAJDHANI, size=12, color=theme.TEXT_SECONDARY, expand=True),
                        net_down_value,
                    ],
                    spacing=6,
                ),
            ],
            spacing=0,
        ),
    )

    def show_notice(text: str, seconds: int = 8) -> None:
        """A missing-setup notice in the same toast style, from any thread
        (handed to Flet's own loop, see push_update)."""

        async def _show() -> None:
            with ui_lock:
                page.show_dialog(
                    ft.SnackBar(
                        ft.Row(
                            [
                                ft.Icon(ft.Icons.INFO_OUTLINE, color=theme.STATUS_ELEVATED, size=20),
                                ft.Text(text, font_family=theme.FONT_RAJDHANI, size=15, weight=ft.FontWeight.W_600, color=theme.TEXT_PRIMARY, expand=True),
                            ],
                            spacing=10,
                        ),
                        bgcolor=theme.CONFIRM_BG,
                        behavior=ft.SnackBarBehavior.FLOATING,
                        width=560,
                        duration=ft.Duration(seconds=seconds),
                        show_close_icon=True,
                        elevation=6,
                    )
                )

        try:
            page.run_task(_show)
        except Exception:
            logger.exception("Could not show a notice")

    if notice_hook is not None:
        notice_hook["show"] = show_notice

    # F2 Stage 2: the Microphone row is real now. Second line: the live
    # device's name, or that it needs setting up (the full missing-setup
    # sentence is in the tooltip, the click notice and the startup notice --
    # it is too long for this narrow column).
    mic_refs: dict = {}
    mic_subtitle = ft.Text("--", font_family=theme.FONT_RAJDHANI, size=11, color=theme.TEXT_DIM, max_lines=2)

    def _mic_status_view():
        if voice is None:
            return False, "Needs setup -- click for the fix", SETTINGS["setup_messages"]["voice_library_missing"]
        status = voice.mic_status
        if not status.live and status.capture == STARTING:
            # R15: voice starts in the background after the window shows.
            return False, STARTING, "The microphone is starting. It's ready in a few seconds."
        if status.live:
            name = status.device_name or "default microphone"
            capture = f" ({status.capture})" if status.capture else ""
            return True, name, status.note or f"Listening on {name}{capture}. Click to choose another microphone."
        return False, "Needs setup -- click for the fix", status.note or setup_message("mic_none")

    def _choose_microphone(name: str) -> None:
        """Developer picked a microphone (L0 dashboard action): switch the
        live stream to it and save the choice BY NAME in settings.toml
        ([audio] input_device_name; "" = Windows' default)."""
        live = voice.mic_status
        if live.live and name and live.device_name and name.strip().lower() == live.device_name.strip().lower():
            return  # already listening on it: nothing to switch
        started = time.monotonic()
        status = voice.switch_microphone(name)
        took = time.monotonic() - started
        try:
            save_string_setting("audio", "input_device_name", name)
        except Exception:
            logger.exception("Could not save the microphone choice")
        label = name or "Windows default"
        # Who changed it and how long it took, so a switch is never a mystery.
        logbook.record_ui_action(f"microphone set to {label} (picked in the Microphone list, switched in {took:.1f} s)")
        show_notice(status.note or f"Microphone: {status.device_name} ({status.capture}).")
        with ui_lock:
            apply_mic_status()
        push_update()

    def _on_mic_click(e=None) -> None:
        """Click the Microphone row: pick which microphone she listens on
        (developer request -- the used the built-in
        array instead of the earphone mic)."""
        if voice is None:
            show_notice(_mic_status_view()[2])
            return
        live_name = voice.mic_status.device_name or ""
        # The radio mark shows the device she is listening on now, not the
        # saved name.
        current = live_name if voice.mic_status.live else str(SETTINGS["audio"]["input_device_name"])
        opened_at = time.monotonic()

        def option(label: str, value: str) -> ft.Control:
            chosen = value == current
            def pick(e=None, value=value) -> None:
                # A click that arrives just as the list opens
                # was meant for what was there before; it never switches.
                if time.monotonic() - opened_at < float(SETTINGS["audio"]["picker_click_guard_seconds"]):
                    return
                page.pop_dialog()
                threading.Thread(target=_choose_microphone, args=(value,), daemon=True).start()
            return ft.TextButton(
                content=ft.Row(
                    [
                        ft.Icon(ft.Icons.RADIO_BUTTON_CHECKED if chosen else ft.Icons.RADIO_BUTTON_UNCHECKED, color=theme.CYAN_ACCENT, size=16),
                        ft.Text(label, font_family=theme.FONT_RAJDHANI, size=15, color=theme.TEXT_PRIMARY),
                    ],
                    spacing=8,
                ),
                on_click=pick,
            )

        options = [option("Windows default", "")] + [option(n, n) for n in list_input_device_names()]
        page.show_dialog(
            ft.AlertDialog(
                title=ft.Text("MICROPHONE", font_family=theme.FONT_ORBITRON, size=14, color=theme.CYAN_ACCENT),
                content=ft.Column(
                    [
                        ft.Text(f"Live now: {live_name or 'none'}", font_family=theme.FONT_MONO, size=12, color=theme.TEXT_SECONDARY),
                        *options,
                    ],
                    tight=True,
                    spacing=2,
                    scroll=ft.ScrollMode.AUTO,
                ),
                actions=[ft.TextButton(content=ft.Text("CLOSE", font_family=theme.FONT_ORBITRON, size=11), on_click=lambda e: page.pop_dialog())],
                bgcolor=theme.PANEL_BG,
            )
        )

    mic_row = _sensor_row(ft.Icons.MIC, "Microphone", page, ui_lock, on_click=_on_mic_click, subtitle=mic_subtitle, refs=mic_refs)
    shown_mic: dict = {"view": None}

    def apply_mic_status() -> bool:
        """Update the Microphone row from the live status. Returns True if
        anything changed (caller pushes the update). Caller holds ui_lock."""
        view = _mic_status_view()
        if view == shown_mic["view"]:
            return False
        shown_mic["view"] = view
        live, line, full = view
        mic_refs["live"] = live
        mic_subtitle.value = line
        mic_subtitle.color = theme.CYAN_ACCENT if live else theme.STATUS_ELEVATED
        mic_row.tooltip = full
        mic_refs["icon"].color = theme.CYAN_ACCENT if live else theme.TEXT_DIM
        mic_refs["thumb"].bgcolor = theme.CYAN_ACCENT if live else theme.TEXT_SECONDARY
        mic_refs["track"].alignment = ft.Alignment.CENTER_RIGHT if live else ft.Alignment.CENTER_LEFT
        mic_refs["track"].border = _border_all(1, theme.CYAN_ACCENT if live else theme.CHIP_BORDER)
        return True

    apply_mic_status()

    # F8 (and F9 next): a capture row is a real sensor now -- on (cyan, the
    # mic row's own "live" look) only while a capture is running.
    def _capture_click(text: str):
        return lambda e=None: show_notice(text, seconds=5)

    capture_rows: dict = {}

    def _capture_row(icon, label: str, sensor: str, idle_text: str, click_text: str) -> ft.Container:
        refs: dict = {}
        subtitle = ft.Text(idle_text, font_family=theme.FONT_RAJDHANI, size=11, color=theme.TEXT_DIM)
        row = _sensor_row(icon, label, page, ui_lock, on_click=_capture_click(click_text), subtitle=subtitle, refs=refs)
        row.tooltip = click_text
        capture_rows[sensor] = (refs, subtitle, idle_text, None)
        return row

    def apply_capture_rows() -> bool:
        """Caller holds ui_lock. True if a row changed."""
        changed = False
        for sensor, (refs, subtitle, idle_text, shown) in list(capture_rows.items()):
            live = sensors.is_on(sensor)
            if live == shown:
                continue
            capture_rows[sensor] = (refs, subtitle, idle_text, live)
            refs["live"] = live
            subtitle.value = "capturing now" if live else idle_text
            subtitle.color = theme.CYAN_ACCENT if live else theme.TEXT_DIM
            refs["icon"].color = theme.CYAN_ACCENT if live else theme.TEXT_DIM
            refs["thumb"].bgcolor = theme.CYAN_ACCENT if live else theme.TEXT_SECONDARY
            refs["track"].alignment = ft.Alignment.CENTER_RIGHT if live else ft.Alignment.CENTER_LEFT
            refs["track"].border = _border_all(1, theme.CYAN_ACCENT if live else theme.CHIP_BORDER)
            changed = True
        return changed

    screen_row = _capture_row(
        ft.Icons.DESKTOP_WINDOWS, "Screen Capture", SCREEN, "off - only when you ask",
        str(SETTINGS["screen"]["row_click_text"]),
    )
    camera_row = _capture_row(
        ft.Icons.CAMERA_ALT, "Camera", CAMERA, "off - only when you ask",
        str(SETTINGS["camera"]["row_click_text"]),
    )
    apply_capture_rows()

    module_rows = []
    for m in MODULE_STATUS:
        color = _MODULE_STATUS_COLORS[m.status]
        # One line per module: the name is cut with "..." when the narrow
        # column can't fit it (it used to break mid-word, "Self Introd /
        # uction"); hovering shows the full name and status.
        module_rows.append(
            ft.Container(
                tooltip=f"{m.code} {m.name}: {_MODULE_STATUS_LABELS[m.status].lower()}",
                content=ft.Row(
                    [
                        ft.Text(m.code, font_family=theme.FONT_ORBITRON, size=11, color=theme.TEXT_DIM, width=36),  # fits "F16b"
                        ft.Text(m.name, font_family=theme.FONT_RAJDHANI, size=13, color=theme.TEXT_SECONDARY, expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Container(
                            content=ft.Text(_MODULE_STATUS_LABELS[m.status], font_family=theme.FONT_ORBITRON, size=9, color=color, weight=ft.FontWeight.W_700),
                            padding=ft.Padding(6, 1, 6, 1),
                            border=_border_all(1, color),
                            border_radius=8,
                        ),
                    ],
                    spacing=6,
                ),
            )
        )

    column1 = ft.Column(
        [
            _panel(
                padding=_COL1_PANEL_PADDING,
                content=ft.Column(
                    [
                        _panel_title("Sensor Array"),
                        mic_row,
                        camera_row,
                        screen_row,
                    ],
                    spacing=4,  # was 10; see _COL1_PANEL_PADDING
                ),
            ),
            _panel(
                padding=_COL1_PANEL_PADDING,
                content=ft.Column(
                    [_panel_title("System Vitals"), cpu_tile, ram_tile, disk_tile, net_tile],
                    spacing=2,
                ),
            ),
            _panel(
                expand=True,
                padding=_COL1_PANEL_PADDING,
                content=ft.Column(
                    [
                        _panel_title("Modules"),
                        # Scrolls inside the panel, with the scrollbar always
                        # showing, so every row is reachable at any window
                        # height. Right padding keeps the bar off the badges.
                        ft.ListView(
                            controls=module_rows,
                            expand=True,
                            spacing=3,
                            scroll=ft.ScrollMode.ALWAYS,
                            padding=ft.Padding(0, 0, 8, 0),
                        ),
                    ],
                    spacing=6,
                    expand=True,
                ),
            ),
        ],
        # STRETCH: none of the three panels above set their own width, so
        # they fill whatever pixel width the Row's flex split (below) gives
        # this Column -- the default (loose) constraint would instead let
        # each panel shrink to its own content size, ignoring the flex
        # weight entirely (the same silent-override failure mode as the
        # fixed-pixel-width bug this replaces).
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        spacing=12,
        expand=_COL1_FLEX,
    )

    # -- column 2: core + activity log --------------------------------
    # Core ball: a single solid glowing sphere, no ring outlines -- the two
    # thin rotating rings (ft.Container borders, no fill) that used to
    # surround it are gone. Diameter is ~30% of column 2's actual computed
    # width above (clamped so it stays sane at extreme window sizes), not
    # a hardcoded constant, so it scales if the window/flex split changes.
    _orb_diameter = max(160, min(340, round(_col2_px * 0.30)))
    orb_core = ft.Container(
        width=_orb_diameter,
        height=_orb_diameter,
        border_radius=_orb_diameter / 2,
        bgcolor=theme.CYAN_ACCENT,
        shadow=ft.BoxShadow(blur_radius=_orb_diameter * 0.5, color=theme.CYAN_ACCENT),
    )
    orb = ft.Container(content=orb_core, width=_orb_diameter, height=_orb_diameter, alignment=ft.Alignment.CENTER)

    # Scaled up to match the now much larger core ball (was sized for the
    # old 140px ring/56px-ball core).
    state_pill_text = ft.Text("SLEEPING", font_family=theme.FONT_ORBITRON, size=22, weight=ft.FontWeight.W_700, color=theme.TEXT_SECONDARY)
    state_pill = ft.Container(
        content=state_pill_text,
        padding=ft.Padding(24, 10, 24, 10),
        border=_border_all(1, theme.CHIP_BORDER),
        border_radius=22,
        bgcolor=theme.TILE_BG,
    )

    # F2 Stage 2: real microphone levels, but ONLY while a voice command is
    # being recorded (LISTENING). WAITING audio is never drawn: the rest of
    # the time this is the same static dim placeholder as before.
    _waveform_rest = [6 + (i % 5) * 3 for i in range(28)]
    waveform_bars = [
        ft.Container(width=4, height=_waveform_rest[i], bgcolor=theme.ICON_INACTIVE, opacity=0.35, border_radius=2) for i in range(28)
    ]
    waveform_live = {"on": False}

    def apply_waveform(levels: List[float]) -> bool:
        """Caller holds ui_lock. Returns True if the bars changed."""
        if not levels:
            if not waveform_live["on"]:
                return False
            waveform_live["on"] = False
            for bar, height in zip(waveform_bars, _waveform_rest):
                bar.height, bar.bgcolor, bar.opacity = height, theme.ICON_INACTIVE, 0.35
            return True
        waveform_live["on"] = True
        padded = [0.0] * (len(waveform_bars) - len(levels)) + list(levels)[-len(waveform_bars):]
        for bar, level in zip(waveform_bars, padded):
            bar.height, bar.bgcolor, bar.opacity = 4 + level * 36, theme.CYAN_ACCENT, 0.5 + 0.5 * level
        return True
    waveform = ft.Row(waveform_bars, spacing=3, alignment=ft.MainAxisAlignment.CENTER)

    # reverse=True anchors content to the BOTTOM of the panel (empty space
    # collects at the top, like a chat log) -- Flutter's ListView renders
    # index 0 at the anchored end, so new rows are inserted at index 0
    # (newest first in the list), not appended.
    # auto_scroll must stay OFF here: on a reversed list it scrolls to the END
    # of the list, which is the OLDEST row -- the panel showed the first
    # command of the session instead of the latest. Offset 0 (the default)
    # already anchors the newest row at the bottom.
    activity_feed = ft.ListView(expand=True, spacing=4, reverse=True)
    activity_empty_text = ft.Text("No activity yet.", font_family=theme.FONT_RAJDHANI, size=13, color=theme.TEXT_DIM, italic=True)

    command_box = ft.TextField(
        hint_text="Type a command...",
        expand=True,
        color=theme.TEXT_PRIMARY,
        bgcolor=theme.TILE_BG,
        border_color=theme.CHIP_BORDER,
        focused_border_color=theme.CYAN_ACCENT,
        text_style=ft.TextStyle(font_family=theme.FONT_MONO, size=13),
        # text_style only covers what's typed -- the placeholder has its
        # own style and silently fell back to the system font without this.
        # Was TEXT_DIM (#7099AF), too faint to read at a glance -- "NOMINAL"
        # (the comparison point) actually renders in STATUS_OK green, not
        # TEXT_DIM, so it was never a fair contrast match. TEXT_SECONDARY
        # (#A6D4E8, the same color panel/label text uses) is a real match.
        hint_style=ft.TextStyle(font_family=theme.FONT_MONO, size=13, color=theme.TEXT_SECONDARY),
    )

    def submit_command(e=None) -> None:
        with ui_lock:
            text = command_box.value or ""
            command_box.value = ""
        _schedule_page_update(page, ui_lock)
        if text.strip():
            threading.Thread(target=orchestrator.handle_typed_command, args=(text,), daemon=True).start()

    command_box.on_submit = submit_command
    send_button = ft.Button(
        content=ft.Text("SEND", font_family=theme.FONT_ORBITRON, size=12, weight=ft.FontWeight.W_700),
        on_click=submit_command,
        color=theme.TEXT_PRIMARY,
        bgcolor=theme.PANEL_BG,
    )

    def do_stop(e=None, source: str = "STOP button") -> None:
        controller.request_stop(source=source)

    stop_button = ft.Container(
        content=ft.Text("STOP", font_family=theme.FONT_ORBITRON, size=16, weight=ft.FontWeight.W_800, color=theme.TEXT_PRIMARY),
        padding=ft.Padding(24, 10, 24, 10),
        border=_border_all(2, theme.STATUS_CRITICAL),
        border_radius=8,
        bgcolor=theme.PANEL_BG,
        alignment=ft.Alignment.CENTER,
        on_click=do_stop,
        animate=ft.Animation(600, ft.AnimationCurve.EASE_IN_OUT),
        shadow=ft.BoxShadow(blur_radius=8, color=theme.STATUS_CRITICAL),
    )

    def on_keyboard(e: ft.KeyboardEvent) -> None:
        # In-window backup for Stop. The system-wide one (Ctrl+Shift+X, via
        # Windows' RegisterHotKey) lives in jarvis/voice/hotkeys.py.
        if e.key == "Escape":
            do_stop(source="Esc key")

    page.on_keyboard_event = on_keyboard

    bottom_bar = ft.Container(
        content=ft.Row(
            [
                command_box,
                send_button,
                _kbd("ENTER"),
                _kbd("ESC"),
                stop_button,
            ],
            spacing=10,
        ),
        padding=ft.Padding(20, 12, 20, 16),
        border=ft.Border(top=ft.BorderSide(1, theme.PANEL_BORDER)),
    )

    # COPY LOG (developer-approved): the whole session's activity
    # log to the clipboard as plain text. L0, recorded as "copied activity
    # log". Built from the logbook's stored records, which are redacted
    # when stored (R11), so the copy is redacted the same way.
    clipboard = ft.Clipboard()

    async def copy_log(e=None) -> None:
        records = logbook.records()
        in_progress = logbook.live_snapshot()
        if in_progress is not None:
            records.append(in_progress)
        await clipboard.set(format_session_log(records))
        logbook.record_ui_action("copied activity log")

    copy_log_label = ft.Text("COPY LOG", font_family=theme.FONT_ORBITRON, size=11, weight=ft.FontWeight.W_700, color=theme.CYAN_ACCENT,
                             no_wrap=True, max_lines=1)

    def _copy_log_hover(e: ft.HoverEvent) -> None:
        with ui_lock:
            copy_log_button.border = _border_all(1, theme.CYAN_ACCENT if e.data else theme.CHIP_BORDER)
        push_update()

    copy_log_button = ft.Container(
        content=copy_log_label,
        padding=ft.Padding(8, 3, 8, 3),
        border=_border_all(1, theme.CHIP_BORDER),
        border_radius=4,
        bgcolor="#2900E5FF",
        tooltip="Copy this session's activity log",
        on_click=copy_log,
        on_hover=_copy_log_hover,
    )

    column2 = ft.Column(
        [
            _panel(
                expand=True,
                content=ft.Column(
                    [
                        ft.Row([orb], alignment=ft.MainAxisAlignment.CENTER),
                        ft.Row([state_pill], alignment=ft.MainAxisAlignment.CENTER),
                        ft.Container(height=6),
                        waveform,
                        # Breathing room before Live Activity (settings.toml:
                        # live_activity_top_spacer). Was 100px; trimmed so the
                        # activity panel gets a little more height.
                        ft.Container(height=_LIVE_ACTIVITY_TOP_SPACER),
                        ft.Divider(color=theme.PANEL_BORDER),
                        _live_activity_header(copy_log_button),
                        # SelectionArea: drag across entries with the mouse, then Ctrl+C.
                        ft.Stack([activity_empty_text, ft.SelectionArea(content=activity_feed, expand=True)], expand=True),
                    ],
                    spacing=10,
                    expand=True,
                ),
            )
        ],
        expand=_COL2_FLEX,
    )

    # -- column 3: confirmations + skills + webcam/quick commands ----
    # Step 5: column 3 brought up to the same spacing rhythm as columns 1/2
    # (title row, then an explicit Divider before body content, matching
    # the pattern column2's activity panel already uses) plus the
    # PRIORITY badge the locked design spec calls for on Confirmation
    # Queue -- same bordered-pill treatment as the Modules panel's status
    # badges, in STATUS_ELEVATED to match CONFIRM_BG/CONFIRM_BORDER's amber.
    priority_badge = ft.Container(
        content=ft.Text("PRIORITY", font_family=theme.FONT_ORBITRON, size=9, color=theme.STATUS_ELEVATED, weight=ft.FontWeight.W_700, style=ft.TextStyle(letter_spacing=1)),
        padding=ft.Padding(6, 2, 6, 2),
        border=_border_all(1, theme.STATUS_ELEVATED),
        border_radius=8,
    )
    confirmation_column = ft.Column([_confirmation_empty_state()], spacing=8)
    confirmation_panel = ft.Container(
        content=ft.Column(
            [
                # No Divider here -- column 1's panels (the actual named
                # reference for this rhythm) are just title + spacing=10 +
                # body, no divider between them. The divider added here in
                # an earlier round matched column 2's Live Activity divider
                # instead, which separates two different content blocks
                # within one panel, not title-from-body -- wrong reference.
                #
                # Found while auditing this panel (not one of the two
                # requested items, but a real defect in what's being
                # touched): the PRIORITY badge had gone completely invisible
                # -- "CONFIRMATION QUEUE" plus the badge together don't fit
                # column 3's actual (narrower, post-proportions-change)
                # width, and Flet/Flutter's Row just silently drops the
                # overflowing child instead of erroring. Confirmed via a
                # cropped screenshot of the live panel: the badge was gone,
                # not just clipped. Fixed by letting the title shrink
                # (expand + ellipsis) so the fixed-size badge always has
                # room and stays visible.
                ft.Row(
                    [
                        ft.Text(
                            "CONFIRMATION QUEUE",
                            font_family=theme.FONT_ORBITRON,
                            size=13,
                            weight=ft.FontWeight.W_700,
                            color=theme.TEXT_SECONDARY,
                            style=ft.TextStyle(letter_spacing=2),
                            expand=True,
                            max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        priority_badge,
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                confirmation_column,
            ],
            spacing=10,
        ),
        bgcolor=theme.CONFIRM_BG,
        border=_border_all(1, theme.CONFIRM_BORDER),
        border_radius=10,
        padding=14,
        animate=ft.Animation(900, ft.AnimationCurve.EASE_IN_OUT),
    )

    # F6 Internet panel. R3: shows LOCAL /
    # ONLINE / LIMITED until HH:MM from the shared OnlineStatus -- real data.
    _initial_internet = internet_display(online_status.snapshot())
    internet_dot = ft.Container(
        width=10,
        height=10,
        border_radius=5,
        bgcolor=_initial_internet.color,
        shadow=ft.BoxShadow(blur_radius=8, color=_initial_internet.color),
    )
    internet_value_text = ft.Text(
        _initial_internet.value,
        font_family=theme.FONT_MONO,
        size=15,
        weight=ft.FontWeight.W_700,
        color=_initial_internet.color,
    )
    internet_detail_text = ft.Text(_initial_internet.detail, font_family=theme.FONT_RAJDHANI, size=12, color=theme.TEXT_DIM)
    def show_internet(shown) -> None:
        # Runs on whichever thread changed OnlineStatus (e.g. the Groq
        # call's), so the panel switches immediately, not on a tick.
        with ui_lock:
            internet_value_text.value = shown.value
            internet_value_text.color = shown.color
            internet_detail_text.value = shown.detail
            internet_dot.bgcolor = shown.color
            internet_dot.shadow = ft.BoxShadow(blur_radius=8, color=shown.color)
        push_update()

    internet_binding = InternetPanelBinding(online_status, show_internet)

    internet_panel = _panel(
        content=ft.Column(
            [
                _panel_title("Internet"),
                ft.Row([internet_dot, internet_value_text], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                internet_detail_text,
            ],
            spacing=10,
        )
    )

    # F9: the live preview while "turn on the camera" is running -- the
    # latest small JPEG straight from memory (raw bytes, never a file);
    # otherwise the camera-off note.
    webcam_off = ft.Column(
        [ft.Icon(ft.Icons.VIDEOCAM_OFF, color=theme.TEXT_DIM, size=28),
         ft.Text("Camera off. Say 'turn on the camera'.", font_family=theme.FONT_RAJDHANI, size=11, color=theme.TEXT_DIM, text_align=ft.TextAlign.CENTER)],
        alignment=ft.MainAxisAlignment.CENTER,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
    )
    webcam_image = ft.Image(src=b"", fit=ft.BoxFit.CONTAIN, border_radius=6, gapless_playback=True)
    webcam_area = ft.Container(expand=True, alignment=ft.Alignment.CENTER, content=webcam_off)
    shown_frame: dict = {"jpeg": None}

    def apply_webcam_frame() -> bool:
        """Caller holds ui_lock. True if the panel changed."""
        stream = vision.stream if vision is not None else None
        jpeg = stream.latest_jpeg() if stream is not None and stream.running else None
        if jpeg is shown_frame["jpeg"]:
            return False
        shown_frame["jpeg"] = jpeg
        if jpeg is None:
            webcam_area.content = webcam_off
        else:
            webcam_image.src = jpeg
            webcam_area.content = webcam_image
        return True

    webcam_panel = _panel(
        expand=True,
        content=ft.Column(
            [
                _panel_title("Webcam Feed"),
                webcam_area,
            ],
            spacing=10,
            expand=True,
        ),
    )

    def quick_submit(text: str) -> None:
        threading.Thread(target=orchestrator.handle_typed_command, args=(text,), daemon=True).start()

    def quick_find(e=None) -> None:
        # "Find a File" has no single fixed query to submit one-click --
        # unlike the other two, a real search needs a real name. Prefills
        # the same command box; pressing Enter from there goes through the
        # exact same orchestrator path as any other typed command, same as
        # the other two buttons' direct submits.
        #
        # Deliberately NOT calling command_box.focus() -- it's async in
        # this Flet version (1.0.0) and not essential; the ui_lock below
        # is the fix for the actual crash this button reproducibly caused
        # (see ui_lock's own comment), not the focus call, which was ruled
        # out by testing a plain sync version without it -- that crashed
        # too, until ui_lock was added.
        with ui_lock:
            command_box.value = "find "
        _schedule_page_update(page, ui_lock)

    # Not expand=True -- unlike webcam_panel (a feed area that benefits
    # from filling remaining space), this is just three buttons and should
    # size to its own content, not stretch to fill a share of the column.
    quick_commands_panel = _panel(
        content=ft.Column(
            [
                _panel_title("Quick Commands"),
                _quick_command_button("Introduce Yourself", ft.Icons.WAVING_HAND, lambda e: quick_submit("introduce yourself")),
                _quick_command_button("Open Chrome", ft.Icons.LANGUAGE, lambda e: quick_submit("open chrome")),
                _quick_command_button("Find a File", ft.Icons.SEARCH, quick_find),
            ],
            spacing=10,
        ),
    )

    # Structural fix (locked design): column 3 is ONE single column with
    # all four panels stacked top to bottom -- Confirmation Queue, Internet
    # (replaced Skills Registry), Webcam Feed, Quick Commands --
    # all the same width, not Webcam/Quick Commands split into a
    # side-by-side Row sub-layout.
    column3 = ft.Column(
        [
            confirmation_panel,
            internet_panel,
            webcam_panel,
            quick_commands_panel,
        ],
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        spacing=12,
        expand=_COL3_FLEX,
    )

    body = ft.Container(
        content=ft.Row([column1, ft.VerticalDivider(color=theme.PANEL_BORDER), column2, ft.VerticalDivider(color=theme.PANEL_BORDER), column3], expand=True, spacing=12),
        padding=ft.Padding(20, 16, 20, 0),
        expand=True,
        gradient=ft.RadialGradient(center=ft.Alignment.CENTER, radius=1.4, colors=[theme.BG_VIGNETTE, theme.BG_BASE]),
    )

    page.add(ft.Column([top_bar_container, body, bottom_bar], spacing=0, expand=True))

    # F2 missing-setup notes (voice output not set up, Zira missing): shown
    # once at startup in the same notice style as the sensor toasts, so the
    # developer sees exactly what to fix even before saying anything.
    if notices:
        with ui_lock:
            page.show_dialog(
                ft.SnackBar(
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.INFO_OUTLINE, color=theme.STATUS_ELEVATED, size=20),
                            ft.Text(
                                "\n".join(notices),
                                font_family=theme.FONT_RAJDHANI,
                                size=15,
                                weight=ft.FontWeight.W_600,
                                color=theme.TEXT_PRIMARY,
                                expand=True,
                            ),
                        ],
                        spacing=10,
                    ),
                    bgcolor=theme.CONFIRM_BG,
                    behavior=ft.SnackBarBehavior.FLOATING,
                    width=640,
                    duration=ft.Duration(seconds=int(SETTINGS["dashboard"]["startup_notice_seconds"])),
                    show_close_icon=True,
                    elevation=6,
                )
            )

    # -- refresh loop (the dashboard's own thread; see module docstring) --
    net_sampler = NetworkRateSampler()
    state_display = StateDisplay(
        controller.state,
        min_dwell=_STATE_DWELL_MIN,
        max_dwell=_STATE_DWELL_MAX,
        chain_budget=_STATE_CHAIN_BUDGET,
    )
    feed_tracker = NewRecordTracker()
    live: dict = {"id": None, "row": None, "lines": None}  # the in-progress command's row on screen
    try:
        _DIAG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DIAG_LOG_PATH.write_text("", encoding="utf-8")  # fresh log per launch
    except OSError:
        pass

    def clock_loop() -> None:
        """Its own thread, waking on each whole-second boundary and pushing
        just the two clock Texts. Kept out of refresh_loop on purpose: that
        loop ticks on its own schedule, so a clock read inside it showed
        whatever second it happened to land on -- correct values, but
        appearing 0.7-1.2s apart instead of a steady 1.0s. Waking at the
        boundary itself makes every change exactly one second after the
        last."""
        while not window_closed.is_set():
            now = datetime.now()
            with ui_lock:
                top_date_text.value = now.strftime("%a, %b %d %Y").upper()
                top_clock_text.value = now.strftime("%H:%M:%S")
            push_update()
            # +2ms so the wake-up lands just after the boundary, never just before it.
            time.sleep(max(0.001, 1.0 - (time.time() % 1.0) + 0.002))

    def refresh_loop() -> None:
        interval = float(SETTINGS["dashboard"]["refresh_interval_seconds"])
        slow_interval = float(SETTINGS["dashboard"]["slow_panels_interval_seconds"])
        glow_interval = float(SETTINGS["dashboard"]["stop_glow_interval_seconds"])
        stop_glow_on = False
        next_slow = next_glow = next_internet_check = 0.0
        memory_warning = LowMemoryWarning()
        last_pending_key: str | None = None
        while True:
            # Any exception in a tick is written to the diag log (there is no
            # console under pythonw.exe) instead of silently killing this
            # daemon thread, which would freeze every value on screen.
            if window_closed.is_set():
                return
            dirty = False
            warning: str | None = None
            # R15: no animation work while the window is minimised or hidden.
            paused = visibility.hidden
            try:
                with ui_lock:
                    # page.update() is only sent when something actually changed:
                    # the loop now ticks fast (so a state change shows within a
                    # tick), and sending a full update 20x a second when nothing
                    # moved would just load the client for no reason.
                    now = time.monotonic()
                    try:
                        # -- controller state --
                        # Every real transition arrives via the bridge (the
                        # controller's listener pushes each one); StateDisplay
                        # replays them in order, each held briefly, so a command
                        # that flashes through LISTENING/UNDERSTANDING/SPEAKING
                        # in milliseconds is still visible.
                        state_display.feed(bridge.drain())
                        state = state_display.current(controller.state)
                        if state_pill_text.value != state.name:
                            state_pill_text.value = state.name
                            state_pill_text.color = _STATE_COLORS.get(state, theme.TEXT_SECONDARY)
                            state_pill.border = _border_all(1, _STATE_COLORS.get(state, theme.CHIP_BORDER))
                            dirty = True

                        # -- confirmation queue: the real pending confirmation, if any --
                        # Rebuilt only when it changes -- replacing these controls
                        # every tick made the client re-create them constantly.
                        pending = controller.pending_confirmation()
                        pending_key = (
                            None
                            if pending is None
                            else repr((pending.action_name, dict(pending.params), pending.confirmations_received))
                        )
                        if pending_key != last_pending_key:
                            confirmation_column.controls = _confirmation_rows(pending, screen, system, files)
                            last_pending_key = pending_key
                            dirty = True

                        # -- F8/F9 capture rows: on only while a capture runs --
                        if apply_capture_rows():
                            dirty = True
                        if not paused and apply_webcam_frame():  # R15: no preview updates while hidden
                            dirty = True

                        # -- waveform: real levels only while a voice command
                        # is being recorded (LISTENING), never in WAITING --
                        if not paused:
                            levels = voice.levels() if voice is not None and controller.state == State.LISTENING else []
                            if apply_waveform(levels):
                                dirty = True

                        # -- STOP breathing glow (paused while hidden, R15) --
                        if not paused and now >= next_glow:
                            stop_glow_on = not stop_glow_on
                            stop_button.shadow.blur_radius = 18 if stop_glow_on else 6
                            next_glow = now + glow_interval
                            dirty = True

                        # -- gauges + network (slower cadence: nothing here needs 20 Hz) --
                        if now >= next_slow:
                            next_slow = now + slow_interval
                            dirty = True
                            # R15 fallback, in case a minimise/restore event was missed.
                            visibility.poll(getattr(page.window, "minimized", None), getattr(page, "app_visible", None))
                            apply_mic_status()  # e.g. Windows' mic block found a moment after start
                            gauges = read_gauges()
                            if gauges.available:
                                cpu_value.value = f"{gauges.cpu_percent:.0f}%"
                                cpu_bar.value = gauges.cpu_percent / 100
                                cpu_status.value = cpu_status_word(gauges).upper()
                                cpu_status.color = (
                                    theme.STATUS_ELEVATED if cpu_status_word(gauges) == "elevated" else theme.STATUS_OK
                                )

                                ram_value.value = f"{gauges.memory_percent:.0f}%"
                                ram_bar.value = gauges.memory_percent / 100
                                ram_status.value = ram_status_word(gauges).upper()
                                ram_status.color = (
                                    theme.STATUS_ELEVATED if ram_status_word(gauges) == "elevated" else theme.STATUS_OK
                                )

                                disk_value.value = f"{gauges.disk_percent:.0f}%"
                                disk_bar.value = gauges.disk_percent / 100
                                disk_status.value = disk_status_word(gauges).upper()
                                disk_status.color = (
                                    theme.STATUS_ELEVATED if disk_status_word(gauges) == "elevated" else theme.STATUS_OK
                                )

                                low = is_memory_low(gauges)
                                if low:  # the gauge turns red at once
                                    ram_status.value = "LOW MEMORY"
                                    ram_status.color = theme.STATUS_CRITICAL
                                if memory_warning.sample(low):  # the warning waits (R15)
                                    warning = low_memory_warning_text(gauges)
                            else:
                                # R14: the tile only has room for a word; the full
                                # missing-setup message is the startup notice and
                                # each tile's tooltip (see gauges_setup_message).
                                cpu_value.value = ram_value.value = disk_value.value = "--"
                                cpu_status.value = ram_status.value = disk_status.value = "PSUTIL MISSING"
                                cpu_status.color = ram_status.color = disk_status.color = theme.STATUS_ELEVATED

                            rates = net_sampler.sample()
                            if rates.available:
                                net_up_value.value = _format_rate(rates.upload_kbps)
                                net_down_value.value = _format_rate(rates.download_kbps)
                            else:
                                net_up_value.value = net_down_value.value = "--"

                        # -- activity feed: real F5 decision records, newest at bottom --
                        new_records = feed_tracker.take_new(logbook.records())
                        if new_records:
                            # Newest-first in the list, to match the reversed ListView
                            # above (see its comment) -- each new record goes at index
                            # 0, oldest end up at the tail and are trimmed there.
                            for record in new_records:
                                final_row = _activity_row(format_record(record))
                                shown = live["row"]
                                if live["id"] == record.id and shown in activity_feed.controls:
                                    # This command was already on screen as a live row:
                                    # it becomes its final row, in place.
                                    activity_feed.controls[activity_feed.controls.index(shown)] = final_row
                                else:
                                    activity_feed.controls.insert(0, final_row)
                                if live["id"] == record.id:
                                    live.update(id=None, row=None, lines=None)
                            if len(activity_feed.controls) > _ACTIVITY_FEED_MAX_ROWS:
                                del activity_feed.controls[_ACTIVITY_FEED_MAX_ROWS:]
                            dirty = True

                        # The command still running: show it now, and keep adding the
                        # steps it really reports ("searching files...", "found 2
                        # matches") to that same row as they happen.
                        in_progress = logbook.live_snapshot()
                        if in_progress is not None and in_progress.heard:
                            lines = format_record(in_progress).lines
                            if live["id"] != in_progress.id:
                                live.update(id=in_progress.id, row=_activity_row(format_record(in_progress)), lines=lines)
                                activity_feed.controls.insert(0, live["row"])
                                dirty = True
                            elif lines != live["lines"] and live["row"] in activity_feed.controls:
                                new_row = _activity_row(format_record(in_progress))
                                activity_feed.controls[activity_feed.controls.index(live["row"])] = new_row
                                live.update(row=new_row, lines=lines)
                                dirty = True
                        activity_empty_text.visible = len(activity_feed.controls) == 0
                    except Exception:
                        _diag_log("refresh_loop panel update raised:\n" + traceback.format_exc())
                        dirty = True

                if dirty:
                    push_update()
                if warning is not None and warn is not None:
                    warn(warning)  # outside ui_lock: it shows a notice and speaks
                # Internet panel: changes arrive as events (show_internet);
                # this only catches a LIMITED cooldown running out. Outside
                # ui_lock -- the binding takes ui_lock itself.
                if now >= next_internet_check:
                    internet_binding.refresh()
                    next_internet_check = now + slow_interval
            except Exception:
                _diag_log("refresh_loop tick raised:\n" + traceback.format_exc())
            time.sleep(interval)

    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=clock_loop, daemon=True).start()


def _format_rate(kbps: float) -> str:
    if kbps >= 1024:
        return f"{kbps / 1024:.1f} MB/s"
    return f"{kbps:.0f} KB/s"


def _confirmation_empty_state() -> ft.Column:
    """Item 4: three lines, each a step down in size/weight from the last
    -- main line, then a smaller secondary status line, then a smaller
    still helper caption explaining the stacking-by-priority behavior.
    Shared by build()'s initial content and _confirmation_rows() below so
    the two can't drift out of sync with each other."""
    return ft.Column(
        [
            ft.Text("No pending actions", font_family=theme.FONT_RAJDHANI, size=13, color=theme.TEXT_DIM),
            ft.Text("awaiting input", font_family=theme.FONT_RAJDHANI, size=11, color=theme.TEXT_DIM),
            ft.Text(
                "multiple actions stack here, highest risk first",
                font_family=theme.FONT_RAJDHANI,
                size=10,
                color=theme.TEXT_DIM,
                italic=True,
            ),
        ],
        spacing=2,
    )


def _screen_preview_rows(preview) -> List[ft.Control]:
    """F8's R4 preview: the exact image (in memory, raw JPEG bytes -- never a
    file) and the exact text that will be sent, plus the two warnings."""
    rows: List[ft.Control] = []
    if preview.jpeg is not None:
        rows.append(ft.Image(src=preview.jpeg, height=110, fit=ft.BoxFit.CONTAIN, border_radius=4))
        rows.append(ft.Text(f"Image as sent: {preview.size[0]}x{preview.size[1]} JPEG, {len(preview.jpeg) // 1024} KB. Black = protected windows.",
                            font_family=theme.FONT_RAJDHANI, size=11, color=theme.TEXT_DIM))
    rows.append(ft.Text("Text that will be sent:", font_family=theme.FONT_RAJDHANI, size=11, color=theme.TEXT_SECONDARY))
    rows.append(ft.Container(
        height=90,
        content=ft.Column([ft.Text(preview.text or "(no text read)", font_family=theme.FONT_MONO, size=10, color=theme.TEXT_SECONDARY, selectable=True)],
                          scroll=ft.ScrollMode.AUTO),
    ))
    rows.append(ft.Text(f"Your question: {preview.question}", font_family=theme.FONT_RAJDHANI, size=11, color=theme.TEXT_SECONDARY))
    rows.append(ft.Text("This will be sent online.", font_family=theme.FONT_RAJDHANI, size=12, weight=ft.FontWeight.W_700, color=theme.STATUS_ELEVATED))
    if not bool(SETTINGS["screen"]["online_terms_checked"]):
        rows.append(ft.Text("Online service terms not yet checked by you", font_family=theme.FONT_RAJDHANI, size=11, color=theme.STATUS_ELEVATED))
    return rows


def _file_preview_rows(preview) -> List[ft.Control]:
    """F19's R4 preview: the file's name, size and the EXACT text that will
    be sent (read from the temporary copy that is sent), plus the warnings."""
    limit = int(SETTINGS["file_processor"]["max_text_preview_chars"])
    part = f", the {preview.part}" if preview.part else ""
    rows: List[ft.Control] = [
        ft.Text(f"{preview.name}{part}: {preview.chars:,} characters, sha256 {preview.sha256[:12]}", font_family=theme.FONT_RAJDHANI,
                size=11, color=theme.TEXT_SECONDARY),
        ft.Text("Text that will be sent:", font_family=theme.FONT_RAJDHANI, size=11, color=theme.TEXT_SECONDARY),
        ft.Container(height=120, content=ft.Column([ft.Text(preview.text(limit), font_family=theme.FONT_MONO, size=10,
                                                            color=theme.TEXT_SECONDARY, selectable=True)], scroll=ft.ScrollMode.AUTO)),
        ft.Text("This will be sent online.", font_family=theme.FONT_RAJDHANI, size=12, weight=ft.FontWeight.W_700,
                color=theme.STATUS_ELEVATED),
    ]
    if not bool(SETTINGS["online"]["terms_checked"]):
        rows.append(ft.Text("Online service terms not yet checked by you", font_family=theme.FONT_RAJDHANI, size=11,
                            color=theme.STATUS_ELEVATED))
    return rows


def _shutdown_preview_rows(titles: List[str]) -> List[ft.Control]:
    """F22: what is open, so he can see what a restart would close (read
    with F8's read-only window listing; protected windows aren't named)."""
    rows: List[ft.Control] = [ft.Text("Open now:" if titles else "No other windows are open.", font_family=theme.FONT_RAJDHANI,
                                      size=11, color=theme.TEXT_SECONDARY)]
    rows.extend(ft.Text(f"- {title}", font_family=theme.FONT_MONO, size=10, color=theme.TEXT_SECONDARY) for title in titles[:12])
    return rows


def plain_param_lines(params: dict) -> List[str]:
    """A pending lock showed "{'display_name': 'lock
    the screen'}" -- a raw dict. The card reads as words: what it is, then
    any other detail as "name: value"."""
    lines = []
    name = str(params.get("display_name") or "").strip()
    if name:
        lines.append(name[0].upper() + name[1:])
    for key, value in params.items():
        if key != "display_name" and value not in (None, "", [], {}):
            lines.append(f"{key.replace('_', ' ')}: {value}")
    return lines


def _confirmation_rows(pending: PendingConfirmation | None, screen: ScreenCommands | None = None,
                       system: SystemCommands | None = None, files: FileCommands | None = None) -> List[ft.Control]:
    # PendingConfirmation is currently a single slot on Controller (F7's
    # own model) -- this always renders at most one card today. Built as
    # a list, highest trust level first, so it needs no rework if F7 ever
    # supports multiple concurrent pending confirmations.
    if pending is None:
        return [_confirmation_empty_state()]
    params = dict(pending.params)
    preview = screen.preview(str(params.get("preview_id"))) if screen is not None and pending.action_name == "send_screen_online" else None
    if preview is not None:
        details = _screen_preview_rows(preview)
    elif pending.action_name == FILE_SEND_ACTION and files is not None and files.preview(str(params.get("preview_id"))) is not None:
        details = _file_preview_rows(files.preview(str(params.get("preview_id"))))
    elif pending.action_name in ("restart", "shutdown") and system is not None:
        details = _shutdown_preview_rows(system.preview_titles)
    else:
        details = [ft.Text(line, font_family=theme.FONT_MONO, size=11, color=theme.TEXT_SECONDARY) for line in plain_param_lines(params)]
    return [
        ft.Column(
            [
                ft.Text(pending.action_name.replace("_", " "), font_family=theme.FONT_ORBITRON, size=13, weight=ft.FontWeight.W_700,
                        color=theme.TEXT_PRIMARY),
                *details,
                ft.Text(
                    f"{pending.trust_level.name} -- {pending.confirmations_received}/{pending.confirmations_required} confirmed. "
                    "Say or type 'confirm'.",
                    font_family=theme.FONT_RAJDHANI,
                    size=12,
                    color=theme.STATUS_ELEVATED,
                ),
            ],
            spacing=2,
        )
    ]


def _activity_row(row) -> ft.Container:
    """Renders format_record()'s clean step sequence (heard -> searching ->
    opening ...) as one entry with one line per step."""
    mark = ft.Icon(ft.Icons.CHECK_CIRCLE, color=theme.STATUS_OK, size=13) if row.verified else ft.Container(width=13)
    header = ft.Row(
        [ft.Text(row.time_label, font_family=theme.FONT_MONO, size=11, color=theme.TEXT_DIM), mark],
        spacing=8,
    )
    body_lines = [
        ft.Text(line, font_family=theme.FONT_MONO, size=12, color=theme.TEXT_SECONDARY) for line in row.lines
    ]
    return ft.Container(
        content=ft.Column([header, *body_lines], spacing=2),
        padding=ft.Padding(0, 0, 0, 8),
        border=ft.Border(bottom=ft.BorderSide(1, theme.PANEL_BORDER)),
    )


def wait_until_idle(controller, timeout: float, poll: float = 0.1) -> bool:
    """True once she is AWAKE or SLEEPING (no command being heard, worked on
    or answered); False if that doesn't happen within `timeout`."""
    deadline = time.monotonic() + timeout
    while controller.state not in (State.AWAKE, State.SLEEPING):
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
    return True


def _show_splash(page: ft.Page) -> None:
    """R15: the first thing the window shows -- dark #03101A,
    a cyan orb, "J.A.R.V.I.S." in Orbitron and "starting…" -- instead of an
    empty window titled "Flet" while the rest loads. The title bar is blanked
    in the same update. Same window size as the dashboard, so nothing jumps."""
    page.title = ""
    page.bgcolor = theme.BG_BASE
    page.padding = 0
    page.fonts = theme.FONT_PATHS
    page.window.min_width = _WINDOW_MIN_WIDTH
    page.window.min_height = _WINDOW_MIN_HEIGHT
    page.window.width = max(1280, _WINDOW_MIN_WIDTH)
    page.window.height = max(820, _WINDOW_MIN_HEIGHT)
    page.window.maximized = False
    page.window.icon = "icon/jarvis.ico"
    orb = ft.Container(width=120, height=120, border_radius=60, bgcolor=theme.CYAN_ACCENT,
                       shadow=ft.BoxShadow(blur_radius=60, color=theme.CYAN_ACCENT))
    page.add(
        ft.Container(
            expand=True,
            alignment=ft.Alignment.CENTER,
            gradient=ft.RadialGradient(center=ft.Alignment.CENTER, radius=1.4, colors=[theme.BG_VIGNETTE, theme.BG_BASE]),
            content=ft.Column(
                [
                    orb,
                    ft.Container(height=24),
                    ft.Text("J.A.R.V.I.S.", font_family=theme.FONT_ORBITRON, size=30, weight=ft.FontWeight.W_800, color=theme.TEXT_PRIMARY,
                            style=ft.TextStyle(letter_spacing=8, shadow=[ft.BoxShadow(blur_radius=16, color=theme.CYAN_ACCENT)])),
                    ft.Text(STARTING, font_family=theme.FONT_RAJDHANI, size=16, color=theme.TEXT_DIM),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                tight=True,
            ),
        )
    )


def main(page: ft.Page) -> None:
    """F6: Flet's entry point. Shows the splash, builds the components and the dashboard, then starts voice
    in the background (R15)."""
    startup_timing.mark("window connected")
    _show_splash(page)
    startup_timing.mark("splash shown")
    app = make_app_components()
    startup_timing.mark("components ready")
    notice_hook: dict = {"show": None}

    def notify(message: str) -> None:
        """Missing-setup rule: she says it and the dashboard shows it."""
        logger.info("Notice: %s", message)
        show = notice_hook.get("show")
        if show is not None:
            show(message)
        threading.Thread(target=speak_when_idle, args=(message,), daemon=True).start()

    def speak_when_idle(message: str) -> None:
        # The low-memory notice was spoken while he was
        # giving a command; her voice muted the microphone and the command was
        # lost. A notice waits (shown at once) until she is AWAKE or asleep.
        if wait_until_idle(app.controller, float(SETTINGS["dashboard"]["notice_wait_seconds"])):
            app.speech.speak(message)

    def on_stop_hotkey() -> None:
        # R8: cut speech at once, then the controller's top-priority Stop.
        app.speech.stop()
        app.controller.request_stop(source=f"Stop hotkey ({SETTINGS['hotkeys']['stop']})")

    def show_only(message: str) -> None:
        # Typed text while asleep: a notice on screen, nothing spoken or logged (F15).
        show = notice_hook.get("show")
        if show is not None:
            show(message)

    app.orchestrator.set_notice(show_only)

    def wake(source: str) -> None:
        # Greeting speech blocks, so it runs off the audio/hotkey thread.
        threading.Thread(target=app.orchestrator.wake, args=(source,), name="jarvis-wake", daemon=True).start()

    # R15: voice -- the wake-word model (2.4 s on this laptop),
    # the microphone and the hotkeys -- starts in the background after the
    # dashboard is up; until then the Microphone row says "starting…".
    voice = BackgroundVoice()
    memory_note = app.memory.store.load_note if app.memory is not None else None
    notices = [note for note in (app.speech.status.note, gauges_setup_message(), memory_note) if note]

    # Clean shutdown when the window is closed: stop the microphone,
    # unregister the hotkeys, stop the threads and release the models
    # before the window goes away. Set BEFORE build(), so these go out with
    # build's own first update: an extra page.update() after build() was
    # seen to minimise the window a few seconds after launch.
    page.window.prevent_close = True

    visibility = WindowVisibility()  # R15: animations pause while minimised/hidden

    async def on_window_event(e) -> None:
        visibility.on_window_event(e.type)
        if e.type == ft.WindowEventType.CLOSE:
            await asyncio.to_thread(shutdown_app, app, voice)
            await page.window.destroy()

    page.window.on_event = on_window_event
    page.controls.clear()  # the splash; build() adds the dashboard in its place
    build(
        page, app.controller, app.orchestrator, app.logbook, app.bridge, app.online_status, notices,
        voice=voice, notice_hook=notice_hook, visibility=visibility, warn=notify,
        sensors=app.sensors, screen=app.screen, vision=app.vision, system=app.system, files=app.files,
    )
    startup_timing.mark("dashboard built")
    startup_timing.write()
    for spoken_note in (app.speech.status.note, memory_note):
        if spoken_note:
            # Missing-setup rule: the note was shown above; she says it too.
            threading.Thread(target=app.speech.speak, args=(spoken_note,), daemon=True).start()

    def voice_ready(system) -> None:
        # F2: say and show each voice setup problem once (mic fallback,
        # missing model, hotkey taken).
        for note in (system.notes if system is not None else []):
            notify(note)
        # Speech-to-text's libraries are no longer imported
        # before the microphone opens; they load here, after "voice ready",
        # so the first command doesn't wait for them.
        try:
            import importlib

            importlib.import_module("faster_whisper")
        except Exception:
            logger.info("faster_whisper not importable; the voice notes above say what to do")
        # Fixed lines (intro, greetings, ...) are pre-rendered now that the
        # startup work is done, so they start at once (voice cache).
        render_cache = getattr(app.speech, "render_cache", None)
        if render_cache is not None:
            render_cache()

    start_in_background(
        voice,
        start=lambda: start_voice(
            app.controller,
            app.orchestrator.handle_command,
            on_stop_hotkey,
            notify=notify,
            wake=wake,
            heard_while_asleep=app.logbook.record_heard_while_asleep,
            ignored=app.logbook.record_ignored,
            is_speaking=app.speech.is_speaking,
            not_confirm_or_cancel=app.orchestrator.remind_confirm_or_cancel,
            pick_pending=app.orchestrator.picks.active,
        ),
        report=app.logbook.record_status,
        file_index=app.file_index,
        store_apps=open_app.STORE_APPS,
        on_ready=voice_ready,
    )


def shutdown_app(app: AppComponents, voice: VoiceSystem | None) -> None:
    """Stop everything that holds a device, a hotkey, a thread or a model.
    Each step is independent: one failing never stops the rest."""
    steps = [
        ("voice input", voice.close if voice is not None else None),
        ("speech", app.speech.stop),
        ("speech engine", app.speech.close),
        ("file index", app.file_index.stop),
        ("camera", app.vision.stop_stream if getattr(app, "vision", None) is not None else None),
        ("controller", app.controller.shutdown),
    ]
    # Error handling: each shutdown step has its own try/except, so one failure never stops the rest.
    for name, step in steps:
        if step is None:
            continue
        try:
            step()
        except Exception:
            logger.exception("Shutdown step failed: %s", name)
    logger.info("JARVIS shut down cleanly")


def run() -> None:
    """Open the dashboard window (blocks until it is closed)."""
    ft.run(main, assets_dir=theme.ASSETS_DIR)
