"""Conversation mode (developer-approved; merges F2 and F15):
ties the microphone, the wake word and speech-to-text to the controller's
state.

What happens to microphone audio depends only on the state:
- While she is SPEAKING, and for [speech] echo_tail_seconds afterwards:
  nothing. Every frame is dropped, so she never responds to her own voice.
- SLEEPING (the startup state): the wake-word model listens for "Hey
  JARVIS" -- on every frame, including while an utterance is being
  recorded (before it was given nothing once speech started, so
  it could never fire on the phrase itself) -- and each utterance is
  transcribed locally and shown in the activity log as "heard while
  asleep: ..." -- nothing is acted on, nothing goes online, nothing is
  kept beyond the session (F15). A transcript that is a wake phrase
  ("wake up JARVIS", fuzzily: "Wake up, Charvis") wakes her.
- Noisy microphones: the speech threshold rises above the recent
  background level ([listen] noise_floor_*), so the end of speech is still
  found; asleep recordings stop at [listen] max_asleep_utterance_seconds.
- AWAKE: no wake word needed. When speech starts she goes to LISTENING,
  records until [listen] end_silence_seconds of quiet (or
  [listen] max_command_seconds), transcribes, and runs it as a command.
  Fragments -- fewer than [conversation] min_words words (unless a known
  one-word command), or Whisper's no-speech probability above
  [conversation] max_no_speech_prob -- are logged as ignored, never sent
  online. A spoken "stop" is a top-priority Stop (R8).
- CONFIRMING: only a confirm or a cancel is acted on (Section 5 still
  applies): "confirm", "cancel", or a sentence whose only command words
  are confirm words or cancel words ("Please send it online. Confirm.",
  Anything else is logged as ignored, and she says "Say
  confirm or cancel." (at most once per [conversation]
  confirm_reminder_seconds;).
- Any other state: nothing.

Recordings live in memory only and are discarded after transcription. The
waveform levels exist only while a command is being recorded (LISTENING).

This module never changes state itself (R6): it submits events and the
controller's table decides.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from jarvis.actions.command_text import confirm_intent
from jarvis.actions.sleep_wake import is_wake_phrase, normalize
from jarvis.controller import Controller, Event, EventType, State
from jarvis.controller.controller import TransitionLogEntry
from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)

_LISTEN = SETTINGS["listen"]
_AUDIO = SETTINGS["audio"]
_SPEECH = SETTINGS["speech"]
_CONVERSATION = SETTINGS["conversation"]

SOURCE_VOICE = "voice"
SOURCE_WAKE_WORD = "wake word"
SOURCE_PUSH_TO_TALK = "push-to-talk"
SOURCE_HOTKEY = "wake hotkey"

MODE_ASLEEP = "asleep"
MODE_AWAKE = "awake"
MODE_CONFIRMING = "confirming"

# int16 RMS that fills a waveform bar.
_FULL_SCALE_RMS = 4000.0


def frame_rms(frame: Any) -> float:
    """The loudness (root mean square) of one audio frame."""
    import numpy as np

    if len(frame) == 0:
        return 0.0
    samples = np.asarray(frame, dtype=np.float32)
    return float(np.sqrt(np.mean(samples * samples)))


def fragment_reason(text: str, no_speech_prob: float) -> Optional[str]:
    """Why an awake utterance is not a command, or None if it is one."""
    words = normalize(text).split()
    if not words:
        return "nothing understood"
    if no_speech_prob > float(_CONVERSATION["max_no_speech_prob"]):
        return "unclear"
    first = text.strip().split()[0] if text.strip() else ""
    if (first[:1].islower() and first.strip(",.").lower() in {str(w) for w in _CONVERSATION["mid_sentence_starts"]}
            and not _order_after_leads(text)):
        # "and other videos.",
        # "and I thought since the interview at this." went online. Whisper
        # writes a lower-case "and" only when it heard the middle of a
        # sentence -- someone else's, never a command he started.
        return "mid-sentence"
    singles = {normalize(w) for w in _CONVERSATION["single_word_commands"]}
    if len(words) < int(_CONVERSATION["min_words"]) and " ".join(words) not in singles and not _is_app_name(words):
        return "too short"
    return None


def _order_after_leads(text: str) -> bool:
    """Whisper wrote "Can you open ChatGPT?" as
    "and you open ChatGPT." and it was dropped as mid-sentence -- silently.
    It still counts as his command when ONLY lead words ([interpreter]
    command_leads: "and", "you", "can", "please" ...) come before a command
    verb; someone else's sentence that merely contains a verb stays ignored."""
    interp = SETTINGS["interpreter"]
    leads = {str(w).lower() for w in interp["command_leads"]}
    verbs = sorted((tuple(str(v).lower().split()) for v in interp["command_verbs"]), key=len, reverse=True)
    words = [w.strip(",.!?;:").lower() for w in text.split()]
    i = 0
    while i < len(words) and words[i] in leads:
        i += 1
    return 0 < i < len(words) and any(tuple(words[i:i + len(v)]) == v for v in verbs)


