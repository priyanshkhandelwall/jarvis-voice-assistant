"""F9 camera access through OpenCV (opencv-python-headless, Apache-2.0).

One look (L1):
- Windows' camera privacy switch is read first (registry, read-only): if
  it blocks desktop apps, she says exactly where to turn it on.
- The configured camera ([camera] index) is opened; other indices are
  probed only inside this capture the developer asked for, up to
  [camera] max_index_probe, never at startup (that would be a
  JARVIS-started capture, L3).
- [camera] warmup_frames frames are read and thrown away while exposure
  settles, then [camera] look_frames frames are kept ([camera]
  look_frame_gap frames skipped between them, so they differ), and the
  camera is RELEASED before anything analyses them. One frame
  said "refrigerator 0.63" in a room with no fridge; an object now has to
  be seen in most of the frames (VisionCommands, detector.vote).
- A near-black frame (mean below [camera] min_mean_brightness) is a failed
  capture (R12): retry once, then say the camera returned a black image.

Continuous (L3, "turn on the camera"): CameraStream keeps the camera open,
gives the dashboard a small JPEG of the latest frame (in memory) and runs
the detector every [camera] stream_detect_seconds, until "turn off the
camera", Stop, Sleep or exit.

Frames are numpy arrays / JPEG bytes in memory only: never saved.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from jarvis.platform import setup_message
from jarvis.sensors import CAMERA, SensorFlags
from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)

_CFG = SETTINGS["camera"]
_MESSAGES = SETTINGS["setup_messages"]

_PRIVACY_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"


class CameraProblem(Exception):
    """What went wrong, as the sentence she says (R14)."""


@dataclass
class Look:
    """One look through the camera: the frames kept, the camera index used, and whether it had to retry."""
    frame: Any  # the first kept BGR numpy array, in memory only
    index: int  # which camera gave it
    retried: bool
    frames: List[Any] = field(default_factory=list)  # every kept frame (frame is frames[0])


def camera_blocked_by_windows() -> bool:
    """Windows' camera privacy setting, read-only: the global switch, or
    "Let desktop apps access your camera" (NonPackaged) set to Deny."""
    try:
        import winreg
    except ImportError:
        return False
    for sub in ("", "\\NonPackaged"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PRIVACY_KEY + sub) as key:
                value, _kind = winreg.QueryValueEx(key, "Value")
                if str(value).lower() == "deny":
                    return True
        except OSError:
            continue
    return False


def _backend():
    import cv2

    return {"dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF}.get(str(_CFG["backend"]).lower(), cv2.CAP_ANY)


def open_capture(index: int):
    """cv2.VideoCapture for `index`, or None when that camera can't be opened."""
    import cv2

    cap = cv2.VideoCapture(index, _backend())
    if not cap.isOpened():
        cap.release()
        return None
    return cap


def mean_brightness(frame) -> float:
    """The frame's average brightness (a black frame means the camera wasn't ready)."""
    return float(frame.mean())


Opener = Callable[[int], Any]


