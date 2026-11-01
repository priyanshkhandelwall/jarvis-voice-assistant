"""F9 local object detection: YOLOX-S (Megvii, Apache-2.0), the 80 COCO
object types, run on onnxruntime (already installed for the wake word and
Kokoro). CPU only, offline. The model file (36 MB) comes from
scripts/download_models.py into models/yolox/. About 115 ms a frame here.

Loaded only when a look needs it and dropped again after [camera]
unload_after_idle_seconds (R15). Frames are numpy arrays in memory only.

Pre/post-processing follows YOLOX's own demo for its 0.1.1rc0 ONNX export:
letterbox to the input size with grey (114) padding, BGR, no normalisation; decode
the grid outputs with strides 8/16/32; score = objectness x class; NMS.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from jarvis.settings import SETTINGS

_CFG = SETTINGS["camera"]
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

COCO_CLASSES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
)


@dataclass(frozen=True)
class Detection:
    """One detected object: label, confidence and box (x1, y1, x2, y2)."""
    label: str
    confidence: float
    box: tuple  # x1, y1, x2, y2 in frame pixels

    @property
    def area(self) -> float:
        """The box's area in pixels."""
        return max(0.0, self.box[2] - self.box[0]) * max(0.0, self.box[3] - self.box[1])


def model_path() -> Path:
    """Where the YOLOX model file lives in models/."""
    return _PROJECT_ROOT / str(_CFG["detector_dir"]) / str(_CFG["detector_file"])


class DetectorMissing(Exception):
    """The model file or a package isn't there; the message says what to run (R14)."""


class ObjectDetector:
    """F9: the local YOLOX-S detector on onnxruntime, loaded on first use and unloaded when idle."""
    def __init__(self, path: Optional[Path] = None, input_size: Optional[int] = None,
                 clock=time.monotonic) -> None:
        self.path = path or model_path()
        self.size = int(input_size or _CFG["detector_input_size"])
        self._session = None
        self._lock = threading.Lock()
        self._last_used = 0.0
        self._clock = clock

    @property
    def loaded(self) -> bool:
        """True while the model is in memory."""
        return self._session is not None

    def _load(self):
        if self._session is None:
            if not self.path.exists():
                raise DetectorMissing(str(SETTINGS["setup_messages"]["detector_missing"]))
            try:
                import onnxruntime as ort
            except ImportError as exc:
                raise DetectorMissing(str(SETTINGS["setup_messages"]["camera_library_missing"])) from exc
            self._session = ort.InferenceSession(str(self.path), providers=["CPUExecutionProvider"])
        return self._session

    def unload_if_idle(self) -> bool:
        """Free the model's memory if it hasn't been used for a while. True if it was unloaded."""
        with self._lock:
            if self._session is not None and self._clock() - self._last_used > float(_CFG["unload_after_idle_seconds"]):
                self._session = None
                return True
        return False

    def detect(self, frame_bgr, min_confidence: Optional[float] = None) -> List[Detection]:
        """Objects in one BGR frame (numpy HxWx3 uint8), most confident first."""
        import cv2
        import numpy as np

        threshold = float(_CFG["min_confidence"] if min_confidence is None else min_confidence)
        with self._lock:
            session = self._load()
            self._last_used = self._clock()
            h, w = frame_bgr.shape[:2]
            ratio = min(self.size / h, self.size / w)
            resized = cv2.resize(frame_bgr, (int(w * ratio), int(h * ratio)), interpolation=cv2.INTER_LINEAR)
            padded = np.full((self.size, self.size, 3), 114, dtype=np.uint8)
            padded[: resized.shape[0], : resized.shape[1]] = resized
            blob = padded.transpose(2, 0, 1)[None].astype(np.float32)
            output = session.run(None, {session.get_inputs()[0].name: blob})[0][0]

        grids, strides = [], []
        for stride in (8, 16, 32):
            n = self.size // stride
            xv, yv = np.meshgrid(np.arange(n), np.arange(n))
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((n * n, 1), stride))
        grid, stride = np.concatenate(grids), np.concatenate(strides)
        xy = (output[:, :2] + grid) * stride
        wh = np.exp(output[:, 2:4]) * stride
        scores = output[:, 4:5] * output[:, 5:]
        classes = scores.argmax(1)
        best = scores[np.arange(len(scores)), classes]
        keep = best >= threshold
        if not keep.any():
            return []
        xy, wh, best, classes = xy[keep], wh[keep], best[keep], classes[keep]
        boxes = np.concatenate([xy - wh / 2, xy + wh / 2], 1) / ratio
        rects = [[float(x1), float(y1), float(x2 - x1), float(y2 - y1)] for x1, y1, x2, y2 in boxes]
        chosen = cv2.dnn.NMSBoxesBatched(rects, best.tolist(), classes.tolist(), threshold, float(_CFG["nms_iou"]))
        found = sorted((Detection(COCO_CLASSES[int(classes[i])], float(best[i]), tuple(float(v) for v in boxes[i]))
                        for i in np.array(chosen).flatten()), key=lambda d: d.confidence, reverse=True)
        # One object, two labels (a dog also scored as a cat): keep the surer one.
        kept: List[Detection] = []
        for d in found:
            if all(_iou(d.box, k.box) < float(_CFG["duplicate_iou"]) for k in kept):
                kept.append(d)
        return kept


def vote(per_frame: List[List[Detection]], min_votes: int, min_confidence: float) -> List[Detection]:
    """(his room has no fridge; one frame said "refrigerator
    0.63"): only objects seen at min_confidence or more in at least
    min_votes of the frames are kept. A one-frame flicker is dropped.

    Per label: how many it is = the fewest seen in any frame that had it
    (two cups only if two were steady); its confidence = the mean of that
    label's best score over the frames that had it; its box from the frame
    where it scored best. Most confident first."""
    per_frame = [[d for d in found if d.confidence >= min_confidence] for found in per_frame]
    labels = dict.fromkeys(d.label for found in per_frame for d in found)
    kept: List[Detection] = []
    for label in labels:
        frames = [[d for d in found if d.label == label] for found in per_frame]
        frames = [sorted(f, key=lambda d: d.confidence, reverse=True) for f in frames if f]
        if len(frames) < min_votes:
            continue
        count = min(len(f) for f in frames)
        best = max(frames, key=lambda f: f[0].confidence)
        mean = sum(f[0].confidence for f in frames) / len(frames)
        top = best[0].confidence
        # Every kept box scaled to the steady mean, so the surest stays surest.
        kept.extend(Detection(d.label, round(mean * d.confidence / top, 4), d.box) for d in best[:count])
    return sorted(kept, key=lambda d: d.confidence, reverse=True)


def _iou(a: tuple, b: tuple) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0