def _is_app_name(words: list) -> bool:
    """"Notepad" alone was dropped as too short. A known app or
    site name on its own opens it (F3's bare names), except the words in
    [conversation] single_word_not_apps ("camera" is F9, not the Camera app)."""
    from jarvis.actions.command_text import known_names

    said = " ".join(words)
    return said in known_names() and said not in {normalize(w) for w in _CONVERSATION["single_word_not_apps"]}


class _Recording:
    """One utterance being recorded. Frames arrive from the audio thread
    through a bounded queue."""

    def __init__(self, mode: str, source: str, preroll: list) -> None:
        self.mode = mode
        self.source = source
        self.frames: "queue.Queue[Any]" = queue.Queue(maxsize=1000)
        self.cancelled = threading.Event()
        # Push-to-talk opens before anyone speaks; everything else starts
        # because speech was already heard.
        self.speech_started = source != SOURCE_PUSH_TO_TALK
        for frame in preroll:
            self.put(frame)

    def put(self, frame: Any) -> None:
        """Add one frame to the recording (dropped if the queue is full)."""
        try:
            self.frames.put_nowait(frame)
        except queue.Full:
            pass


class VoiceListener:
    """F2: the listening loop. Asleep it waits for the wake word; awake it records each utterance,
    transcribes it with Whisper and hands the text to the orchestrator. It never hears her own voice."""
    def __init__(
        self,
        controller: Controller,
        handle_command: Callable[[str, str], None],
        detector: Any = None,
        stt: Any = None,
        notify: Optional[Callable[[str], None]] = None,
        stt_missing_message: Optional[str] = None,
        wake: Optional[Callable[[str], None]] = None,
        heard_while_asleep: Optional[Callable[[str], None]] = None,
        ignored: Optional[Callable[[str, str], None]] = None,
        is_speaking: Optional[Callable[[], bool]] = None,
        clock: Callable[[], float] = time.monotonic,
        not_confirm_or_cancel: Optional[Callable[[], None]] = None,
        pick_pending: Optional[Callable[[], bool]] = None,
    ) -> None:
        """`not_confirm_or_cancel`: called when something else is heard
        while she waits for a confirmation (the orchestrator's reminder)."""
        self._controller = controller
        self._handle_command = handle_command
        self._detector = detector
        self._stt = stt
        self._notify = notify
        self._stt_missing_message = stt_missing_message
        self._wake = wake or (lambda source: None)
        self._heard_while_asleep = heard_while_asleep or (lambda text: None)
        self._ignored = ignored or (lambda text, reason: None)
        self._not_confirm_or_cancel = not_confirm_or_cancel or (lambda: None)
        # "1" after "which one?" was dropped as too short.
        self._pick_pending = pick_pending or (lambda: False)
        self._is_speaking = is_speaking or (lambda: False)
        self._clock = clock
        self._sample_rate = int(_AUDIO["sample_rate"])
        self._block = int(_AUDIO["block_size"])
        self._end_silence = float(_LISTEN["end_silence_seconds"])
        self._max_command = float(_LISTEN["max_command_seconds"])
        self._no_speech_timeout = float(_LISTEN["no_speech_timeout_seconds"])
        self._speech_rms = float(_LISTEN["speech_rms_threshold"])
        self._echo_tail = float(_SPEECH["echo_tail_seconds"])
        self._max_asleep = float(_LISTEN["max_asleep_utterance_seconds"])
        self._noise_factor = float(_LISTEN["noise_floor_factor"])
        frames_per_second = self._sample_rate / self._block
        self._background: deque[float] = deque(maxlen=max(1, int(float(_LISTEN["noise_floor_seconds"]) * frames_per_second)))
        self._threshold = self._speech_rms
        self._threshold_cap = float(_LISTEN["noise_threshold_max_rms"])
        self._frames_since_floor = 0
        # Collections: a bounded deque of the last audio frames, so the start of a command is never cut off.
        self._preroll: deque = deque(maxlen=max(1, int(float(_LISTEN["preroll_seconds"]) * self._sample_rate / self._block)))
        self._recording: Optional[_Recording] = None
        self._muted_until = 0.0
        self._was_muted = False
        self._levels: deque[float] = deque(maxlen=int(_LISTEN["waveform_bars"]))
        self._levels_lock = threading.Lock()
        self.transcriptions = 0  # count only, never the words (for tests/diagnostics)
        # (seconds recorded, why the recording ended, seconds transcribing)
        # of the last utterance: numbers only, for the timing diagnosis.
        self.last_timing: Optional[tuple[float, str, float]] = None

    # -- wiring ----------------------------------------------------------------

    def set_detector(self, detector: Any) -> None:
        """Use this wake-word detector."""
        self._detector = detector

    def attach(self) -> None:
        """Start following controller transitions."""
        self._controller.add_transition_listener(self.on_transition)

    def detach(self) -> None:
        """Stop following transitions and drop any recording."""
        self._controller.remove_transition_listener(self.on_transition)
        self._cancel_recording()

    # -- audio thread ------------------------------------------------------------

    def _muted(self) -> bool:
        """Self-echo guard: while she speaks, and for the tail after."""
        # Why a tail ([speech] echo_tail_seconds) and not just "while
        # speaking": the speakers' buffer and the room's echo keep her last
        # words reaching the microphone briefly after speak() returns. Without
        # it, the end of her own sentence could be heard as a command or as
        # the wake word.
        now = self._clock()
        if self._is_speaking() or self._controller.state == State.SPEAKING:
            self._muted_until = now + self._echo_tail
        if now < self._muted_until:
            if not self._was_muted:
                self._was_muted = True
                self._preroll.clear()
                recording = self._recording
                self._cancel_recording()  # anything half-heard was her own voice
                if recording is not None:
                    # A notice spoken while a command was
                    # being recorded left her LISTENING, deaf, for 5 minutes.
                    self._finish_awake_without_command(recording, "her own voice")
            return True
        if self._was_muted:
            # Tail over: forget anything the wake model heard of her voice.
            self._was_muted = False
            if self._detector is not None:
                self._detector.reset()
        return False

    def on_frames(self, frame: Any) -> None:
        """Called from the microphone callback for every block. Must be fast
        and must never block."""
        if self._muted():
            return
        rms = frame_rms(frame)
        self._note_background(rms)  # her own voice (muted) never counts
        recording = self._recording
        if recording is not None:
            recording.put(frame)
            if recording.mode == MODE_ASLEEP and self._detector is not None:
                # The wake word must hear the phrase itself, which is
                # exactly when a recording is open.
                self._detector.feed(frame)
            if recording.mode == MODE_AWAKE:
                level = min(1.0, rms / _FULL_SCALE_RMS)
                with self._levels_lock:
                    self._levels.append(level)
            return
        state = self._controller.state
        if state == State.SLEEPING:
            if self._detector is not None:
                self._detector.feed(frame)
            self._maybe_start(frame, MODE_ASLEEP, rms)
        elif state == State.AWAKE:
            self._maybe_start(frame, MODE_AWAKE, rms)
        elif state == State.CONFIRMING:
            self._maybe_start(frame, MODE_CONFIRMING, rms)
        # Any other state: the frame is dropped.

    def speech_threshold(self) -> float:
        """Loudness that counts as speech right now: [listen]
        speech_rms_threshold, raised to noise_floor_factor x the background
        level when the microphone is noisy, never above
        noise_threshold_max_rms (so a voice always counts)."""
        return self._threshold

    def _note_background(self, rms: float) -> None:
        """Every frame heard (not her own voice) feeds a rolling window. Its
        20th-percentile loudness is the background: the gaps between words
        are near it even mid-sentence, so speech barely moves it, while a
        mic that hisses at 600 RMS all the time moves it to 600."""
        self._background.append(rms)
        self._frames_since_floor += 1
        if self._frames_since_floor < 4 or len(self._background) < self._background.maxlen // 2:
            return
        self._frames_since_floor = 0
        levels = sorted(self._background)
        floor = levels[len(levels) // 5]
        self._threshold = min(max(self._speech_rms, floor * self._noise_factor), max(self._speech_rms, self._threshold_cap))

    def _maybe_start(self, frame: Any, mode: str, rms: float) -> None:
        """Keep a short pre-roll; when speech starts, open a recording that
        begins with it, so the first syllable isn't lost."""
        self._preroll.append(frame)
        if self._stt is None or rms < self._threshold:
            return
        preroll = list(self._preroll)
        self._preroll.clear()
        self._start_recording(mode, SOURCE_VOICE, preroll)

    def levels(self) -> list[float]:
        """Recent loudness (0-1), only while a command is being recorded.
        Empty otherwise -- sleeping audio is never drawn."""
        recording = self._recording
        if recording is None or recording.mode != MODE_AWAKE:
            return []
        with self._levels_lock:
            return list(self._levels)

    # -- triggers ------------------------------------------------------------------

    def on_wake_word(self, score: float) -> None:
        """WakeWordDetector's callback (its own thread). Only wakes her: in
        AWAKE no wake word is needed."""
        logger.info("Wake word detected (score %.2f)", score)
        if self._controller.state == State.SLEEPING:
            self._cancel_recording()
            self._wake(SOURCE_WAKE_WORD)

    def push_to_talk(self) -> None:
        """The press-to-talk hotkey: wakes her when SLEEPING (it is also the
        wake hotkey); when AWAKE, opens the microphone for one command at
        once, without waiting for speech to be detected."""
        state = self._controller.state
        logger.info("Push-to-talk pressed (state %s)", state.name)
        if state == State.SLEEPING:
            self._cancel_recording()
            self._wake(SOURCE_HOTKEY)
            return
        if self._stt is None:
            if self._notify is not None and self._stt_missing_message:
                self._notify(self._stt_missing_message)
            return
        if state == State.AWAKE and self._recording is None:
            self._start_recording(MODE_AWAKE, SOURCE_PUSH_TO_TALK, [])
            return
        # Any other state: the controller's table decides (and refuses).
        self._controller.submit(Event(EventType.SPEECH_STARTED, {"source": SOURCE_PUSH_TO_TALK}))

    # -- controller transitions (controller thread; keep it quick) ---------------------

    def on_transition(self, entry: TransitionLogEntry) -> None:
        """React to a state change (Stop cancels a recording; sleeping or waking switches what is listened
        for)."""
        if entry.event_type is None:  # Stop (R8)
            self._cancel_recording()
        elif entry.event_type == EventType.SPEECH_STARTED and not entry.accepted:
            recording = self._recording
            if recording is not None and recording.mode == MODE_AWAKE:
                self._cancel_recording()

    def _start_recording(self, mode: str, source: str, preroll: list) -> None:
        recording = _Recording(mode, source, preroll)
        if mode == MODE_AWAKE:
            with self._levels_lock:
                self._levels.clear()
                self._levels.extend(min(1.0, frame_rms(f) / _FULL_SCALE_RMS) for f in preroll)
        self._recording = recording
        if mode == MODE_AWAKE:
            self._controller.submit(Event(EventType.SPEECH_STARTED, {"source": source}))
        threading.Thread(target=self._record_and_transcribe, args=(recording,), name="jarvis-voice-utterance", daemon=True).start()

    def _cancel_recording(self) -> None:
        recording, self._recording = self._recording, None
        if recording is not None:
            recording.cancelled.set()

    # -- utterance worker ------------------------------------------------------------------

    def _still_valid(self, recording: _Recording) -> bool:
        if recording.cancelled.is_set():
            return False
        state = self._controller.state
        if recording.mode == MODE_ASLEEP:
            return state == State.SLEEPING
        if recording.mode == MODE_CONFIRMING:
            return state == State.CONFIRMING
        return state in (State.AWAKE, State.LISTENING)

    def _record_and_transcribe(self, recording: _Recording) -> None:
        import numpy as np

        frames: list[Any] = []
        speech_started = recording.speech_started
        quiet_samples = 0
        total_samples = 0
        end_silence = int(self._end_silence * self._sample_rate)
        longest = self._max_asleep if recording.mode == MODE_ASLEEP else self._max_command
        max_samples = int(longest * self._sample_rate)
        no_speech = int(self._no_speech_timeout * self._sample_rate)
        ended_by = "cancelled"
        try:
            while not recording.cancelled.is_set():
                try:
                    frame = recording.frames.get(timeout=0.25)
                except queue.Empty:
                    if not self._still_valid(recording):
                        return
                    continue
                frames.append(frame)
                total_samples += len(frame)
                # The live threshold: a recording opened by noise before the
                # background was known still ends once it is.
                threshold = self._threshold
                if frame_rms(frame) >= threshold:
                    speech_started = True
                    quiet_samples = 0
                elif speech_started:
                    quiet_samples += len(frame)
                if speech_started and quiet_samples >= end_silence:
                    ended_by = "quiet"
                    break
                if total_samples >= max_samples:
                    ended_by = "max length"
                    break
                if not speech_started and total_samples >= no_speech:
                    ended_by = "no speech"
                    break
        finally:
            if self._recording is recording:
                self._recording = None

        if not self._still_valid(recording):
            return  # Stop, her own voice, or the state moved on: drop the audio
        if not speech_started:
            self._finish_awake_without_command(recording, "no speech")
            return

        audio = np.concatenate(frames)
        frames.clear()
        started = self._clock()
        try:
            result = self._stt.transcribe_detailed(audio)
            text, no_speech_prob = result.text, result.no_speech_prob
        except Exception:
            logger.exception("Speech-to-text failed")
            text, no_speech_prob = "", 1.0
            if self._notify is not None and self._stt_missing_message:
                self._notify(self._stt_missing_message)
        finally:
            del audio  # the recording is never kept
        self.transcriptions += 1
        self.last_timing = (total_samples / self._sample_rate, ended_by, self._clock() - started)
        self._log_timing(recording.mode, self._threshold)
        if not self._still_valid(recording):
            return  # Stop arrived while transcribing
        self._dispatch(recording, text, no_speech_prob)

    def _finish_awake_without_command(self, recording: _Recording, reason: str) -> None:
        if recording.mode == MODE_AWAKE and self._controller.state == State.LISTENING:
            self._controller.submit(Event(EventType.LISTEN_TIMEOUT, {"reason": reason}))

    def _dispatch(self, recording: _Recording, text: str, no_speech_prob: float) -> None:
        said = normalize(text)
        if recording.mode == MODE_ASLEEP:
            if not said:
                return
            if is_wake_phrase(text):
                self._wake("wake phrase")
            else:
                self._heard_while_asleep(text.strip())
            return

        if recording.mode == MODE_CONFIRMING:
            # Whisper's "No." on noise or her own echo (no-speech
            # probability high) must never cancel a pending Wi-Fi or restart.
            if said and no_speech_prob > float(_CONVERSATION["max_no_speech_prob"]):
                self._ignored(text.strip(), "unclear (waiting for confirm or cancel)")
                return
            # "Please send it online. Confirm." counts -- a
            # sentence whose only command words are confirm words (or only
            # cancel words); the orchestrator asks again if it has both.
            if confirm_intent(text) is not None:
                self._handle_command(text, recording.source)
            elif said:
                self._ignored(text.strip(), "waiting for confirm or cancel")
                # "Confine", "can find" -- she says what she
                # needs (the orchestrator limits how often).
                self._not_confirm_or_cancel()
            return

        # AWAKE: every utterance is a command, except fragments.
        if said == "stop" and no_speech_prob <= float(_CONVERSATION["max_no_speech_prob"]):
            self._controller.request_stop(source="spoken stop")
            return
        reason = fragment_reason(text, no_speech_prob)
        if reason == "too short" and self._is_pick(text):
            reason = None  # a list or a "Did you mean ...?" is waiting: "1", "one", "the first" answer it
        if reason is not None:
            if said:
                self._ignored(text.strip(), reason)
            self._finish_awake_without_command(recording, reason)
            return
        if self._controller.state != State.LISTENING:
            return
        self._handle_command(text, recording.source)

    def _is_pick(self, text: str) -> bool:
        from jarvis.actions.command_text import pick_index

        try:
            return bool(self._pick_pending()) and pick_index(normalize(text)) is not None
        except Exception:
            return False

    def _log_timing(self, mode: str, threshold: float) -> None:
        """[wake_word] debug_scores: how long the utterance took -- numbers
        and the reason it ended, never audio or words (F15)."""
        log = getattr(self._detector, "log_line", None)
        if log is None or self.last_timing is None:
            return
        recorded, ended_by, transcribe = self.last_timing
        log(f"utterance ({mode}) recorded {recorded:.1f}s, ended by {ended_by}, threshold {threshold:.0f}, transcribed in {transcribe:.1f}s")

    # -- R15 housekeeping ----------------------------------------------------------------

    def unload_idle_models(self) -> None:
        """Free the speech models' memory when they have been idle for a while."""
        if self._stt is not None and hasattr(self._stt, "unload_if_idle"):
            self._stt.unload_if_idle()