class Camera:
    """`opener` (tests) returns an object with read()/release(), or None."""

    def __init__(self, sensors: Optional[SensorFlags] = None, opener: Optional[Opener] = None,
                 blocked: Callable[[], bool] = camera_blocked_by_windows) -> None:
        self.sensors = sensors or SensorFlags()
        self._open = opener or open_capture
        self._blocked = blocked
        self.last_index: Optional[int] = None
        self.events: List[str] = []  # what happened, for tests and the activity log

    def _indices(self) -> List[int]:
        first = int(_CFG["index"])
        rest = [i for i in range(int(_CFG["max_index_probe"])) if i != first]
        return [first] + rest

    def _open_any(self):
        """The configured camera, else the next that opens (inside this capture only)."""
        if self._blocked():
            raise CameraProblem(setup_message("camera_privacy_blocked"))
        try:
            import cv2  # noqa: F401
        except ImportError as exc:
            raise CameraProblem(str(_MESSAGES["camera_library_missing"])) from exc
        for index in self._indices():
            cap = self._open(index)
            if cap is not None:
                self.events.append(f"opened camera {index}")
                return index, cap
            self.events.append(f"no camera at {index}")
        raise CameraProblem(setup_message("camera_not_found"))

    def _grab_once(self):
        index, cap = self._open_any()
        frames: List[Any] = []
        try:
            for _ in range(int(_CFG["warmup_frames"])):
                cap.read()  # exposure settling: thrown away
            for n in range(max(1, int(_CFG["look_frames"]))):
                if n:
                    for _ in range(int(_CFG["look_frame_gap"])):
                        cap.read()  # spacing, so the kept frames are not near-copies
                ok, frame = cap.read()
                if ok and frame is not None:
                    frames.append(frame)
        finally:
            cap.release()  # released BEFORE the frames are analysed
            self.events.append(f"released camera {index}")
        if not frames:
            raise CameraProblem(str(_MESSAGES["camera_no_frame"]))
        return index, frames

    def one_look(self) -> Look:
        """[camera] look_frames frames after the warm-up (R12: a black look is
        retried once). Camera row on only meanwhile."""
        with self.sensors.active(CAMERA):
            index, frames = self._grab_once()
            if mean_brightness(frames[0]) >= float(_CFG["min_mean_brightness"]):
                self.last_index = index
                return Look(frames[0], index, retried=False, frames=frames)
            self.events.append("black frame, retrying once")
            del frames
            index, frames = self._grab_once()
            if mean_brightness(frames[0]) >= float(_CFG["min_mean_brightness"]):
                self.last_index = index
                return Look(frames[0], index, retried=True, frames=frames)
            raise CameraProblem(str(_MESSAGES["camera_black"]))


class CameraStream:
    """Continuous preview (L3): the latest frame as a small JPEG in memory,
    and detections every [camera] stream_detect_seconds."""

    def __init__(self, camera: Camera, detect: Callable[[Any], List[Any]],
                 on_detections: Callable[[List[Any]], None] = lambda _d: None,
                 on_problem: Callable[[str], None] = lambda _t: None) -> None:
        self._camera = camera
        self._detect = detect
        self._on_detections = on_detections
        self._on_problem = on_problem
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._jpeg: Optional[bytes] = None
        self.frame_count = 0

    @property
    def running(self) -> bool:
        """True while the live preview thread runs."""
        return self._thread is not None and self._thread.is_alive()

    def latest_jpeg(self) -> Optional[bytes]:
        """The newest preview frame as JPEG, or None."""
        with self._lock:
            return self._jpeg

    def start(self) -> None:
        """Start the live preview thread (L3, after his confirm)."""
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jarvis-camera-stream", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Stop the preview and release the camera."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        with self._lock:
            self._jpeg = None  # nothing kept

    def _run(self) -> None:
        import cv2

        try:
            index, cap = self._camera._open_any()
        except CameraProblem as problem:
            self._on_problem(str(problem))
            return
        interval = 1.0 / float(_CFG["stream_fps"])
        detect_every = float(_CFG["stream_detect_seconds"])
        next_detect = time.monotonic() + detect_every
        width = int(_CFG["stream_preview_width"])
        with self._camera.sensors.active(CAMERA):
            try:
                while not self._stop.is_set():
                    started = time.monotonic()
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        self.frame_count += 1
                        h, w = frame.shape[:2]
                        small = cv2.resize(frame, (width, max(1, int(h * width / w))))
                        ok_jpg, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, int(_CFG["stream_jpeg_quality"])])
                        if ok_jpg:
                            with self._lock:
                                self._jpeg = buf.tobytes()
                        if time.monotonic() >= next_detect:
                            next_detect = time.monotonic() + detect_every
                            try:
                                self._on_detections(self._detect(frame))
                            except Exception:
                                logger.exception("Detection on the camera stream failed")
                        del frame
                    self._stop.wait(max(0.0, interval - (time.monotonic() - started)))
            finally:
                cap.release()
                self._camera.events.append(f"released camera {index}")
